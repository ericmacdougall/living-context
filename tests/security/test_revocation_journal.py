"""BUILD-PLAN Step 15 core, no Elasticsearch: revoke order, durable start record, fail-closed across restarts, compaction,
clean stop, break-glass, and a power-loss simulation through the journal's I/O layer (EXPERIMENT R7 subset). Every broken
variant (`broken_*` flags) must turn its check red."""
import os
import tempfile
import time
import unittest
from pathlib import Path

from living_context.gateway import epoch_journal, revocation
from living_context.gateway.epoch_journal import RealIO
from living_context.gateway.fence import SendFence
from living_context.gateway.revocation import CLOSED, FAULT, OPEN, RevocationService
from living_context.gateway.sessions import EpochTable

T = "tenaaa"


class SimIO(RealIO):
    """Power-loss simulation: bytes written and not fsynced, and a name exchange whose directory was not fsynced, are undone."""

    def __init__(self):
        self.pending: dict[str, list] = {}
        self.pending_dir: list = []

    def pwrite(self, fd, data, offset):
        path = os.readlink(f"/proc/self/fd/{fd}")
        self.pending.setdefault(path, []).append((offset, os.pread(fd, len(data), offset)))
        os.pwrite(fd, data, offset)

    def fsync(self, fd):
        self.pending.pop(os.readlink(f"/proc/self/fd/{fd}"), None)
        os.fsync(fd)

    def fsync_dir(self, directory):
        self.pending_dir.clear()
        super().fsync_dir(directory)

    def exchange(self, a, b):
        super().exchange(a, b)
        self.pending_dir.append((a, b))

    def crash(self):
        for a, b in reversed(self.pending_dir):
            super().exchange(a, b)
        for path, writes in self.pending.items():
            fd = os.open(path, os.O_RDWR)
            for offset, old in reversed(writes):
                os.pwrite(fd, old, offset)
            os.close(fd)
        self.pending.clear()
        self.pending_dir.clear()


class SlowIO(RealIO):
    def __init__(self, delay=0.0, on_fsync=None, fail=False):
        self.delay, self.on_fsync, self.fail = delay, on_fsync, fail

    def fsync(self, fd):
        if self.on_fsync:
            self.on_fsync()
        if self.fail:
            raise OSError(5, "injected")
        time.sleep(self.delay)
        super().fsync(fd)


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name) / T
        self.size = 8192
        epoch_journal.provision(self.dir, self.size)
        self.fence = SendFence()

    def svc(self, **kw):
        self.epochs = EpochTable(lambda t: self.fence)
        s = RevocationService(T, self.epochs, self.dir, **kw)
        self.addCleanup(lambda: s._j and s._j.close())
        return s


class RevokeOrder(Base):
    def check_denial_before_fsync(self, **kw):
        seen = []
        io = SlowIO(0.05, on_fsync=lambda: seen.append(self.epochs.is_revoked(T, "p1")))
        s = self.svc(io=io, **kw)
        s.start()
        seen.clear()
        s.revoke("p1")
        self.assertTrue(seen and all(seen), "the revoke waited for the journal fsync before denying")

    def test_denial_is_in_memory_before_the_journal_fsync(self):
        self.check_denial_before_fsync()

    def test_broken_denial_after_the_fsync_is_red(self):
        with self.assertRaises(AssertionError) as cm:
            self.check_denial_before_fsync(broken_wait_for_journal=True)
        self.assertIn("waited for the journal fsync", str(cm.exception))

    def test_a_revoke_is_durable_and_rebuilds_the_table_after_a_clean_restart(self):
        s = self.svc()
        s.start()
        rc = s.revoke("p1")
        self.assertIsNotNone(rc.durable_at)
        s.clean_stop()
        s2 = self.svc()
        self.assertEqual(s2.start(), OPEN)
        self.assertTrue(self.epochs.is_revoked(T, "p1"))
        self.assertEqual(self.epochs.epoch(T, "p1"), rc.epoch)

    def test_a_persist_failure_never_blocks_the_revoke_but_a_required_persist_does(self):
        def blocked(principal, epoch):
            raise RuntimeError("429")
        s = self.svc(persist=blocked)
        s.start()
        rc = s.revoke("p1")
        self.assertTrue(self.epochs.is_revoked(T, "p1"))
        self.assertEqual((rc.persisted, s.persist_failures), (False, 1))
        self.assertIsNotNone(rc.durable_at)
        s2 = self.svc(persist=blocked, broken_require_persist=True)
        s2.state, s2.lane_open = OPEN, True
        s2._j = epoch_journal.Journal(self.dir / "epochs.journal")
        with self.assertRaises(RuntimeError):
            s2.revoke("p2")
        self.assertFalse(self.epochs.is_revoked(T, "p2"), "the broken variant is red when its revoke is not effective")


