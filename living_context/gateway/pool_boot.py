"""Start-up of one tenant's pool state (POOLS 2.5): the directory (pools, grants) and the token table are built from the
best-effort copy in `lc-<t>-authz`, and THEN the epoch journal is applied on top, so a grant or state that a later journal record
removed stays removed. Call `load` before `RevocationService.start` (which replays the journal into `PoolService.apply_record`).

The authz copy is read in one snapshot (a PIT, `_shard_doc`, `search_after`) until it is exhausted, and any failed page raises: the
start-up fails. Each record is judged by `reads.restored` (its index, its id, its tenant, its kind) before anything is built from it; a
record that is not exactly what it claims, or that does not parse, is not restored and the read is not complete, so it confirms nothing
(review round 8, F3). A removed grant is a record with `ops: []` (`PoolService._persist_grant`). Token documents are stored as one JSON
string (a fixed mapping, not an object keyed by pool id), bound to the document (tenant, principal), and an expired one is deleted when
it is read.
"""
from __future__ import annotations

import json
from typing import Any, Callable, Optional

from ..policy import pools as pl
from .pool_admin import PoolService
from .reads import IncompleteRead, delete_doc, put_doc_landed, record_id, restore_scan
from .sessions import Sessions

PAGE = 1000   # documents per page of the start-up read; the read is paged until exhausted (no cap)


def token_persister(authz: Callable[[], Any], tenant: str) -> Callable[[str, dict], None]:
    def persist(h: str, d: dict) -> None:
        doc = {"tenant": tenant, "cls": "tenantwide", "kind": "token", "lin_leaves": [], "lin_leaf_ids": [], "lin_nodes": [],
               "lin_subjects": [d["principal"]], "lin_queries": [], "derivation": "token", "erasure_rewrite": 0, "schema_v": 1,
               "created_at": "1970-01-01T00:00:00+00:00", "hash": h, "exp": float(d.get("exp", 0)), "rec_json": json.dumps(d, sort_keys=True)}
        try:   # only a well-formed acknowledgement about exactly this record counts as written (review round 6)
            put_doc_landed(authz(), f"lc-{tenant}-authz", f"tk:{h}", doc, "refresh=true", alias=True)
        except IncompleteRead as e:
            raise RuntimeError("authz write refused") from e
    return persist


RECORD_IDS = {"pool": record_id("pp", "pool"), "pgrant": record_id("pg", "principal", "pool"), "token": record_id("tk", "hash")}


def _pool(s: dict, tenant: str) -> pl.Pool:
    if not isinstance(s.get("pool"), str) or not pl.POOL_ID.match(s["pool"]) or s.get("state", pl.ACTIVE) not in pl.STATES \
            or type(s.get("counter", 0)) is not int or s.get("counter", 0) < 0:
        raise ValueError("pool record")
    return pl.Pool(s["pool"], tenant, s["pool_cls"], s["pool_kind"], s["label"], s.get("card", ""), bool(s.get("private")),
                   s.get("state", pl.ACTIVE), s.get("counter", 0))


def _grant(s: dict) -> tuple[frozenset, int]:
    ops, c = s.get("ops"), s.get("counter")
    if not isinstance(ops, list) or not all(isinstance(o, str) for o in ops) or not set(ops) <= pl.OPERATIONS or type(c) is not int \
            or c < 0:
        raise ValueError("grant record")
    return frozenset(ops), c


def _token(s: dict, tenant: str) -> dict:
    """The token record inside a token document, bound to the document: of this tenant and of the principal the document names."""
    rec = json.loads(s["rec_json"]) if "rec_json" in s else s["rec"]
    if not isinstance(rec, dict) or rec.get("tenant") != tenant or s.get("lin_subjects") != [rec.get("principal")]:
        raise ValueError("token record not bound to its document")
    return rec


def load(authz: Callable[[], Any], tenant: str, directory: pl.PoolDirectory, sessions: Sessions, svc: PoolService, *, page: int = PAGE,
         now: Optional[float] = None) -> dict:
    """Read the pool, grant and token records back. Returns counts, the persisted grants and `complete`: a failed or partial read
    raises; a record that is not exactly what it claims (`reads.restored`: index, id, tenant, kind; review round 8, F3), or one that
    does not parse, is not restored and makes `complete` False, so no removal is confirmed after this start
    (`PoolService.confirm_against_authz`)."""
    now = sessions._clock() if now is None else now   # the session table's own clock (a test clock included)
    out = {"pools": 0, "grants": 0, "tokens": 0, "expired_deleted": 0, "persisted": {}, "complete": True}
    sources, out["complete"] = restore_scan(authz(), f"lc-{tenant}-authz", tenant, RECORD_IDS, page=page)
    for s in (x for x in sources if x["kind"] == "pool"):
        try:
            directory.restore(_pool(s, tenant))
            out["pools"] += 1
        except (KeyError, ValueError, TypeError):
            out["complete"] = False
    for s in (x for x in sources if x["kind"] == "pgrant"):
        try:
            ops, counter = _grant(s)
        except (KeyError, ValueError, TypeError):
            out["complete"] = False
            continue
        out["persisted"][(s["principal"], s["pool"])] = ops
        if not ops or directory.get(tenant, s["pool"]) is None:
            continue   # a removed grant (`ops: []`), or a grant of a pool that is gone
        directory.restore_grant(tenant, s["principal"], s["pool"], ops, counter)
        out["grants"] += 1
    for s in (x for x in sources if x["kind"] == "token"):
        try:
            rec = _token(s, tenant)
            if float(rec.get("exp", 0)) <= now:   # an expired token is dead weight: delete its document (best effort)
                try:   # counted only when this request deleted it; any other answer leaves it for the next start
                    out["expired_deleted"] += delete_doc(authz(), f"lc-{tenant}-authz", f"tk:{s['hash']}", alias=True)
                except IncompleteRead:
                    pass
                continue
            sessions.restore_token(s["hash"], rec)
            out["tokens"] += 1
        except (KeyError, ValueError, TypeError):
            out["complete"] = False
    return out
