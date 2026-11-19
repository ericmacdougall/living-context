"""Step 5: sessions, epoch table, send fence, capped hand-off, journal skeleton, name exchange."""
import os
import socket
import tempfile
import threading
import time
import unittest
from pathlib import Path

from living_context.gateway import epoch_journal, fence, fsx
from living_context.gateway.sessions import EpochTable, Sessions
from living_context.policy import Principal, Role


class Adapter:
    def verify(self, tenant, proof):
        return {b"proof-alice": "alice", b"proof-bob": "bob"}.get(proof)


def make():
    fences = {}
    ff = lambda t: fences.setdefault(t, fence.SendFence())
    ep = EpochTable(ff)
    grants = {("tenaaa", "tok-alice"): Principal("tenaaa", "tok-alice", frozenset([Role.READER]), frozenset(["base"]), frozenset()),
              ("tenaaa", "tok-bob"): Principal("tenaaa", "tok-bob", frozenset([Role.WRITER]), frozenset(), frozenset(["base"]))}
    s = Sessions({"tenaaa": Adapter()}, ep, lambda t, who: "tok-" + who, lambda t, p: grants.get((t, p)))
    return s, ep, ff


class SessionTests(unittest.TestCase):
    def test_mint_only_from_a_verified_proof_and_tenant_comes_from_the_record(self):
        s, ep, _ = make()
        self.assertIsNone(s.mint("tenaaa", b"nope"))
        self.assertIsNone(s.mint("tenbbb", b"proof-alice"))  # no adapter for that tenant
        tok = s.mint("tenaaa", b"proof-alice")
        self.assertTrue(tok.startswith("tenaaa."))
        rec = s.resolve(tok)
        self.assertEqual((rec.tenant, rec.principal_token), ("tenaaa", "tok-alice"))
        self.assertEqual(s.principal(rec).read_set, frozenset(["base"]))

    def test_forged_prefix_and_junk_tokens_are_refused(self):
        s, *_ = make()
        tok = s.mint("tenaaa", b"proof-alice")
        rest = tok.split(".", 1)[1]
        for bad in ("tenbbb." + rest, rest, "tenaaa." + rest[:-1], "tenaaa." + "0" * 64, "", None, "a.b.c"):
            self.assertIsNone(s.resolve(bad), bad)

    def test_revoke_denies_at_once_and_a_revoked_principal_cannot_mint(self):
        s, ep, _ = make()
        tok = s.mint("tenaaa", b"proof-alice")
        self.assertIsNotNone(s.resolve(tok))
        r = ep.bump("tenaaa", "tok-alice")
        self.assertIsNotNone(r.denied_at)
        self.assertIsNone(s.resolve(tok))
        self.assertIsNone(s.mint("tenaaa", b"proof-alice"))
        self.assertIsNotNone(s.resolve(s.mint("tenaaa", b"proof-bob")))  # others unaffected

    def test_class_grant_removal_bump_without_revoke_invalidates_old_sessions_only(self):
        s, ep, _ = make()
        tok = s.mint("tenaaa", b"proof-alice")
        ep.bump("tenaaa", "tok-alice", revoke=False)
        self.assertIsNone(s.resolve(tok))
        self.assertIsNotNone(s.resolve(s.mint("tenaaa", b"proof-alice")))

    def test_revoke_discards_unsent_buffers_and_never_waits_on_a_slow_reader(self):
        _, ep, ff = make()
        buf = [b"x" * 100]
        ep.register_buffer("tenaaa", "tok-alice", buf)
        t0 = time.time()
        ep.bump("tenaaa", "tok-alice")
        self.assertEqual(buf, [])
        self.assertLess(time.time() - t0, 0.5)


