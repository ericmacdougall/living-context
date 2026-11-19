"""The redact set on the read path (DESIGN 14.3; BUILD-PLAN Step 14).

From the boundary until the physical purge has finished, documents of the closure still exist in Elasticsearch, so every read
consults the published redact set in memory: direct search drops hits (`DirectSearch(redacted=...)`), a pinned tree view hides
nodes and child entries, and pointer resolution answers `unavailable` (Resolver's `erased`). Ids are matched as given and
without the version suffix (`<id>:<v>`)."""
from __future__ import annotations

from typing import Callable


def _bare(doc_id) -> str:
    return str(doc_id).split(":")[0]


class RedactedView:
    """Wraps a tree view (MemView or EsView): a redacted node has no entries, redacted children are not shown."""

    def __init__(self, view, is_redacted: Callable[[str], bool]):
        self._v, self._red = view, is_redacted
        self.gen = getattr(view, "gen", 0)

    def _hidden(self, *ids) -> bool:
        return any(self._red(i) for i in ids if i) or any(self._red(_bare(i)) for i in ids if i)

    def top(self) -> list:
        return [r for r in self._v.top() if not self._hidden(r)]

    def entries(self, ref: str) -> list:
        if self._hidden(ref):
            return []
        return [e for e in self._v.entries(ref)
                if not self._hidden(e.get("child_id"), f"{e.get('child_id')}:{e.get('child_version', '')}")]
