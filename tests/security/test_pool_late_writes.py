"""Review round 8, the pool lane of F1: a pool-grant write that lands late never brings a removed grant back.

At ad6aa1a a pool grant's authz write was best effort and unconditional (`PoolService.grant` -> `_persist_grant`, a plain PUT). The
cluster client times out (`es.EsClient`, `timeout`) while the request can still reach the index, so the grant's write could land AFTER
a later removal: after the removal's own delete was confirmed (its journal record then dropped at once), or after a start-up that saw
no grant in the copy and confirmed the removal from that absence. Compaction then dropped the record, and the next start rebuilt the
grant from the late write. The rule now (`PoolService._authz_put`): every write of a grant record is a compare-and-set against the
record it read, built from memory AFTER that read; a removal writes the record (`ops: []`), never a delete; a removal is confirmed only
by a write of the grant that landed after it, never by a read; pool counters are reserved per process in the epoch journal, so the
counter order of a grant and its removal holds across restarts.

The fake authz index (`tests/unit/fake_es.FakeAuthz`) holds a write (`hold`: the client sees a 503) and lands it later (`land_late`),
checking its compare-and-set conditions then, as the cluster does; tests/security/test_pool_late_writes_es.py replays a held request
on the real cluster. Everything else is the real code: the pool service, the epoch journal, compaction and the start-up."""
import tempfile
import threading
import unittest
from types import SimpleNamespace

from living_context.gateway.sessions import TokenError
from living_context.policy import Principal, Role
from tests.security.test_pool_tokens import T, World, q


