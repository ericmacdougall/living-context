"""Review round 7 (pools C7, the admin-lane form of F18): a class-grant REDUCTION holds across a restart whatever its authz write
answered, and the administrator is never told ok for one that would not.

A reduction is a class removal (`remove_class`) or a grant that takes roles or classes away. The gateway answers it from memory; the
authz copy (`lc-<t>-authz`) is what the next start reads back. Before round 7 a refused or malformed answer to that write was caught,
counted and the call answered ok, so after a restart the removed class or the larger grant was back. The rule now
(`admin.GrantStore`): the reduction is journalled in the tenant's epoch journal before the answer, replayed over the grants read back
at start-up, and kept by compaction until a validated authz write (or a complete start-up read) confirms it. Additions stay best effort
(a lost addition is lost access, fail safe).

No Elasticsearch: the authz index is a fake that answers each PUT with a well-formed acknowledgement, or with the planted answer (and
then writes nothing). Everything else is the real code: the composition root `boot.start_tenant` (epoch journal, the grant persister
through `reads.put_doc_landed`, the start-up read through `reads.scan`), the admin lane `Admin`, compaction, a clean stop, a crash
(no shutdown record) with the operator's break-glass recovery.

The classes `AReductionHoldsAcrossARestart` and `TheTenantClosed` use only the API that existed before round 7, so they run red
against it; `GrantStoreRules` covers the store's new rules directly."""
import tempfile
import unittest
from pathlib import Path

from living_context.gateway import epoch_journal
from living_context.gateway.admin import Admin, GrantStore
from living_context.gateway.boot import start_tenant
from living_context.gateway.fence import SendFence
from living_context.gateway.revocation import FAULT, OPEN, Refused, RevocationService
from living_context.gateway.sessions import EpochTable, Sessions
from living_context.keystore.store import Keystore
from living_context.policy import ClassLattice, Principal, Role

T = "tenaaa"
LAT = ClassLattice(["base", "restricted"], [("restricted", "base")])
AUTHZ = f"lc-{T}-authz"
SH = {"total": 1, "successful": 1, "failed": 0}


def tok(tenant, who):
    return f"tok-{who}"


def ack(did, seq):
    return {"_index": AUTHZ, "_id": did, "_version": seq, "result": "updated", "_shards": SH, "_seq_no": seq, "_primary_term": 1}


# the answers of the F18 matrix: a malformed acknowledgement, an acknowledgement about another record, a plain failure
REFUSALS = {"a malformed 200": (200, {}), "a foreign 200": (200, ack("gr:tok-someone-else", 99)), "a 503": (503, {})}


class FakeAuthz:
    """The authz index of one tenant: a PUT lands (well-formed acknowledgement) unless `refuse` is set, when it answers `refuse` and
    writes nothing. A scan (PIT, one search page, PIT close) returns every stored grant."""

    def __init__(self):
        self.docs, self.refuse, self.seq, self.refused = {}, None, 0, 0

    def request(self, method, path, body=None, **kw):
        if method == "PUT" and "/_doc/" in path:
            did = path.split("/_doc/")[1].split("?")[0]
            if self.refuse is not None:
                self.refused += 1
                return self.refuse
            self.seq += 1
            self.docs[did] = body
            return 200, ack(did, self.seq)
        if method == "POST" and path.startswith(f"/{AUTHZ}/_pit"):
            return 200, {"id": "pit1", "_shards": SH}
        if method == "POST" and path.startswith("/_search"):
            hits = [{"_index": AUTHZ, "_id": d, "_source": s, "sort": [i]} for i, (d, s) in enumerate(sorted(self.docs.items()))]
            return 200, {"timed_out": False, "_shards": SH, "hits": {"hits": hits}}
        if method == "DELETE" and path == "/_pit":
            return 200, {"succeeded": True, "num_freed": 1}
        return 400, {"error": {"type": "unexpected", "reason": f"{method} {path}"}}


class Gate:
    def __init__(self, authz):
        self.authz = authz

    def client(self, tenant, role):
        assert role == "authz"
        return self.authz


class Idp:
    def verify(self, tenant, proof):
        return proof[len(b"proof-"):].decode() if proof.startswith(b"proof-") else None


