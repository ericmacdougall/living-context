"""The gateway's class-level reader: the ONE code path that reads a pool's unscored documents out of the class-level indices
(POOLS 1.2, 10; ADAPTIVE 2.2 "layout 1"; BUILD-PLAN Step 25, NEG-16).

While a pool is at stage 0, its heads (`lh:<pool>:<leaf>`), its control documents (`pm:<pool>`, ...), its log rows and its stored
queries live in the class-level `-ctl`, `-log` and `-sq` indices that several pools share. No session reads them: the gateway derives
what a session sees. Every read goes through this class, which

* adds the `pool` term itself, in one place (`_body`), and checks the FINAL request again in `check_request`, which refuses a request
  without a top-level `pool` term in the filter context;
* accepts only get-by-id (the id embeds the pool: `<prefix>:<pool>:...`, and a mismatched id refuses the whole call), multi-get
  (every id checked, one bad id refuses all), filter-context searches (term, terms, range, exists, and bool of those), counts and
  aggregations of filter-context requests;
* refuses any scored request: `match`, `multi_match`, `knn`, `script_score`, `function_score`, `query_string`, `more_like_this`,
  `percolate`, a script anywhere, a top-level `knn` or `rank` section next to a filter, and every unknown clause;
* never returns a document whose `pool` is not the requested one (a leak guard behind the term).

`READ_PATHS` lists the methods that read; a test enumerates them. The `broken_*` flags exist for the tests' broken variants only.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Optional, Sequence

from .es import EsClient
from .indices import class_index, check_pool_id
from .reads import IncompleteRead, complete_count, complete_search, doc_read, mget_items

_ID_CHARS = re.compile(r"^[A-Za-z0-9:_.-]{3,200}$")
READABLE_KINDS = ("ctl", "log", "sq")
READ_PATHS = ("get", "mget", "search", "count", "aggregate")
_FILTER_LEAVES = {"term", "terms", "range", "exists"}
_ALLOWED_TOP = {"query", "size", "_source", "sort", "aggs", "track_total_hits", "from", "search_after"}
_SCORED_OR_UNSAFE = ("match", "multi_match", "knn", "script_score", "function_score", "query_string", "simple_query_string",
                     "more_like_this", "percolate", "script", "rank", "wildcard", "regexp", "fuzzy", "prefix", "match_phrase",
                     "nested", "has_child", "has_parent", "rescore", "runtime_mappings", "script_fields", "rank_feature",
                     "semantic", "sparse_vector", "text_expansion", "intervals", "span_term", "combined_fields")


class ReaderRefused(Exception):
    """The reader refused a request. The caller sees one typed `unavailable` (POOLS 2.7); the code is for the gateway's own log."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class Doc:
    id: str
    source: Mapping[str, Any]
    seq_no: int = -1
    primary_term: int = -1


def _walk(node: Any):
    if isinstance(node, dict):
        for k, v in node.items():
            yield k
            yield from _walk(v)
    elif isinstance(node, list):
        for v in node:
            yield from _walk(v)


def _clause_ok(c: Any) -> bool:
    if not isinstance(c, dict) or len(c) != 1:
        return False
    (k, v), = c.items()
    if k == "terms":   # a list of plain values: never a terms LOOKUP ({"index": ..., "id": ..., "path": ...}), which reads another document
        return isinstance(v, dict) and all(isinstance(x, list) and all(isinstance(y, (str, int, float, bool)) for y in x) for x in v.values())
    if k in _FILTER_LEAVES:
        return isinstance(v, dict)
    if k == "bool":
        return isinstance(v, dict) and set(v) <= {"filter", "must_not", "should", "minimum_should_match"} and all(
            all(_clause_ok(x) for x in (vv if isinstance(vv, list) else [vv])) for kk, vv in v.items() if kk != "minimum_should_match")
    return False


