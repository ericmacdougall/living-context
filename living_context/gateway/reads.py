"""The ONE validator of a complete, authoritative search read (pools delta review, class C1).

An authorization, revocation, start-up or erasure decision that is taken from a search must be taken from a COMPLETE answer. Elasticsearch
can answer HTTP 200 with a partial one: `timed_out: true`, failed or missing shards (when partial results are allowed), or an early
termination. Such a page is not "no more documents": a caller that treats it so confirms a grant removal that never landed (F5) or leaves
erasure residue. So every such read

* asks for no partial results (`search_path`, which adds `allow_partial_search_results=false`), and
* passes the answer through `complete_hits`, which returns the page's hits only for HTTP 200 with `timed_out` false, every shard
  successful, no failed shard and no early termination, and raises `IncompleteRead` for anything else. The caller fails closed.

A static test (tests/unit/test_registration.py) checks that every search in the authz, start-up and erasure modules goes through both.
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
