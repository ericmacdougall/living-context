"""BUILD-PLAN Step 15, the remaining broken variants of the revocation journal (EXPERIMENT R7), no Elasticsearch.

Each check below passes on the build and is turned red by its stated broken variant:
  * compaction that exchanges the two names before the spare's file fsync, or skips the directory fsync, or drops the start
    record (the first two go red only through the check of the journal-rebuilt table after a simulated power loss at every
    persistence boundary; the third through a restart that must not admit a gateway whose journal lost its start record);
  * one journal shared by all tenants (a second tenant inherits the first tenant's revokes);
  * a revoke lane left open while the clean stop drains;
  * a shutdown record written before a pending append has drained;
  * admission opened before the start record's fsync returned;
  * a compaction that carries the previous run's shutdown record into the new file (a crash after it reads as a clean stop).
The power-loss simulation undoes every byte written and not fsynced, and every name exchange whose directory was not fsynced,
addressed by inode (a file keeps its identity when the two names are exchanged)."""
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path

from living_context.gateway import epoch_journal, revocation
from living_context.gateway.epoch_journal import Journal, RealIO
from living_context.gateway.fence import SendFence
from living_context.gateway.revocation import FAULT, OPEN, RevocationService
from living_context.gateway.sessions import EpochTable

T = "tenaaa"


class PowerLoss(Exception):
    pass


class BoundaryIO(RealIO):
    """Counts every persistence operation, can stop at the k-th (the operation does not happen), and `crash()` undoes what a
    power loss would."""

    def __init__(self, directory: Path):
        self.dir = Path(directory)
        self.pending: list = []        # (inode, offset, old bytes) written and not fsynced
        self.pending_dir: list = []    # name exchanges whose directory was not fsynced
        self.n, self.fail_at = 0, None

    def _tick(self):
        self.n += 1
        if self.fail_at is not None and self.n == self.fail_at:
            raise PowerLoss()

    def pwrite(self, fd, data, offset):
        self._tick()
        self.pending.append((os.fstat(fd).st_ino, offset, os.pread(fd, len(data), offset)))
        os.pwrite(fd, data, offset)

    def fsync(self, fd):
        self._tick()
        ino = os.fstat(fd).st_ino
        self.pending = [p for p in self.pending if p[0] != ino]
        os.fsync(fd)

    def fsync_dir(self, directory):
        self._tick()
        self.pending_dir.clear()
        super().fsync_dir(directory)

    def exchange(self, a, b):
        self._tick()
        super().exchange(a, b)
        self.pending_dir.append((a, b))

    def crash(self):
        for a, b in reversed(self.pending_dir):
            super().exchange(a, b)
        by_ino = {p.stat().st_ino: p for p in self.dir.iterdir()}
        for ino, offset, old in reversed(self.pending):
            fd = os.open(by_ino[ino], os.O_RDWR)
            os.pwrite(fd, old, offset)
            os.close(fd)
        self.pending.clear()
        self.pending_dir.clear()


class GateIO(RealIO):
    """The first fsync after `arm()` blocks until `release` is set: an append in flight that the test can hold."""

    def __init__(self):
        self.armed, self.entered, self.release = False, threading.Event(), threading.Event()

    def arm(self):
        self.armed = True

    def fsync(self, fd):
        if self.armed and not self.entered.is_set():
            self.entered.set()
            self.release.wait(20)
        super().fsync(fd)


class Base(unittest.TestCase):
    size = 8192

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.fence = SendFence()
        self.n_dirs = 0

    def fresh(self):
        self.n_dirs += 1
        d = self.root / f"d{self.n_dirs}" / T
        epoch_journal.provision(d, self.size)
        return d

    def svc(self, d, tenant=T, epochs=None, **kw):
        self.epochs = epochs or EpochTable(lambda t: self.fence)
        s = RevocationService(tenant, self.epochs, d, **kw)
        self.addCleanup(lambda: s._j and s._j.close())
        return s

    @staticmethod
    def close(s):
        if s._j is not None:
            s._j.close()
            s._j = None


