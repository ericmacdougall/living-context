"""F10: the send hand-off runs POOLS 2.4 checks 1 to 4 for the token and for every pool whose content the response carries, under the
shared side of the tenant's send fence, before EVERY chunk, and a failure aborts the whole response (POOLS 2.4, 2.5, AC-2; DESIGN 4.4).

Review F10: the hand-off check was the principal's epoch alone, so an ungrant, a detach, a single-token revoke (its own, by id, or of its
parent) and the token's expiry between the build of a response and its hand-off let the response go out whole. Each case pauses a real
sender of the socket server (`server.Server`) over the real gateway, pool service and pool tools (tests/unit/pool_world.py), at one of two
points:
* between build and hand-off (`Pausing`: a handler wrapper with both `handle`, which the server at 71b23a5 calls, and `serve`, which the
  server calls now). The removal lands; the client must get the typed `unavailable` line, never the content.
* inside the fence, between the check and the write of the FIRST chunk of a multi-chunk response (the paused-sender hook of DESIGN 4.4,
  through a `fence.handoff` wrapper; chunks bounded at 1 KiB). The removal must wait for that write (it takes the exclusive side), and the
  next chunk must abort: the connection closes after the first chunk and the line is never completed.
Positive controls: a removal of ANOTHER pool, and a revoke of a SIBLING token, leave the paused response to go out whole.

Deviation (labelled): macOS has no SIOCOUTQ, so on darwin `fence.queued` answers 0. That keeps the check, the fence and the chunking and
drops only the kernel-queue term of the 64 KiB cap, which tests/security/test_sessions_fence.py and test_erasure_send_es.py test on Linux.
"""
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from living_context.gateway import fence
from living_context.gateway.pool_tools import PoolTools
from living_context.gateway.server import Server
from tests.unit.pool_world import T, TEXT, UNAV, Line, World


class Pausing:
    """The gateway, paused once (when armed) after it has built an answer and before the server hands it off."""

    def __init__(self, inner):
        self.inner = inner
        self.armed = False
        self.paused, self.resume = threading.Event(), threading.Event()

    def arm(self):
        self.paused.clear()
        self.resume.clear()
        self.armed = True

    def _pause(self):
        if self.armed:
            self.armed = False
            self.paused.set()
            self.resume.wait(5)

    def handle(self, token, request):
        out = self.inner.handle(token, request)
        self._pause()
        return out

    def serve(self, token, request):
        out = self.inner.serve(token, request)
        self._pause()
        return out

    def __getattr__(self, name):   # mint_line, send_ok (where the gateway has it)
        return getattr(self.inner, name)