class Node:
    """One gateway process of the tenant: the composition root over the tenant's directories and its (fake) authz index."""

    def __init__(self, test, w, **admin_kw):
        self.fence = SendFence()
        self.epochs = EpochTable(lambda t: self.fence)
        self.rt = start_tenant(T, gate=Gate(w.authz), epochs=self.epochs, keystore=w.ks, **w.dirs)
        test.addCleanup(lambda: self.rt.revocation._j and self.rt.revocation._j.close())
        self.admin = Admin(self.rt.grants, self.epochs, {T: LAT}, lambda t: self.rt.revocation, tok, **admin_kw)
        self.sessions = Sessions({T: Idp()}, self.epochs, tok, lambda t, p: self.rt.grants.get(t, p))

    def get(self, who):
        return self.rt.grants.get(T, tok(T, who))


class World:
    def __init__(self, test, **admin_kw):
        tmp = tempfile.TemporaryDirectory()
        test.addCleanup(tmp.cleanup)
        d = Path(tmp.name)
        self.test, self.admin_kw = test, admin_kw
        self.dirs = dict(epoch_dir=d / "epochs", tombstone_paths=(d / "tomb" / "t.log", d / "tomb-replica" / "t.log"),
                         holds_path=d / "holds" / "h.json")
        epoch_journal.provision(self.dirs["epoch_dir"], 16384)
        self.ks = Keystore(d / "ks.db", T)
        self.authz = FakeAuthz()
        self.node = Node(test, self, **admin_kw)
        test.assertEqual(self.node.rt.revocation.state, OPEN)
        for a in ("a1", "a2"):
            self.put(a, {Role.TENANT_ADMIN})
        self.put("r1", {Role.READER}, read={"base", "restricted"})
        test.assertEqual(self.authz.docs["gr:tok-r1"]["read"], ["base", "restricted"])   # the grant landed before any refusal

    def put(self, who, roles, read=(), write=()):
        self.node.rt.grants.put(Principal(T, tok(T, who), frozenset(roles), frozenset(read), frozenset(write)))

    def restart(self, how):
        """Compaction, then a clean stop or a crash (no shutdown record: the next start stays closed until the operator's
        break-glass recovery), then a new process over the same directories and the same authz index."""
        svc = self.node.rt.revocation
        svc.compact()
        if how == "clean":
            svc.clean_stop()
        else:
            svc._j.close()
            svc._j = None
            svc._drop_lock()
        self.node = Node(self.test, self, **self.admin_kw)
        svc = self.node.rt.revocation
        if how == "crash":
            self.test.assertEqual(svc.state, FAULT)
            svc.break_glass([], [tok(T, "a1")])
        self.test.assertEqual(svc.state, OPEN)
        self.test.assertEqual(self.node.rt.grant_load_error, "", "the start-up read of the authz copy failed")
        self.test.assertEqual(self.node.rt.loaded_grants, 3)

    def journal_reductions(self):
        """The reduction records the compacted journal holds (read from the file, after a clean stop)."""
        j = epoch_journal.Journal(self.node.rt.revocation.journal_path)
        try:
            return [r for r in j.records if r.get("t") == "grant_reduce"]
        finally:
            j.close()


def remove_restricted(n):
    return n.admin.remove_class(n.get("a1"), T, LAT, "r1", "restricted")


def grant_base_only(n):
    return n.admin.grant(n.get("a1"), T, LAT, "r1", {"grant_roles": ["reader"], "read": ["base"]})


REDUCTIONS = {"remove_class": remove_restricted, "a reducing grant": grant_base_only}


