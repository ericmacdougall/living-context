"""Review round 8, the pool lane of F1, on a real Elasticsearch: a pool-grant write that reaches the index late never brings a removed
grant back (tests/security/test_pool_late_writes.py has the same cases on the fake index).

The grant's write is kept by the client wrapper instead of being sent (the caller sees a 503, as when the client times out), and is
sent to the cluster later, unchanged, with the request line the code built. At ad6aa1a that request was an unconditional PUT, which the
cluster applies whenever it arrives; now it is a compare-and-set against the record the write read, which the cluster refuses with its
typed 409 once the removal's own write moved the record on. The tenant is wired by the composition root (`start_pools`). Needs
LC_TEST_ES_URL, LC_TEST_CA and LC_TEST_PW; without them the module FAILS (it never skips)."""
import unittest

from living_context.policy import pools as pl
from tests.security.test_pools_delta_es import Root
from tests.security.test_pools_es import T, UNAV


class Held:
    """The tenant's real authz client, except that a PUT of a held record is kept, not sent (the caller sees a 503)."""

    def __init__(self, inner, owner):
        self.inner, self.owner = inner, owner

    def request(self, method, path, body=None, **kw):
        if method == "PUT" and any(f"/_doc/{d}" in path for d in self.owner.hold):
            self.owner.held.append((method, path, body))
            return 503, {}
        return self.inner.request(method, path, body, **kw)


class HeldGate:
    def __init__(self, gate):
        self.gate, self.hold, self.held = gate, set(), []

    def client(self, tenant, role):
        c = self.gate.client(tenant, role)
        return Held(c, self) if role == "authz" else c


class LateGrantWrites(Root):
    labels = 0

    def start(self):
        self.hg = HeldGate(self.gate)
        self.rev.clean_stop()
        self.build(gate=self.hg)

    def restart(self):
        self.rev.compact()
        self.rev.clean_stop()
        self.build(gate=self.hg)

    def pool(self):
        LateGrantWrites.labels += 1
        r = self.adm(op="pool_create", cls="base", kind="team", label=f"late crew {LateGrantWrites.labels}")
        self.assertTrue(r["ok"], r)
        return r["pool"]

    def removed(self, a, why):
        self.assertEqual(self.dir.live_ops(T, "pt-user-alice", a), frozenset(), f"the removed grant came back: {why}")
        r = self.gw.mint(T, b"user-alice", {"conversation": f"c-{LateGrantWrites.labels}-{len(why)}", "scope": {a: ["query"]}})
        self.assertEqual(r, UNAV, why)

    def land_late(self):
        """Send every held request to the cluster as built; the answers."""
        out = [self.gate.client(T, "authz").request(m, p, b) for m, p, b in self.hg.held]
        self.hg.held = []
        return out

    def finish(self, a, why):
        answers = self.land_late()
        self.restart()
        self.removed(a, why)
        self.restart()
        self.removed(a, why + ", a second restart")
        for st, r in answers:   # the mechanism, after the outcome: the cluster refused the late write with its typed conflict
            self.assertEqual(st, 409, f"the late write was applied: {why}")
            self.assertEqual(r["error"]["type"], "version_conflict_engine_exception")

    def test_a_late_grant_write_after_a_confirmed_removal(self):
        self.start()
        a = self.pool()
        self.hg.hold = {f"pg:pt-user-alice:{a}"}
        self.assertTrue(self.adm(op="pool_grant", who="user-alice", pool=a)["ok"])
        self.hg.hold = set()
        self.assertEqual(len(self.hg.held), 1)
        r = self.adm(op="pool_ungrant", who="user-alice", pool=a)
        self.assertEqual((r["ok"], r["durable"]), (True, True))
        self.assertEqual(self.rt.service._unconfirmed, {})   # its own write landed
        self.finish(a, "a late write after a confirmed removal")

    def test_a_late_grant_write_after_a_removal_confirmed_at_the_next_start(self):
        self.start()
        a = self.pool()
        self.hg.hold = {f"pg:pt-user-alice:{a}"}
        self.assertTrue(self.adm(op="pool_grant", who="user-alice", pool=a)["ok"])
        self.hg.hold = set()
        self.block_authz(True)
        try:
            self.assertTrue(self.adm(op="pool_ungrant", who="user-alice", pool=a)["durable"])
        finally:
            self.block_authz(False)
        self.restart()   # the copy holds no grant of alice on `a`
        self.removed(a, "the start")
        self.finish(a, "a late write after a start that confirmed the removal")

    def test_control_a_grant_whose_write_lands_late_with_nothing_after_it_is_kept(self):
        self.start()
        a = self.pool()
        self.hg.hold = {f"pg:pt-user-alice:{a}"}
        self.assertTrue(self.adm(op="pool_grant", who="user-alice", pool=a)["ok"])
        self.hg.hold = set()
        (st, r), = self.land_late()
        self.assertIn(st, (200, 201), r)                      # nothing wrote the record since it was read: the write lands
        self.restart()
        self.assertEqual(self.dir.live_ops(T, "pt-user-alice", a), pl.DEFAULT_GRANT)


if __name__ == "__main__":
    unittest.main()
