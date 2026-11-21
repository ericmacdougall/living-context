"""One tenant's pools wired as the composition root wires them (gateway, pool service, pool tools, writer, class reader, sessions, epoch
table, send fences), with the documents in tests/unit/fake_es.py's DocStore and the revocation journal in memory (`Journal`; the real
one preallocates with posix_fallocate, which only the Linux host has). Used by the F10 send-fence tests."""
import json
import socket
import time
from pathlib import Path

from living_context.config import Constants
from living_context.gateway import pointers
from living_context.gateway.admin import GrantStore
from living_context.gateway.class_reader import ClassReader
from living_context.gateway.fence import SendFence
from living_context.gateway.handler import Gateway
from living_context.gateway.indices import pool_index
from living_context.gateway.pool_admin import PoolService
from living_context.gateway.pool_tools import PoolRouter, PoolTools
from living_context.gateway.sessions import EpochTable, Sessions
from living_context.ingest.subjects import Guard, TokenSource
from living_context.ingest.writer import Writer
from living_context.keystore import Keystore
from living_context.policy import ClassLattice, Principal, Role
from living_context.policy import pools as pl
from tests.unit.fake_es import DocStore

T = "tena"
LAT = {T: ClassLattice(["base"], [])}
OPS = frozenset({"query", "save", "update", "retract", "review"})
UNAV = {"ok": False, "error": "unavailable"}
TEXT = "the gannet roost is on the stack above the north landing, " * 40   # about 2.4 KB of text, several 1 KiB chunks once wrapped


class Adapter:
    human_presence = True

    def verify(self, tenant, proof):
        return proof.decode() if proof.startswith(b"user") else None


class NoModel:
    def __getattr__(self, name):
        raise AssertionError("no model at stage 0")


class Clock:
    def __init__(self):
        self.t = time.time()

    def __call__(self):
        return self.t


class Journal:
    """The tenant's revocation service, in memory: it admits, and `journalled` keeps the order of the real one (POOLS 2.5): the removal
    runs under the EXCLUSIVE side of the tenant's send fence, then its record is kept (here in a list), then the best-effort persist."""

    def __init__(self, epochs, tenant=T):
        self.epochs, self.tenant, self.records = epochs, tenant, []
        self.state, self.lane_open = "open", True
        self.extra_replay = self.extra_snapshot = None

    def admit(self):
        return True

    def journalled(self, apply, record, *, persist=None):
        result = self.epochs.under_fence(self.tenant, apply)
        self.records.append(record(result))
        if persist is not None:
            try:
                persist(result)
            except Exception:
                pass
        return result, {"denied_at": time.time(), "durable_at": time.time(), "persisted": None}


class World:
    def __init__(self, tmp: Path, n_pools: int = 2, **broken):
        self.clock = Clock()
        self.es = DocStore()
        self.ks = Keystore(tmp / "ks.db", T)
        self.dir = pl.PoolDirectory()
        store = GrantStore()
        store.restore(Principal(T, "pt-user-alice", frozenset({Role.READER, Role.WRITER}), frozenset({"base"}), frozenset({"base"})))
        store.restore(Principal(T, "pt-user-admin", frozenset({Role.TENANT_ADMIN}), frozenset({"base"}), frozenset({"base"})))
        store.restore(Principal(T, "pt-user-admin2", frozenset({Role.TENANT_ADMIN}), frozenset({"base"}), frozenset({"base"})))
        store.restore(Principal(T, "pt-user-bob", frozenset({Role.READER, Role.WRITER}), frozenset({"base"}), frozenset({"base"})))
        grants = lambda t, k: store.get(t, k)
        fences = {}
        self.fence_for = lambda t: fences.setdefault(t, SendFence())
        self.epochs = EpochTable(self.fence_for)
        self.consts = Constants(tok_live_max=100, mint_rate_per_h=1000)
        self.sessions = Sessions({T: Adapter()}, self.epochs, lambda t, w: f"pt-{w}", grants, consts=self.consts, directory=self.dir,
                                 clock=self.clock)
        self.reader = ClassReader(lambda cls: self.es, T)
        self.writer = Writer(lambda: self.es, self.ks, T, Guard(TokenSource(self.ks)), reader=self.reader)
        self.keys = pointers.PointerKeys(tmp / "pk")
        self.tools = PoolTools(T, self.dir, self.sessions, LAT, self.writer, self.reader, lambda cls: self.es, self.keys,
                               consts=self.consts, model=NoModel())
        self.journal = Journal(self.epochs)
        self.service = PoolService(T, self.dir, self.sessions, self.journal, LAT[T], lambda t, w: f"pt-{w}", grants=grants,
                                   **{"authz_client": lambda: self.es, **broken})
        self.router = PoolRouter(self.service, self.tools)
        self.gw = Gateway(None, self.sessions, LAT, pools=lambda t: self.router if t == T else None)
        self.pools = []
        for i in range(n_pools):
            pid = self.dir.create(T, "base", "team", f"pool{i:02d}").id
            self.dir.grant(T, "pt-user-alice", pid, OPS)
            self.pools.append(pid)
        self.admin = self.sessions.mint(T, b"user-admin")
        self.admin2 = self.sessions.mint(T, b"user-admin2")

    def scoped(self, conversation, pools=None, ttl_s=600, human=False):
        return self.sessions.mint_scoped(T, b"user-alice", conversation=conversation,
                                         scope={p: OPS for p in (self.pools if pools is None else pools)}, ttl_s=ttl_s, human=human)

    def plain(self, who="alice"):
        """A plain session (no token scope) of `who`."""
        return self.sessions.mint(T, f"user-{who}".encode())

    def hold(self, pool, private=()):
        """A held save of alice's in `pool` (what the write floor keeps for a human to review, POOLS 2.8), whose private source set is
        `private`; its hold id. Planted through the writer, since this world has no session taint states."""
        return self.writer.hold_tainted("pt-user-alice", "base", pool, list(private), TEXT, {"actor": "agent", "origin": "agent"})

    def admin_op(self, **req):
        return self.gw.handle(self.admin, req)

    def leaf_candidates(self):
        """A stand-in for `PoolTools._pool_candidates` (BM25 and kNN, which DocStore does not answer): every leaf of the pool's index."""
        es = self.es

        def candidates(tools, pool, text, info):
            idx = pool_index(T, pool.cls, pool.id, "leaf")
            rows = []
            for (ix, did), (src, _) in sorted(es.docs.items()):
                if ix == idx:
                    info[(pool.id, did)] = src
                    rows.append((did, 1.0))
            return rows
        return candidates

    def close(self):
        self.ks.close()


class Line:
    """One client connection to the socket server."""

    def __init__(self, address):
        self.c = socket.create_connection(address, timeout=10)
        self.buf = b""

    def send(self, token, **req):
        self.c.sendall((json.dumps({"token": token, "request": req}) + "\n").encode())

    def read(self, timeout=10.0):
        """Bytes up to and including the next newline, or every byte until the server closes the connection or `timeout` passes.
        Returns (bytes, closed)."""
        self.c.settimeout(timeout)
        while b"\n" not in self.buf:
            try:
                got = self.c.recv(65536)
            except socket.timeout:
                return self.buf, False
            if not got:
                out, self.buf = self.buf, b""
                return out, True
            self.buf += got
        line, _, self.buf = self.buf.partition(b"\n")
        return line + b"\n", False

    def answer(self):
        got, closed = self.read()
        return json.loads(got) if got.endswith(b"\n") else {"truncated": len(got), "closed": closed}

    def close(self):
        self.c.close()
