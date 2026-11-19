"""A pinned in-memory view of one built tree (BUILD-PLAN Step 10 tests and the stub gate). The product reads the same
shape from the node index at the pinned generation; this view keeps `last_seen` in the entries to show that the holder
call never receives it."""
from __future__ import annotations

from typing import Mapping, Optional

from ..structure.build import Tree


class MemView:
    def __init__(self, tree: Tree, gen: int = 1, pending: Optional[Mapping[str, list]] = None, mangle=None):
        self.tree, self.gen, self._pending, self._mangle = tree, gen, dict(pending or {}), mangle

    def top(self) -> list:
        return list(self.tree.top)

    def entries(self, ref: str) -> list:
        node = self.tree.nodes[ref]
        out = [dict(ch, pending=False, last_seen="2026-01-01T00:00:00Z") for ch in node.children]
        out += [dict(p, pending=True, is_leaf=True, last_seen="2026-01-01T00:00:00Z") for p in self._pending.get(ref, [])]
        return self._mangle(ref, out) if self._mangle else out


class ViewError(Exception):
    pass


class EsView:
    """The same view read from the tenant's indices at a PINNED generation (DESIGN 5.6, 8; SEC-V1). The head is read once, when
    the task pins; every later read names that generation's manifest and node versions, so a head flip, a rebuild or a
    rollback during the task changes nothing the task sees. `repin_each_call` is the BROKEN variant used by tests."""

    def __init__(self, es, tenant: str, cls: str, family: str, *, repin_each_call: bool = False):
        from ..gateway.indices import class_index
        self._es, self._family, self._repin = es, family, repin_each_call
        self._ctl, self._node = class_index(tenant, cls, "ctl"), class_index(tenant, cls, "node")
        self.gen = self.W = 0
        self._top: list = []
        self._pin()

    def _get(self, index: str, doc_id: str):
        st, r = self._es.request("GET", f"/{index}/_doc/{doc_id}")
        if st == 404:
            return None
        if st != 200:
            raise ViewError(f"read failed ({st})")
        return r["_source"]

    def _pin(self) -> None:
        head = self._get(self._ctl, f"hd:{self._family}")
        if head is None:
            self.gen, self.W, self._top = 0, 0, []
            return
        mf = self._get(self._ctl, f"mf:{self._family}:{head['gen']}")
        if mf is None:
            raise ViewError("head names a generation without a manifest")
        self.gen, self.W, self._top = int(head["gen"]), int(mf.get("W", 0)), list(mf["top"])

    def top(self) -> list:
        if self._repin:
            self._pin()
        return list(self._top)

    def entries(self, ref: str) -> list:
        doc = self._get(self._node, ref)
        if doc is None:
            raise ViewError("node version not found")
        return [dict(ch, pending=False) for ch in doc.get("children", [])]
