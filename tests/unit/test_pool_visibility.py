"""F16 (round 4, CRITICAL, auth boundary): seeing a pool is checked live at every call.

Review F16: `pool_list` and `pool_describe` checked a scoped token's mint-time scope and the pool state only (pool_admin.py:127-135 at
dbc528f), so after every grant of the principal on pool A was removed, a NEW request with the unchanged token still listed A and still
described it, card, label, class and private flag included. POOLS 2.7 counts "not granted" as unavailable, and a card is administrator
text visible to grantees (4.1). Every request here goes through `Gateway.serve` over the pools world of tests/unit/pool_world.py.

Callers: a scoped token of alice, alice's plain session (a member), the tenant administrator's plain session. The one rule
(`pool_authz.sees`): a scoped token sees a pool when some operation of its scope on it passes check 4 now (active, scope, live grant,
class); a member when some operation passes check 4 now; the administrator every pool of the tenant. `VIEW` is a pseudo-need, never an
operation: it cannot be granted, minted or delegated.
"""
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from living_context.gateway import pool_authz
from living_context.policy import ClassLattice
from living_context.policy import pools as pl
from tests.unit import pool_world
from tests.unit.pool_world import OPS, T, UNAV, World

CARD = "the gannet roost card: north landing, ask the warden first"


