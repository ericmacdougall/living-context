"""BUILD-PLAN Step 11 remainder: the stub gate (EXPERIMENT 10 S0: A1-tree-only recall@10 at least 0.80, SHUFFLED-SUMMARIES at
least 0.30 lower) recomputed from the PROTOCOL LOG rows on a real Elasticsearch, not from the in-memory digest. Every offered
leaf of the tree list is one log row (`offered_by_tree`, rank); recall@10 is read back from those rows through the tenant's
read key. The two figures must equal the in-memory figures of tests.unit.test_fanout (the fixture numbers). Broken control: a log
that does not mark the tree as a source reads a recall of 0 and fails the gate."""
import os
import tempfile
import unittest
from pathlib import Path

from living_context.feedback.log import Offer, ProtocolLog
from living_context.gateway.es import EsClient
from living_context.gateway.handler import EsGate
from living_context.gateway.provision import provision_tenant
from living_context.ids import decode_pointer
from living_context.ingest.subjects import Guard, TokenSource
from living_context.keystore import Keystore
from living_context.policy import ClassLattice
from living_context.recall.fanout import Recall
from living_context.recall.view import MemView
from tests.security.test_protocol_log_es import reset
from tests.unit.test_fanout import LAT, mk, questions, recall_at_10, shuffle_summaries, tree

URL, CA, PW = (os.environ.get(k) for k in ("LC_TEST_ES_URL", "LC_TEST_CA", "LC_TEST_PW"))
T = "plog"
LATT = ClassLattice(["base"], [])


class StubGateFromTheLog(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not (URL and CA and PW):
            raise RuntimeError("LC_TEST_ES_URL, LC_TEST_CA and LC_TEST_PW must point at a scratch cluster")
        cls.tmp = tempfile.TemporaryDirectory()
        root = Path(cls.tmp.name)
        cls.admin = EsClient(URL, CA, PW, scheme="basic", user="elastic")
        reset(cls.admin)
        r = provision_tenant(cls.admin, T, ["base"], key_dir=root / "keys", data_root=root / "data", journal_size=65536)
        cls.gate = EsGate(URL, CA, {T: r["keys"]})
        cls.ks = Keystore(root / "ks.db", T)
        cls.guard = Guard(TokenSource(cls.ks))
        cls.qs = questions()

    @classmethod
    def tearDownClass(cls):
        cls.ks.close()
        reset(cls.admin)
        cls.tmp.cleanup()

    def log_arm(self, arm, view, drop_source=False):
        plog = ProtocolLog(lambda: self.gate.client(T, "log"), T, LATT, self.guard)
        ids = {}
        for i, (q, gold) in enumerate(self.qs):
            with tempfile.TemporaryDirectory() as d:
                port, _ = mk(d)
                res = Recall(port, T, "base", "px").recall(q, view)
            offers = [Offer(decode_pointer(p["pointer"]).id, 1, "base", () if drop_source else ("tree",), rank)
                      for rank, p in enumerate(res.digest["sources"]["tree"])]
            sid = f"{self._testMethodName[5:9]}{arm}n{i:03d}"  # unique per test: a row is created once
            plog.record_digest(sid, "actortok", "stub-1", ["base"], q, offers, store_query=False)
            ids[sid] = gold
        self.assertEqual(plog.write_failures, 0)
        return ids

    def recall_from_rows(self, arm, gold_by_sid):
        self.admin.request("POST", f"/lc-{T}-*/_refresh")
        hit = 0
        for sid, gold in gold_by_sid.items():
            st, r = self.gate.client(T, "read-base").request("POST", f"/lc-{T}-base-log/_search", {
                "size": 10, "query": {"bool": {"filter": [{"term": {"search_id": sid}}, {"term": {"offered_by_tree": True}},
                                                          {"range": {"rank": {"lt": 10}}}]}}})
            self.assertEqual(st, 200)
            hit += gold in [h["_source"]["pointer"]["leaf_id"] for h in r["hits"]["hits"]]
        return hit / len(gold_by_sid)

    def figures(self, **kw):
        a1 = self.recall_from_rows("a1", self.log_arm("a1", MemView(tree()), **kw))
        shuf = self.recall_from_rows("shuf", self.log_arm("shuf", MemView(tree(), mangle=shuffle_summaries), **kw))
        return a1, shuf

    def test_the_gate_holds_on_the_log_rows_and_equals_the_in_memory_figures(self):
        a1, shuf = self.figures()
        mem_a1, mem_shuf = recall_at_10(MemView(tree()), self.qs), recall_at_10(MemView(tree(), mangle=shuffle_summaries), self.qs)
        print(f"stub gate from the protocol log: A1 tree-only recall@10 {a1:.3f}, SHUFFLED-SUMMARIES {shuf:.3f}; "
              f"in memory {mem_a1:.3f} and {mem_shuf:.3f}")
        self.assertEqual((a1, shuf), (mem_a1, mem_shuf), "the log rows do not carry the offered tree list")
        self.assertGreaterEqual(a1, 0.80)
        self.assertGreaterEqual(a1 - shuf, 0.30)

    def test_broken_log_without_the_tree_source_reads_zero_and_fails_the_gate(self):
        a1, _ = self.figures(drop_source=True)
        self.assertEqual(a1, 0.0)
        with self.assertRaises(AssertionError):
            self.assertGreaterEqual(a1, 0.80)
