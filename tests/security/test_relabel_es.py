"""Upward class relabel (DESIGN 14.1, review F19c; BUILD-PLAN Step 14): moving a leaf to a class a former reader may not read is an
erasure of the old-class copy. The tombstone has the scope (leaf id, old class) and its redact set, is published at the boundary,
and the old copy (source record, leaf projection, members, the node versions that derive from it, its key) is gone; the new
version is written in the new class with a higher `lv`. A restore's replay of that tombstone removes a resurrected old-class copy
and leaves the new-class copy alone.

Broken variants, each red for its stated reason: a relabel that leaves the old copy (nothing erased); a closure that ignores the
class (a replay after the new copy was written deletes the new-class copy too). The module FAILS without LC_TEST_ES_URL,
LC_TEST_CA, LC_TEST_PW."""
import tempfile
import unittest
from pathlib import Path

from living_context.erasure.executor import Executor, State
from living_context.erasure.restore import restore_replay
from living_context.gateway.es import EsClient
from living_context.gateway.fence import SendFence
from living_context.gateway.gate import WriteGate
from living_context.gateway.handler import EsGate
from living_context.gateway.indices import class_index, tenant_index
from living_context.gateway.journal import TombstoneJournal
from living_context.gateway.provision import provision_tenant
from living_context.keystore import Keystore
from living_context.ingest.subjects import Guard, TokenSource
from living_context.verifier.check import Report
from tests.security import vworld
from tests.security.vworld import VersionsBase

B_ERASE_S = 60.0


