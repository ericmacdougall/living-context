"""The composition root of one tenant's pools: what the gateway start-up calls to wire the pool service, the agent tools, the class reader,
the pointer keys and the taint state, and to read the pool state back before admission opens (POOLS 2.5, 11).

Order, as `pool_boot` requires: the pool, grant and token records are read back from the authz copy (a failed or partial read raises, so the
tenant does not start), THEN the epoch journal is started (it replays on top, so a removal the copy still holds stays removed) and must
OPEN (a torn or unterminated journal, or one held by another process, refuses the start: `PoolStartRefused`), THEN this process's block of
pool counters is reserved in the journal, THEN each unconfirmed removal is written again and confirmed when that write lands (after a
complete read only). While the runtime serves, `PoolRouter.admit` (the journal's admission) gates every pool and token operation. The result is a `PoolRouter` for `Gateway(pools=...)`; the Gateway's `mint` and the scoped
operations are the only way to obtain or change a scoped token."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Optional

from ..config import Constants
from ..policy import ClassLattice
from ..policy import pools as pl
from . import pool_boot
from .class_reader import ClassReader
from .handler import EsGate
from .pointers import PointerKeys
from .pool_admin import PoolService
from .pool_tools import PoolRouter, PoolTools
from .revocation import OPEN, RevocationService
from .session_state import SessionStates, es_load, es_persist
from .sessions import Sessions


class PoolStartRefused(RuntimeError):
    """The tenant's pools do not start: the authz read was not complete, or the epoch journal did not open (torn, unterminated, held
    by another process). No runtime exists, so nothing can mint a scoped token or reach a pool from the state read so far."""


@dataclass
class PoolRuntime:
    router: PoolRouter
    service: PoolService
    tools: PoolTools
    reader: ClassReader
    loaded: dict


def start_pools(tenant: str, *, gate: EsGate, sessions: Sessions, directory: pl.PoolDirectory, lattice: ClassLattice,
                revocation: RevocationService, writer: Any, pointer_key_path: Path, create_index: Callable[[str, dict], None],
                grants: Callable[[str, str], Any], principal_token: Callable[[str, str], str],
                eraser: Optional[Callable[[str, str], Any]] = None, consts: Optional[Constants] = None) -> PoolRuntime:
    """`sessions` must have been built with the same `directory` and with `token_persist=pool_boot.token_persister(...)`."""
    reader = ClassReader(lambda cls: gate.client(tenant, f"read-{cls}"), tenant)
    svc = PoolService(tenant, directory, sessions, revocation, lattice, principal_token, create_index=create_index,
                      ctl_client=lambda cls: gate.client(tenant, "ingest"), authz_client=lambda: gate.client(tenant, "authz"),
                      reader=reader, grants=grants, eraser=eraser)
    try:
        loaded = pool_boot.load(lambda: gate.client(tenant, "authz"), tenant, directory, sessions, svc)
    except Exception as e:   # a failed or partial read (reads.IncompleteRead included): the tenant does not start
        raise PoolStartRefused("authz read incomplete") from e
    if revocation.start() != OPEN:   # the journal refused before replaying its removals: the state read so far is not authoritative
        raise PoolStartRefused("epoch journal did not open")
    try:
        svc.reserve_counters()   # before any change: this process's counters are above every earlier process's
    except Exception as e:
        raise PoolStartRefused("counter reservation not durable") from e
    svc.confirm_against_authz(complete=loaded["complete"])
    states = SessionStates({tenant: lattice}, es_persist(lambda: gate.client(tenant, "ingest"), tenant),
                           es_load(lambda: gate.client(tenant, "ingest"), tenant))
    tools = PoolTools(tenant, directory, sessions, {tenant: lattice}, writer, reader, lambda cls: gate.client(tenant, f"read-{cls}"),
                      PointerKeys(pointer_key_path), consts=consts, states=states)
    return PoolRuntime(PoolRouter(svc, tools), svc, tools, reader, loaded)