class Fixture(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.w = World(Path(self._tmp.name))
        self.A, self.B = self.w.pools
        made = self.w.admin_op(op="pool_create", cls="base", kind="team", label="carded", card=CARD)
        self.assertTrue(made.get("ok"), made)
        self.C = made["pool"]
        self.assertEqual(self.w.admin_op(op="pool_grant", who="user-alice", pool=self.C, ops=sorted(OPS)), {"ok": True})

    def tearDown(self):
        self.w.close()
        self._tmp.cleanup()

    def tok(self, pools=None):
        return self.w.sessions.mint_scoped(T, b"user-alice", conversation="c1", scope={p: OPS for p in (pools or [self.A, self.B, self.C])},
                                           ttl_s=600)

    def ask(self, token, **req):
        return self.w.gw.serve(token, req).body

    def listed(self, token):
        out = self.ask(token, op="pool_list")
        self.assertTrue(out.get("ok"), out)
        return {r["pool"] for r in out["pools"]}

    def ungrant(self, pool):
        self.assertTrue(self.w.admin_op(op="pool_ungrant", who="user-alice", pool=pool).get("ok"))


class FreshRequestAfterUngrant(Fixture):
    def test_a_fresh_describe_after_the_ungrant_is_unavailable_and_carries_no_card(self):
        tok = self.tok()
        before = self.ask(tok, op="pool_describe", pool=self.C)
        self.assertEqual((before.get("ok"), before.get("card")), (True, CARD), before)   # control: granted, the card is shown
        self.ungrant(self.C)
        after = self.ask(tok, op="pool_describe", pool=self.C)
        self.assertEqual(after, UNAV, "the card of an ungranted pool was answered to a fresh request")
        self.assertNotIn("gannet roost card", str(after))
        self.assertEqual(self.ask(tok, op="pool_describe", pool="z" * 16), after)   # the same answer as a pool that does not exist

    def test_a_fresh_list_after_the_ungrant_leaves_the_pool_out(self):
        tok = self.tok()
        self.assertEqual(self.listed(tok), {self.A, self.B, self.C})
        self.ungrant(self.C)
        self.assertEqual(self.listed(tok), {self.A, self.B}, "an ungranted pool is still listed to a fresh request")

    def test_a_partial_ungrant_keeps_the_pool_visible_while_one_scoped_operation_still_passes(self):
        tok = self.tok()
        self.assertTrue(self.w.admin_op(op="pool_ungrant", who="user-alice", pool=self.C, ops=["query", "review", "retract"]).get("ok"))
        self.assertEqual(self.ask(tok, op="pool_describe", pool=self.C).get("card"), CARD)   # save and update are still held
        self.assertIn(self.C, self.listed(tok))

    def test_the_members_plain_session_and_the_administrator(self):
        member = self.w.plain()
        self.assertEqual(self.ask(member, op="pool_describe", pool=self.C).get("card"), CARD)
        self.ungrant(self.C)
        self.assertEqual(self.ask(member, op="pool_describe", pool=self.C), UNAV)
        self.assertNotIn(self.C, self.listed(member))
        self.assertEqual(self.ask(self.w.admin, op="pool_describe", pool=self.C).get("card"), CARD)   # the administrator sees every pool
        self.assertIn(self.C, self.listed(self.w.admin))
        self.assertEqual(self.listed(self.w.plain("bob")), set())   # a member with no grant sees nothing

    def test_a_detached_pool_is_seen_by_the_administrator_only(self):
        tok, member = self.tok(), self.w.plain()
        self.assertTrue(self.w.admin_op(op="pool_detach", pool=self.C).get("ok"))
        for who in (tok, member):
            self.assertEqual(self.ask(who, op="pool_describe", pool=self.C), UNAV)
            self.assertNotIn(self.C, self.listed(who))
        self.assertEqual(self.ask(self.w.admin, op="pool_describe", pool=self.C).get("state"), pl.DETACHED)


class ClassOfThePool(unittest.TestCase):
    """A live grant on a pool whose class is outside the principal's read and write sets does not make the pool visible (check 4)."""

    def test_a_pool_of_a_class_the_principal_may_not_read_or_write_is_not_seen(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.dict(pool_world.LAT, {T: ClassLattice(["base", "restricted"],
                                                                                                  [("restricted", "base")])}):
            w = World(Path(d))
            r = w.dir.create(T, "restricted", "team", "restricted one", CARD).id
            w.dir.grant(T, "pt-user-alice", r, OPS)
            tok = w.sessions.mint_scoped(T, b"user-alice", conversation="c1", scope={r: OPS, w.pools[0]: OPS}, ttl_s=600)
            for who in (tok, w.plain()):
                self.assertEqual(w.gw.serve(who, {"op": "pool_describe", "pool": r}).body, UNAV)
                self.assertEqual({x["pool"] for x in w.gw.serve(who, {"op": "pool_list"}).body["pools"]} & {r}, set())
                self.assertTrue(w.gw.serve(who, {"op": "pool_describe", "pool": w.pools[0]}).body.get("ok"))   # control: base
            w.close()


class ViewIsNotAnOperation(Fixture):
    def test_view_cannot_be_granted_minted_or_delegated(self):
        self.assertNotIn(pool_authz.VIEW, pl.OPERATIONS)
        self.assertEqual(self.w.admin_op(op="pool_grant", who="user-alice", pool=self.A, ops=[pool_authz.VIEW]), {"ok": False, "error": "refused"})
        with self.assertRaises(Exception):
            self.w.sessions.mint_scoped(T, b"user-alice", conversation="c2", scope={self.A: frozenset({pool_authz.VIEW})}, ttl_s=60)
        tok = self.tok()
        self.assertEqual(self.ask(tok, op="token_delegate", scope={self.A: [pool_authz.VIEW]}, ttl_s=60), UNAV)

    def test_the_hand_off_refuses_an_unknown_need_and_checks_a_mixed_one_whole(self):
        tok = self.tok()
        recheck = lambda needs: pool_authz.recheck(self.w.sessions, self.w.dir, pool_world.LAT, tok, needs)
        self.assertTrue(recheck({self.A: {pool_authz.VIEW, pl.QUERY}}))
        self.assertFalse(recheck({self.A: {"administer"}}))
        self.assertFalse(recheck({self.A: set()}))
        self.assertTrue(self.w.admin_op(op="pool_ungrant", who="user-alice", pool=self.A, ops=["query"]).get("ok"))
        self.assertTrue(recheck({self.A: {pool_authz.VIEW}}))             # save, update, retract, review are still held
        self.assertFalse(recheck({self.A: {pool_authz.VIEW, pl.QUERY}}))  # but not the query the answer carries


if __name__ == "__main__":
    unittest.main()
