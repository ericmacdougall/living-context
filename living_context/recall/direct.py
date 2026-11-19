"""Direct leaf search, the safety net (DESIGN 8.5; BUILD-PLAN Step 7).

For every class the principal may recall, one `_msearch` of two sub-searches on that class's leaf index:
BM25 (`multi_match` over text, title, entities) and kNN over the embedding. The two ranked lists of each
class are fused by client-side reciprocal rank fusion (k 60, window 50); fused lists of different
classes are merged by fused score, because raw BM25 and kNN scores are not comparable across indices
(ASSUMPTION). The index names to touch come from the policy decision and the naming builders only: a
class outside the read set is never named in any request (`plan` records every path sent).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Optional, Sequence

from ..embed import stub as embed_stub
from ..gateway.es import EsClient
from ..gateway.indices import class_index
from ..policy import ClassLattice, Op, Principal, decide
from .fusion import K_RRF, WINDOW, rrf

HYBRID, BM25_ONLY, KNN_ONLY = "hybrid", "bm25", "knn"


@dataclass(frozen=True)
class Hit:
    cls: str
    doc_id: str
    title: str
    size: int
    score: float


class DirectSearch:
    def __init__(self, client_for: Callable[[str], EsClient], tenant: str, *,
                 embed: Callable[[str], list] = embed_stub.embed, window: int = WINDOW, k: int = K_RRF,
                 mode: str = HYBRID):
        self._client_for, self.tenant, self._embed = client_for, tenant, embed
        self._window, self._k, self._mode = window, k, mode
        self.plan: list[str] = []

    def classes_for(self, p: Principal, lattice: ClassLattice, wanted: Optional[Sequence[str]] = None) -> list[str]:
        pool = sorted(p.read_set) if wanted is None else sorted(set(wanted))
        return [c for c in pool if decide(p, Op.RECALL, tenant=self.tenant, lattice=lattice, cls=c)]

    def search(self, p: Principal, lattice: ClassLattice, text: str, *, size: int = 10,
               classes: Optional[Sequence[str]] = None) -> list[Hit]:
        merged: list[tuple[float, str, str, dict]] = []
        for cls in self.classes_for(p, lattice, classes):
            index = class_index(self.tenant, cls, "leaf")
            lines, want = [], []
            if self._mode in (HYBRID, BM25_ONLY):
                lines += [{"index": index}, {"size": self._window, "_source": ["title", "size_tokens"],
                                              "query": {"multi_match": {"query": text, "fields": ["text", "title", "entities"]}}}]
                want.append("bm25")
            if self._mode in (HYBRID, KNN_ONLY):
                lines += [{"index": index}, {"size": self._window, "_source": ["title", "size_tokens"],
                                              "knn": {"field": "embedding", "query_vector": self._embed(text),
                                                      "k": self._window, "num_candidates": max(2 * self._window, 100)}}]
                want.append("knn")
            self.plan.append(f"/_msearch:{index}")
            st, body = self._client_for(cls).request("POST", "/_msearch", lines, ndjson=True)
            if st != 200 or not isinstance(body, dict):
                continue  # an unavailable class contributes nothing; the caller sees fewer hits, never an error body
            lists, srcs = [], {}
            for resp in body.get("responses", []):
                ids = []
                if isinstance(resp, dict) and "error" not in resp:
                    for h in resp.get("hits", {}).get("hits", []):
                        ids.append(h["_id"])
                        srcs[h["_id"]] = h.get("_source") or {}
                lists.append(ids)
            for doc_id, score in rrf(lists, self._k, self._window):
                merged.append((score, cls, doc_id, srcs.get(doc_id, {})))
        merged.sort(key=lambda t: (-t[0], t[1], t[2]))
        return [Hit(cls, doc_id, src.get("title", ""), int(src.get("size_tokens", 0)), score)
                for score, cls, doc_id, src in merged[:size]]
