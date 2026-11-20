"""The gateway's composition root (DESIGN 3, 15; POOLS 2.5, 11): what a deployment starts. One call wires, for every tenant, the durable
state (`boot.start_tenant`: tombstones, guard, class grants), the writer, the pool eraser and the pools (`pool_root.start_pools`: the authz
read-back, then the ONE start of the tenant's epoch journal, which must open), then the request handler (`handler.Gateway`) and the local
socket server (`server.Server`), whose `mint` line is the only way to obtain a token over the socket.

A tenant whose pools do not start (an incomplete authz read, a journal that does not open) is not served: `start_app` raises
`pool_root.PoolStartRefused` rather than returning a gateway that could serve stale grants. Index creation and deletion are cluster
privileges no tenant key holds; the caller passes them in (`TenantSetup.create_index`, `drop_index`), as provisioning does.

Not wired here (stated limit): the class-level administrator operations (`admin.Admin`); `ADMIN_OPS` answer `refused` from this root."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Optional, Sequence

from ..config import Constants
from ..erasure.pool_scope import PoolEraser
from ..ingest.writer import Writer
from ..keystore.store import Keystore
from ..policy import ClassLattice
from ..policy import pools as pl
from . import boot, pool_boot
from .class_reader import ClassReader
from .fence import SendFence
from .gate import WriteGate
from .handler import EsGate, Gateway
from .pool_root import PoolRuntime, start_pools
from .server import Server
from .sessions import EpochTable, IdentityAdapter, Sessions


@dataclass
class TenantSetup:
    tenant: str
    lattice: ClassLattice
    epoch_dir: Path
    tombstone_paths: tuple
    keystore: Keystore
    holds_path: Path
    pointer_key_path: Path
    create_index: Callable[[str, dict], None]
    drop_index: Callable[[str], None]


@dataclass
class App:
    gateway: Gateway
    server: Server
    sessions: Sessions
    epochs: EpochTable
    tenants: dict
    pools: dict

    def stop(self) -> None:
        """Clean stop: the socket closes first, then each tenant's journal (admission and the revoke lane, the drain, the shutdown
        record)."""
        self.server.stop()
        for rt in self.tenants.values():
            rt.revocation.clean_stop()


def start_app(gate: EsGate, setups: Sequence[TenantSetup], adapters: Mapping[str, IdentityAdapter],
              principal_token: Callable[[str, str], str], *, consts: Optional[Constants] = None, host: str = "127.0.0.1",
              port: int = 0) -> App:
    consts = consts or Constants()
    fences: dict = {}
    fence_for = lambda t: fences.setdefault(t, SendFence())
    epochs = EpochTable(fence_for)
    directory = pl.PoolDirectory()
    tenants = {s.tenant: boot.start_tenant(s.tenant, gate=gate, epochs=epochs, epoch_dir=s.epoch_dir, tombstone_paths=s.tombstone_paths,
                                           keystore=s.keystore, holds_path=s.holds_path, start_journal=False) for s in setups}

    def grants(t: str, k: str):
        rt = tenants.get(t)
        return rt.grants.get(t, k) if rt is not None else None

    def persist_token(h: str, d: dict) -> None:   # each token record goes to ITS tenant's authz copy
        pool_boot.token_persister(lambda: gate.client(d["tenant"], "authz"), d["tenant"])(h, d)

    sessions = Sessions(dict(adapters), epochs, principal_token, grants, consts=consts, directory=directory, token_persist=persist_token)
    pools: dict[str, PoolRuntime] = {}
    writers: dict[str, Writer] = {}
    try:
        for s in setups:
            t = s.tenant
            wgate = WriteGate()
            reader = ClassReader(lambda cls, t=t: gate.client(t, f"read-{cls}"), t)
            writers[t] = Writer(lambda t=t: gate.client(t, "ingest"), s.keystore, t, tenants[t].guard, reader=reader, gate=wgate,
                                consts=consts)
            eraser = PoolEraser(lambda t=t: gate.client(t, "erase"), s.drop_index, t, s.keystore, tenants[t].tombstones, wgate)
            pools[t] = start_pools(t, gate=gate, sessions=sessions, directory=directory, lattice=s.lattice,
                                   revocation=tenants[t].revocation, writer=writers[t], pointer_key_path=s.pointer_key_path,
                                   create_index=s.create_index, grants=grants, principal_token=principal_token,
                                   eraser=lambda cls, pool, e=eraser: e.delete(cls, pool), consts=consts)
    except BaseException:
        for rt in tenants.values():
            rt.revocation.clean_stop()
        raise
    gw = Gateway(gate, sessions, {s.tenant: s.lattice for s in setups}, writers=writers.get,
                 pools=lambda t: pools[t].router if t in pools else None)
    server = Server(gw, sessions, epochs, fence_for, consts=consts, host=host, port=port).start()
    return App(gw, server, sessions, epochs, tenants, pools)
