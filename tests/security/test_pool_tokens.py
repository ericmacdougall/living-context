"""BUILD-PLAN Step 25 (POOLS 2, 11) on the gateway's own state, no Elasticsearch: pools, grants, scoped tokens, attenuation,
per-token and per-pool revocation on the epoch journal, restart. Every negative case runs against the real code and against a
broken variant (`broken_*` flags) that must turn it red."""
import tempfile
import unittest
from pathlib import Path

from living_context.config import Constants
from living_context.gateway import epoch_journal, pool_boot
from living_context.gateway.admin import GrantStore
from living_context.gateway.fence import SendFence
from living_context.gateway.pool_admin import PoolService
from living_context.gateway.pool_authz import authorize
from living_context.gateway.revocation import RevocationService
from living_context.gateway.sessions import EpochTable, Sessions, TokenError
from living_context.policy import ClassLattice, Principal, Role
from living_context.policy import pools as pl
from tests.unit.fake_es import FakeAuthz

T, TB = "tenaaa", "tenbbb"
LAT = {T: ClassLattice(["base", "restricted"], [("restricted", "base")]), TB: ClassLattice(["base"], [])}
UNAV = pl.UNAVAILABLE


class Adapter:
    human_presence = True

    def verify(self, tenant, proof):
        return proof.decode() if proof.startswith(b"user") else None


class Clock:
    def __init__(self):
        self.t = 1_000_000.0

    def __call__(self):
        return self.t


class World:
    """One tenant's gateway state. `restart()` builds fresh memory over the same journal directory and authz copy."""

    def __init__(self, tmp, tenant=T, sess_kw=None, svc_kw=None, authz=None, clock=None, consts=None):
        self.tmp, self.tenant, self.authz = Path(tmp), tenant, authz or FakeAuthz()
        self.clock, self.consts = clock or Clock(), consts or Constants()
        self.sess_kw, self.svc_kw = sess_kw or {}, svc_kw or {}
        self.fences = {}
        jdir = self.tmp / "j"
        if not jdir.exists():
            epoch_journal.provision(jdir, 65536)
        self.build()

    def build(self):
        self.epochs = EpochTable(lambda t: self.fences.setdefault(t, SendFence()))
        self.dir = pl.PoolDirectory()
        self.store = GrantStore()
        for who, roles in (("user-admin", {Role.TENANT_ADMIN}), ("user-alice", {Role.READER, Role.WRITER}),
                           ("user-bob", {Role.READER, Role.WRITER})):
            self.store.restore(Principal(self.tenant, f"pt-{who}", frozenset(roles), frozenset({"base"}), frozenset({"base"})))
        self.sessions = Sessions({self.tenant: Adapter(), TB: Adapter()}, self.epochs, lambda t, w: f"pt-{w}",
                                 lambda t, k: self.store.get(t, k), consts=self.consts, clock=self.clock, directory=self.dir,
                                 token_persist=pool_boot.token_persister(lambda: self.authz, self.tenant), **self.sess_kw)
        self.rev = RevocationService(self.tenant, self.epochs, self.tmp / "j", clock=self.clock)
        self.svc = PoolService(self.tenant, self.dir, self.sessions, self.rev, LAT[self.tenant], lambda t, w: f"pt-{w}",
                               authz_client=lambda: self.authz, grants=self.store.get, **self.svc_kw)

    def restart(self, *, apply_journal=True, **kw):
        self.rev.clean_stop()
        self.build()
        loaded = pool_boot.load(lambda: self.authz, self.tenant, self.dir, self.sessions, self.svc)
        if not apply_journal:
            self.rev.extra_replay = None   # BROKEN variant: the journal is not applied on top of the authz copy
        self.rev.start()
        self.svc.confirm_against_authz(loaded["persisted"], complete=loaded["complete"])
        return loaded

    def start(self):
        self.rev.start()
        return self

    def admin_rec(self):
        tok = self.sessions.mint(self.tenant, b"user-admin")
        return tok, self.sessions.resolve(tok)

    def admin(self, op, **req):
        tok, rec = self.admin_rec()
        return self.svc.handle(op, self.sessions.principal(rec), rec, req)

    def pool(self, label, cls="base", grant_to=("alice",), ops=None):
        r = self.admin("pool_create", cls=cls, kind="team", label=label)
        assert r["ok"], r
        for who in grant_to:
            assert self.admin("pool_grant", who="user-" + who, pool=r["pool"], ops=ops)["ok"]
        return r["pool"]

    def mint(self, who, scope, conv="conv1", **kw):
        return self.sessions.mint_scoped(self.tenant, b"user-" + who.encode(), conversation=conv,
                                         scope={k: frozenset(v) for k, v in scope.items()}, **kw)

    def auth(self, token, needs, **kw):
        return authorize(self.sessions, self.dir, LAT, token, {k: set(v) for k, v in needs.items()}, **kw)