class FailClosed(Base):
    def test_an_fsync_over_the_bound_closes_the_tenant_and_the_revoke_stays_denied(self):
        s = self.svc(io=SlowIO(0.3), d_fsync_s=0.1)
        s.start()
        rc = s.revoke("p1")
        self.assertTrue(self.epochs.is_revoked(T, "p1"))
        self.assertIsNone(rc.durable_at)
        self.assertEqual((s.state, s.admit()), (FAULT, False))

    def test_a_journal_error_closes_the_tenant(self):
        io = SlowIO()
        s = self.svc(io=io)
        s.start()
        io.fail = True
        rc = s.revoke("p1")
        self.assertEqual((s.state, rc.durable_at), (FAULT, None))

    def check_stays_closed(self, **kw):
        s = self.svc(io=SlowIO(0.3), d_fsync_s=0.1, **kw)
        s.start()
        s.revoke("p1")
        s.clean_stop()        # a fault-closed tenant gets no shutdown record
        s2 = self.svc()
        self.assertEqual(s2.start(), FAULT, "a fault-closed tenant was readmitted after a restart")

    def test_fault_closed_stays_closed_across_restarts(self):
        self.check_stays_closed()

    def test_broken_shutdown_record_for_a_fault_closed_tenant_is_red(self):
        with self.assertRaises(AssertionError) as cm:
            self.check_stays_closed(broken_shutdown_when_faulted=True)
        self.assertIn("readmitted", str(cm.exception))

    def check_incomplete(self, **kw):
        s = self.svc()
        s.start()
        s.revoke("p1")
        s._j.close()          # abrupt stop: no shutdown record
        s._j = None
        s2 = self.svc(**kw)
        self.assertEqual(s2.start(), FAULT, "a journal without a shutdown record was accepted at start")

    def test_an_incomplete_journal_at_start_closes_the_tenant(self):
        self.check_incomplete()

    def test_broken_start_accepting_an_unterminated_journal_is_red(self):
        with self.assertRaises(AssertionError) as cm:
            self.check_incomplete(broken_accept_unterminated=True)
        self.assertIn("without a shutdown record", str(cm.exception))

    def test_a_torn_journal_closes_the_tenant(self):
        s = self.svc()
        s.start()
        s.revoke("p1")
        end = s._j.end
        s.clean_stop()
        with open(self.dir / "epochs.journal", "r+b") as f:
            f.seek(end - 40)
            f.write(b"\xff\xff\xff\xff")
        self.assertEqual(self.svc().start(), FAULT)


