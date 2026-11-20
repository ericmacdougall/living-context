"""Gateway start-up: the composition root of one tenant's durable state (DESIGN 4.4, 14.6, 20; BUILD-PLAN Step 15).

What a restart must bring back, and from where:
  * revokes and epoch bumps: the tenant's epoch journal (`RevocationService.start` replays it, writes a durable start record and
    only then admits);
  * the erased-identifier and bare-name sets of the guard (E3): the tombstone journal (`Guard.load_journal`); without this a
    re-ingest of an erased identifier would be accepted again after a restart;
  * grants: the best-effort copy in `lc-<t>-authz` (`GrantStore.persist` writes it, `load_grants` reads it back). Memory first: a
    failed write never blocks a call, and a principal whose grant is not found cannot mint a session (fail closed);
  * legal holds and their queue: the hold registry's own durable file.
`offboard_tenant` is the tenant-wide revocation (offboarding, key compromise): the gateway forgets every client of the tenant at
once (`EsGate.drop_tenant`) and, given the key owner's client, the keys are invalidated at the cluster too.

`broken_skip_load` exists for the restart test's broken variant only: the start-up reads nothing back."""
from __future__ import annotations

import datetime
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional

from ..erasure.hold import HoldRegistry
from ..ingest.subjects import Guard, TokenSource
from ..keystore.store import Keystore
from ..policy import Principal, Role
from .admin import GrantStore
from .handler import EsGate
from .journal import TombstoneJournal
from .keyowner import invalidate_all
from .reads import complete_hits, search_path
from .revocation import OPEN, RevocationService
from .sessions import EpochTable



def grant_doc(tenant: str, p: Principal) -> dict:
    """The authz record of a grant: lineage names the principal's token, so erasing the person deletes it (14.1)."""
    return {"tenant": tenant, "cls": "tenantwide", "kind": "grant", "lin_leaves": [], "lin_leaf_ids": [], "lin_nodes": [],
            "lin_subjects": [p.token], "lin_queries": [], "derivation": "grant", "erasure_rewrite": 0, "schema_v": 1,
            "created_at": datetime.datetime.now(datetime.timezone.utc).isoformat(), "principal": p.token,
            "roles": sorted(r.value for r in p.roles), "read": sorted(p.read_set), "write": sorted(p.write_set),
            "own_class": p.own_class or ""}


def grant_persister(gate: EsGate, tenant: str) -> Callable[[Principal], None]:
    def persist(p: Principal) -> None:
        st, _ = gate.client(tenant, "authz").request("PUT", f"/lc-{tenant}-authz/_doc/gr:{p.token}?refresh=true",
                                                             grant_doc(tenant, p))
        if st not in (200, 201):
            raise RuntimeError(f"authz write refused ({st})")
    return persist


def load_grants(gate: EsGate, tenant: str, *, page: int = 1000) -> list[Principal]:
    """Every grant record of the tenant's authz index, paged with `search_after` until exhausted. A failed or partial page is an error
    (`reads.complete_hits`; the caller starts with no grants, which fails closed: nobody can mint a session until a grant is made
    again)."""
    c = gate.client(tenant, "authz")
    out, after = [], None
    while True:
        body = {"size": page, "sort": [{"_doc": "asc"}], "query": {"term": {"kind": "grant"}}}
        if after is not None:
            body["search_after"] = after
        st, r = c.request("POST", search_path(f"lc-{tenant}-authz"), body)
        hits = complete_hits(st, r)
        if not hits:
            return out
        for h in hits:
            s = h["_source"]
            try:
                out.append(Principal(tenant, s["principal"], frozenset(Role(x) for x in s["roles"]), frozenset(s.get("read", [])),
                                     frozenset(s.get("write", [])), s.get("own_class") or None))
            except (KeyError, ValueError):
                continue   # a record that does not parse is no grant
        after = hits[-1]["sort"]


@dataclass
class TenantRuntime:
    tenant: str
    revocation: RevocationService
    tombstones: TombstoneJournal
    guard: Guard
    holds: HoldRegistry
    grants: GrantStore
    loaded_grants: int = 0
    grant_load_error: str = ""   # why the grants could not be read back (an empty store fails closed)
    guard_records: int = 0


def start_tenant(tenant: str, *, gate: EsGate, epochs: EpochTable, epoch_dir: Path, tombstone_paths: tuple[Path, Path],
                 keystore: Keystore, holds_path: Path, d_fsync_s: float = 2.0, broken_skip_load: bool = False,
                 start_journal: bool = True) -> TenantRuntime:
    """Start one tenant. Admission opens only when the epoch journal replayed cleanly and wrote its start record; a tenant whose
    journal is torn or unterminated stays closed (`rt.revocation.state`) until the operator's break-glass recovery.
    `start_journal=False`: the caller starts the journal itself, exactly once, after it has registered the pool records' replay and
    read the pool state back (the composition root, `app.start_app`, via `pool_root.start_pools`)."""
    rev = RevocationService(tenant, epochs, epoch_dir, d_fsync_s=d_fsync_s)
    if start_journal:
        rev.start()
    tomb = TombstoneJournal(*tombstone_paths)
    guard = Guard(TokenSource(keystore))
    grants = GrantStore(persist=grant_persister(gate, tenant))
    rt = TenantRuntime(tenant, rev, tomb, guard, HoldRegistry(holds_path), grants)
    if not broken_skip_load:
        rt.guard_records = guard.load_journal(tomb.records())
        try:
            for p in load_grants(gate, tenant):
                grants.restore(p)
                rt.loaded_grants += 1
        except Exception as e:   # nothing readable: no grants, nobody can mint a session until they are made again (fail closed)
            rt.grant_load_error = f"{type(e).__name__}: {e}"
    return rt


def offboard_tenant(gate: EsGate, tenant: str, key_owner: Optional[Any] = None) -> int:
    """Tenant-wide revocation: every gateway path stops at once; with the key owner's client the cluster keys are invalidated too.
    Returns the number of keys invalidated (0 without a key owner)."""
    gate.drop_tenant(tenant)
    return len(invalidate_all(key_owner, tenant)) if key_owner is not None else 0