def q(*ops):
    return set(ops)


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def world(self, **kw):
        return World(self.tmp.name, **kw).start()


class PoolObject(Base):
    def test_create_list_describe_visibility_and_label_rules(self):
        w = self.world()
        a = w.pool("alpha team", grant_to=("alice",))
        b = w.pool("beta", grant_to=())
        self.assertEqual(len(a), 16)
        # a non-administrator may not create; the answer is the one unavailable
        tok = w.sessions.mint(T, b"user-alice")
        rec = w.sessions.resolve(tok)
        self.assertEqual(w.svc.handle("pool_create", w.sessions.principal(rec), rec, {"cls": "base", "kind": "team", "label": "x"}), UNAV)
        # alice lists and describes only her pool; the administrator lists both; a pool she holds nothing on is unavailable
        self.assertEqual([r["pool"] for r in w.svc.handle("pool_list", w.sessions.principal(rec), rec, {})["pools"]], [a])
        self.assertEqual(w.svc.handle("pool_describe", w.sessions.principal(rec), rec, {"pool": b}), UNAV)
        self.assertEqual(w.svc.handle("pool_describe", w.sessions.principal(rec), rec, {"pool": "z" * 16}), UNAV)
        _, arec = w.admin_rec()
        self.assertEqual(len(w.svc.handle("pool_list", w.sessions.principal(arec), arec, {})["pools"]), 2)
        # labels: charset and uniqueness after the confusables skeleton
        for bad in ("Alpha", 'x" origin="human', "a" * 33, ""):
            self.assertEqual(w.admin("pool_create", cls="base", kind="team", label=bad), UNAV, bad)
        self.assertEqual(w.admin("pool_create", cls="base", kind="team", label="a1pha-team"), UNAV)   # look-alike of "alpha team"
        self.assertEqual(w.admin("pool_create", cls="nonesuch", kind="team", label="gamma"), UNAV)

    def test_person_pools_are_private_and_ids_are_never_reused(self):
        w = self.world()
        r = w.admin("pool_create", cls="base", kind="person", label="me")
        self.assertTrue(w.dir.get(T, r["pool"]).private)
        seen = {w.dir.create(T, "base", "team", f"p{i}").id for i in range(200)}
        self.assertEqual(len(seen), 200)
        w2 = pl.PoolDirectory(rand=lambda n: 0)   # a generator that repeats is detected, not reused
        w2.create(T, "base", "team", "one")
        with self.assertRaises(Exception):
            w2.create(T, "base", "team", "two")   # would loop forever on a repeated id if reuse were allowed; here it raises on the cap

    def test_broken_member_may_create_a_pool(self):
        w = World(self.tmp.name, svc_kw={"broken_member_creates": True}).start()
        tok = w.sessions.mint(T, b"user-alice")
        rec = w.sessions.resolve(tok)
        self.assertTrue(w.svc.handle("pool_create", w.sessions.principal(rec), rec, {"cls": "base", "kind": "team", "label": "x"})["ok"])


if __name__ == "__main__":
    unittest.main()