class LateGrantWrites(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def world(self):
        return World(self.tmp.name).start()

    def removed(self, w, a, why, ops=frozenset()):
        self.assertEqual(w.dir.live_ops(T, "pt-user-alice", a), ops, f"the removed grant came back: {why}")
        if "query" not in ops:
            with self.assertRaises(TokenError):
                w.mint("alice", {a: q("query")}, conv=f"after-{len(why)}")

    def finish(self, w, a, why, ops=frozenset(), *, lands=False):
        """The late write reaches the index (refused there, unless `lands`: nothing wrote the record since it was read); then
        compaction, a restart, a second compaction and restart: the grant stays removed."""
        w.authz.land_late()
        self.assertEqual(w.authz.late, [])
        refused = w.authz.late_refused
        w.rev.compact()
        w.restart()
        self.removed(w, a, why, ops)
        w.rev.compact()
        w.restart()
        self.removed(w, a, why + ", a second restart", ops)
        w.rev.clean_stop()
        self.assertEqual(refused, 0 if lands else 1, f"the late write was {'refused' if lands else 'accepted'}: {why}")

    def test_a_late_grant_write_after_a_confirmed_removal(self):
        w = self.world()
        a = w.pool("alpha", grant_to=())
        w.authz.hold = {f"pg:pt-user-alice:{a}"}
        self.assertTrue(w.admin("pool_grant", who="user-alice", pool=a)["ok"])   # its write times out at the client
        w.authz.hold = set()
        r = w.admin("pool_ungrant", who="user-alice", pool=a)
        self.assertEqual((r["ok"], r["denied"], r["durable"]), (True, True, True))
        self.assertEqual(w.svc._unconfirmed, {})                                   # the removal's own write landed: confirmed
        self.finish(w, a, "a late write after a confirmed removal")

    def test_a_late_grant_write_after_a_removal_confirmed_at_the_next_start(self):
        w = self.world()
        a = w.pool("alpha", grant_to=())
        w.authz.hold = {f"pg:pt-user-alice:{a}"}
        self.assertTrue(w.admin("pool_grant", who="user-alice", pool=a)["ok"])
        w.authz.hold, w.authz.down = set(), True
        self.assertTrue(w.admin("pool_ungrant", who="user-alice", pool=a)["durable"])   # the removal's write fails
        w.authz.down = False
        w.restart()   # the copy holds no grant of alice on `a`: at ad6aa1a that absence confirmed the removal
        self.removed(w, a, "the start")
        self.finish(w, a, "a late write after a start that confirmed the removal")

    def test_a_late_wider_write_after_a_partial_removal(self):
        w = self.world()
        a = w.pool("alpha", grant_to=())
        w.authz.hold = {f"pg:pt-user-alice:{a}"}
        self.assertTrue(w.admin("pool_grant", who="user-alice", pool=a, ops=["query", "save"])["ok"])
        w.authz.hold = set()
        self.assertTrue(w.admin("pool_ungrant", who="user-alice", pool=a, ops=["query"])["durable"])
        self.assertEqual(w.svc._unconfirmed, {})
        self.finish(w, a, "a late wider write after a partial removal", ops=frozenset({"save"}))

    def test_a_late_write_of_an_earlier_process_after_its_counters_were_skipped(self):
        # process 1 grants alice `query` on `a` (written), takes two counters the copy never records (pool creations whose writes
        # fail), then adds `save` at a counter past every counter the copy holds; that write is still in flight when process 1 stops.
        # Process 2 removes the grant while the copy cannot be written, and the late write lands (nothing wrote the record since it was
        # read). Without a reservation, process 2's removal takes a counter BELOW the late grant's, and the next start keeps the late
        # grant as one "made after the removal".
        w = self.world()
        a = w.pool("alpha", grant_to=())
        self.assertTrue(w.admin("pool_grant", who="user-alice", pool=a, ops=["query"])["ok"])
        w.authz.down = True
        for label in ("lost one", "lost two"):
            self.assertEqual(w.admin("pool_create", cls="base", kind="team", label=label), {"ok": False, "error": "unavailable"})
        w.authz.down, w.authz.hold = False, {f"pg:pt-user-alice:{a}"}
        self.assertTrue(w.admin("pool_grant", who="user-alice", pool=a, ops=["save"])["ok"])
        w.authz.hold = set()
        w.restart()
        self.assertEqual(w.dir.live_ops(T, "pt-user-alice", a), frozenset({"query"}))   # process 2: the copy as written
        w.authz.down = True
        self.assertTrue(w.admin("pool_ungrant", who="user-alice", pool=a)["durable"])    # removed, not written
        w.authz.down = False
        self.finish(w, a, "a late write of an earlier process", lands=True)   # the counters, not the index, keep this one out


class ReadThenSnapshot(unittest.TestCase):
    """The order inside one write: the stored record is read BEFORE memory is snapshotted. Thread A (a grant) is stopped at its read of
    the record; thread B removes the grant and its write lands; A then goes on. A write that had snapshotted memory before its read
    would now write the old, wider grant conditioned on B's record, and land."""

    def test_a_grant_write_paused_at_its_read_cannot_land_the_grant_over_a_later_removal(self):
        with tempfile.TemporaryDirectory() as tmp:
            w = World(tmp).start()
            a = w.pool("alpha", grant_to=())
            gid = f"pg:pt-user-alice:{a}"
            arrived, go = threading.Event(), threading.Event()
            real = w.authz.request
            first = [True]

            def request(method, path, body=None, **kw):
                if method == "GET" and f"/_doc/{gid}" in path and first[0] and threading.current_thread().name == "grant":
                    first[0] = False
                    arrived.set()
                    go.wait(10)
                return real(method, path, body, **kw)
            w.authz.request = request
            out = {}
            t = threading.Thread(target=lambda: out.update(r=w.admin("pool_grant", who="user-alice", pool=a)), name="grant")
            t.start()
            if not arrived.wait(2):   # at ad6aa1a the grant writes without reading the record first: it is not stopped
                t.join(10)
            r = w.admin("pool_ungrant", who="user-alice", pool=a)
            go.set()
            t.join(10)
            self.assertTrue(out["r"]["ok"] and r["ok"])
            self.assertEqual(w.dir.live_ops(T, "pt-user-alice", a), frozenset())
            self.assertEqual(w.authz.docs.get(gid, {}).get("ops"), [], "the grant's write landed over the removal")
            w.rev.compact()
            w.restart()
            self.assertEqual(w.dir.live_ops(T, "pt-user-alice", a), frozenset(), "the removed grant came back")
            w.rev.clean_stop()


class MarkedConfirmation(unittest.TestCase):
    """A landed grant write confirms only the removals its body holds (those at a counter up to its mark). Thread A (a grant of `save`)
    has taken its body and is stopped before its write; the grant is removed while the copy cannot be written (the removal stays
    unconfirmed); A's write then lands, with the old, wider body. A confirmation that ignored the mark would take A's write as the
    removal's, compaction would drop the removal, and the next start would restore the grant from A's record."""

    def test_a_grant_write_that_lands_after_a_removal_does_not_confirm_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            w = World(tmp).start()
            a = w.pool("alpha", grant_to=())
            self.assertTrue(w.admin("pool_grant", who="user-alice", pool=a, ops=["query"])["ok"])
            gid = f"pg:pt-user-alice:{a}"
            arrived, go = threading.Event(), threading.Event()
            real = w.authz.request

            def request(method, path, body=None, **kw):
                if method == "PUT" and f"/_doc/{gid}" in path and threading.current_thread().name == "grant":
                    arrived.set()
                    go.wait(10)
                return real(method, path, body, **kw)
            w.authz.request = request
            out = {}
            t = threading.Thread(target=lambda: out.update(r=w.admin("pool_grant", who="user-alice", pool=a, ops=["save"])),
                                 name="grant")
            t.start()
            self.assertTrue(arrived.wait(10))
            w.authz.down = True
            self.assertTrue(w.admin("pool_ungrant", who="user-alice", pool=a)["durable"])   # removed in memory; its write fails
            w.authz.down = False
            go.set()
            t.join(10)
            self.assertTrue(out["r"]["ok"])
            self.assertEqual(sorted(w.authz.docs[gid]["ops"]), ["query", "save"])           # A's write landed, with the old body
            self.assertEqual(len(w.svc._unconfirmed), 1, "a write built before the removal confirmed it")
            w.rev.compact()
            w.restart()
            self.assertEqual(w.dir.live_ops(T, "pt-user-alice", a), frozenset(), "the removed grant came back")
            with self.assertRaises(TokenError):
                w.mint("alice", {a: q("query")}, conv="after")
            w.rev.clean_stop()


class ErasedPool(unittest.TestCase):
    """A removal still unconfirmed when its pool is deleted is not written again at the next start: the erasure removed every record
    of the pool, and a start-up write would put a record naming the pool and the principal back into the copy. Start-up never restores
    a grant of a pool that is gone (`pool_boot.load`), so the removal needs no record any more."""

    def test_a_pending_removal_of_an_erased_pool_writes_no_record_at_the_next_start(self):
        with tempfile.TemporaryDirectory() as tmp:
            holder = {}

            def eraser(cls, pool):   # the erasure's effect on the authz copy: every record of the pool is deleted
                fa = holder["w"].authz
                gone = [d for d in fa.docs if d == f"pp:{pool}" or (d.startswith("pg:") and d.endswith(":" + pool))]
                for d in gone:
                    del fa.docs[d]
                    fa.seqs.pop(d, None)
                return SimpleNamespace(docs_deleted=len(gone), indices_dropped=[])
            w = holder["w"] = World(tmp, svc_kw={"eraser": eraser}).start()
            a = w.pool("alpha")
            gid = f"pg:pt-user-alice:{a}"
            w.authz.down = True
            self.assertTrue(w.admin("pool_ungrant", who="user-alice", pool=a)["durable"])   # its write fails: unconfirmed
            w.authz.down = False
            self.assertEqual(len(w.svc._unconfirmed), 1)
            admins = [Principal(T, f"pt-user-{n}", frozenset({Role.TENANT_ADMIN}), frozenset({"base"}), frozenset({"base"}))
                      for n in ("admin", "admin2")]
            self.assertEqual(w.svc.delete(admins[0], a), {"ok": False, "error": "pending"})
            self.assertTrue(w.svc.delete(admins[1], a)["deleted"])
            self.assertFalse([d for d in w.authz.docs if d.endswith(a)])
            for _ in range(2):
                w.rev.compact()
                w.restart()
                self.assertIsNone(w.dir.get(T, a))
                self.assertNotIn(gid, w.authz.docs, "the start-up wrote a record of an erased pool")
                self.assertEqual(w.svc._unconfirmed, {})
            w.rev.clean_stop()


class Reservation(unittest.TestCase):
    def test_counters_are_reserved_in_the_journal_and_each_start_begins_above_the_last_ceiling(self):
        with tempfile.TemporaryDirectory() as tmp:
            w = World(tmp).start()
            a = w.pool("alpha")
            w.restart()
            first = w.svc._ceiling
            c1 = w.dir.next_counter(T)
            self.assertLess(c1, first)
            w.rev.compact()                  # compaction keeps the ceiling
            w.restart()
            self.assertGreater(w.dir.next_counter(T), first)
            self.assertGreater(w.svc._ceiling, first)
            w.dir.limit_counter(T, w.dir.counter(T))   # a block that is used up refuses, never reuses
            self.assertEqual(w.admin("pool_grant", who="user-bob", pool=a), {"ok": False, "error": "unavailable"})
            w.rev.clean_stop()


if __name__ == "__main__":
    unittest.main()