class Relabel(VersionsBase):
    T = "vrlb"
    _n = 0

    @classmethod
    def setUpClass(cls):
        if not (vworld.URL and vworld.CA and vworld.PW):
            raise RuntimeError("LC_TEST_ES_URL, LC_TEST_CA and LC_TEST_PW must point at a scratch cluster")
        cls.tmp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.tmp.name)
        cls.admin = EsClient(vworld.URL, vworld.CA, vworld.PW, scheme="basic", user="elastic")
        cls.reset()
        r = provision_tenant(cls.admin, cls.T, ["base", "priv"], key_dir=cls.root / "keys", data_root=cls.root / "data",
                             journal_size=65536)
        cls.gate = EsGate(vworld.URL, vworld.CA, {cls.T: r["keys"]})
        cls.ks = Keystore(cls.root / "ks.db", cls.T)
        cls.guard = Guard(TokenSource(cls.ks))

    def setUp(self):
        Relabel._n += 1
        self.wgate, self.fence, self.state = WriteGate(), SendFence(), State()
        d = Path(tempfile.mkdtemp(dir=self.root))
        self.journal = TombstoneJournal(d / "j" / "t.log", d / "r" / "t.log")
        self.email = f"rlb{Relabel._n}x{id(self) % 9973}@example.com"

    def wipe(self):
        super().wipe()
        for kind in ("leaf", "node", "member", "edge", "ctl"):
            self.admin.request("POST", f"/lc-{self.T}-priv-{kind}/_delete_by_query?refresh=true&conflicts=proceed",
                               {"query": {"match_all": {}}})

    def executor(self, **kw):
        return Executor(lambda: self.gate.client(self.T, "erase"), self.structure(), self.T, self.ks, self.journal, self.wgate,
                        self.fence, self.state, purge_deadline_s=2, purge_interval_s=1,
                        es_log=lambda: self.gate.client(self.T, "log"), **kw)

    def n_docs(self, index, query):
        self.admin.request("POST", f"/{index}/_refresh")
        st, r = self.admin.request("POST", f"/{index}/_count", {"query": query})
        return r["count"] if st == 200 else -1

    def old_copy_docs(self, leaf_id, max_lv=1):
        """Documents of the old class that still hold the leaf: source records, leaf projections, members, node lineage."""
        n = 0
        for lv in range(1, max_lv + 1):
            n += self.n_docs(tenant_index(self.T, "src"), {"ids": {"values": [f"{leaf_id}:{lv}"]}})
            n += self.n_docs(class_index(self.T, "base", "leaf"), {"ids": {"values": [f"{leaf_id}:{lv}"]}})
        for kind in ("member", "node", "edge"):
            n += self.n_docs(class_index(self.T, "base", kind), {"term": {"lin_leaf_ids": leaf_id}})
        return n

    def plant(self):
        w = self.world()
        w.bootstrap(topics=4, per=4)
        res = w.add(0, 1, extra=f" {self.email}", place=True)[0]
        r = w.rebuilder().run()
        self.assertIn(r.status, ("published", "nothing"), r)
        self.admin.request("POST", f"/lc-{self.T}-*/_refresh")
        self.assertGreater(self.old_copy_docs(res.leaf_id), 2, "the old copy was not planted in leaf, member and node")
        self.assertTrue(self.ks.has_key(res.leaf_id, 1))
        return w, res

    def new_copy(self, w, res):
        text = f"relabelled copy of {res.leaf_id[:6]} kept in the new class"
        out = w.writer.relabel_write("tok", res.leaf_id, res.lv + 1, "priv", text)
        self.assertTrue(out.ok, out.status)
        self.assertEqual(out.lv, res.lv + 1)
        return out

    # ---- the relabel ----------------------------------------------------------------------------------------------------
    def check_relabel(self, **kw):
        w, res = self.plant()
        other = w.add(1, 1, place=True)[0]
        before_other = self.n_docs(class_index(self.T, "base", "leaf"), {"ids": {"values": [f"{other.leaf_id}:1"]}})
        rc = self.executor(**kw).relabel_erase([res.leaf_id], "base")
        self.assertEqual(self.state.seq, 1, "the tombstone was not published")
        self.assertLessEqual(rc.boundary_s, B_ERASE_S)
        self.assertIn(f"{res.leaf_id}:1", self.state.redact, "the old version is not in the redact set")
        self.assertEqual(self.old_copy_docs(res.leaf_id), 0, "the old-class copy was left")
        self.assertFalse(self.ks.has_key(res.leaf_id, 1), "the old version's key was left")
        self.assertEqual(rc.recount, 0)
        self.assertEqual(self.n_docs(class_index(self.T, "base", "leaf"), {"ids": {"values": [f"{other.leaf_id}:1"]}}), before_other,
                         "a neighbour in the old class was erased")
        return w, res

    def test_an_upward_relabel_erases_the_old_class_copy_at_the_boundary_and_the_new_copy_is_written(self):
        w, res = self.check_relabel()
        out = self.new_copy(w, res)
        self.assertEqual(self.n_docs(class_index(self.T, "priv", "leaf"), {"ids": {"values": [f"{res.leaf_id}:{out.lv}"]}}), 1)
        self.assertTrue(self.ks.has_key(res.leaf_id, out.lv), "the new version has no key")
        self.assertEqual(self.old_copy_docs(res.leaf_id, max_lv=1), 0)
        scope = list(self.journal.records())[0]["scope"]
        self.assertEqual((scope["cls"], scope["leaf_ids"]), ("base", [res.leaf_id]))

    def test_broken_relabel_that_leaves_the_old_copy_is_red(self):
        with self.assertRaises(AssertionError) as cm:
            self.check_relabel(broken_relabel_keeps_old_copy=True)
        self.assertRegex(str(cm.exception), "old-class copy was left|key was left")

    # ---- the restore's replay -------------------------------------------------------------------------------------------
    def replay_after_resurrection(self, **kw):
        w, res = self.plant()
        old = {}
        for idx, did in ((tenant_index(self.T, "src"), f"{res.leaf_id}:1"), (class_index(self.T, "base", "leaf"), f"{res.leaf_id}:1")):
            st, d = self.admin.request("GET", f"/{idx}/_doc/{did}")
            self.assertEqual(st, 200)
            old[(idx, did)] = d["_source"]
        self.executor().relabel_erase([res.leaf_id], "base")
        out = self.new_copy(w, res)
        for (idx, did), src in old.items():   # an older snapshot is restored: the old-class copy comes back
            st, _ = self.admin.request("PUT", f"/{idx}/_doc/{did}?refresh=true", src)
            self.assertIn(st, (200, 201))
        self.assertGreater(self.old_copy_docs(res.leaf_id), 0, "the snapshot did not resurrect the old copy")
        ex = self.executor(**kw)

        def verify():
            n = self.old_copy_docs(res.leaf_id)
            return Report(steps=[("old_class", 1, n)], found={"old_class": n})

        result = restore_replay(ex, self.journal, verify)
        self.assertEqual(result.replayed, 1)
        self.assertEqual(self.n_docs(class_index(self.T, "priv", "leaf"), {"ids": {"values": [f"{res.leaf_id}:{out.lv}"]}}), 1,
                         "the replay deleted the new-class copy")
        self.assertTrue(self.ks.has_key(res.leaf_id, out.lv), "the replay destroyed the new version's key")
        self.assertEqual(result.residue, 0, result.reason)
        self.assertTrue(result.enabled)

    def test_the_restore_replay_removes_a_resurrected_old_copy_and_keeps_the_new_class_copy(self):
        self.replay_after_resurrection()

    def test_broken_replay_that_ignores_the_class_deletes_the_new_class_copy_red(self):
        with self.assertRaises(AssertionError) as cm:
            self.replay_after_resurrection(broken_relabel_all_classes=True)
        self.assertRegex(str(cm.exception), "deleted the new-class copy|destroyed the new version")


if __name__ == "__main__":
    unittest.main()
