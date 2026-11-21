"""F16 (round 4): a built pool_list, pool_describe or hold review is not sent after the caller lost the pool.

Review F16: pool_list and pool_describe recorded no pool needs, so `Gateway.send_ok` let their built answers out after an ungrant or a
detach while the token stayed valid. Each case pauses a real sender of the socket server over the real gateway, pool service and pool
tools (tests/unit/pool_world.py) between build and hand-off (`Pausing`, from test_send_fence_pools), runs the removal, and reads what the
client gets: the typed `unavailable` line, never the listing, the card or the held text. The hold review (pool_hold_show) is driven live
here too, for its target pool and for a pool of its private source set.

Positive controls: with no removal, or a removal of a pool the answer does not name, each answer (a scoped token's, a member's plain
session's, the administrator's, the pool_create receipt) is delivered whole.

Deviation (labelled): macOS has no SIOCOUTQ, so on darwin `fence.queued` answers 0 (as in test_send_fence_pools).
"""
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from living_context.gateway import fence
from living_context.gateway.server import Server
from tests.unit.pool_world import OPS, T, UNAV, Line, World
from tests.unit.test_send_fence_pools import Pausing

CARD = "the gannet roost card: north landing, ask the warden first"


class SendFenceVisibility(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.patches = [mock.patch.object(fence, "queued", lambda sock: 0)] if sys.platform == "darwin" else []
        for p in self.patches:
            p.start()
        self.w = World(Path(self._tmp.name))
        self.A, self.B = self.w.pools
        made = self.w.admin_op(op="pool_create", cls="base", kind="team", label="carded", card=CARD)
        self.assertTrue(made.get("ok"), made)
        self.C = made["pool"]
        self.assertEqual(self.w.admin_op(op="pool_grant", who="user-alice", pool=self.C, ops=sorted(OPS)), {"ok": True})
        self.tok = self.w.sessions.mint_scoped(T, b"user-alice", conversation="c1", scope={p: OPS for p in (self.A, self.B, self.C)},
                                               ttl_s=600)
        self.handler = Pausing(self.w.gw)
        self.server = Server(self.handler, self.w.sessions, self.w.epochs, self.w.fence_for, idle_s=5.0).start()
        self.lines = []

    def tearDown(self):
        for ln in self.lines:
            ln.close()
        self.server.stop()
        self.w.close()
        for p in self.patches:
            p.stop()
        self._tmp.cleanup()

    def paused_answer(self, token, req, removal=None):
        ln = Line(self.server.address)
        self.lines.append(ln)
        self.handler.arm()
        ln.send(token, **req)
        self.assertTrue(self.handler.paused.wait(5), "the sender did not reach the pause")
        if removal is not None:
            removal()
        self.handler.resume.set()
        return ln.answer()

    def ungrant(self, pool):
        return lambda: self.assertTrue(self.w.admin_op(op="pool_ungrant", who="user-alice", pool=pool).get("ok"))

    def detach(self, pool):
        return lambda: self.assertTrue(self.w.admin_op(op="pool_detach", pool=pool).get("ok"))

    def assert_aborted(self, got):
        self.assertEqual(got, UNAV, "the paused answer went out after the caller lost the pool")
        self.assertNotIn("gannet roost card", str(got))
        self.assertEqual(self.server.aborted, 1)

    def assert_delivered(self, got):
        self.assertTrue(isinstance(got, dict) and got.get("ok") is True, got)
        self.assertEqual(getattr(self.server, "aborted", 0), 0)
        return got

    # ---- pool_describe, with its card -----------------------------------------------------------------------------------------
    def test_a_paused_describe_with_its_card_is_aborted_by_an_ungrant(self):
        self.assert_aborted(self.paused_answer(self.tok, {"op": "pool_describe", "pool": self.C}, self.ungrant(self.C)))

    def test_a_paused_describe_with_its_card_is_aborted_by_a_detach(self):
        self.assert_aborted(self.paused_answer(self.tok, {"op": "pool_describe", "pool": self.C}, self.detach(self.C)))

    def test_a_members_paused_describe_is_aborted_by_an_ungrant(self):
        self.assert_aborted(self.paused_answer(self.w.plain(), {"op": "pool_describe", "pool": self.C}, self.ungrant(self.C)))

    # ---- pool_list: aborted whole ------------------------------------------------------------------------------------------------
    def test_a_paused_list_is_aborted_whole_by_an_ungrant_of_one_listed_pool(self):
        self.assert_aborted(self.paused_answer(self.tok, {"op": "pool_list"}, self.ungrant(self.C)))

    def test_a_paused_list_is_aborted_whole_by_a_detach_of_one_listed_pool(self):
        self.assert_aborted(self.paused_answer(self.tok, {"op": "pool_list"}, self.detach(self.B)))

    def test_a_members_paused_list_is_aborted_by_an_ungrant(self):
        self.assert_aborted(self.paused_answer(self.w.plain(), {"op": "pool_list"}, self.ungrant(self.A)))

    # ---- pool_hold_show, live -----------------------------------------------------------------------------------------------------
    def hold_show(self, removal):
        hid = self.w.hold(self.A, [self.B])
        human = self.w.sessions.mint_scoped(T, b"user-alice", conversation="h1", scope={self.A: OPS, self.B: OPS}, ttl_s=600, human=True)
        return self.paused_answer(human, {"op": "pool_hold_show", "pool": self.A, "hold": hid}, removal)

    def test_a_paused_hold_review_is_aborted_by_an_ungrant_of_its_pool(self):
        self.assert_aborted(self.hold_show(self.ungrant(self.A)))

    def test_a_paused_hold_review_is_aborted_by_an_ungrant_of_a_pool_of_its_private_source(self):
        self.assert_aborted(self.hold_show(self.ungrant(self.B)))

    # ---- positive controls ----------------------------------------------------------------------------------------------------
    def test_control_a_paused_describe_and_list_go_out_whole_with_no_removal(self):
        self.assertEqual(self.assert_delivered(self.paused_answer(self.tok, {"op": "pool_describe", "pool": self.C}))["card"], CARD)
        got = self.assert_delivered(self.paused_answer(self.tok, {"op": "pool_list"}))
        self.assertEqual({r["pool"] for r in got["pools"]}, {self.A, self.B, self.C})

    def test_control_a_members_and_the_administrators_paused_answers_go_out_whole(self):
        member = self.w.plain()
        self.assertEqual(self.assert_delivered(self.paused_answer(member, {"op": "pool_describe", "pool": self.C}))["card"], CARD)
        self.assertEqual(len(self.assert_delivered(self.paused_answer(member, {"op": "pool_list"}))["pools"]), 3)
        self.assertEqual(len(self.assert_delivered(self.paused_answer(self.w.admin, {"op": "pool_list"}))["pools"]), 3)
        # the administrator sees a pool in any state, so a detach does not abort its listing
        got = self.assert_delivered(self.paused_answer(self.w.admin, {"op": "pool_describe", "pool": self.C}, self.detach(self.C)))
        self.assertEqual(got["card"], CARD)

    def test_control_a_paused_create_receipt_goes_out(self):
        got = self.assert_delivered(self.paused_answer(self.w.admin, {"op": "pool_create", "cls": "base", "kind": "team", "label": "delta"}))
        self.assertEqual(len(got["pool"]), 16)

    def test_control_an_ungrant_of_a_pool_the_answer_does_not_name_leaves_it_whole(self):
        only_c = self.w.sessions.mint_scoped(T, b"user-alice", conversation="c2", scope={self.C: OPS}, ttl_s=600)
        got = self.assert_delivered(self.paused_answer(only_c, {"op": "pool_list"}, self.ungrant(self.A)))
        self.assertEqual([r["pool"] for r in got["pools"]], [self.C])
        self.assertEqual(self.assert_delivered(self.paused_answer(only_c, {"op": "pool_describe", "pool": self.C},
                                                                  self.ungrant(self.B)))["card"], CARD)

    def test_control_a_paused_hold_review_goes_out_with_no_removal(self):
        got = self.assert_delivered(self.hold_show(None))
        self.assertIn("gannet roost", got["text"])


if __name__ == "__main__":
    unittest.main()
