"""The ONE place an Elasticsearch answer is judged (pools reviews C1 and C7, rounds 3 to 6).

An authorization, revocation, taint, journal-retention, durability, erasure or start-up decision taken from what Elasticsearch answers
must be taken from an answer that is well formed and about what was asked. HTTP 200 proves neither: Elasticsearch answers 200 with a
partial search (`timed_out`, failed shards), a multi-get answers 200 with a per-item error, and a proxy or a broken node can answer
`(200, {})` to a write that wrote nothing. A caller that reads such an answer by its status confirms a removal that never landed
(round 5, F18), reports a raised taint as persisted (F19), or certifies an erasure or an absence that is not there (F11).

So every such answer goes, as it comes back, into one of the VALIDATORS below: a pure function of `(status, body, <what was asked>)`
that returns what the caller may rely on, or raises `IncompleteRead`. The caller writes `validator(*client.request(...), ...)`: the raw
answer is never bound to a name, so no code path can read it unchecked. The request-owning helpers (`refresh`, `aliases`, `scan`,
`read_doc`, `put_doc`, `delete_doc`, `mget_docs`) do the same.

* search and count: `complete_hits`, `complete_search`, `complete_count` (HTTP 200, `timed_out` false, every shard successful, none
  failed, no early termination; `search_path` asks for no partial results);
* broadcast operations (refresh, flush, force merge): `broadcast` (HTTP 200 and a `_shards` section with no failed shard);
* a single document: `doc_read`, `doc_written`, `doc_deleted`, each about exactly the `_index` and `_id` sent (an alias-addressed request,
  `alias=True`, may be answered by the physical index behind it: the alias, "-p" and 8 hex characters), with the sequence fields of the
  write; a version conflict is a typed 409 about that index; a verified absence is a 404 about that document or a 404
  `index_not_found_exception` naming the index (`absent`);
* a multi-get: `mget_items` (every item in the order asked, about exactly that index and id, `found` strictly true or false, no
  per-item error);
* index administration: `acknowledged`, `index_created`, `setting_value`, `deleted_docs`;
* aliases and points in time: `alias_answer`, `pit_answer`;
* API keys: `api_keys_active`, `api_keys_invalidated`, `api_key_created` (a key that is not in the answer is never reported as handled;
  `keyowner.invalidate` reads the active keys back).

A multi-page traversal goes through `scan`: a point in time (PIT) and the `_shard_doc` sort give every document of one consistent
snapshot a unique position, so `search_after` neither skips nor repeats one (review F12); each page passes `complete_hits`, a page that
does not advance the cursor raises, the scan ends only at a short page, and the PIT is closed when it ends.

tests/unit/test_read_paths.py walks the whole package by AST: a request call that is not the starred argument of one of these
validators (the verifier's own, DESIGN 14.7, in verifier/check.py) must be on its commented per-site allow-list; and every validator and
every request-owning helper here must refuse the malformed-answer matrix of tests/unit/test_reads_contract.py.
"""
from __future__ import annotations

import re
from typing import Any, Optional, Sequence


class IncompleteRead(RuntimeError):
    """An answer that is not a complete, well-formed answer about what was asked. No body or index name is carried."""


def search_path(index: str, *params: str) -> str:
    """The path of a search that never accepts partial results."""
    return f"/{index}/_search?" + "&".join(("allow_partial_search_results=false",) + tuple(params))


def _int(v: Any) -> bool:
    return type(v) is int


def _shards_complete(body: Any) -> bool:
    shards = body.get("_shards") if isinstance(body, dict) else None
    return isinstance(shards, dict) and _int(shards.get("failed")) and shards["failed"] == 0 and _int(shards.get("total")) \
        and _int(shards.get("successful")) and shards["successful"] == shards["total"]   # integers only: False == 0 and True == 1


# ---- search and count -----------------------------------------------------------------------------------------------------------
def complete_hits(status: int, body: Any) -> list:
    """The hits of one page of a COMPLETE search answer, or `IncompleteRead`."""
    return complete_search(status, body)["hits"]["hits"]


def complete_search(status: int, body: Any) -> dict:
    """The whole COMPLETE search answer (for its aggregations), or `IncompleteRead`."""
    if status != 200 or not isinstance(body, dict):
        raise IncompleteRead("search failed")
    if body.get("timed_out") is not False or body.get("terminated_early"):
        raise IncompleteRead("search timed out or terminated early")
    if not _shards_complete(body):
        raise IncompleteRead("not every shard answered")
    hits = body.get("hits")
    if not isinstance(hits, dict) or not isinstance(hits.get("hits"), list) or not all(isinstance(h, dict) for h in hits["hits"]):
        raise IncompleteRead("no hits section")
    return body


