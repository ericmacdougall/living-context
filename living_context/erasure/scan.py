"""Lineage scan of one tenant's indices (DESIGN 14.2; the first piece of the erasure closure, BUILD-PLAN Step 14).

Finds every document that names a subject token, a leaf id or a node id in one of its `lin_*` fields, in every
index of the tenant (`lc-<t>-*`: source, control, authz and each class's leaf, node, edge, member, log, query, note
and cache indices). The closure is by lineage fields, never by string search over bodies. Model outputs are inside
it because every cache entry carries its call's lineage (`model/cache.py`).
"""
from __future__ import annotations

from typing import Iterable, NamedTuple

from ..gateway.es import EsClient
from ..gateway.indices import check_id

PAGE = 500


class ScanError(Exception):
    pass


class Hit(NamedTuple):
    index: str
    id: str
    kind: str = ""
    lin_leaf_ids: tuple = ()
    lin_nodes: tuple = ()
    search_id: str = ""            # a log row or stored query: the search it records
    lin_subjects: tuple = ()
    item_subjects: tuple = ()      # a log row: the subjects of the offered item (the row's actor is not among them)
    actor: str = ""


TENANT_WIDE = ("src", "ctl", "authz")


def index_class(tenant: str, index: str) -> str:
    """The access class an index of the tenant belongs to (`lc-<t>-<class>-<kind>[-<suffix>]`), or `tenantwide`."""
    parts = index[len(f"lc-{tenant}-"):].split("-")
    return "tenantwide" if parts[0] in TENANT_WIDE else parts[0]


def find(es: EsClient, tenant: str, *, subjects: Iterable[str] = (), leaf_ids: Iterable[str] = (),
         nodes: Iterable[str] = (), queries: Iterable[str] = ()) -> list[Hit]:
    """Return (physical-or-alias index, doc id) of every document whose lineage names any of the given values. `queries` is
    the second net of DESIGN 14.2: documents shaped by an erased search name its id in `lin_queries`, in an edge's `evidence`
    or in a member's `shaped_by`."""
    pattern = f"lc-{check_id(tenant, 'tenant')}-*"
    queries = sorted(set(queries))
    should = [{"terms": {f: sorted(set(v))}} for f, v in (("lin_subjects", subjects), ("lin_leaf_ids", leaf_ids),
                                                          ("lin_nodes", nodes)) if v]
    if queries:
        should += [{"terms": {f: queries}} for f in ("lin_queries", "evidence", "shaped_by")]
    if not should:
        return []
    es.request("POST", f"/{pattern}/_refresh")
    out, after = [], None
    while True:
        body = {"size": PAGE, "_source": ["kind", "lin_leaf_ids", "lin_nodes", "search_id", "lin_subjects", "item_subjects", "actor"], "query": {"bool": {"should": should, "minimum_should_match": 1}},
                "sort": [{"_doc": "asc"}]}
        if after is not None:
            body["search_after"] = after
        st, r = es.request("POST", f"/{pattern}/_search?ignore_unavailable=true", body)
        if st != 200:
            raise ScanError(f"lineage scan failed ({st})")
        hits = r["hits"]["hits"]
        if not hits:
            return out
        out += [Hit(h["_index"], h["_id"], h["_source"].get("kind", ""), tuple(h["_source"].get("lin_leaf_ids", [])),
                    tuple(h["_source"].get("lin_nodes", [])),
                    h["_source"].get("search_id", ""), tuple(h["_source"].get("lin_subjects", [])),
                    tuple(h["_source"].get("item_subjects", [])), h["_source"].get("actor", "")) for h in hits]
        after = hits[-1]["sort"]


def delete(es: EsClient, found: Iterable[tuple]) -> int:
    n = 0
    for h in found:
        index, doc_id = h[0], h[1]
        st, _ = es.request("DELETE", f"/{index}/_doc/{doc_id}")
        if st not in (200, 404):
            raise ScanError(f"delete failed ({st})")
        n += st == 200
    return n


def find_text(es: EsClient, tenant: str, needle: str) -> list[Hit]:
    """NOT the closure: a string search over every field of every index of the tenant. It exists so the tests can show that a
    closure by string search misses a summary that paraphrases the subject (E2)."""
    pattern = f"lc-{check_id(tenant, 'tenant')}-*"
    es.request("POST", f"/{pattern}/_refresh")
    q = '"' + needle.replace("\\", "\\\\").replace('"', '\\"') + '"'
    st, r = es.request("POST", f"/{pattern}/_search?ignore_unavailable=true", {
        "size": 1000, "_source": ["kind", "lin_leaf_ids", "lin_nodes"],
        "query": {"query_string": {"query": q, "default_field": "*", "lenient": True}}})
    if st != 200:
        raise ScanError(f"text scan failed ({st})")
    return [Hit(h["_index"], h["_id"], h["_source"].get("kind", ""), tuple(h["_source"].get("lin_leaf_ids", [])),
                tuple(h["_source"].get("lin_nodes", []))) for h in r["hits"]["hits"]]
