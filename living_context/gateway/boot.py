"""Gateway start-up: the composition root of one tenant's durable state (DESIGN 4.4, 14.6, 20; BUILD-PLAN Step 15).

What a restart must bring back, and from where:
  * revokes and epoch bumps: the tenant's epoch journal (`RevocationService.start` replays it, writes a durable start record and
    only then admits);
  * the erased-identifier and bare-name sets of the guard (E3): the tombstone journal (`Guard.load_journal`); without this a
    re-ingest of an erased identifier would be accepted again after a restart;
  * grants: the copy in `lc-<t>-authz` (`GrantStore.persist` writes it with the grant's revision, `load_grant_revs` reads it back),
    with the reductions the copy may not hold replayed over it from the epoch journal (`GrantStore.attach`, before the journal
    starts; `GrantStore.reduce`). Memory first: a failed write of an addition never blocks a call (the grant is lost at the next
    start, fail safe), a reduction holds whatever the write answered, and a principal whose grant is not found cannot mint a
    session (fail closed);
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
from .reads import IncompleteRead, put_doc_landed, scan
from .revocation import OPEN, RevocationService
from .sessions import EpochTable



def grant_doc(tenant: str, p: Principal, rev: int = 0) -> dict:
    """The authz record of a grant at revision `rev` (`GrantStore`): lineage names the principal's token, so erasing the person
    deletes it (14.1)."""
    return {"tenant": tenant, "cls": "tenantwide", "kind": "grant", "lin_leaves": [], "lin_leaf_ids": [], "lin_nodes": [],
            "lin_subjects": [p.token], "lin_queries": [], "derivation": "grant", "erasure_rewrite": 0, "schema_v": 1,
            "created_at": datetime.datetime.now(datetime.timezone.utc).isoformat(), "principal": p.token,
            "roles": sorted(r.value for r in p.roles), "read": sorted(p.read_set), "write": sorted(p.write_set),
            "own_class": p.own_class or "", "rev": rev}


def grant_persister(gate: EsGate, tenant: str) -> Callable[..., None]:
    """The authz copy of a grant. Written only on a well-formed acknowledgement about exactly this record (`reads.put_doc_landed`
    through the authz alias); any other answer raises, so the caller counts the write as failed (review round 6)."""
    def persist(p: Principal, rev: int = 0) -> None:
        try:
            put_doc_landed(gate.client(tenant, "authz"), f"lc-{tenant}-authz", f"gr:{p.token}", grant_doc(tenant, p, rev),
                           "refresh=true", alias=True)
        except IncompleteRead as e:
            raise RuntimeError("authz write refused") from e
    return persist


def load_grants(gate: EsGate, tenant: str, *, page: int = 1000) -> list[Principal]:
    """Every grant of the tenant's authz index (`load_grant_revs`, without the revisions)."""
    return [p for p, _ in load_grant_revs(gate, tenant, page=page)]


def load_grant_revs(gate: EsGate, tenant: str, *, page: int = 1000) -> list[tuple[Principal, int]]:
    """Every grant record of the tenant's authz index with its revision (0 for a record written before revisions), in one snapshot
    paged until exhausted (`reads.scan`). A failed or partial page is an error (`reads.complete_hits`; the caller starts with no
    grants, which fails closed: nobody can mint a session until a grant is made again)."""
    out = []
    for h in scan(gate.client(tenant, "authz"), f"lc-{tenant}-authz", {"query": {"term": {"kind": "grant"}}}, page=page):
        s = h["_source"]
        try:
            rev = s.get("rev", 0)
            if type(rev) is not int or rev < 0:
                raise ValueError("revision")
            out.append((Principal(tenant, s["principal"], frozenset(Role(x) for x in s["roles"]), frozenset(s.get("read", [])),
                                  frozenset(s.get("write", [])), s.get("own_class") or None), rev))
        except (KeyError, ValueError, TypeError):
            continue   # a record that does not parse is no grant
    return out


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
    grants = GrantStore(persist=grant_persister(gate, tenant))
    grants.attach(tenant, rev)   # before the journal starts: its replay narrows the grants read back to the unconfirmed reductions
    if start_journal:
        rev.start()
    tomb = TombstoneJournal(*tombstone_paths)
    guard = Guard(TokenSource(keystore))
    rt = TenantRuntime(tenant, rev, tomb, guard, HoldRegistry(holds_path), grants)
    if not broken_skip_load:
        rt.guard_records = guard.load_journal(tomb.records())
        try:
            for p, r in load_grant_revs(gate, tenant):
                grants.restore(p, r)
                rt.loaded_grants += 1
            grants.loaded(tenant)   # the read was complete (a failed or partial one raised before any restore)
        except Exception as e:   # nothing readable: no grants, nobody can mint a session until they are made again (fail closed)
            rt.grant_load_error = f"{type(e).__name__}: {e}"
    return rt


def offboard_tenant(gate: EsGate, tenant: str, key_owner: Optional[Any] = None) -> int:
    """Tenant-wide revocation: every gateway path stops at once; with the key owner's client the cluster keys are invalidated too.
    Returns the number of keys invalidated (0 without a key owner)."""
    gate.drop_tenant(tenant)
    return len(invalidate_all(key_owner, tenant)) if key_owner is not None else 0