def complete_count(status: int, body: Any) -> int:
    """The count of a COMPLETE `_count` answer (HTTP 200, every shard successful, none failed), or `IncompleteRead`."""
    if status != 200 or not _shards_complete(body) or not _int(body.get("count")) or body["count"] < 0:
        raise IncompleteRead("count failed or incomplete")
    return body["count"]


def broadcast(status: int, body: Any) -> None:
    """A refresh, flush or force merge that no shard failed: HTTP 200 and a `_shards` section with integer counts and `failed` 0. The
    cluster counts an unassigned copy as neither successful nor failed, so this cannot prove that every primary acted (an unassigned
    replica is normal); the reads that rely on a refresh are complete searches, which refuse while a primary is unassigned."""
    shards = body.get("_shards") if isinstance(body, dict) else None
    if status != 200 or not isinstance(shards, dict) or not _int(shards.get("failed")) or shards["failed"] != 0 \
            or not _int(shards.get("total")) or not _int(shards.get("successful")):
        raise IncompleteRead("broadcast operation failed")


def refresh(client: Any, index: str, *params: str) -> None:
    """Refresh `index` so a following read sees every acknowledged write (`broadcast`)."""
    broadcast(*client.request("POST", f"/{index}/_refresh" + ("?" + "&".join(params) if params else "")))


# ---- names, absence, aliases, points in time -------------------------------------------------------------------------------------
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


def alias_answer(status: int, body: Any, name: str) -> list:
    """The physical indices behind the exact name `name` from a `GET /<name>/_alias` answer. [] only for a verified absence (`absent`).
    An HTTP 200 must be well formed and about `name`: at least one index, each with an `aliases` object, and either the one index
    `name` itself or indices that all carry the alias `name`. Anything else raises (review F11)."""
    if status == 200:
        if isinstance(body, dict) and body and all(isinstance(k, str) and k and isinstance(v, dict) and isinstance(v.get("aliases"), dict)
                                                   for k, v in body.items()) \
                and (set(body) == {name} or all(name in v["aliases"] for v in body.values())):
            return sorted(body)
        raise IncompleteRead("malformed alias answer")
    if absent(status, body, name):
        return []
    raise IncompleteRead("alias discovery failed")


def aliases(client: Any, name: str) -> list:
    """The physical indices behind the exact name `name` (an alias or an index), or [] for its verified absence (`alias_answer`)."""
    return alias_answer(*client.request("GET", f"/{exact_name(name)}/_alias"), name)


def pit_answer(status: int, body: Any, index: str, missing_ok: bool = False) -> Optional[str]:
    """The id of a point in time opened on `index` (HTTP 200, a string id, every shard successful). None only when `missing_ok` and the
    answer is a verified absence of the exact name `index` (`absent`)."""
    if missing_ok and absent(status, body, index):
        return None
    if status != 200 or not isinstance(body, dict) or not isinstance(body.get("id"), str) or not body["id"] \
            or not _shards_complete(body):
        raise IncompleteRead("point in time could not be opened")
    return body["id"]


def scan(client: Any, index: str, body: dict, *, page: int = 500, ignore_unavailable: bool = False, missing_ok: bool = False,
         keep_alive: str = "1m") -> list:
    """Every hit of `body["query"]` over `index` (a name, alias or pattern) in one consistent snapshot: a PIT, the `_shard_doc` sort
    and `search_after`, `complete_hits` on every page. `missing_ok`: a verified absence of the exact name `index` is an empty result;
    any other failure raises `IncompleteRead`. A page whose last sort position does not move past the previous one raises (a server
    that repeats a page would otherwise be read for ever). The PIT is closed when the scan ends."""
    if not isinstance(page, int) or page < 1:
        raise ValueError("page must be a positive integer")
    params = [f"keep_alive={keep_alive}", "allow_partial_search_results=false"] + (["ignore_unavailable=true"] if ignore_unavailable else [])
    pit = pit_answer(*client.request("POST", f"/{index}/_pit?" + "&".join(params)), index, missing_ok)
    if pit is None:
        return []
    out, after = [], None
    try:
        while True:
            q = {k: v for k, v in body.items() if k not in ("sort", "search_after", "size", "pit", "track_total_hits")}
            q.update({"size": page, "pit": {"id": pit, "keep_alive": keep_alive}, "sort": [{"_shard_doc": "asc"}],
                      "track_total_hits": False})
            if after is not None:
                q["search_after"] = after
            r = complete_search(*client.request("POST", "/_search?allow_partial_search_results=false", q))
            hits = r["hits"]["hits"]
            if isinstance(r.get("pit_id"), str) and r["pit_id"]:
                pit = r["pit_id"]
            out += hits
            if len(hits) < page:
                return out
            last = hits[-1].get("sort")
            if not isinstance(last, list) or not last or (after is not None and not last > after):
                raise IncompleteRead("a page without a sort position past the previous page")
            after = last
    finally:
        try:
            client.request("DELETE", "/_pit", {"id": pit})   # best effort: an unclosed PIT expires after keep_alive
        except Exception:
            pass


