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

A single document is deleted, read or compare-and-set written through `delete_doc`, `read_doc` and `put_doc` (review round 5, the F11
class: a malformed answer never certifies an erasure, a removal or an absence). Each owns its request, so the answer is checked against
the index and id actually sent: a delete is done only on HTTP 200 `result: deleted` or a verified absence (HTTP 404 `result: not_found`,
or a read 404 `found: false`, about exactly that `_index` and `_id`; or `absent`: the index itself is not there); anything else raises
`IncompleteRead`, so the caller refuses and the step stays retryable.

tests/unit/test_read_paths.py enumerates every ES request call site of the authz, erasure, start-up, pool, gateway and ingest modules
(an AST walk of the package) and fails if one is neither validated here nor on its commented allow-list.
"""
from __future__ import annotations

import re
from typing import Any, Optional


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


def exact_name(name: Any) -> str:
    """`name` when it is one exact index or alias name (no wildcard, list or exclusion): only an exact name has a verifiable absence."""
    if not isinstance(name, str) or not name or any(c in name for c in "*?,") or name.startswith(("-", "_", "+")):
        raise ValueError("an exact index or alias name is required")
    return name


def absent(st: int, r: Any, name: str) -> bool:
    """Whether an answer is a verified absence of the exact name `name`: HTTP 404, `index_not_found_exception`, and the error names
    `name` (`index` or `resource.id`). Any other answer, an empty or malformed 200 included, proves nothing (review F11)."""
    err = r.get("error") if isinstance(r, dict) else None
    return st == 404 and isinstance(err, dict) and err.get("type") == "index_not_found_exception" \
        and name in (err.get("index"), err.get("resource.id"))


def aliases(client: Any, name: str) -> list:
    """The physical indices behind the exact name `name` (an alias or an index). [] only for a verified absence of that name
    (`absent`). An HTTP 200 must be well formed and about `name`: at least one index, each with an `aliases` object, and either the one
    index `name` itself or indices that all carry the alias `name`. Any other answer raises, so a failed or malformed discovery never
    reads as "no indices" (review F11)."""
    exact_name(name)
    st, r = client.request("GET", f"/{name}/_alias")
    if st == 200:
        if isinstance(r, dict) and r and all(isinstance(k, str) and k and isinstance(v, dict) and isinstance(v.get("aliases"), dict)
                                             for k, v in r.items()) \
                and (set(r) == {name} or all(name in v["aliases"] for v in r.values())):
            return sorted(r)
        raise IncompleteRead("malformed alias answer")
    if absent(st, r, name):
        return []
    raise IncompleteRead("alias discovery failed")


def scan(client: Any, index: str, body: dict, *, page: int = 500, ignore_unavailable: bool = False, missing_ok: bool = False,
         keep_alive: str = "1m") -> list:
    """Every hit of `body["query"]` over `index` (a name, alias or pattern) in one consistent snapshot: a PIT, the `_shard_doc` sort
    and `search_after`, `complete_hits` on every page. `missing_ok`: a verified absence of the exact name `index` (`absent`: HTTP 404
    `index_not_found_exception` naming it, on opening the PIT) is an empty result; any other failure raises `IncompleteRead`. The PIT is closed when the scan ends."""
    if not isinstance(page, int) or page < 1:
        raise ValueError("page must be a positive integer")
    params = [f"keep_alive={keep_alive}", "allow_partial_search_results=false"] + (["ignore_unavailable=true"] if ignore_unavailable else [])
    st, r = client.request("POST", f"/{index}/_pit?" + "&".join(params))
    if missing_ok and absent(st, r, index):
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


_PHYSICAL = re.compile(r"-p[0-9a-f]{8}")


def same_index(asked: str, answered: Any, *, alias: bool = False) -> bool:
    """Whether a document answer's `_index` is the index the request named. Strict by default: the name itself (an erasure asks by the
    concrete `_index` of a scan hit, so nothing else may answer). `alias=True` for a request through an alias (the authz, class-leaf and
    boot paths): Elasticsearch then answers with the physical index, which is the alias, "-p" and 8 hex characters
    (`indices.physical_name`), and only that form is accepted besides the name itself."""
    if not isinstance(answered, str):
        return False
    if answered == asked:
        return True
    return alias and answered.startswith(asked) and _PHYSICAL.fullmatch(answered[len(asked):]) is not None


def _about(r: Any, index: str, did: str, alias: bool) -> bool:
    return isinstance(r, dict) and r.get("_id") == did and same_index(index, r.get("_index"), alias=alias)


def _doc_path(index: str, did: Any, params: tuple) -> str:
    exact_name(index)
    if not isinstance(did, str) or not did:
        raise ValueError("a document id is required")
    return f"/{index}/_doc/{did}" + ("?" + "&".join(params) if params else "")


def delete_doc(client: Any, index: str, did: str, *params: str, alias: bool = False) -> bool:
    """Delete the document `did` of the exact index (or alias) `index`. True: this request deleted it (HTTP 200, `result: deleted`, about
    exactly that `_index` and `_id`). False: it was verifiably not there (HTTP 404, `result: not_found`, about exactly that `_index` and
    `_id`; or `absent`: a 404 `index_not_found_exception` that names `index`). Any other answer (an empty or foreign 404, a 200 that does
    not say `deleted`, an error status) raises `IncompleteRead`: nothing is confirmed and the caller's step stays retryable."""
    st, r = client.request("DELETE", _doc_path(index, did, params))
    if st == 200 and _about(r, index, did, alias) and r.get("result") == "deleted":
        return True
    if st == 404 and ((_about(r, index, did, alias) and r.get("result") == "not_found") or absent(st, r, index)):
        return False
    raise IncompleteRead("document delete not confirmed")


def read_doc(client: Any, index: str, did: str, *, alias: bool = False) -> Optional[dict]:
    """The GET answer for the document `did` of the exact index (or alias) `index` when it was found (HTTP 200, `found: true`, about
    exactly that `_index` and `_id`, with a `_source` object and integer `_seq_no` and `_primary_term` for a compare-and-set). None only
    for a verified absence (HTTP 404, `found: false`, about exactly that `_index` and `_id`; or `absent`). Anything else raises
    `IncompleteRead`."""
    st, r = client.request("GET", _doc_path(index, did, ()))
    if st == 200 and _about(r, index, did, alias) and r.get("found") is True and isinstance(r.get("_source"), dict) \
            and type(r.get("_seq_no")) is int and type(r.get("_primary_term")) is int:
        return r
    if st == 404 and ((_about(r, index, did, alias) and r.get("found") is False) or absent(st, r, index)):
        return None
    raise IncompleteRead("document read not confirmed")


def put_doc(client: Any, index: str, did: str, body: dict, *params: str, alias: bool = False) -> bool:
    """Write the document `did` of the exact index (or alias) `index`. True: the write landed (HTTP 200 or 201, `result` `created` or
    `updated`, about exactly that `_index` and `_id`). False: HTTP 409, a lost compare-and-set (`if_seq_no`) or an existing id
    (`op_type=create`): nothing was written and the caller re-reads. Anything else raises `IncompleteRead`."""
    st, r = client.request("PUT", _doc_path(index, did, params), body)
    if st in (200, 201) and _about(r, index, did, alias) and r.get("result") in ("created", "updated"):
        return True
    if st == 409:
        return False
    raise IncompleteRead("document write not confirmed")
