"""Conversation state: the taint (POOLS 2.8). Keyed by (tenant, principal, conversation), never by token, so a re-mint, a renewal, a
delegated child and a gateway restart all see the same record.

The taint is a pair: a class (the join of the classes of everything the gateway has handed to the conversation) and a private set (the
private pools any of it came from). It only rises. It is durable: the new value is written (`persist`, a compare-and-set in the tenant
control index) and the write waited for BEFORE the response that raises it is handed off; if the write fails the response is aborted
(fail closed). A rise happens at most once per class and per private pool of a conversation, so this is a handful of writes.

`floor` is the channel class: the class every conversation starts at. A tenant whose lattice has no single lowest class has no floor
(None): then every pool is non-neutral until named, which is the safe side.
"""
from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Callable, Iterable, Optional

from ..policy import ClassLattice


class StateError(Exception):
    """The durable write or read failed: the caller aborts the response and answers `unavailable`."""


@dataclass(frozen=True)
class Taint:
    cls: Optional[str]
    private: frozenset = frozenset()


def floor_class(lattice: ClassLattice) -> Optional[str]:
    lows = [c for c in lattice.classes if all(lattice.dominates(o, c) for o in lattice.classes)]
    return lows[0] if len(lows) == 1 else None


class SessionStates:
    def __init__(self, lattices, persist: Callable[[tuple, Callable], Taint], load: Callable[[tuple], Optional[Taint]], *,
                 broken_keyed_by_token: bool = False, broken_memory_only: bool = False):
        self._lat, self._persist, self._load = lattices, persist, load
        self._mem: dict[tuple, Taint] = {}
        self._lock = threading.Lock()
        self._b = dict(token=broken_keyed_by_token, mem=broken_memory_only)

    def _key(self, rec) -> tuple:
        return (rec.tenant, rec.principal_token, rec.hash if self._b["token"] else rec.conversation)

    def get(self, rec) -> Taint:
        k = self._key(rec)
        with self._lock:
            t = self._mem.get(k)
        if t is None:
            try:
                t = None if self._b["mem"] else self._load(k)
            except Exception as e:
                raise StateError("state unreadable") from e
            t = t or Taint(floor_class(self._lat[rec.tenant]))
            with self._lock:
                self._mem.setdefault(k, t)
        return t

    def neutral(self, rec, pool) -> bool:
        """POOLS 2.8: the pool's class is dominated by the taint class, and it is not private or is already in the private set."""
        t, lat = self.get(rec), self._lat[rec.tenant]
        return t.cls is not None and lat.dominates(t.cls, pool.cls) and (not pool.private or pool.id in t.private)

    def raised_by(self, rec, pools: Iterable) -> Taint:
        """The taint after `pools` are read, without storing it."""
        t, lat = self.get(rec), self._lat[rec.tenant]
        classes = [c for c in [t.cls] + [p.cls for p in pools] if c is not None]
        cls = lat.join(*classes) if classes else None
        return Taint(cls, t.private | frozenset(p.id for p in pools if p.private))

    def _join(self, tenant: str, a: Taint, b: Taint) -> Taint:
        lat = self._lat[tenant]
        classes = [c for c in (a.cls, b.cls) if c is not None]
        return Taint(lat.join(*classes) if classes else None, a.private | b.private)

    def raise_to(self, rec, new: Taint) -> bool:
        """Raise the taint to at least `new`, durably, BEFORE the caller hands the response off. The stored value is joined with `new`
        inside a compare-and-set loop (never replaced), so two parallel raises both land and the taint can never fall. True when the
        taint rose."""
        old = self.get(rec)
        k = self._key(rec)
        final = self._join(rec.tenant, old, new)
        try:
            if not self._b["mem"]:
                final = self._persist(k, lambda stored: self._join(rec.tenant, stored or Taint(floor_class(self._lat[rec.tenant])), new))
        except Exception as e:
            raise StateError("state not stored") from e
        with self._lock:
            cur = self._mem.get(k, old)
            self._mem[k] = self._join(rec.tenant, cur, final)   # joined, never assigned: a parallel raise is kept
        return final != old

    def blocks_write(self, rec, pool) -> bool:
        """The write floor: a save into `pool` whose class does not dominate the taint class, or whose private set is neither empty nor
        exactly {pool}, is not written there."""
        t, lat = self.get(rec), self._lat[rec.tenant]
        if t.cls is not None and not lat.dominates(pool.cls, t.cls):
            return True
        return bool(t.private) and t.private != frozenset({pool.id})

    def forget_memory(self) -> None:
        """A gateway restart, for the tests: memory is gone, the durable value is read back."""
        with self._lock:
            self._mem.clear()