class AReductionHoldsAcrossARestart(unittest.TestCase):
    def reduced(self, w, why):
        g = w.node.get("r1")
        self.assertIsNotNone(g, why)
        self.assertIn(Role.READER, g.roles, why)
        self.assertIn("base", g.read_set, why)
        self.assertNotIn("restricted", g.read_set, f"the removed class is back after the restart: {why}")

    def test_a_reduction_whose_authz_write_is_refused_holds_after_compaction_and_a_restart(self):
        for name, reduce in REDUCTIONS.items():
            for answer, refusal in REFUSALS.items():
                for how in ("clean", "crash"):
                    with self.subTest(reduction=name, answer=answer, restart=how):
                        w = World(self)
                        w.authz.refuse = refusal
                        self.assertTrue(reduce(w.node).get("ok"))
                        self.assertGreaterEqual(w.authz.refused, 1, "the reduction wrote nothing to refuse")
                        self.assertNotIn("restricted", w.node.get("r1").read_set)
                        w.authz.refuse = None
                        self.assertEqual(w.authz.docs["gr:tok-r1"]["read"], ["base", "restricted"])   # the copy is stale
                        w.restart(how)
                        self.reduced(w, f"{name}, {answer}, {how}")
                        # the record outlives a second compaction too: nothing has confirmed it
                        w.restart("clean")
                        self.reduced(w, f"{name}, {answer}, {how}, a second restart")
                        w.node.rt.revocation.clean_stop()
                        self.assertEqual(len(w.journal_reductions()), 1, "compaction dropped an unconfirmed reduction")

    def test_control_a_confirmed_write_drops_the_record_and_the_reduction_still_holds(self):
        for name, reduce in REDUCTIONS.items():
            for how in ("clean", "crash"):
                with self.subTest(reduction=name, restart=how):
                    w = World(self)
                    self.assertTrue(reduce(w.node).get("ok"))
                    self.assertEqual(w.authz.docs["gr:tok-r1"]["read"], ["base"])   # the write landed
                    w.restart(how)
                    self.reduced(w, f"{name}, {how}")
                    w.node.rt.revocation.clean_stop()
                    self.assertEqual(w.journal_reductions(), [], "a confirmed reduction was kept by compaction")

    def test_a_swap_keeps_only_what_both_grants_hold(self):
        # read {base, restricted} -> read {base} and write {base}: the removal holds; the addition was lost with its write (fail safe)
        for answer, refusal in REFUSALS.items():
            with self.subTest(answer=answer):
                w = World(self)
                w.authz.refuse = refusal
                n = w.node
                self.assertTrue(n.admin.grant(n.get("a1"), T, LAT, "r1", {"grant_roles": ["reader"], "read": ["base"],
                                                                          "write": ["base"]}).get("ok"))
                w.authz.refuse = None
                w.restart("clean")
                g = w.node.get("r1")
                self.assertEqual((g.read_set, g.write_set), (frozenset({"base"}), frozenset()))

    def test_a_role_swap_leaves_no_grant_and_no_session(self):
        # reader -> writer: the reader role is taken away (journalled), the writer role was an addition whose write failed
        for answer, refusal in REFUSALS.items():
            with self.subTest(answer=answer):
                w = World(self)
                w.authz.refuse = refusal
                n = w.node
                self.assertTrue(n.admin.grant(n.get("a1"), T, LAT, "r1", {"grant_roles": ["writer"], "write": ["base"]}).get("ok"))
                w.authz.refuse = None
                w.restart("clean")
                self.assertIsNone(w.node.get("r1"), "a reader role taken away is back after the restart")
                self.assertIsNone(w.node.sessions.mint(T, b"proof-r1"))
                self.assertIsNotNone(w.node.sessions.mint(T, b"proof-a1"))   # control: the store still mints

    def test_a_newer_grant_that_landed_is_never_undone_by_an_older_reduction(self):
        w = World(self)
        w.authz.refuse = (503, {})
        self.assertTrue(remove_restricted(w.node).get("ok"))
        w.authz.refuse = None
        n = w.node
        self.assertTrue(n.admin.grant(n.get("a1"), T, LAT, "r1", {"grant_roles": ["reader"], "read": ["base", "restricted"]}).get("ok"))
        w.restart("clean")
        self.assertIn("restricted", w.node.get("r1").read_set, "an older reduction undid a newer grant")
        w.node.rt.revocation.clean_stop()
        self.assertEqual(w.journal_reductions(), [], "the later landed write did not confirm the reduction")

    def test_a_newer_grant_whose_write_failed_too_is_lost_and_the_reduction_holds(self):
        w = World(self)
        w.authz.refuse = (503, {})
        self.assertTrue(remove_restricted(w.node).get("ok"))
        n = w.node
        self.assertTrue(n.admin.grant(n.get("a1"), T, LAT, "r1", {"grant_roles": ["reader"], "read": ["base", "restricted"]}).get("ok"))
        self.assertIn("restricted", n.get("r1").read_set)   # in memory the addition holds
        w.authz.refuse = None
        w.restart("clean")
        self.assertNotIn("restricted", w.node.get("r1").read_set)