class SendFencePools(unittest.TestCase):
    broken: dict = {}

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.patches = [mock.patch.object(fence, "queued", lambda sock: 0)] if sys.platform == "darwin" else []
        for p in self.patches:
            p.start()
        self.w = World(Path(self._tmp.name), **self.broken)
        self.A, self.B = self.w.pools
        self.tok, self.sib = self.w.scoped("c1"), self.w.scoped("c2")
        self.pa = self.save(self.A, "a1")
        self.pb = self.save(self.B, "b1")
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

    def save(self, pool, key):
        out = self.w.gw.handle(self.tok, {"op": "pool_save", "pool": pool, "text": TEXT, "key": key})
        self.assertTrue(out.get("ok"), out)
        return out["pointer"]

    def line(self):
        ln = Line(self.server.address)
        self.lines.append(ln)
        return ln

    def ok(self, out):
        self.assertTrue(isinstance(out, dict) and out.get("ok"), out)
        return out

    def paused_answer(self, token, req, removal):
        """`req` on a fresh connection, its sender paused between build and hand-off while `removal` runs; the client's answer."""
        ln = self.line()
        self.handler.arm()
        ln.send(token, **req)
        self.assertTrue(self.handler.paused.wait(5), "the sender did not reach the pause")
        removal()
        self.handler.resume.set()
        return ln.answer()

    def fetch(self, token=None, pointer=None):
        return {"op": "pool_fetch", "pointer": pointer or self.pa}

    def assert_aborted(self, got):
        self.assertEqual(got, UNAV, "the paused response went out after the removal")
        self.assertEqual(self.server.aborted, 1)

    def assert_delivered(self, got):
        self.assertEqual(got.get("status"), "ok", got)
        self.assertIn("gannet roost", got.get("text", ""))
        self.assertEqual(getattr(self.server, "aborted", 0), 0)

    # ---- between build and hand-off: each removal aborts the response ------------------------------------------------------------
    def test_an_ungrant_of_the_pool_aborts_the_paused_response(self):
        got = self.paused_answer(self.tok, self.fetch(), lambda: self.ok(self.w.admin_op(op="pool_ungrant", who="user-alice", pool=self.A)))
        self.assert_aborted(got)

    def test_a_detach_of_the_pool_aborts_the_paused_response(self):
        got = self.paused_answer(self.tok, self.fetch(), lambda: self.ok(self.w.admin_op(op="pool_detach", pool=self.A)))
        self.assert_aborted(got)

    def test_the_tokens_own_revoke_aborts_its_paused_response(self):
        got = self.paused_answer(self.tok, self.fetch(), lambda: self.ok(self.w.gw.handle(self.tok, {"op": "token_revoke"})))
        self.assert_aborted(got)

    def test_an_administrators_revoke_by_id_aborts_the_paused_response(self):
        tid = self.w.sessions.token_id(self.w.sessions.resolve(self.tok))
        got = self.paused_answer(self.tok, self.fetch(), lambda: self.ok(self.w.admin_op(op="token_revoke", token_id=tid)))
        self.assert_aborted(got)

    def test_a_revoke_of_the_parent_aborts_the_childs_paused_response(self):
        child = self.ok(self.w.gw.handle(self.tok, {"op": "token_delegate", "scope": {self.A: ["query"]}, "ttl_s": 300}))["token"]
        got = self.paused_answer(child, self.fetch(), lambda: self.ok(self.w.gw.handle(self.tok, {"op": "token_revoke"})))
        self.assert_aborted(got)

    def test_the_tokens_expiry_aborts_its_paused_response(self):
        def expire():
            self.w.clock.t += 601
        got = self.paused_answer(self.tok, self.fetch(), expire)
        self.assert_aborted(got)

    def test_a_query_over_two_pools_is_aborted_whole_when_one_is_ungranted(self):
        with mock.patch.object(PoolTools, "_pool_candidates", self.w.leaf_candidates()):
            got = self.paused_answer(self.tok, {"op": "pool_query", "query": "gannet roost", "pools": [self.A, self.B]},
                                     lambda: self.ok(self.w.admin_op(op="pool_ungrant", who="user-alice", pool=self.B)))
        self.assertEqual(got, UNAV, "items of the remaining pool were sent (items dropped instead of abort)")
        self.assertEqual(self.server.aborted, 1)

    # ---- positive controls ----------------------------------------------------------------------------------------------------
    def test_control_an_ungrant_of_another_pool_leaves_the_response_whole(self):
        got = self.paused_answer(self.tok, self.fetch(), lambda: self.ok(self.w.admin_op(op="pool_ungrant", who="user-alice", pool=self.B)))
        self.assert_delivered(got)

    def test_control_a_detach_of_another_pool_leaves_the_response_whole(self):
        got = self.paused_answer(self.tok, self.fetch(), lambda: self.ok(self.w.admin_op(op="pool_detach", pool=self.B)))
        self.assert_delivered(got)

    def test_control_a_sibling_tokens_revoke_leaves_the_response_whole(self):
        got = self.paused_answer(self.tok, self.fetch(), lambda: self.ok(self.w.gw.handle(self.sib, {"op": "token_revoke"})))
        self.assert_delivered(got)

    def test_control_a_query_of_one_pool_is_whole_when_another_is_ungranted(self):
        with mock.patch.object(PoolTools, "_pool_candidates", self.w.leaf_candidates()):
            got = self.paused_answer(self.tok, {"op": "pool_query", "query": "gannet roost", "pools": [self.A]},
                                     lambda: self.ok(self.w.admin_op(op="pool_ungrant", who="user-alice", pool=self.B)))
        self.assertEqual([i["pool"] for i in got.get("items", [])], [self.A], got)
        self.assertEqual(getattr(self.server, "aborted", 0), 0)

    def test_control_the_bearers_own_revoke_receipt_is_delivered(self):
        ln = self.line()
        ln.send(self.tok, op="token_revoke")
        got = ln.answer()
        self.assertTrue(got.get("ok") is True and got.get("denied") is True, got)

    # ---- inside the fence: the removal is ordered after the paused write, and the next chunk aborts -----------------------------
    def in_fence(self):
        """Pause the FIRST hand-off of a fetch between its check and its write (under the shared side), start an ungrant of its pool,
        then resume. Returns (the removal waited for the fence, event order, bytes the client got, the server closed the connection)."""
        real = fence.handoff
        armed = {"on": True}
        paused, resume, done = threading.Event(), threading.Event(), threading.Event()
        log = []

        def hook():
            paused.set()
            resume.wait(5)
            log.append("first chunk written")   # the write follows at once, still under the shared side

        def handoff(sock, data, f, check, **kw):
            if armed["on"]:
                armed["on"] = False
                kw["before_write"] = hook
            return real(sock, data, f, check, **kw)

        def removal():
            out = self.w.admin_op(op="pool_ungrant", who="user-alice", pool=self.A)
            log.append("denied" if out.get("ok") else f"removal failed: {out}")
            done.set()

        ln = self.line()
        with mock.patch.object(fence, "handoff", handoff), mock.patch.object(fence, "SEND_BOUND", 1024):
            ln.send(self.tok, **self.fetch())
            self.assertTrue(paused.wait(5), "the sender did not reach its first hand-off")
            th = threading.Thread(target=removal)
            th.start()
            fz = self.w.fence_for(T)
            for _ in range(5000):   # until the removal waits for the exclusive side, or returned without taking it
                if fz._writers_waiting or done.is_set():
                    break
                time.sleep(0.001)
            waited = bool(fz._writers_waiting) and not done.is_set()
            resume.set()
            th.join(5)
            got, closed = ln.read(timeout=5)
        return waited, log, got, closed

    def test_a_removal_waits_for_the_paused_write_and_the_next_chunk_aborts_the_response(self):
        waited, log, got, closed = self.in_fence()
        self.assertTrue(waited, "the removal returned while a hand-off held the shared side (it must take the exclusive side)")
        self.assertEqual(log, ["first chunk written", "denied"])
        self.assertNotIn(b"\n", got, "the response was completed after the boundary")
        self.assertLessEqual(len(got), 1024)
        self.assertTrue(closed, "the connection must end after a partly sent response is aborted")
        self.assertEqual(self.server.aborted, 1)


class PlantedRemovalOutsideTheFence(SendFencePools):
    """The broken variant of POOLS 2.5 (`broken_removal_outside_journal`: the grant table changed outside the fence): the in-fence case
    must see it. The other cases of the parent class run again over this variant; only the ordering case is expected to change."""
    broken = {"broken_removal_outside_journal": True}

    def test_a_removal_waits_for_the_paused_write_and_the_next_chunk_aborts_the_response(self):
        waited, log, _, _ = self.in_fence()
        self.assertFalse(waited)
        self.assertEqual(log[0], "denied", log)   # the denial came before the write it should have waited for


if __name__ == "__main__":
    unittest.main()