# ---- the durable copy (tenant control index, compare-and-set) ---------------------------------------------------------------
import datetime
import hashlib

from .reads import IncompleteRead, put_doc, read_doc

PERSIST_TRIES = 20   # lost compare-and-sets in a row before a raise is refused (a handful of parallel raises at most, module doc)


def _doc_id(key: tuple) -> str:
    return "cv:" + hashlib.sha256("\x00".join(key).encode()).hexdigest()[:40]


def _taint_of(source: dict, tenant: str, key: tuple) -> Taint:
    """The taint a stored record holds. The record must be this conversation's session record (tenant, principal and conversation as
    written by `es_persist`) with a string class and a list of string pool ids; anything else is not a taint to build on."""
    priv = source.get("taint_private")
    if source.get("kind") != "session" or source.get("tenant") != tenant or source.get("conversation") != key[2] \
            or source.get("lin_subjects") != [key[1]] or not isinstance(source.get("taint_cls"), str) \
            or not isinstance(priv, list) or not all(isinstance(p, str) for p in priv):
        raise IncompleteRead("not this conversation's taint record")
    return Taint(source["taint_cls"] or None, frozenset(priv))


def es_persist(client: Callable[[], object], tenant: str) -> Callable[[tuple, Callable], Taint]:
    """`persist(key, merge)`: inside a compare-and-set loop, read the stored taint, write `merge(stored)`, return what was written.

    The read is `reads.read_doc` and the write `reads.put_doc`, both through the control alias (review round 6, F19): the stored record
    is exactly this document, found with its sequence fields, or verifiably absent; the write landed only on a well-formed
    acknowledgement about this document. A malformed or foreign answer raises `StateError` at once (the caller aborts the response and
    answers `unavailable`); only the typed version conflict of a lost compare-and-set is retried, at most PERSIST_TRIES times."""
    def persist(key: tuple, merge: Callable) -> Taint:
        idx, did = f"lc-{tenant}-ctl", _doc_id(key)
        try:
            for _ in range(PERSIST_TRIES):
                cur = read_doc(client(), idx, did, alias=True)
                t = merge(None if cur is None else _taint_of(cur["_source"], tenant, key))
                body = {"tenant": tenant, "cls": "tenantwide", "kind": "session", "lin_leaves": [], "lin_leaf_ids": [], "lin_nodes": [],
                        "lin_subjects": [key[1]], "lin_queries": [], "derivation": "taint", "erasure_rewrite": 0, "schema_v": 1,
                        "created_at": datetime.datetime.now(datetime.timezone.utc).isoformat(), "taint_cls": t.cls or "",
                        "taint_private": sorted(t.private), "conversation": key[2]}
                cas = ("op_type=create",) if cur is None else (f"if_seq_no={cur['_seq_no']}", f"if_primary_term={cur['_primary_term']}")
                if put_doc(client(), idx, did, body, "refresh=true", *cas, alias=True):
                    return t
        except IncompleteRead as e:
            raise StateError("taint not stored") from e
        raise StateError("taint not stored")
    return persist


def es_load(client: Callable[[], object], tenant: str) -> Callable[[tuple], Optional[Taint]]:
    """`load(key)`: the stored taint, or None ("no taint yet") only for a verified absence of exactly this record (`reads.read_doc`: a
    404 `found: false` about this `_index` and `_id`, or the index absent). Any other answer raises `StateError`: an empty or foreign 404,
    or a 200 that is not this record found, would otherwise read as the floor and lower the taint after a restart (review round 5)."""
    def load(key: tuple) -> Optional[Taint]:
        try:
            r = read_doc(client(), f"lc-{tenant}-ctl", _doc_id(key), alias=True)
            return None if r is None else _taint_of(r["_source"], tenant, key)
        except IncompleteRead as e:
            raise StateError("taint unreadable") from e
    return load