# ---- single documents -----------------------------------------------------------------------------------------------------------
_PHYSICAL = re.compile(r"-p[0-9a-f]{8}")


def same_index(asked: str, answered: Any, *, alias: bool = False) -> bool:
    """Whether an answer's `_index` is the index the request named. Strict by default: the name itself (an erasure asks by the concrete
    `_index` of a scan hit, so nothing else may answer). `alias=True` for a request through an alias (the authz, control, class-leaf and
    boot paths): Elasticsearch then answers with the physical index, which is the alias, "-p" and 8 hex characters
    (`indices.physical_name`), and only that form is accepted besides the name itself."""
    if not isinstance(answered, str):
        return False
    if answered == asked:
        return True
    return alias and answered.startswith(asked) and _PHYSICAL.fullmatch(answered[len(asked):]) is not None


def _about(r: Any, index: str, did: str, alias: bool) -> bool:
    return isinstance(r, dict) and r.get("_id") == did and same_index(index, r.get("_index"), alias=alias)


def _seq(r: dict) -> bool:
    return _int(r.get("_seq_no")) and r["_seq_no"] >= 0 and _int(r.get("_primary_term")) and r["_primary_term"] >= 1


def _landed(r: dict) -> bool:
    """A write or delete acknowledgement carries its sequence fields and the shard section of the primary that applied it."""
    sh = r.get("_shards")
    return _seq(r) and isinstance(sh, dict) and _int(sh.get("successful")) and sh["successful"] >= 1 and _int(sh.get("failed"))


def _doc_path(index: str, did: Any, params: tuple) -> str:
    exact_name(index)
    if not isinstance(did, str) or not did:
        raise ValueError("a document id is required")
    return f"/{index}/_doc/{did}" + ("?" + "&".join(params) if params else "")


def doc_read(status: int, body: Any, index: str, did: str, alias: bool = False) -> Optional[dict]:
    """A GET answer for the document `did` of `index`: the answer when found (HTTP 200, `found: true`, about exactly that `_index` and
    `_id`, a `_source` object and the integer `_seq_no` and `_primary_term` a compare-and-set needs). None only for a verified absence
    (HTTP 404, `found: false`, about exactly that `_index` and `_id`; or `absent`). Anything else raises."""
    if status == 200 and _about(body, index, did, alias) and body.get("found") is True and isinstance(body.get("_source"), dict) \
            and _seq(body):
        return body
    if status == 404 and ((_about(body, index, did, alias) and body.get("found") is False) or absent(status, body, index)):
        return None
    raise IncompleteRead("document read not confirmed")


def version_conflict(status: int, body: Any, index: str, alias: bool = False) -> bool:
    """Whether an answer is the typed version conflict of a write to `index`: HTTP 409, `version_conflict_engine_exception`, naming
    that index (a lost compare-and-set, or an existing id under `op_type=create`). A bare 409 proves nothing."""
    err = body.get("error") if isinstance(body, dict) else None
    return status == 409 and isinstance(err, dict) and err.get("type") == "version_conflict_engine_exception" \
        and same_index(index, err.get("index"), alias=alias)


def doc_written(status: int, body: Any, index: str, did: str, alias: bool = False) -> bool:
    """A PUT answer for the document `did` of `index`. True: the write landed (HTTP 200 or 201, `result` `created` or `updated`, about
    exactly that `_index` and `_id`, with its sequence fields and a successful primary). False: the typed version conflict
    (`version_conflict`): nothing was written and the caller re-reads. Anything else raises: an empty or foreign 200, a bare 409."""
    if status in (200, 201) and _about(body, index, did, alias) and body.get("result") in ("created", "updated") and _landed(body):
        return True
    if version_conflict(status, body, index, alias):
        return False
    raise IncompleteRead("document write not confirmed")


