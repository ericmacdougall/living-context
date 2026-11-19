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
