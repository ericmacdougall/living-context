"""The flat hybrid search of arm A0 and the direct list of A1 (EXPERIMENT 4; DESIGN 8.5), with the BM25 field set as ONE parameter.

What EXPERIMENT 4 says: "A0: flat hybrid over leaves: BM25 + kNN, client-side RRF k = 60". It does not name the BM25 fields.
What DESIGN 8.5 says of the direct list that A1 runs "in parallel" (and that EXPERIMENT 4 calls "the direct search of A0"):
"BM25 over `text`, `title`, `entities` and kNN over `embedding`". So there are two readings:

  strong (default)  BM25 over text, title and entities: the product's own direct search, the same function in A0 and in A1's direct
                    list. The baseline is the stronger one; a gain of A1 over it is the harder claim. Title is the code-built
                    top-terms title (INV-7) and entities are the extracted terms.
  text only         BM25 over the leaf text alone: a plain flat text search. The weaker baseline: if A1's direct list keeps
                    the three fields, A1 minus A0 is inflated by the fields A0 lacks; if both use text only, both are weaker.

`A0_FIELDS_DEFAULT` is the strong reading. A run states which it used in its result file (`a0_fields`). The same parameter feeds the
direct list of A1 so that A0 and A1's direct source are always the same function (EXPERIMENT 2.2 (c) reads "the direct list").
Everything else (window 50, k 60, kNN settings, one `_msearch`, class index naming) is the request shape of
`living_context.recall.direct.DirectSearch`; a test compares the two request bodies.
"""
from __future__ import annotations

from typing import Callable, Optional, Sequence

from living_context.embed import stub as embed_stub
from living_context.gateway.es import EsClient
from living_context.gateway.indices import class_index
from living_context.policy import ClassLattice, Op, Principal, decide
from living_context.recall.direct import Hit
from living_context.recall.fusion import K_RRF, WINDOW, rrf

A0_FIELDS_STRONG = ("text", "title", "entities")
A0_FIELDS_TEXT = ("text",)
A0_FIELDS_DEFAULT = A0_FIELDS_STRONG
ALLOWED = (A0_FIELDS_STRONG, A0_FIELDS_TEXT)


class FlatSearch:
    def __init__(self, client_for: Callable[[str], EsClient], tenant: str, *, embed: Callable[[str], list] = embed_stub.embed,
                 fields: Sequence[str] = A0_FIELDS_DEFAULT, window: int = WINDOW, k: int = K_RRF):
        if tuple(fields) not in ALLOWED:
            raise ValueError("a0 fields must be one of %s" % (ALLOWED,))
        self._client_for, self.tenant, self._embed = client_for, tenant, embed
        self.fields, self._window, self._k = list(fields), window, k
        self.plan: list = []

    def bodies(self, text: str) -> list:
        w = self._window
        return [{"size": w, "_source": ["title", "size_tokens"], "query": {"multi_match": {"query": text, "fields": self.fields}}},
                {"size": w, "_source": ["title", "size_tokens"],
                 "knn": {"field": "embedding", "query_vector": self._embed(text), "k": w, "num_candidates": max(2 * w, 100)}}]

    def search(self, p: Principal, lattice: ClassLattice, text: str, *, size: int = 10, classes: Optional[Sequence[str]] = None) -> list:
        merged = []
        wanted = sorted(p.read_set) if classes is None else sorted(set(classes))
        for cls in [c for c in wanted if decide(p, Op.RECALL, tenant=self.tenant, lattice=lattice, cls=c)]:
            index = class_index(self.tenant, cls, "leaf")
            lines = []
            for b in self.bodies(text):
                lines += [{"index": index}, b]
            self.plan.append(f"/_msearch:{index}")
            st, body = self._client_for(cls).request("POST", "/_msearch", lines, ndjson=True)
            if st != 200 or not isinstance(body, dict):
                continue
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
        return [Hit(cls, doc_id, src.get("title", ""), int(src.get("size_tokens", 0)), score) for score, cls, doc_id, src in merged[:size]]
