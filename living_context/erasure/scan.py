"""Lineage scan of one tenant's indices (DESIGN 14.2; the first piece of the erasure closure, BUILD-PLAN Step 14).

Finds every document that names a subject token, a leaf id or a node id in one of its `lin_*` fields, in every
index of the tenant (`lc-<t>-*`: source, control, authz and each class's leaf, node, edge, member, log, query, note
and cache indices). The closure is by lineage fields, never by string search over bodies. Model outputs are inside
it because every cache entry carries its call's lineage (`model/cache.py`).
"""
from __future__ import annotations

from typing import Iterable

from ..gateway.es import EsClient
from ..gateway.indices import check_id

PAGE = 500


class ScanError(Exception):
    pass


def find(es: EsClient, tenant: str, *, subjects: Iterable[str] = (), leaf_ids: Iterable[str] = (),
         nodes: Iterable[str] = ()) -> list[tuple[str, str]]:
    """Return (physical-or-alias index, doc id) of every document whose lineage names any of the given values."""
    pattern = f"lc-{check_id(tenant, 'tenant')}-*"
    should = [{"terms": {f: sorted(set(v))}} for f, v in (("lin_subjects", subjects), ("lin_leaf_ids", leaf_ids),
                                                          ("lin_nodes", nodes)) if v]
    if not should:
        return []
    es.request("POST", f"/{pattern}/_refresh")
    out, after = [], None
    while True:
        body = {"size": PAGE, "_source": False, "query": {"bool": {"should": should, "minimum_should_match": 1}},
                "sort": [{"_doc": "asc"}]}
        if after is not None:
            body["search_after"] = after
        st, r = es.request("POST", f"/{pattern}/_search?ignore_unavailable=true", body)
        if st != 200:
            raise ScanError(f"lineage scan failed ({st})")
        hits = r["hits"]["hits"]
        if not hits:
            return out
        out += [(h["_index"], h["_id"]) for h in hits]
        after = hits[-1]["sort"]


def delete(es: EsClient, found: Iterable[tuple[str, str]]) -> int:
    n = 0
    for index, doc_id in found:
        st, _ = es.request("DELETE", f"/{index}/_doc/{doc_id}")
        if st not in (200, 404):
            raise ScanError(f"delete failed ({st})")
        n += st == 200
    return n
