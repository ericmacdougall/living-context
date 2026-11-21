"""Lineage scan of one tenant's indices (DESIGN 14.2; the first piece of the erasure closure, BUILD-PLAN Step 14).

Finds every document that names a subject token, a leaf id or a node id in one of its `lin_*` fields, in every
index of the tenant (`lc-<t>-*`: source, control, authz and each class's leaf, node, edge, member, log, query, note
and cache indices). The closure is by lineage fields, never by string search over bodies. Model outputs are inside
it because every cache entry carries its call's lineage (`model/cache.py`).
"""
from __future__ import annotations

from typing import Iterable, NamedTuple

from ..gateway import reads
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
    body = {"_source": ["kind", "lin_leaf_ids", "lin_nodes", "search_id", "lin_subjects", "item_subjects", "actor"],
            "query": {"bool": {"should": should, "minimum_should_match": 1}}}
    try:   # one consistent snapshot with a unique cursor; a failed refresh, PIT or page, or a partial page, raises (F12, F13)
        reads.refresh(es, pattern)
        hits = reads.scan(es, pattern, body, page=PAGE, ignore_unavailable=True)
    except reads.IncompleteRead as e:
        raise ScanError("lineage scan failed") from e
    return [Hit(h["_index"], h["_id"], h["_source"].get("kind", ""), tuple(h["_source"].get("lin_leaf_ids", [])),
                tuple(h["_source"].get("lin_nodes", [])),
                h["_source"].get("search_id", ""), tuple(h["_source"].get("lin_subjects", [])),
                tuple(h["_source"].get("item_subjects", [])), h["_source"].get("actor", "")) for h in hits]


def delete(es: EsClient, found: Iterable[tuple]) -> int:
    """Delete each (index, id, ...) found by a scan; the index is the hit's concrete `_index`, so the answer must name exactly it
    (`reads.delete_doc`, strict). Returns how many this call deleted (a verified absence counts 0); any other answer raises `ScanError`
    and nothing is certified (review round 5)."""
    n = 0
    for h in found:
        try:
            n += reads.delete_doc(es, h[0], h[1])
        except reads.IncompleteRead as e:
            raise ScanError("delete not confirmed") from e
    return n


def find_text(es: EsClient, tenant: str, needle: str) -> list[Hit]:
    """NOT the closure: a string search over every field of every index of the tenant. It exists so the tests can show that a
    closure by string search misses a summary that paraphrases the subject (E2)."""
    pattern = f"lc-{check_id(tenant, 'tenant')}-*"
    q = '"' + needle.replace("\\", "\\\\").replace('"', '\\"') + '"'
    try:   # checked like every read, although no decision is taken from it (the E2 demonstration)
        reads.refresh(es, pattern)
        hits = reads.complete_hits(*es.request("POST", f"/{pattern}/_search?ignore_unavailable=true", {
            "size": 1000, "_source": ["kind", "lin_leaf_ids", "lin_nodes"],
            "query": {"query_string": {"query": q, "default_field": "*", "lenient": True}}}))
    except reads.IncompleteRead as e:
        raise ScanError("text scan failed") from e
    return [Hit(h["_index"], h["_id"], h["_source"].get("kind", ""), tuple(h["_source"].get("lin_leaf_ids", [])),
                tuple(h["_source"].get("lin_nodes", []))) for h in hits]
