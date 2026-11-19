"""The protocol log (DESIGN 5.7, 9; BUILD-PLAN Step 11): one row per offered item per recall, in the log index of the JOIN
of the reader's channel class and the item's class (INV-3), and the stored query of the search.

`opened` is written HERE, by the gateway side, when a lookup serves a leaf (`mark_opened`): the agent's own claim of use
goes to `used_cited` only (`claim_used`) and never sets `opened`. A row is written after the digest is returned and never
blocks it: a failed write is counted. The write is a commit, so it takes the shared side of the tenant's write gate and
is refused when it names an erased subject or leaf. Rows are immutable except for the two one-way transitions above.
Subject detection runs on the query text with the same guard as leaves (review F17): the row and the stored query carry
the tokens of every person the query mentions, the actor and the offered item's subjects."""
from __future__ import annotations

import datetime
import hashlib
from dataclasses import dataclass
from typing import Callable, Iterable, Optional

from ..embed import stub as embed_stub
from ..gateway import validator
from ..gateway.es import EsClient
from ..gateway.gate import NoGate
from ..gateway.indices import class_index
from ..ingest.subjects import Guard
from ..policy import ClassLattice
from ..policy.classes import ClassError

SCHEMA_V = 1


@dataclass(frozen=True)
class Offer:
    leaf_id: str
    lv: int
    item_cls: str
    sources: tuple          # ("tree", "direct", "edge"); the set of sources that offered it
    rank: int
    confidence: int = 0
    subjects: tuple = ()
    exploration: bool = False
    propensity: float = 1.0
    fallback: bool = False


def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def row_id(search_id: str, leaf_id: str, lv: int) -> str:
    return f"{search_id}:{leaf_id}:{lv}"


