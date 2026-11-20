"""Reflection (DESIGN 10; BUILD-PLAN Step 17, minimal producer, stub only). Off the hot path, per (tenant, class).

Three pools per recent search: (a) what the tree offered, (b) what the direct search offered, (c) a deeper and looser pool that
reflection builds itself (`pool_c`). A switch (`use_deep=False`) removes pool (c) for the NO-AUDIT arm. Every candidate is judged by
the separate judge, which never sees the source. A pair the judge could not label is unknown, not irrelevant.

`tree_missed` = judged relevant and not offered by the TREE source for that search, whatever the direct search or the link
expansion did; `any_missed` = not offered by any source (pool (c) is reflection's own search, not an offer). The repair signal is
`tree_missed`. An offered item that was not opened gets no negative note. Notes carry lineage (the leaf, the search, the query's
subject tokens) and the join class, and are written as commits: under the shared side of the tenant's write gate, refused when the
leaf or a subject was erased at a published tombstone (E4), never raised.

`broken_never_offered_by_any_source` is the BROKEN variant of the tests: `tree_missed` is read as `any_missed`, so a leaf the
direct search offered records nothing."""
from __future__ import annotations

import datetime
from dataclasses import dataclass, field
from typing import Callable, Iterable, Optional

from ..gateway import validator
from ..gateway.gate import NoGate
from ..gateway.indices import class_index
from ..policy import ClassLattice
from .judge import Judge

SCHEMA_V = 1


@dataclass
class Pools:
    tree: frozenset = frozenset()      # (a) offered by the tree
    direct: frozenset = frozenset()    # (b) offered by the direct search (the digest's top 10)
    linked: frozenset = frozenset()    # offered by link expansion
    deep: frozenset = frozenset()      # (c) reflection's own deeper, looser search


@dataclass(frozen=True)
class Note:
    leaf_id: str
    label: int
    tree_missed: bool
    any_missed: bool
    pool: str                      # where reflection found it: "tree", "direct", "linked" or "deep"


def _pool_of(leaf_id: str, p: Pools) -> str:
    for name in ("tree", "direct", "linked", "deep"):
        if leaf_id in getattr(p, name):
            return name
    return ""


def reflect(judge: Judge, query: str, pools: Pools, text_of: Callable[[str], Optional[str]], *, use_deep: bool = True,
            query_subjects: Iterable[str] = (), search_id: str = "", broken_never_offered_by_any_source: bool = False) -> list[Note]:
    cands = set(pools.tree) | set(pools.direct) | set(pools.linked) | (set(pools.deep) if use_deep else set())
    offered_any = set(pools.tree) | set(pools.direct) | set(pools.linked)
    notes = []
    for leaf_id in sorted(cands):
        text = text_of(leaf_id)
        if text is None:
            continue                                   # erased or gone: nothing to judge
        lab = judge.label(query, leaf_id, text, lin_subjects=query_subjects, lin_queries=[search_id] if search_id else [])
        if lab is None or not lab.relevant:
            continue                                   # unknown is not irrelevant, and an irrelevant offer gets no note
        any_missed = leaf_id not in offered_any
        tree_missed = any_missed if broken_never_offered_by_any_source else leaf_id not in pools.tree
        if tree_missed or any_missed:
            notes.append(Note(leaf_id, lab.label, tree_missed, any_missed, _pool_of(leaf_id, pools)))
    return notes


def note_doc(tenant: str, cls: str, n: Note, *, search_id: str, query_subjects: Iterable[str]) -> dict:
    return {"tenant": tenant, "cls": cls, "kind": "note", "lin_leaves": [], "lin_leaf_ids": [n.leaf_id], "lin_nodes": [],
            "lin_subjects": sorted(set(query_subjects)), "lin_queries": [search_id] if search_id else [], "derivation": "reflection",
            "erasure_rewrite": 0, "created_at": datetime.datetime.now(datetime.timezone.utc).isoformat(), "schema_v": SCHEMA_V,
            "search_id": search_id, "label": n.label, "tree_missed": n.tree_missed, "any_missed": n.any_missed, "pool": n.pool}


@dataclass
class NoteWriter:
    """Writes notes as commits (E4). `erased_leaf` and `erased_subject` consult the published tombstones."""
    es: Callable[[], object]
    tenant: str
    lattice: ClassLattice
    gate: object = field(default_factory=NoGate)
    erased_leaf: Callable[[str], bool] = lambda leaf_id: False
    erased_subject: Callable[[str], bool] = lambda token: False
    check_tombstones: bool = True       # False is the BROKEN variant: the commit never consults the tombstones
    write_failures: int = 0
    refused_erased: int = 0

    def write(self, notes: Iterable[Note], *, search_id: str, query: str, reader_classes: Iterable[str], item_cls_of: Callable[[str], str],
              query_subjects: Iterable[str]) -> int:
        written, subjects = 0, sorted(set(query_subjects))
        with self.gate.shared():          # a commit: waits while an erasure holds the exclusive side
            channel = self.lattice.join(*reader_classes)
            for n in notes:
                if self.check_tombstones and (self.erased_leaf(n.leaf_id) or any(self.erased_subject(t) for t in subjects)):
                    self.refused_erased += 1
                    continue
                cls = self.lattice.join(channel, item_cls_of(n.leaf_id))
                idx = class_index(self.tenant, cls, "note")
                doc = note_doc(self.tenant, cls, n, search_id=search_id, query_subjects=subjects)
                validator.validate(doc, session_tenant=self.tenant, index=idx, index_tenant=self.tenant, index_cls=cls)
                st, _ = self.es().request("PUT", f"/{idx}/_doc/{search_id}:{n.leaf_id}?op_type=create", doc)
                if st in (200, 201):
                    written += 1
                else:
                    self.write_failures += 1
        return written
