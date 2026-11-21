"""The ONE validator of a complete, authoritative search read (pools delta review, class C1).

An authorization, revocation, start-up or erasure decision that is taken from a search must be taken from a COMPLETE answer. Elasticsearch
can answer HTTP 200 with a partial one: `timed_out: true`, failed or missing shards (when partial results are allowed), or an early
termination. Such a page is not "no more documents": a caller that treats it so confirms a grant removal that never landed (F5) or leaves
erasure residue. So every such read

* asks for no partial results (`search_path`, which adds `allow_partial_search_results=false`), and
* passes the answer through `complete_hits`, which returns the page's hits only for HTTP 200 with `timed_out` false, every shard
  successful, no failed shard and no early termination, and raises `IncompleteRead` for anything else. The caller fails closed.

A traversal of more than one page goes through `scan`: a point in time (PIT) gives one consistent snapshot of every index the pattern
names, and the `_shard_doc` sort gives every document of that snapshot a unique position, so `search_after` neither skips nor repeats a
document when a page boundary falls between equal sort values in different shards or indices (review F12; the `_doc` sort is unique
only inside one shard and is not stable across a refresh or merge). Each page passes `complete_hits`, the scan ends only at a short page,
and the PIT is closed when the scan ends. `refresh` and `aliases` are the checked forms of the two other reads such decisions depend
on: a refresh that did not reach every shard, or an alias lookup that failed, raises instead of reading as "nothing there".

tests/unit/test_read_paths.py enumerates every ES request call site of the authz, erasure, start-up, pool, gateway and ingest modules
(an AST walk of the package) and fails if one is neither validated here nor on its commented allow-list.
"""
from __future__ import annotations

from typing import Any


class IncompleteRead(RuntimeError):
    """A search answer that is not a complete read. No body or index name is carried."""


def search_path(index: str, *params: str) -> str:
    """The path of a search that never accepts partial results."""
    return f"/{index}/_search?" + "&".join(("allow_partial_search_results=false",) + tuple(params))


def complete_hits(status: int, body: Any) -> list:
    """The hits of one page of a COMPLETE search answer, or `IncompleteRead`."""
    if status != 200 or not isinstance(body, dict):
        raise IncompleteRead("search failed")
    if body.get("timed_out") is not False or body.get("terminated_early"):
        raise IncompleteRead("search timed out or terminated early")
    shards = body.get("_shards")
    if not isinstance(shards, dict) or shards.get("failed") != 0 or not isinstance(shards.get("total"), int) \
            or shards.get("successful") != shards.get("total"):
        raise IncompleteRead("not every shard answered")
    hits = body.get("hits")
    if not isinstance(hits, dict) or not isinstance(hits.get("hits"), list):
        raise IncompleteRead("no hits section")
    return hits["hits"]



def complete_count(status: int, body: Any) -> int:
    """The count of a COMPLETE `_count` answer (HTTP 200, every shard successful, none failed), or `IncompleteRead`."""
    if status != 200 or not _shards_complete(body) or not isinstance(body.get("count"), int):
        raise IncompleteRead("count failed or incomplete")
    return body["count"]

def _shards_complete(body: Any) -> bool:
    shards = body.get("_shards") if isinstance(body, dict) else None
    return isinstance(shards, dict) and shards.get("failed") == 0 and isinstance(shards.get("total"), int) \
        and shards.get("successful") == shards.get("total")


def refresh(client: Any, index: str, *params: str) -> None:
    """Refresh `index` so a following read sees every acknowledged write; anything but HTTP 200 with no failed shard raises.
    (Unassigned replicas are not counted as a failure: `failed` is, and a primary that did not refresh is one.)"""
    st, r = client.request("POST", f"/{index}/_refresh" + ("?" + "&".join(params) if params else ""))
    shards = r.get("_shards") if isinstance(r, dict) else None
    if st != 200 or not isinstance(shards, dict) or shards.get("failed") != 0:
        raise IncompleteRead("refresh failed")


def aliases(client: Any, name: str) -> list:
    """The physical indices behind `name` (an alias or an index). A verified absence (HTTP 404, `index_not_found_exception`) is [];
    any other failure or a malformed answer raises, so a failed discovery never reads as "no indices" (review F11)."""
    st, r = client.request("GET", f"/{name}/_alias")
    if st == 200 and isinstance(r, dict) and all(isinstance(k, str) for k in r):
        return sorted(r)
    err = r.get("error") if isinstance(r, dict) else None
    if st == 404 and isinstance(err, dict) and err.get("type") == "index_not_found_exception":
        return []
    raise IncompleteRead("alias discovery failed")


def scan(client: Any, index: str, body: dict, *, page: int = 500, ignore_unavailable: bool = False, missing_ok: bool = False,
         keep_alive: str = "1m") -> list:
    """Every hit of `body["query"]` over `index` (a name, alias or pattern) in one consistent snapshot: a PIT, the `_shard_doc` sort
    and `search_after`, `complete_hits` on every page. `missing_ok`: a verified absent index (HTTP 404 `index_not_found_exception` on
    opening the PIT) is an empty result; any other failure raises `IncompleteRead`. The PIT is closed when the scan ends."""
    if not isinstance(page, int) or page < 1:
        raise ValueError("page must be a positive integer")
    params = [f"keep_alive={keep_alive}", "allow_partial_search_results=false"] + (["ignore_unavailable=true"] if ignore_unavailable else [])
    st, r = client.request("POST", f"/{index}/_pit?" + "&".join(params))
    if st == 404 and missing_ok:
        err = r.get("error") if isinstance(r, dict) else None
        if isinstance(err, dict) and err.get("type") == "index_not_found_exception":
            return []
    if st != 200 or not isinstance(r, dict) or not isinstance(r.get("id"), str) or ("_shards" in r and not _shards_complete(r)):
        raise IncompleteRead("point in time could not be opened")
    pit, out, after = r["id"], [], None
    try:
        while True:
            q = {k: v for k, v in body.items() if k not in ("sort", "search_after", "size", "pit", "track_total_hits")}
            q.update({"size": page, "pit": {"id": pit, "keep_alive": keep_alive}, "sort": [{"_shard_doc": "asc"}],
                      "track_total_hits": False})
            if after is not None:
                q["search_after"] = after
            st, r = client.request("POST", "/_search?allow_partial_search_results=false", q)
            hits = complete_hits(st, r)
            if isinstance(r.get("pit_id"), str):
                pit = r["pit_id"]
            out += hits
            if len(hits) < page:
                return out
            after = hits[-1].get("sort")
            if not isinstance(after, list) or not after:
                raise IncompleteRead("a page without a sort position")
    finally:
        try:
            client.request("DELETE", "/_pit", {"id": pit})   # best effort: an unclosed PIT expires after keep_alive
        except Exception:
            pass