class ProtocolLog:
    def __init__(self, es: Callable[[], EsClient], tenant: str, lattice: ClassLattice, guard: Guard, *, gate=None,
                 erased_leaf: Callable[[str], bool] = lambda leaf_id: False, join_with_item_class: bool = True,
                 detect_query_subjects: bool = True):
        self._es, self.tenant, self._lat, self._guard = es, tenant, lattice, guard
        self._gate = gate or NoGate()
        self._erased_leaf = erased_leaf
        self._detect = detect_query_subjects  # False is the BROKEN variant: subjects are taken from the leaves only
        self._join_item = join_with_item_class  # False is the BROKEN variant used by tests: the row sits in the item's class only
        self.write_failures = 0

    def join_class(self, reader_classes: Iterable[str], item_cls: str) -> str:
        channel = self._lat.join(*reader_classes)
        return self._lat.join(channel, item_cls) if self._join_item else item_cls

    def _base(self, kind: str, cls: str, **extra) -> dict:
        d = {"tenant": self.tenant, "cls": cls, "kind": kind, "lin_leaves": [], "lin_leaf_ids": [], "lin_nodes": [],
             "lin_subjects": [], "lin_queries": [], "derivation": "protocol", "erasure_rewrite": 0, "created_at": _now(),
             "schema_v": SCHEMA_V}
        d.update(extra)
        return d

    def _put(self, cls: str, kind: str, doc_id: str, doc: dict) -> bool:
        idx = class_index(self.tenant, cls, kind)
        validator.validate(doc, session_tenant=self.tenant, index=idx, index_tenant=self.tenant, index_cls=cls)
        st, _ = self._es().request("PUT", f"/{idx}/_doc/{doc_id}?op_type=create", doc)
        return st in (200, 201)

    def record_digest(self, search_id: str, actor: str, actor_model: str, reader_classes: Iterable[str], query_text: str,
                      offers: Iterable[Offer], *, pins: Optional[dict] = None, query_class: str = "recall",
                      store_query: bool = True) -> int:
        """Write the stored query and one row per offer. Never raises: failures are counted. Returns rows written."""
        reader_classes = tuple(reader_classes)
        offers = list(offers)
        q_tokens = self._guard.tokens_in(query_text) if self._detect else []
        digest = hashlib.sha256(query_text.encode()).hexdigest()
        written = 0
        try:
            with self._gate.shared():  # a commit: waits while an erasure holds the exclusive side
                channel = self._lat.join(*reader_classes)
                if store_query and not any(self._guard.token_erased(t) for t in q_tokens + [actor]):
                    sq = self._base("sq", channel, lin_subjects=sorted({actor, *q_tokens}), text=query_text,
                                    embedding=embed_stub.embed(query_text), search_id=search_id)
                    if not self._put(channel, "sq", search_id, sq):
                        self.write_failures += 1
                for rank_n, o in enumerate(offers):
                    try:
                        cls = self.join_class(reader_classes, o.item_cls)
                    except ClassError:
                        self.write_failures += 1
                        continue
                    subjects = sorted({actor, *q_tokens, *o.subjects})
                    if self._erased_leaf(o.leaf_id) or any(self._guard.token_erased(t) for t in subjects):
                        self.write_failures += 1   # checked against the published tombstones like every commit that waited
                        continue
                    row = self._base("log", cls, lin_leaf_ids=[o.leaf_id], lin_subjects=subjects, search_id=search_id,
                                     ts=_now(), actor=actor, actor_model=actor_model, query_class=query_class,
                                     prompt_hash=digest, pins=pins or {}, pointer={"leaf_id": o.leaf_id, "lv": o.lv},
                                     sources=sorted(o.sources), offered_by_tree="tree" in o.sources, rank=o.rank,
                                     offer_set_size=len(offers), propensity=o.propensity, exploration=o.exploration,
                                     holder_confidence=o.confidence, fallback=o.fallback, opened=False, asked=False,
                                     used_cited=False, used_behavioural=None, item_cls=o.item_cls)
                    if self._put(cls, "log", row_id(search_id, o.leaf_id, o.lv), row):
                        written += 1
                    else:
                        self.write_failures += 1
        except Exception:
            self.write_failures += 1
        return written

    def _flip(self, search_id: str, reader_classes: Iterable[str], item_cls: str, leaf_id: str, lv: int, fields: dict) -> bool:
        cls = self.join_class(tuple(reader_classes), item_cls)
        idx = class_index(self.tenant, cls, "log")
        with self._gate.shared():
            if self._erased_leaf(leaf_id):
                return False
            st, _ = self._es().request("POST", f"/{idx}/_update/{row_id(search_id, leaf_id, lv)}", {"doc": fields})
        return st == 200

    def mark_opened(self, search_id: str, reader_classes: Iterable[str], item_cls: str, leaf_id: str, lv: int, actor: str) -> bool:
        """Called by the gateway's lookup after it has served the leaf; the only writer of `opened`."""
        return self._flip(search_id, reader_classes, item_cls, leaf_id, lv,
                          {"opened": True, "opened_ts": _now(), "opened_by": actor})

    def claim_used(self, search_id: str, reader_classes: Iterable[str], item_cls: str, leaf_id: str, lv: int) -> bool:
        """The agent's own claim: kept in `used_cited`, never trusted for `opened`."""
        return self._flip(search_id, reader_classes, item_cls, leaf_id, lv, {"used_cited": True})


class GatewayLookup:
    """The gateway's `lookup`: it serves the leaf through the resolver and, itself, writes `opened` for the offered row. No
    callback from the agent or from the resolver's sink decides it, so a claim of use without a lookup leaves it false."""

    def __init__(self, resolver, store, plog: ProtocolLog):
        self._r, self._store, self._log = resolver, store, plog

    def lookup(self, search_id: str, reader_classes: Iterable[str], pointer: str, *, actor: str) -> dict:
        from ..ids import KIND_LEAF, IdError, decode_pointer
        out = self._r.resolve(pointer, actor=actor)
        if out.get("status") == "ok":
            try:
                p = decode_pointer(pointer)
            except IdError:
                return out
            if p.kind == KIND_LEAF:
                doc = self._store.leaf(p.id, p.version)
                if doc is not None:
                    self._log.mark_opened(search_id, reader_classes, doc["cls"], p.id, p.version, actor)
        return out