class CompactionPowerLoss(Base):
    def rebuilt_matches(self, d, want):
        s2 = self.svc(d)
        s2.break_glass([], ["adm1"])   # an unclean end: the operator reopens it; the rebuilt table must equal the live one
        return self.epochs.snapshot(T) == want

    def check_every_boundary(self, **kw):
        bad, k = [], 1
        while True:
            d = self.fresh()
            io = BoundaryIO(d)
            s = self.svc(d, io=io, **kw)
            s.start()
            for p in ("p1", "p2", "p3"):
                s.revoke(p)
            want = self.epochs.snapshot(T)
            io.n, io.fail_at = 0, k
            try:
                s.compact()
                done = True
            except PowerLoss:
                done = False
            io.crash()
            self.close(s)
            if not self.rebuilt_matches(d, want):
                bad.append(k)
            if done:
                return bad, k
            k += 1

    def check_revoke_after_compaction(self, **kw):
        d = self.fresh()
        io = BoundaryIO(d)
        s = self.svc(d, io=io, **kw)
        s.start()
        for p in ("p1", "p2", "p3"):
            s.revoke(p)
        s.compact()
        s.revoke("p4")            # acknowledged durable in the new file
        want = self.epochs.snapshot(T)
        io.crash()
        self.close(s)
        self.assertTrue(self.rebuilt_matches(d, want), "a revoke acknowledged after the compaction was lost by a power loss")

    def check_start_record_kept(self, **kw):
        d = self.fresh()
        io = BoundaryIO(d)
        s = self.svc(d, io=io, **kw)
        s.start()
        s.compact()               # nothing is revoked: the compacted file holds the start record and nothing else
        io.crash()
        self.close(s)
        self.assertEqual(self.svc(d).start(), FAULT, "a gateway whose journal lost its start record was admitted as if it had stopped cleanly")

    def test_compaction_is_correct_at_every_persistence_boundary(self):
        bad, n = self.check_every_boundary()
        self.assertEqual(bad, [], f"the rebuilt table differed after a power loss at boundary {bad}")
        self.assertGreater(n, 5, "the simulation did not visit the boundaries")
        self.check_revoke_after_compaction()
        self.check_start_record_kept()

    def test_broken_exchange_before_the_file_fsync_is_red(self):
        with self.assertRaises(AssertionError) as cm:
            bad, _ = self.check_every_boundary(broken_exchange_before_fsync=True)
            self.assertEqual(bad, [], f"the rebuilt table differed after a power loss at boundary {bad}")
        self.assertIn("rebuilt table differed", str(cm.exception))

    def test_broken_compaction_in_place_instead_of_into_the_spare_is_red(self):
        with self.assertRaises(AssertionError) as cm:
            bad, _ = self.check_every_boundary(broken_compaction_in_place=True)
            self.assertEqual(bad, [], f"the rebuilt table differed after a power loss at boundary {bad}")
        self.assertIn("rebuilt table differed", str(cm.exception))

    def test_broken_skipped_directory_fsync_is_red(self):
        with self.assertRaises(AssertionError) as cm:
            self.check_revoke_after_compaction(broken_skip_dir_fsync=True)
        self.assertIn("acknowledged after the compaction was lost", str(cm.exception))

    def test_broken_compaction_that_drops_the_start_record_is_red(self):
        with self.assertRaises(AssertionError) as cm:
            self.check_start_record_kept(broken_compaction_drops_start=True)
        self.assertIn("lost its start record", str(cm.exception))


class SharedJournal(Base):
    def check_isolated(self, shared: bool):
        da = self.fresh()
        db = da if shared else self.fresh()
        epochs = EpochTable(lambda t: self.fence)
        sa, sb = self.svc(da, "tenaaa", epochs), self.svc(db, "tenbbb", epochs)
        sa.start()
        sa.revoke("p1")
        sa.clean_stop()
        sb.start()
        self.assertTrue(epochs.is_revoked("tenaaa", "p1"))
        self.assertFalse(epochs.is_revoked("tenbbb", "p1"), "tenant B inherited tenant A's revoke from a shared journal")

    def test_each_tenant_has_its_own_journal_and_a_revoke_stays_in_its_tenant(self):
        self.check_isolated(shared=False)

    def test_broken_one_journal_shared_by_all_tenants_is_red(self):
        with self.assertRaises(AssertionError) as cm:
            self.check_isolated(shared=True)
        self.assertIn("inherited", str(cm.exception))