def _has_pool_term(query: Any, pool: str) -> bool:
    """A top-level conjunct: the query is the term itself, or a bool whose `filter` list holds it. A term inside `should` or
    `must_not` does not count."""
    if not isinstance(query, dict):
        return False
    t = query.get("term")
    if isinstance(t, dict) and "pool" in t:
        v = t["pool"]
        return v == pool or (isinstance(v, dict) and v.get("value") == pool)
    b = query.get("bool")
    if isinstance(b, dict):
        f = b.get("filter")
        f = f if isinstance(f, list) else ([f] if f else [])
        return any(_has_pool_term(x, pool) for x in f)
    return False


_AGG_TYPES = {"terms", "cardinality", "value_count", "min", "max", "sum", "avg"}   # an allow-list: `global`, `top_hits`, `filters`, scripts and the rest are refused


def _aggs_ok(aggs: Any, depth: int = 0) -> bool:
    if not isinstance(aggs, dict) or depth > 3:
        return False
    for spec in aggs.values():
        if not isinstance(spec, dict):
            return False
        types = [k for k in spec if k != "aggs"]
        if len(types) != 1 or types[0] not in _AGG_TYPES or not isinstance(spec[types[0]], dict) or "script" in spec[types[0]]:
            return False
        if "aggs" in spec and not _aggs_ok(spec["aggs"], depth + 1):
            return False
    return True


def check_request(body: Any, pool: str) -> None:
    """Refuse a search body that is not a pure filter-context request carrying the pool term."""
    if not isinstance(body, dict) or not set(body) <= _ALLOWED_TOP:
        raise ReaderRefused("unknown_section")
    keys = set(_walk(body))
    if any(k in keys for k in _SCORED_OR_UNSAFE):
        raise ReaderRefused("scored_or_unsafe")
    if "aggs" in body and not _aggs_ok(body["aggs"]):
        raise ReaderRefused("aggregation_not_allowed")
    q = body.get("query")
    if q is None or not _clause_ok(q) or not _has_pool_term(q, pool):
        raise ReaderRefused("no_pool_term")


def id_names_pool(doc_id: Any, pool: str) -> bool:
    return isinstance(doc_id, str) and doc_id.count(":") >= 1 and doc_id.split(":")[1] == pool


