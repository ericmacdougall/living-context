"""Start-up of one tenant's pool state (POOLS 2.5): the directory (pools, grants) and the token table are built from the
best-effort copy in `lc-<t>-authz`, and THEN the epoch journal is applied on top, so a grant or state that a later journal record
removed stays removed. Call `load` before `RevocationService.start` (which replays the journal into `PoolService.apply_record`).

The read is complete or the start-up fails: the authz copy is read in one snapshot (a PIT, `_shard_doc`, `search_after`) until it is exhausted, and any failed page raises
(a partial read is never used to confirm that a removal landed). Token documents are stored as one JSON string (a fixed mapping, not an
object keyed by pool id) and an expired one is deleted when it is read.
"""
from __future__ import annotations

import json
from typing import Any, Callable, Optional

from ..policy import pools as pl
from .pool_admin import PoolService
from .reads import scan
from .sessions import Sessions

PAGE = 1000   # documents per page of the start-up read; the read is paged until exhausted (no cap)


def token_persister(authz: Callable[[], Any], tenant: str) -> Callable[[str, dict], None]:
    def persist(h: str, d: dict) -> None:
        doc = {"tenant": tenant, "cls": "tenantwide", "kind": "token", "lin_leaves": [], "lin_leaf_ids": [], "lin_nodes": [],
               "lin_subjects": [d["principal"]], "lin_queries": [], "derivation": "token", "erasure_rewrite": 0, "schema_v": 1,
               "created_at": "1970-01-01T00:00:00+00:00", "hash": h, "exp": float(d.get("exp", 0)), "rec_json": json.dumps(d, sort_keys=True)}
        st, _ = authz().request("PUT", f"/lc-{tenant}-authz/_doc/tk:{h}?refresh=true", doc)
        if st not in (200, 201):
            raise RuntimeError("authz write refused")
    return persist


def _read_all(authz: Callable[[], Any], tenant: str, page: int) -> list:
    """Every pool, grant and token record of the authz copy: one consistent snapshot read in pages with a unique cursor (`reads.scan`:
    a PIT and `_shard_doc`). A failed or partial page raises (`reads.complete_hits`): the start-up fails."""
    hits = scan(authz(), f"lc-{tenant}-authz", {"query": {"terms": {"kind": ["pool", "pgrant", "token"]}}}, page=page)
    return [{**h["_source"], "_id": h["_id"]} for h in hits]


def load(authz: Callable[[], Any], tenant: str, directory: pl.PoolDirectory, sessions: Sessions, svc: PoolService, *, page: int = PAGE,
         now: Optional[float] = None) -> dict:
    """Read the pool, grant and token records back. Returns counts, the persisted grants (for confirming removals) and `complete`
    (always True here: an incomplete read raises)."""
    now = sessions._clock() if now is None else now   # the session table's own clock (a test clock included)
    out = {"pools": 0, "grants": 0, "tokens": 0, "expired_deleted": 0, "persisted": {}, "complete": True}
    hits = _read_all(authz, tenant, page)
    for s in (x for x in hits if x["kind"] == "pool"):
        directory.restore(pl.Pool(s["pool"], tenant, s["pool_cls"], s["pool_kind"], s["label"], s.get("card", ""), bool(s.get("private")),
                                  s.get("state", pl.ACTIVE), int(s.get("counter", 0))))
        out["pools"] += 1
    for s in (x for x in hits if x["kind"] == "pgrant"):
        if directory.get(tenant, s["pool"]) is None:
            continue
        directory.restore_grant(tenant, s["principal"], s["pool"], s["ops"], int(s["counter"]))
        out["persisted"][(s["principal"], s["pool"])] = frozenset(s["ops"])
        out["grants"] += 1
    for s in (x for x in hits if x["kind"] == "token"):
        try:
            rec = json.loads(s["rec_json"]) if "rec_json" in s else s["rec"]
            if float(rec.get("exp", 0)) <= now:   # an expired token is dead weight: delete its document (best effort)
                authz().request("DELETE", f"/lc-{tenant}-authz/_doc/{s['_id']}")
                out["expired_deleted"] += 1
                continue
            sessions.restore_token(s["hash"], rec)
            out["tokens"] += 1
        except (KeyError, ValueError, TypeError):
            continue
    return out
