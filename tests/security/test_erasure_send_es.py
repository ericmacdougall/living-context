"""SEC-E8 (d), BUILD-PLAN Step 14: eight concurrent senders of one tenant, each paused by the test hook after its final check
(tombstone sequence and redact set) and before its socket write, while the erasure's boundary is requested.

The publication step takes the exclusive side of the tenant's send fence, so it waits for every paused sender to write; the
gateway trace then shows no hand-off whose check precedes the boundary and whose write follows it, every later hand-off of those
responses is aborted, and the bytes each sender's connection still holds in the kernel at the boundary are at most 64 KiB.

Broken variants, each red for its stated reason: a send that does not hold the fence (a paused sender writes covered bytes after
the boundary); a hand-off that writes the whole buffer without the queue-capped count (a queue above 64 KiB); a hand-off whose
final check is dropped (a later hand-off writes after the boundary). The module FAILS without LC_TEST_ES_URL, LC_TEST_CA,
LC_TEST_PW. N_SEND = 8 (EXPERIMENT 9.4, ASSUMPTION)."""
import socket
import tempfile
import threading
import time
import unittest
from pathlib import Path

from living_context.erasure.hold import HoldRegistry
from living_context.erasure.executor import Executor, State
from living_context.gateway import fence as fmod
from living_context.gateway.fence import Aborted, SendFence
from living_context.gateway.gate import WriteGate
from living_context.gateway.journal import TombstoneJournal
from tests.security.vworld import VersionsBase

N_SEND = 8
PAYLOAD = b"x" * 8192


def tcp_pair():
    ls = socket.socket()
    ls.bind(("127.0.0.1", 0))
    ls.listen(1)
    cli = socket.create_connection(ls.getsockname())
    conn, _ = ls.accept()
    ls.close()
    fmod.configure(conn)   # the peer never reads
    return conn, cli


class EraseSendFence(VersionsBase):
    T = "esnd"
    _n = 0

    def setUp(self):
        EraseSendFence._n += 1
        self.wgate, self.fence, self.state = WriteGate(), SendFence(), State()
        d = Path(tempfile.mkdtemp(dir=self.root))
        self.journal = TombstoneJournal(d / "j" / "t.log", d / "r" / "t.log")
        self.email = f"esnd{EraseSendFence._n}x{id(self) % 9973}@example.com"

    def executor(self):
        return Executor(lambda: self.gate.client(self.T, "erase"), self.structure(), self.T, self.ks, self.journal, self.wgate,
                        self.fence, self.state, holds=HoldRegistry(), purge_deadline_s=2, purge_interval_s=1, es_log=lambda: self.gate.client(self.T, "log"))

    def run_senders(self, *, use_fence=True, capped=True, recheck=True):
        w = self.world()
        subject = self.ks.subject_token(self.email)
        w.add(0, 3, extra=f" {self.email}")
        trace, lock = [], threading.Lock()
        paused, release = [threading.Event() for _ in range(N_SEND)], threading.Event()
        conns = [tcp_pair() for _ in range(N_SEND)]
        errors = []

        def note(ev):
            with lock:
                trace.append(ev)

        check = (lambda: self.state.seq == 0) if recheck else (lambda: True)

        def sender(i):
            conn = conns[i][0]
            try:
                for _ in range(40):   # fill the kernel queue before the pause (the peer never reads)
                    fmod.handoff(conn, PAYLOAD, self.fence, check, capped=capped)

                def hook():
                    note(("checked", i))
                    paused[i].set()
                    release.wait(30)
                if use_fence:
                    fmod.handoff(conn, PAYLOAD * 16, self.fence, check, before_write=hook, capped=capped)
                else:   # BROKEN: check, pause, write, no fence held
                    if not check():
                        raise Aborted()
                    hook()
                    conn.send(PAYLOAD * 16 if not capped else PAYLOAD)
                note(("wrote", i))
                try:   # a later hand-off of the same response, after the boundary
                    fmod.handoff(conn, PAYLOAD, self.fence, check, capped=capped)
                    note(("wrote_late", i))
                except Aborted:
                    note(("aborted_late", i))
            except Exception as e:   # pragma: no cover - reported below
                errors.append(repr(e))

        threads = [threading.Thread(target=sender, args=(i,), daemon=True) for i in range(N_SEND)]
        for t in threads:
            t.start()
        for p in paused:
            self.assertTrue(p.wait(20), f"a sender did not reach the pause: {errors}")
        queues = {}

        def boundary():   # called inside the exclusive side, right after the tombstone and redact set became effective
            note(("boundary", -1))
            for i, (conn, _) in enumerate(conns):
                queues[i] = fmod.queued(conn)

        done = threading.Event()
        threading.Thread(target=lambda: (self.executor().erase_subject(subject, apply_guard=lambda: (boundary(),
                         self.guard.erase_identifier(self.email))), done.set()), daemon=True).start()
        if use_fence:
            time.sleep(1.5)   # the publication step reaches the fence and waits for the paused senders
            self.assertNotIn(("boundary", -1), trace, "the boundary was published while senders were paused")
        else:   # BROKEN: nothing holds the boundary back; the senders stay paused until it is published
            end = time.time() + 40
            while ("boundary", -1) not in trace and time.time() < end:
                time.sleep(0.05)
        release.set()
        self.assertTrue(done.wait(60), "the erasure did not finish")
        for t in threads:
            t.join(10)
        self.assertEqual(errors, [])
        for conn, cli in conns:
            conn.close()
            cli.close()
        return trace, queues

    def verdict(self, trace, queues):
        b = trace.index(("boundary", -1))
        for i in range(N_SEND):
            checked, wrote = trace.index(("checked", i)), trace.index(("wrote", i))
            self.assertFalse(checked < b < wrote, f"sender {i}: a hand-off whose check precedes the boundary wrote after it")
            late = [e for e in trace if e[1] == i and e[0] in ("wrote_late", "aborted_late")]
            self.assertEqual(late, [("aborted_late", i)], f"sender {i}: a hand-off after the boundary was not aborted")
            self.assertLessEqual(queues[i], fmod.SEND_BOUND, f"sender {i}: the kernel holds more than 64 KiB at the boundary")
        return len(queues)

    def test_eight_paused_senders_never_write_across_the_boundary_and_hold_at_most_64_KiB(self):
        trace, queues = self.run_senders()
        self.assertEqual(self.verdict(trace, queues), N_SEND)
        self.assertEqual(self.state.seq, 1)

    def test_broken_send_that_does_not_hold_the_fence_is_red(self):
        trace, queues = self.run_senders(use_fence=False)
        with self.assertRaises(AssertionError) as cm:
            self.verdict(trace, queues)
        self.assertIn("check precedes the boundary wrote after it", str(cm.exception))

    def test_broken_handoff_without_the_queue_capped_count_is_red(self):
        trace, queues = self.run_senders(capped=False)
        with self.assertRaises(AssertionError) as cm:
            self.verdict(trace, queues)
        self.assertIn("more than 64 KiB", str(cm.exception))

    def test_broken_handoff_without_the_final_check_is_red(self):
        trace, queues = self.run_senders(recheck=False)
        with self.assertRaises(AssertionError) as cm:
            self.verdict(trace, queues)
        self.assertIn("after the boundary was not aborted", str(cm.exception))


if __name__ == "__main__":
    unittest.main()
