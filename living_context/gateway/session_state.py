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
    def __init__(self, lattices, persist: Callable[[tuple, Taint], None], load: Callable[[tuple], Optional[Taint]], *,
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

    def raise_to(self, rec, new: Taint) -> bool:
        """Store a higher taint durably BEFORE the caller hands the response off. True when it rose."""
        old = self.get(rec)
        if new == old:
            return False
        k = self._key(rec)
        try:
            if not self._b["mem"]:
                self._persist(k, new)
        except Exception as e:
            raise StateError("state not stored") from e
        with self._lock:
            self._mem[k] = new
        return True

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


def _doc_id(key: tuple) -> str:
    return "cv:" + hashlib.sha256("\x00".join(key).encode()).hexdigest()[:40]


def es_persist(client: Callable[[], object], tenant: str) -> Callable[[tuple, Taint], None]:
    def persist(key: tuple, t: Taint) -> None:
        idx, did = f"lc-{tenant}-ctl", _doc_id(key)
        for _ in range(20):
            st, cur = client().request("GET", f"/{idx}/_doc/{did}")
            body = {"tenant": tenant, "cls": "tenantwide", "kind": "session", "lin_leaves": [], "lin_leaf_ids": [], "lin_nodes": [],
                    "lin_subjects": [key[1]], "lin_queries": [], "derivation": "taint", "erasure_rewrite": 0, "schema_v": 1,
                    "created_at": datetime.datetime.now(datetime.timezone.utc).isoformat(), "taint_cls": t.cls or "",
                    "taint_private": sorted(t.private), "conversation": key[2]}
            q = "?refresh=true"
            if st == 200 and isinstance(cur, dict) and cur.get("found"):
                q += f"&if_seq_no={cur['_seq_no']}&if_primary_term={cur['_primary_term']}"
            else:
                q += "&op_type=create"
            st2, _ = client().request("PUT", f"/{idx}/_doc/{did}{q}", body)
            if st2 in (200, 201):
                return
            if st2 != 409:
                break
        raise StateError("taint not stored")
    return persist


def es_load(client: Callable[[], object], tenant: str) -> Callable[[tuple], Optional[Taint]]:
    def load(key: tuple) -> Optional[Taint]:
        st, r = client().request("GET", f"/lc-{tenant}-ctl/_doc/{_doc_id(key)}")
        if st == 404:
            return None
        if st != 200 or not isinstance(r, dict):
            raise StateError("taint unreadable")
        s = r.get("_source") or {}
        return Taint(s.get("taint_cls") or None, frozenset(s.get("taint_private", [])))
    return load
