"""The pools through the real Gateway handler on a real Elasticsearch, wired by the composition root `pool_root.start_pools` (review: the
foundation was tested as classes, not as the product): mint over the socket operation, save, query, fetch, update, retract, scoped-token
default-deny, delegation, revocation, and a restart that keeps a removed grant removed. Needs LC_TEST_ES_URL, LC_TEST_CA and LC_TEST_PW."""
import tempfile
import unittest
from pathlib import Path

from living_context.config import Constants
from living_context.gateway import epoch_journal, pool_boot
from living_context.gateway.class_reader import ClassReader
from living_context.gateway.handler import Gateway
from living_context.gateway.pool_root import start_pools
from living_context.gateway.revocation import RevocationService
from living_context.gateway.sessions import Sessions
from living_context.ingest.subjects import Guard, TokenSource
from living_context.ingest.writer import Writer
from living_context.policy import pools as pl
from tests.security.test_pools_es import Adapter, Base, LAT, T, UNAV


class GatewayE2E(Base):
    def setUp(self):
        self.consts = Constants(tok_live_max=100, mint_rate_per_h=1000)
        self.jdir = Path(tempfile.mkdtemp(dir=self.root)) / "j"
        epoch_journal.provision(self.jdir, 65536)
        self.pkeys = self.root / f"e2e-pk{Base.n + 1}"
        super().setUp()                                                   # which calls build()

    def build(self, **kw):
        self.dir = pl.PoolDirectory()
        self.sessions = Sessions({T: Adapter()}, self.epochs, lambda t, w: f"pt-{w}", lambda t, k: self.store.get(t, k), consts=self.consts,
                                 directory=self.dir, token_persist=pool_boot.token_persister(lambda: self.gate.client(T, "authz"), T))
        self.rev = RevocationService(T, self.epochs, self.jdir)
        wreader = ClassReader(lambda cls: self.cc(f"read-{cls}"), T)
        self.writer = Writer(lambda: self.cc("ingest"), self.ks, T, Guard(TokenSource(self.ks)), reader=wreader, gate=self.wgate)
        self.rt = start_pools(T, gate=self.gate, sessions=self.sessions, directory=self.dir, lattice=LAT[T], revocation=self.rev, writer=self.writer,
                              pointer_key_path=self.pkeys, create_index=self._create, grants=lambda t, k: self.store.get(t, k),
                              principal_token=lambda t, w: f"pt-{w}", eraser=lambda cls, pool: self.eraser().delete(cls, pool), consts=self.consts)
        self.gw = Gateway(self.gate, self.sessions, LAT, writers=lambda t: self.writer, pools=lambda t: self.rt.router)

    def restart(self):
        self.rev.clean_stop()
        self.build()

    def adm(self, **req):
        return self.gw.handle(self.sessions.mint(T, b"user-admin"), req)

    def mint_tok(self, who, scope, conv="c1"):
        r = self.gw.mint(T, b"user-" + who.encode(), {"conversation": conv, "scope": {k: sorted(v) for k, v in scope.items()}})
        self.assertTrue(r["ok"], r)
        return r["token"]

    def test_the_whole_flow_through_the_handler_and_a_restart(self):
        r = self.adm(op="pool_create", cls="base", kind="team", label="alpha team")
        self.assertTrue(r["ok"], r)
        a = r["pool"]
        ops = ["query", "save", "update", "retract"]
        self.assertTrue(self.adm(op="pool_grant", who="user-alice", pool=a, ops=ops)["ok"])
        tok = self.mint_tok("alice", {a: ops})
        s = self.gw.handle(tok, {"op": "pool_save", "pool": a, "text": "the kestrel nests on the north ledge", "key": "k1"})
        self.assertTrue(s["ok"], s)
        self.refresh()
        q = self.gw.handle(tok, {"op": "pool_query", "query": "kestrel ledge"})
        self.assertEqual([i["pointer"] for i in q["items"]], [s["pointer"]])
        f = self.gw.handle(tok, {"op": "pool_fetch", "pointer": s["pointer"]})
        self.assertEqual(f["status"], "ok")
        u = self.gw.handle(tok, {"op": "pool_update", "pointer": s["pointer"], "mode": "correct", "text": "the kestrel nests on the south ledge", "key": "u1"})
        self.assertTrue(u["ok"], u)
        self.refresh()
        # scoped default-deny through the real handler: no class-level operation, no administrator operation
        for req in ({"op": "write", "cls": "base", "doc": {"text": "x"}}, {"op": "search", "cls": "base", "text": "kestrel"},
                    {"op": "lookup", "pointer": "x"}, {"op": "pool_create", "cls": "base", "kind": "team", "label": "beta"},
                    {"op": "pool_detach", "pool": a}, {"op": "pool_grant", "who": "user-bob", "pool": a},
                    {"op": "grant"}, {"op": "break_glass_revoke"}):
            self.assertEqual(self.gw.handle(tok, req), UNAV, req["op"])
        # delegation narrows, never widens
        d = self.gw.handle(tok, {"op": "token_delegate", "scope": {a: ["query"]}, "ttl_s": 60})
        self.assertTrue(d["ok"], d)
        child = d["token"]
        self.assertEqual(len(self.gw.handle(child, {"op": "pool_query", "query": "kestrel ledge"})["items"]), 1)
        self.assertEqual(self.gw.handle(child, {"op": "pool_save", "pool": a, "text": "child write", "key": "c1"}), UNAV)
        self.assertEqual(self.gw.handle(child, {"op": "token_delegate", "scope": {a: ["query"]}, "ttl_s": 30}), UNAV)   # a child cannot delegate
        self.assertTrue(self.gw.handle(tok, {"op": "token_renew"})["ok"])
        # retract, then revoke the root: the child ends with it
        g = self.gw.handle(tok, {"op": "pool_retract", "pointers": [u["pointer"]], "reason": "wrong", "key": "r1"})
        self.assertEqual(g["results"], ["retracted"])
        self.assertTrue(self.gw.handle(tok, {"op": "token_revoke"})["ok"])
        self.assertEqual(self.gw.handle(tok, {"op": "pool_query", "query": "kestrel"}), UNAV)
        self.assertEqual(self.gw.handle(child, {"op": "pool_query", "query": "kestrel"}), UNAV)
        # restart: the pool and grant come back from the authz copy, a pointer sealed before still opens, a removed grant stays removed
        self.restart()
        tok2 = self.mint_tok("alice", {a: ops}, conv="c2")
        self.assertEqual(self.gw.handle(tok2, {"op": "pool_fetch", "pointer": s["pointer"]})["status"], "superseded")
        self.assertTrue(self.adm(op="pool_ungrant", who="user-alice", pool=a)["ok"])
        self.assertEqual(self.gw.handle(tok2, {"op": "pool_query", "query": "kestrel"}), UNAV)
        self.restart()
        r = self.gw.mint(T, b"user-alice", {"conversation": "c3", "scope": {a: ["query"]}})
        self.assertEqual(r, UNAV)
        # the socket mint takes no `human` flag and refuses a pool not held and a bad shape
        self.assertEqual(self.gw.mint(T, b"user-alice", {"conversation": "c4", "scope": {a: ["query"]}, "human": True}), UNAV)
        self.assertEqual(self.gw.mint(T, b"user-alice", {"scope": "nope"}), UNAV)


if __name__ == "__main__":
    unittest.main()