class FenceTests(unittest.TestCase):
    def test_writer_preference_and_exclusion(self):
        f = fence.SendFence()
        order = []
        with f.shared():
            w = threading.Thread(target=lambda: (f.exclusive().__enter__(), order.append("w")))
            w.start()
            time.sleep(0.1)
            got = []
            r = threading.Thread(target=lambda: (f.shared().__enter__(), got.append(1)))
            r.start()
            time.sleep(0.1)
            self.assertEqual(got, [])  # a new reader waits behind the waiting writer
            self.assertEqual(order, [])
        w.join(1)
        self.assertEqual(order, ["w"])

    def pair(self):
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.bind(("127.0.0.1", 0))
        srv.listen(1)
        cli = socket.create_connection(srv.getsockname())
        conn, _ = srv.accept()
        srv.close()
        fence.configure(conn)
        return conn, cli

    def test_capped_handoff_keeps_the_kernel_queue_within_64_KiB_and_uncapped_does_not(self):
        peaks = {}
        for capped in (True, False):
            conn, cli = self.pair()  # the peer never reads
            f = fence.SendFence()
            peak = 0
            for _ in range(200):
                fence.handoff(conn, b"x" * 8192, f, lambda: True, capped=capped)
                peak = max(peak, fence.queued(conn))
            peaks[capped] = peak
            conn.close()
            cli.close()
        self.assertLessEqual(peaks[True], fence.SEND_BOUND)
        self.assertGreater(peaks[False], fence.SEND_BOUND)  # the broken variant is caught

    def test_check_failure_aborts_and_nothing_is_written(self):
        conn, cli = self.pair()
        with self.assertRaises(fence.Aborted):
            fence.handoff(conn, b"secret", fence.SendFence(), lambda: False)
        cli.settimeout(0.2)
        with self.assertRaises(socket.timeout):
            cli.recv(10)
        conn.close()
        cli.close()

    def test_paused_sender_blocks_the_flip_so_no_write_follows_it(self):
        """A sender paused between its final check and its write holds the shared side; the
        revoke's flip waits for it, so the write precedes the flip. A hand-off without the fence
        (broken) writes after the flip."""
        for use_fence in (True, False):
            s, ep, ff = make()
            tok = s.mint("tenaaa", b"proof-alice")
            rec = s.resolve(tok)
            conn, cli = self.pair()
            f = ff("tenaaa")
            paused, release, flipped_at, wrote_at = threading.Event(), threading.Event(), [], []

            def hook():
                paused.set()
                release.wait(2)

            def sender():
                chk = lambda: ep.current("tenaaa", rec.principal_token, rec.epoch)
                if use_fence:
                    fence.handoff(conn, b"covered", f, chk, before_write=hook)
                else:  # broken: check, pause, write, no fence held
                    chk() or (_ for _ in ()).throw(fence.Aborted())
                    hook()
                    conn.send(b"covered")
                wrote_at.append(time.time())

            t = threading.Thread(target=sender)
            t.start()
            paused.wait(2)
            rv = threading.Thread(target=lambda: (ep.bump("tenaaa", rec.principal_token), flipped_at.append(time.time())))
            rv.start()
            time.sleep(0.2)
            release.set()
            t.join(2)
            rv.join(2)
            if use_fence:
                self.assertLessEqual(wrote_at[0], flipped_at[0])  # write completed before the flip
            else:
                self.assertGreater(wrote_at[0], flipped_at[0])  # broken: covered bytes written after the flip
            conn.close()
            cli.close()


class JournalSkeleton(unittest.TestCase):
    def test_preallocated_files_append_replay_and_torn_detection(self):
        with tempfile.TemporaryDirectory() as d:
            j, spare = epoch_journal.provision(Path(d) / "tenaaa", 4096)
            self.assertEqual((j.stat().st_size, spare.stat().st_size), (4096, 4096))
            self.assertGreaterEqual(j.stat().st_blocks * 512, 4096)  # allocated, not sparse
            jr = epoch_journal.Journal(j)
            for i in range(5):
                jr.append({"p": f"t{i}", "e": i})
            jr.close()
            again = epoch_journal.Journal(j)
            self.assertEqual([r["e"] for r in again.records], list(range(5)))
            self.assertFalse(again.torn)
            end = again.end
            again.close()
            with open(j, "r+b") as f:  # tear the last record
                f.seek(end - 3)
                f.write(b"\xff\xff\xff")
            self.assertTrue(epoch_journal.Journal(j).torn)
            full = epoch_journal.Journal(spare)
            with self.assertRaises(epoch_journal.JournalFull):
                for _ in range(1000):
                    full.append({"x": "y" * 50})
            self.assertEqual(spare.stat().st_size, 4096)  # never grows

    def test_exchange_swaps_names_and_provisioning_check_works(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertTrue(fsx.supported(d))
            a, b = os.path.join(d, "a"), os.path.join(d, "b")
            Path(a).write_bytes(b"1")
            Path(b).write_bytes(b"22")
            fsx.exchange(a, b)
            self.assertEqual((Path(a).read_bytes(), Path(b).read_bytes()), (b"22", b"1"))
            with self.assertRaises(OSError):
                fsx.exchange(a, os.path.join(d, "missing"))
            self.assertFalse(fsx.supported(os.path.join(d, "nodir")))


if __name__ == "__main__":
    unittest.main()
