"""Start-up of one tenant's pool state (POOLS 2.5): the directory (pools, grants) and the token table are built from the
best-effort copy in `lc-<t>-authz`, and THEN the epoch journal is applied on top, so a grant or state that a later journal record
removed stays removed. Call `load` before `RevocationService.start` (which replays the journal into `PoolService.apply_record`).

`broken_journal_not_applied` exists for the restart test's broken variant only: the table is rebuilt from the authz copy alone.
"""
from __future__ import annotations

from typing import Any, Callable

from ..policy import pools as pl
from .pool_admin import PoolService
from .sessions import Sessions

PAGE = 10000   # a tenant with more pool records than this is a stated limit


def token_persister(authz: Callable[[], Any], tenant: str) -> Callable[[str, dict], None]:
    def persist(h: str, d: dict) -> None:
        doc = {"tenant": tenant, "cls": "tenantwide", "kind": "token", "lin_leaves": [], "lin_leaf_ids": [], "lin_nodes": [],
               "lin_subjects": [d["principal"]], "lin_queries": [], "derivation": "token", "erasure_rewrite": 0, "schema_v": 1,
               "created_at": "1970-01-01T00:00:00+00:00", "hash": h, "rec": d}
        st, _ = authz().request("PUT", f"/lc-{tenant}-authz/_doc/tk:{h}?refresh=true", doc)
        if st not in (200, 201):
            raise RuntimeError("authz write refused")
    return persist


def load(authz: Callable[[], Any], tenant: str, directory: pl.PoolDirectory, sessions: Sessions, svc: PoolService) -> dict:
    """Read the pool, grant and token records back. Returns counts and the persisted grants (for confirming removals)."""
    st, r = authz().request("POST", f"/lc-{tenant}-authz/_search", {
        "size": PAGE, "query": {"terms": {"kind": ["pool", "pgrant", "token"]}}})
    if st != 200 or not isinstance(r, dict):
        raise RuntimeError("authz read failed")
    out = {"pools": 0, "grants": 0, "tokens": 0, "persisted": {}}
    hits = [h["_source"] for h in r["hits"]["hits"]]
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
            sessions.restore_token(s["hash"], s["rec"])
            out["tokens"] += 1
        except (KeyError, ValueError):
            continue
    return out