class ClassReader:
    def __init__(self, client_for: Callable[[str], EsClient], tenant: str, *, broken_no_pool_term: bool = False,
                 broken_allow_scored: bool = False, broken_id_unchecked: bool = False, broken_no_leak_guard: bool = False):
        self._client_for, self.tenant = client_for, tenant
        self._b = dict(no_term=broken_no_pool_term, scored=broken_allow_scored, id=broken_id_unchecked, leak=broken_no_leak_guard)
        self.plan: list[tuple[str, str]] = []   # (path, index) of every request sent: the request-plan assertion reads it

    # ---- the one place a request is built and the one place it is sent ---------------------------------------------------
    def _body(self, pool: str, filters: Sequence[dict], **extra) -> dict:
        flt = list(filters)
        if not self._b["no_term"]:
            flt = [{"term": {"pool": pool}}] + flt
        body = {"query": {"bool": {"filter": flt}}}
        body.update(extra)
        return body

    def _send(self, cls: str, kind: str, op: str, pool: str, *, doc_ids: Sequence[str] = (), body: Any = None) -> Any:
        """Send one read and judge its answer as it returns (`reads`): a get is `doc_read` (exactly this document through the class alias,
        found with its sequence fields, or verifiably absent), a multi-get `mget_items` (every item in order, about exactly the id asked,
        no item error), a search `complete_search` and a count `complete_count` (HTTP 200, every shard, no time-out: a hold missing from
        a partial answer would read as no hold, F13). Any other answer refuses (review round 6: a `(200, {})` head was "no head", so a
        retract answered `retracted` with nothing retracted)."""
        if kind not in READABLE_KINDS:
            raise ReaderRefused("kind")
        if op == "search" and not (self._b["no_term"] or self._b["scored"]):
            check_request(body, pool)
        index = class_index(self.tenant, cls, kind)
        es = self._client_for(cls)
        try:
            if op == "get":
                self.plan.append((f"/{{index}}/_doc/{doc_ids[0]}", index))
                return doc_read(*es.request("GET", f"/{index}/_doc/{doc_ids[0]}"), index, doc_ids[0], True)
            if op == "mget":
                self.plan.append(("/{index}/_mget", index))
                return mget_items(*es.request("POST", f"/{index}/_mget", {"ids": list(doc_ids)}), [(index, i) for i in doc_ids], True)
            if op == "count":
                self.plan.append(("/{index}/_count", index))
                return complete_count(*es.request("POST", f"/{index}/_count", body))
            self.plan.append(("/{index}/_search", index))
            return complete_search(*es.request("POST", f"/{index}/_search", body))
        except IncompleteRead as e:
            raise ReaderRefused("incomplete") from e

    def _check_ids(self, ids: Sequence[str], pool: str) -> None:
        if not all(isinstance(i, str) and _ID_CHARS.match(i) for i in ids):
            raise ReaderRefused("bad_id")   # also keeps a path separator or a query string out of the request path
        if not self._b["id"] and not all(id_names_pool(i, pool) for i in ids):
            raise ReaderRefused("foreign_id")

    def _leak(self, hits: Sequence[Mapping], pool: str) -> None:
        if not self._b["leak"] and any((h.get("_source") or {}).get("pool") != pool for h in hits):
            raise ReaderRefused("leak")

    # ---- the read paths -----------------------------------------------------------------------------------------------------
    def get(self, cls: str, kind: str, doc_id: str, pool: str) -> Optional[Doc]:
        check_pool_id(pool)
        self._check_ids([doc_id], pool)
        r = self._send(cls, kind, "get", pool, doc_ids=[doc_id])
        if r is None:   # a verified absence of exactly this document
            return None
        self._leak([r], pool)
        return Doc(doc_id, r["_source"], r["_seq_no"], r["_primary_term"])

    def mget(self, cls: str, kind: str, doc_ids: Sequence[str], pool: str) -> dict[str, Optional[Doc]]:
        check_pool_id(pool)
        self._check_ids(list(doc_ids), pool)   # one id of another pool refuses the whole call
        if not doc_ids:
            return {}
        items = self._send(cls, kind, "mget", pool, doc_ids=list(doc_ids))
        self._leak([d for d in items if d is not None], pool)
        return {i: None if d is None else Doc(i, d["_source"], d["_seq_no"], d["_primary_term"]) for i, d in zip(doc_ids, items)}

    def search(self, cls: str, kind: str, pool: str, filters: Sequence[dict] = (), *, size: int = 100,
               source: Optional[Sequence[str]] = None, sort: Optional[list] = None) -> list[Doc]:
        check_pool_id(pool)
        extra: dict = {"size": size}
        if source is not None:
            extra["_source"] = list(source)
        if sort is not None:
            extra["sort"] = sort
        body = self._body(pool, filters, **extra)
        hits = self._send(cls, kind, "search", pool, body=body)["hits"]["hits"]
        if not all(isinstance(h.get("_id"), str) and isinstance(h.get("_source", {}), dict) for h in hits):
            raise ReaderRefused("incomplete")
        self._leak(hits, pool)
        return [Doc(h["_id"], h.get("_source") or {}) for h in hits]

    def count(self, cls: str, kind: str, pool: str, filters: Sequence[dict] = ()) -> int:
        check_pool_id(pool)
        body = {"query": self._body(pool, filters)["query"]}
        if not self._b["scored"] and not self._b["no_term"]:
            check_request(body, pool)
        return self._send(cls, kind, "count", pool, body=body)

    def aggregate(self, cls: str, kind: str, pool: str, aggs: Mapping[str, Any], filters: Sequence[dict] = ()) -> Mapping[str, Any]:
        check_pool_id(pool)
        body = self._body(pool, filters, size=0, aggs=dict(aggs))
        r = self._send(cls, kind, "search", pool, body=body).get("aggregations")
        if not isinstance(r, dict) or not all(isinstance(r.get(name), dict) for name in aggs):
            raise ReaderRefused("incomplete")   # an answer without the aggregation asked is not "no buckets"
        return r