class CleanStopDrain(Base):
    def scenario(self, late_revoke: bool, **kw):
        d = self.fresh()
        gate = GateIO()
        s = self.svc(d, io=gate, **kw)
        s.start()
        gate.arm()
        out = {}
        t1 = threading.Thread(target=lambda: out.__setitem__("p1", s.revoke("p1")))
        t1.start()
        self.assertTrue(gate.entered.wait(10), "the append in flight never reached its fsync")
        t2 = threading.Thread(target=s.clean_stop)
        t2.start()
        time.sleep(0.4)
        threads = [t1, t2]
        if late_revoke:
            def late():
                try:
                    s.revoke("p2")
                    out["late"] = "admitted"
                except revocation.Refused:
                    out["late"] = "refused"
                except Exception as e:   # a revoke that raced the drain in any other way is also not refused
                    out["late"] = f"error:{type(e).__name__}"
            t3 = threading.Thread(target=late)
            t3.start()
            threads.append(t3)
            time.sleep(0.4)
        gate.release.set()
        for t in threads:
            t.join(30)
        return d, out

    def check_lane_closed(self, **kw):
        d, out = self.scenario(True, **kw)
        self.assertEqual(out.get("late"), "refused", "a revoke was admitted while the clean stop was draining")
        s2 = self.svc(d)
        self.assertEqual(s2.start(), OPEN)
        self.assertTrue(self.epochs.is_revoked(T, "p1"))
        self.assertFalse(self.epochs.is_revoked(T, "p2"))

    def check_shutdown_last(self, **kw):
        d, out = self.scenario(False, **kw)
        recs = [r["t"] for r in Journal(d / "epochs.journal").records]
        ok = "revoke" in recs and recs[-1:] == ["shutdown"]
        self.assertTrue(ok, f"the shutdown record was written before the pending append drained: the journal is {recs}")
        s2 = self.svc(d)
        self.assertEqual(s2.start(), OPEN)
        self.assertTrue(self.epochs.is_revoked(T, "p1"))

    def test_the_clean_stop_closes_the_lane_before_it_drains(self):
        self.check_lane_closed()

    def test_broken_lane_left_open_during_the_drain_is_red(self):
        with self.assertRaises(AssertionError) as cm:
            self.check_lane_closed(broken_lane_open_during_drain=True)
        self.assertIn("admitted while the clean stop was draining", str(cm.exception))

    def test_the_shutdown_record_follows_the_drained_append(self):
        self.check_shutdown_last()

    def test_broken_shutdown_before_the_pending_append_drains_is_red(self):
        with self.assertRaises(AssertionError) as cm:
            self.check_shutdown_last(broken_shutdown_before_drain=True)
        self.assertIn("pending append drained", str(cm.exception))


class StartAndCompactionState(Base):
    def check_no_admission_before_the_start_record_is_durable(self, **kw):
        d = self.fresh()
        gate = GateIO()
        s = self.svc(d, io=gate, **kw)
        gate.arm()
        t = threading.Thread(target=s.start)
        t.start()
        self.assertTrue(gate.entered.wait(10), "the start record never reached its fsync")
        admitted = s.admit() or s.lane_open
        gate.release.set()
        t.join(30)
        self.assertFalse(admitted, "admission began before the start record's fsync returned")
        self.assertTrue(s.admit(), "the tenant was not admitted after the start record became durable")

    def test_admission_waits_for_the_durable_start_record(self):
        self.check_no_admission_before_the_start_record_is_durable()

    def test_broken_admission_before_the_start_fsync_is_red(self):
        with self.assertRaises(AssertionError) as cm:
            self.check_no_admission_before_the_start_record_is_durable(broken_admit_before_start_fsync=True)
        self.assertIn("before the start record's fsync", str(cm.exception))

    def check_compaction_does_not_inherit_the_shutdown_state(self, **kw):
        d = self.fresh()
        s1 = self.svc(d)
        s1.start()
        s1.revoke("p1")
        s1.clean_stop()                       # the previous run ended cleanly: its journal holds a shutdown record
        s2 = self.svc(d, **kw)
        self.assertEqual(s2.start(), OPEN)
        s2.revoke("p2")
        s2.compact()
        self.close(s2)                        # the process dies: no clean stop, no shutdown record of this run
        s3 = self.svc(d)
        self.assertEqual(s3.start(), FAULT, "a crash after a compaction was read as a clean stop (the shutdown state was copied)")

    def test_a_compaction_leaves_the_run_unterminated(self):
        self.check_compaction_does_not_inherit_the_shutdown_state()

    def test_broken_compaction_copying_the_previous_shutdown_is_red(self):
        with self.assertRaises(AssertionError) as cm:
            self.check_compaction_does_not_inherit_the_shutdown_state(broken_compaction_copies_shutdown=True)
        self.assertIn("read as a clean stop", str(cm.exception))


if __name__ == "__main__":
    unittest.main()