def doc_deleted(status: int, body: Any, index: str, did: str, alias: bool = False) -> bool:
    """A DELETE answer for the document `did` of `index`. True: this request deleted it (HTTP 200, `result: deleted`, about exactly that
    `_index` and `_id`, with its sequence fields). False: it was verifiably not there (HTTP 404, `result: not_found` about exactly that
    `_index` and `_id`, with its sequence fields; or `absent`). Anything else raises: nothing is confirmed, the step stays retryable."""
    if status == 200 and _about(body, index, did, alias) and body.get("result") == "deleted" and _landed(body):
        return True
    if status == 404 and ((_about(body, index, did, alias) and body.get("result") == "not_found" and _seq(body))
                          or absent(status, body, index)):
        return False
    raise IncompleteRead("document delete not confirmed")


def mget_items(status: int, body: Any, wanted: Sequence[tuple], alias: bool = False) -> list:
    """A multi-get answer for `wanted`, a list of (index, id): one item per request, in order, each about exactly that index and id,
    without a per-item `error`, with `found` strictly true or false. Returns, in order, the item when found (with a `_source` object and
    its sequence fields) and None when verifiably not found. Anything else raises: a short or reordered list, a foreign item, an item
    error (an unavailable shard or a missing index is not "the document is absent")."""
    docs = body.get("docs") if status == 200 and isinstance(body, dict) else None
    if not isinstance(docs, list) or len(docs) != len(wanted):
        raise IncompleteRead("multi-get answer incomplete")
    out = []
    for (index, did), d in zip(wanted, docs):
        if not _about(d, index, did, alias) or "error" in d:
            raise IncompleteRead("multi-get item not about the document asked")
        if d.get("found") is True and isinstance(d.get("_source"), dict) and _seq(d):
            out.append(d)
        elif d.get("found") is False:
            out.append(None)
        else:
            raise IncompleteRead("multi-get item not confirmed")
    return out


def read_doc(client: Any, index: str, did: str, *, alias: bool = False) -> Optional[dict]:
    """GET the document `did` of the exact index (or alias) `index` (`doc_read`)."""
    return doc_read(*client.request("GET", _doc_path(index, did, ())), index, did, alias)


def put_doc(client: Any, index: str, did: str, body: dict, *params: str, alias: bool = False) -> bool:
    """PUT the document `did` of the exact index (or alias) `index` (`doc_written`): True landed, False a typed version conflict."""
    return doc_written(*client.request("PUT", _doc_path(index, did, params), body), index, did, alias)


def put_doc_landed(client: Any, index: str, did: str, body: dict, *params: str, alias: bool = False) -> None:
    """PUT a document that no compare-and-set guards (an authz record, a persisted copy): it must land. A version conflict raises too."""
    if not put_doc(client, index, did, body, *params, alias=alias):
        raise IncompleteRead("document write not confirmed (version conflict)")


def delete_doc(client: Any, index: str, did: str, *params: str, alias: bool = False) -> bool:
    """DELETE the document `did` of the exact index (or alias) `index` (`doc_deleted`): True deleted, False verifiably not there."""
    return doc_deleted(*client.request("DELETE", _doc_path(index, did, params)), index, did, alias)


def mget_docs(client: Any, wanted: Sequence[tuple], *, alias: bool = False) -> list:
    """Multi-get `wanted` [(index, id), ...] (`mget_items`)."""
    for index, did in wanted:
        _doc_path(index, did, ())
    if not wanted:
        return []
    return mget_items(*client.request("POST", "/_mget", {"docs": [{"_index": i, "_id": d} for i, d in wanted]}), wanted, alias)


# ---- index administration ---------------------------------------------------------------------------------------------------------
def acknowledged(status: int, body: Any) -> None:
    """HTTP 200 with `acknowledged: true` (a settings change)."""
    if status != 200 or not isinstance(body, dict) or body.get("acknowledged") is not True:
        raise IncompleteRead("not acknowledged")


def index_created(status: int, body: Any, name: str) -> None:
    """The creation of the index `name`: HTTP 200, `acknowledged: true`, and the answer names exactly that index."""
    if status != 200 or not isinstance(body, dict) or body.get("acknowledged") is not True or body.get("index") != name:
        raise IncompleteRead("index creation not confirmed")