class TheTenantClosed(unittest.TestCase):
    def test_a_reduction_whose_journal_record_is_not_durable_is_never_answered_ok(self):
        # the broken no-bump variant takes away the epoch bump that would otherwise raise on the closed tenant: the reduction's own
        # journal receipt is what must refuse the answer
        for name, reduce in REDUCTIONS.items():
            with self.subTest(reduction=name):
                w = World(self, broken_removal_without_bump=True)
                svc = w.node.rt.revocation
                svc._append = lambda rec, svc=svc: (svc._append_fault("planted append failure"), False)[1]
                with self.assertRaises(Refused):
                    reduce(w.node)
                self.assertNotEqual(svc.state, OPEN)
                self.assertNotIn("restricted", w.node.get("r1").read_set)   # memory holds the reduction (fail safe)

    def test_a_closed_tenant_refuses_a_reduction_and_changes_nothing(self):
        w = World(self)
        w.node.rt.revocation.clean_stop()
        for name, reduce in REDUCTIONS.items():
            with self.subTest(reduction=name):
                try:
                    answer = reduce(w.node)
                except Refused:
                    answer = {"ok": False, "error": "unavailable"}   # the handler's typed answer for a closed tenant
                self.assertFalse(answer.get("ok"))
                self.assertIn("restricted", w.node.get("r1").read_set)


class GrantStoreRules(unittest.TestCase):
    P = Principal(T, "tok-x", frozenset({Role.READER, Role.REORGANISER}), frozenset({"base", "restricted"}), frozenset({"base"}),
                  "base")

    def journal(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        d = Path(tmp.name) / T
        epoch_journal.provision(d, 16384)
        svc = RevocationService(T, EpochTable(lambda t: SendFence()), d)
        self.addCleanup(lambda: svc._j and svc._j.close())
        return svc

    def test_put_refuses_every_kind_of_reduction(self):
        p = self.P
        for name, q in (("a role", Principal(T, p.token, frozenset({Role.READER}), p.read_set, p.write_set, p.own_class)),
                        ("a read class", Principal(T, p.token, p.roles, frozenset({"base"}), p.write_set, p.own_class)),
                        ("a write class", Principal(T, p.token, p.roles, p.read_set, frozenset(), p.own_class)),
                        ("the own class", Principal(T, p.token, p.roles, p.read_set, p.write_set, None))):
            with self.subTest(takes_away=name):
                st = GrantStore()
                st.put(p)
                with self.assertRaises(ValueError):
                    st.put(q)
                self.assertEqual(st.get(T, p.token), p)
        st = GrantStore()
        st.put(p)
        wider = Principal(T, p.token, p.roles | {Role.WRITER}, p.read_set, p.write_set | {"restricted"}, p.own_class)
        st.put(wider)   # control: an addition passes
        self.assertEqual(st.get(T, p.token), wider)

    def test_a_reduction_needs_the_tenant_journal(self):
        with self.assertRaises(RuntimeError):
            GrantStore().reduce(self.P)

    def test_replay_narrows_a_stale_copy_and_never_a_newer_one(self):
        rec = {"t": "grant_reduce", "principal": "tok-x", "roles": ["reader"], "read": ["base"], "write": [], "own_class": "", "rev": 4}
        for copy_rev, narrowed in ((3, True), (4, False), (5, False)):
            with self.subTest(copy_rev=copy_rev):
                st = GrantStore()
                st.attach(T, self.journal())
                st.apply_record(T, rec)            # the journal replays first (start_journal=True) ...
                st.restore(self.P, copy_rev)       # ... then the copy is read back
                st.loaded(T)
                g = st.get(T, "tok-x")
                self.assertEqual(g == self.P, not narrowed)
                if narrowed:
                    self.assertEqual((g.roles, g.read_set, g.write_set, g.own_class), (frozenset({Role.READER}), frozenset({"base"}),
                                                                                       frozenset(), None))
                self.assertEqual(len(st.snapshot_records(T)), 1 if narrowed else 0)

    def test_only_a_complete_read_confirms_the_reduction_of_a_principal_the_copy_does_not_hold(self):
        rec = {"t": "grant_reduce", "principal": "tok-x", "roles": ["reader"], "read": ["base"], "write": [], "own_class": "", "rev": 2}
        st = GrantStore()
        st.attach(T, self.journal())
        st.apply_record(T, rec)
        self.assertEqual(len(st.snapshot_records(T)), 1)   # a failed or partial read: `loaded` is never called, the record stays
        st.loaded(T)
        self.assertEqual(st.snapshot_records(T), [])

    def test_a_superseded_write_is_not_sent(self):
        sent = []
        st = GrantStore(persist=lambda p, rev: sent.append(rev))
        st.put(self.P)
        with self.assertRaises(RuntimeError):
            st._persist_at(self.P, 0)   # revision 0 is older than the grant in memory (1)
        self.assertEqual(sent, [1])
        self.assertEqual(st.persist_failures, 1)


if __name__ == "__main__":
    unittest.main()