class PowerLoss(Base):
    def check_start_record_durable(self, **kw):
        io = SimIO()
        s = self.svc(io=io)
        s.start()
        s.clean_stop()
        s2 = self.svc(io=io, **kw)
        self.assertEqual(s2.start(), OPEN)      # admitted
        io.crash()                              # power loss right after admission
        s3 = self.svc()
        self.assertEqual(s3.start(), FAULT, "a gateway that had admitted requests was re-admitted as if it had stopped cleanly")

    def test_the_start_record_is_durable_before_admission(self):
        self.check_start_record_durable()

    def test_broken_start_record_without_fsync_is_red(self):
        with self.assertRaises(AssertionError) as cm:
            self.check_start_record_durable(broken_start_without_fsync=True)
        self.assertIn("re-admitted", str(cm.exception))

    def test_compaction_survives_a_power_loss_at_every_boundary(self):
        for crash_point in ("before_exchange", "after_exchange_before_dir_fsync", "after"):
            with self.subTest(crash_point):
                self.tmp.cleanup()
                self.tmp = tempfile.TemporaryDirectory()
                self.dir = Path(self.tmp.name) / T
                epoch_journal.provision(self.dir, self.size)
                io = SimIO()
                s = self.svc(io=io)
                s.start()
                for p in ("p1", "p2", "p3"):
                    s.revoke(p)
                want = self.epochs.snapshot(T)
                if crash_point == "before_exchange":
                    orig = io.exchange
                    io.exchange = lambda a, b: (_ for _ in ()).throw(OSError(5, "power loss"))
                    with self.assertRaises(OSError):
                        s.compact()
                    io.exchange = orig
                else:
                    if crash_point == "after_exchange_before_dir_fsync":
                        io.fsync_dir = lambda d: None
                    s.compact()
                io.crash()
                s._j and s._j.close()
                s._j = None
                s2 = self.svc()
                s2.break_glass([], ["adm1"])   # an unclean end: the operator reopens it; the rebuilt table must equal the live one
                self.assertEqual(self.epochs.snapshot(T), want)

    def test_a_full_journal_compacts_into_the_spare_and_keeps_every_revoke(self):
        s = self.svc()
        s.start()
        for _ in range(30):                       # 300 records do not fit in 8 KiB; the 10 live rows do
            for i in range(10):
                s.revoke(f"principal{i:03d}")
        self.assertIn("compacted", s.events)
        self.assertEqual(s.state, OPEN)
        s.clean_stop()
        s2 = self.svc()
        self.assertEqual(s2.start(), OPEN)
        self.assertTrue(all(self.epochs.is_revoked(T, f"principal{i:03d}") and self.epochs.epoch(T, f"principal{i:03d}") == 30 for i in range(10)))
        self.assertEqual(self.dir.joinpath("epochs.journal").stat().st_size, self.size)  # never grows


class Capacity(Base):
    def test_live_rows_that_exceed_the_journal_close_the_tenant_and_the_denial_stays(self):
        s = self.svc()
        s.start()
        last = None
        for i in range(400):                      # 400 distinct principals cannot fit in 8 KiB even after compaction
            last = s.revoke(f"principal{i:03d}")
            if s.state != OPEN:
                break
        self.assertEqual(s.state, FAULT)
        self.assertIsNone(last.durable_at)
        self.assertTrue(self.epochs.is_revoked(T, last.principal))   # the denial that could not be journalled still holds
        with self.assertRaises(revocation.Refused):
            s.revoke("another")


class Recovery(Base):
    def crashed(self):
        s = self.svc()
        s.start()
        s.revoke("p1")
        s._j.close()
        s._j = None

    def test_break_glass_applies_the_revokes_before_it_reopens_any_administrator(self):
        self.crashed()
        s = self.svc()
        self.assertEqual(s.start(), FAULT)
        s.break_glass(["p9"], ["adm1"])
        self.assertLess(s.events.index("revoke:p9"), s.events.index("admins:adm1"))
        self.assertEqual((s.state, s.may_grant("adm1"), s.may_grant("adm2")), (OPEN, True, False))
        self.assertTrue(self.epochs.is_revoked(T, "p9") and self.epochs.is_revoked(T, "p1"))
        s.clean_stop()
        self.assertEqual(self.svc().start(), OPEN)   # the recovery record and the new start record make a normal restart

    def test_broken_admins_reopened_before_the_revokes_is_red(self):
        self.crashed()
        s = self.svc(broken_admins_first=True)
        s.start()
        s.break_glass(["p9"], ["adm1"])
        with self.assertRaises(AssertionError):
            self.assertLess(s.events.index("revoke:p9"), s.events.index("admins:adm1"))

    def test_clean_stop_closes_the_lane_and_ends_with_a_shutdown_record(self):
        s = self.svc()
        s.start()
        s.revoke("p1")
        s.clean_stop()
        with self.assertRaises(revocation.Refused):
            s.revoke("p2")
        self.assertEqual(epoch_journal.Journal(self.dir / "epochs.journal").records[-1]["t"], "shutdown")


if __name__ == "__main__":
    unittest.main()