def setting_value(status: int, body: Any, index: str, setting: str, alias: bool = False) -> Any:
    """The value of one flat setting of one index from `GET /<index>/_settings/<setting>?flat_settings=true`: HTTP 200, exactly one index
    in the answer, that index (`same_index`), and the setting present."""
    if status != 200 or not isinstance(body, dict) or len(body) != 1:
        raise IncompleteRead("settings answer malformed")
    (name, v), = body.items()
    s = v.get("settings") if isinstance(v, dict) else None
    if not same_index(index, name, alias=alias) or not isinstance(s, dict) or setting not in s:
        raise IncompleteRead("settings answer not about the index asked")
    return s[setting]


def deleted_docs(status: int, body: Any, index: str, alias: bool = False) -> int:
    """The number of deleted (not yet merged away) documents of one index from `GET /<index>/_stats/docs`: HTTP 200, no failed shard
    (`broadcast`: an unassigned replica is not a failure, the primaries' numbers are what is read), exactly one index in the answer and it
    is `index` (`same_index`), and its primaries' `docs.deleted` an integer that the `_all` total agrees with."""
    broadcast(status, body)
    ix = body.get("indices")
    if not isinstance(ix, dict) or len(ix) != 1:
        raise IncompleteRead("stats answer not about one index")
    (name, v), = ix.items()

    def deleted(x):
        d = x.get("primaries", {}).get("docs", {}).get("deleted") if isinstance(x, dict) and isinstance(x.get("primaries"), dict) \
            and isinstance(x["primaries"].get("docs"), dict) else None
        return d if _int(d) and d >= 0 else None

    n = deleted(v)
    if not same_index(index, name, alias=alias) or n is None or deleted(body.get("_all")) != n:
        raise IncompleteRead("stats answer not about the index asked")
    return n


# ---- API keys ---------------------------------------------------------------------------------------------------------------------
def api_keys_active(status: int, body: Any, prefix: str) -> list:
    """The ids of the active keys in a `GET /_security/api_key` answer: HTTP 200, an `api_keys` list, every item with a string `id`, a
    `name` under `prefix` and a boolean `invalidated`. An answer without the list (`(200, {})`) is not "no keys"."""
    keys = body.get("api_keys") if status == 200 and isinstance(body, dict) else None
    if not isinstance(keys, list) or not all(isinstance(k, dict) and isinstance(k.get("id"), str) and k["id"]
                                             and isinstance(k.get("name"), str) and k["name"].startswith(prefix)
                                             and isinstance(k.get("invalidated"), bool) for k in keys):
        raise IncompleteRead("key listing malformed")
    return [k["id"] for k in keys if not k["invalidated"]]


def api_keys_invalidated(status: int, body: Any) -> list:
    """The ids a `DELETE /_security/api_key` answer reports invalidated: HTTP 200, `error_count` 0, and the two id lists. This cluster
    answers an id it did not invalidate (unknown, or already invalidated) with both lists empty and no error, so the answer alone never
    proves that a key is dead: `keyowner.invalidate` reads the active keys back."""
    if status != 200 or not isinstance(body, dict) or body.get("error_count") != 0 or not _int(body.get("error_count")):
        raise IncompleteRead("invalidation failed")
    inv, prev = body.get("invalidated_api_keys"), body.get("previously_invalidated_api_keys")
    if not all(isinstance(x, list) and all(isinstance(i, str) for i in x) for x in (inv, prev)):
        raise IncompleteRead("invalidation answer malformed")
    return list(inv)


def api_key_created(status: int, body: Any, name: str) -> dict:
    """A created key: HTTP 200, a string `id` and `encoded` value, and the `name` asked."""
    if status != 200 or not isinstance(body, dict) or body.get("name") != name or not isinstance(body.get("id"), str) \
            or not body["id"] or not isinstance(body.get("encoded"), str) or not body["encoded"]:
        raise IncompleteRead("key creation not confirmed")
    return body


VALIDATORS = ("complete_hits", "complete_search", "complete_count", "broadcast", "alias_answer", "pit_answer", "doc_read",
              "doc_written", "doc_deleted", "mget_items", "acknowledged", "index_created", "setting_value", "deleted_docs",
              "api_keys_active", "api_keys_invalidated", "api_key_created")
