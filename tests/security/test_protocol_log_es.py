"""BUILD-PLAN Step 11 on a real Elasticsearch: the protocol log (AC-6, SEC-A5, SEC-E9 part). Needs LC_TEST_ES_URL, LC_TEST_CA,
LC_TEST_PW. Broken controls, each red: `opened` taken from the agent's claim; the row stored in the item's class only (a base
reader can read a row that names a restricted item); subject detection on leaves only (the query's mentioned person is
missing from the row and the stored query)."""
import os
import tempfile
import unittest
from pathlib import Path

from living_context.erasure.executor import State
from living_context.feedback.log import GatewayLookup, Offer, ProtocolLog
from living_context.gateway.es import EsClient
from living_context.gateway.handler import EsGate
from living_context.gateway.provision import provision_tenant
from living_context.ids import KIND_LEAF, Pointer, encode_pointer
from living_context.ingest.subjects import Guard, TokenSource
from living_context.keystore import Keystore
from living_context.policy import ClassLattice
from living_context.recall.lookup import Resolver

URL, CA, PW = (os.environ.get(k) for k in ("LC_TEST_ES_URL", "LC_TEST_CA", "LC_TEST_PW"))
T = "plog"
LAT = ClassLattice(["base", "restricted"], [("restricted", "base")])
LEAF_B, LEAF_R = "a" * 32, "b" * 32


def reset(admin):
    st, rows = admin.request("GET", "/_cat/indices/lc-*?format=json&h=index")
    for row in rows if st == 200 and isinstance(rows, list) else []:
        if row["index"].startswith(f"lc-{T}-") or row["index"] == "lc-sys-registry":
            admin.request("DELETE", "/" + row["index"])


class Store:
    def leaf(self, leaf_id, lv):
        return {"cls": "base" if leaf_id == LEAF_B else "restricted", "text": "body"}

    def current_lv(self, leaf_id):
        return 1

    def gen_status(self, gen):
        return "live"

    def forwarding(self, gen, node_id):
        return None


class ProtocolLogOnElasticsearch(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not (URL and CA and PW):
            raise RuntimeError("LC_TEST_ES_URL, LC_TEST_CA and LC_TEST_PW must point at a scratch cluster")
        cls.tmp = tempfile.TemporaryDirectory()
        root = Path(cls.tmp.name)
        cls.admin = EsClient(URL, CA, PW, scheme="basic", user="elastic")
        reset(cls.admin)
        r = provision_tenant(cls.admin, T, ["base", "restricted"], key_dir=root / "keys", data_root=root / "data", journal_size=65536)
        cls.gate = EsGate(URL, CA, {T: r["keys"]})
        cls.ks = Keystore(root / "ks.db", T)
        cls.guard = Guard(TokenSource(cls.ks))

    @classmethod
    def tearDownClass(cls):
        cls.ks.close()
        reset(cls.admin)
        cls.tmp.cleanup()

    def setUp(self):
        self.n = getattr(self, "n", 0)
        ProtocolLogOnElasticsearch.counter = getattr(ProtocolLogOnElasticsearch, "counter", 0) + 1
        self.sid = f"s{self.counter:04d}"
        self.state = State()
        self.email = "kim.vale@example.com"

    def plog(self, **kw):
        return ProtocolLog(lambda: self.gate.client(T, "log"), T, LAT, self.guard,
                           erased_leaf=lambda lid: lid in self.state.erased_leaves, **kw)

    def offers(self):
        return [Offer(LEAF_B, 1, "base", ("tree", "direct"), 0, confidence=3, subjects=("subjb",)),
                Offer(LEAF_R, 1, "restricted", ("direct",), 1, exploration=True, propensity=0.1)]

    def row(self, cls, leaf, key="read-restricted"):
        self.admin.request("POST", f"/lc-{T}-*/_refresh")
        st, r = self.gate.client(T, key).request("POST", f"/lc-{T}-{cls}-log/_search",
                                                 {"query": {"term": {"search_id": self.sid}}, "size": 20})
        if st != 200:
            return None
        hits = [h["_source"] for h in r["hits"]["hits"] if h["_source"]["pointer"]["leaf_id"] == leaf]
        return hits[0] if hits else None

    # ---- rows, join class, propensity, subjects -----------------------------------------------------------------
    def check_rows(self, plog):
        n = plog.record_digest(self.sid, "actortok", "stub-1", ["base", "restricted"], f"what did {self.email} say", self.offers())
        self.assertEqual(n, 2)
        rb, rr = self.row("restricted", LEAF_B), self.row("restricted", LEAF_R)
        self.assertIsNotNone(rb, "the row of a base item read in a restricted channel must sit in the restricted log")
        self.assertIsNotNone(rr)
        self.assertEqual((rb["offered_by_tree"], rb["rank"], rb["offer_set_size"], rb["propensity"]), (True, 0, 2, 1.0))
        self.assertEqual((rr["exploration"], rr["propensity"], rr["opened"], rr["used_cited"]), (True, 0.1, False, False))
        self.assertIsNone(self.row("base", LEAF_B, key="read-base"), "a base reader can read a row of a restricted channel")

    def test_every_row_carries_its_join_class(self):
        self.check_rows(self.plog())

    def test_broken_item_class_only_row_is_red(self):
        with self.assertRaises(AssertionError) as cm:
            self.check_rows(self.plog(join_with_item_class=False))
        self.assertIn("restricted", str(cm.exception))

    def check_subjects(self, plog):
        plog.record_digest(self.sid, "actortok", "stub-1", ["base", "restricted"], f"what did {self.email} say", self.offers())
        tok = self.guard.tokens_in(self.email)[0]
        self.assertIn(tok, self.row("restricted", LEAF_B)["lin_subjects"], "the person the query mentions is missing from the row")
        st, sq = self.admin.request("GET", f"/lc-{T}-restricted-sq/_doc/{self.sid}")
        self.assertIn(tok, sq["_source"]["lin_subjects"], "the stored query does not carry the person's token")
        self.assertIn("subjb", self.row("restricted", LEAF_B)["lin_subjects"])

    def test_query_subjects_are_in_the_row_and_the_stored_query(self):
        self.check_subjects(self.plog())

    def test_broken_detection_on_leaves_only_is_red(self):
        with self.assertRaises(AssertionError) as cm:
            self.check_subjects(self.plog(detect_query_subjects=False))
        self.assertIn("mentions is missing", str(cm.exception))

    # ---- opened is the gateway's, not the agent's ---------------------------------------------------------------
    def lookup_for(self, plog):
        res = Resolver(Store(), ["base", "restricted"], lambda lid: lid in self.state.erased_leaves, lambda ev: None)
        return GatewayLookup(res, Store(), plog)

    def claim_without_lookup(self, plog):
        plog.record_digest(self.sid, "actortok", "stub-1", ["base", "restricted"], "garden", self.offers())
        plog.claim_used(self.sid, ["base", "restricted"], "base", LEAF_B, 1)
        row = self.row("restricted", LEAF_B)
        self.assertTrue(row["used_cited"])
        self.assertFalse(row["opened"], "a claim of use without a lookup set `opened`")

    def test_a_claim_of_use_without_a_lookup_leaves_opened_false(self):
        self.claim_without_lookup(self.plog())

    def test_broken_opened_from_the_claim_is_red(self):
        class Broken(ProtocolLog):
            def claim_used(self, search_id, reader_classes, item_cls, leaf_id, lv):
                return self._flip(search_id, reader_classes, item_cls, leaf_id, lv, {"used_cited": True, "opened": True})
        with self.assertRaises(AssertionError) as cm:
            self.claim_without_lookup(Broken(lambda: self.gate.client(T, "log"), T, LAT, self.guard))
        self.assertIn("without a lookup", str(cm.exception))

    def test_the_gateway_lookup_writes_opened_and_nothing_else_does(self):
        plog = self.plog()
        plog.record_digest(self.sid, "actortok", "stub-1", ["base", "restricted"], "garden", self.offers())
        lk = self.lookup_for(plog)
        out = lk.lookup(self.sid, ["base", "restricted"], encode_pointer(Pointer(KIND_LEAF, 1, LEAF_B, 1)), actor="actortok")
        self.assertEqual(out["status"], "ok")
        row, other = self.row("restricted", LEAF_B), self.row("restricted", LEAF_R)
        self.assertTrue(row["opened"])
        self.assertEqual(row["opened_by"], "actortok")
        self.assertFalse(other["opened"])

    # ---- the digest never waits for the row ---------------------------------------------------------------------
    def test_a_failed_row_write_is_counted_and_does_not_raise(self):
        class Dead:
            def request(self, *a, **k):
                return 503, {}
        plog = ProtocolLog(lambda: Dead(), T, LAT, self.guard)
        self.assertEqual(plog.record_digest(self.sid, "actortok", "stub-1", ["base"], "q", [self.offers()[0]]), 0)
        self.assertGreaterEqual(plog.write_failures, 1)

    def test_a_row_naming_an_erased_leaf_is_refused(self):
        self.state.erased_leaves.add(LEAF_R)
        plog = self.plog()
        self.assertEqual(plog.record_digest(self.sid, "actortok", "stub-1", ["base", "restricted"], "garden", self.offers()), 1)
        self.assertIsNone(self.row("restricted", LEAF_R))
        self.assertEqual(plog.write_failures, 1)

    # ---- stored-query percolator and ask ------------------------------------------------------------------------
    def test_the_stored_query_is_a_lexical_percolator_that_finds_a_matching_leaf_text(self):
        self.plog().record_digest(self.sid, "actortok", "stub-1", ["base"], "orchard gate code heron", [self.offers()[0]])
        self.admin.request("POST", f"/lc-{T}-*/_refresh")
        def hits(text):
            st, r = self.admin.request("POST", f"/lc-{T}-base-sq/_search", {"query": {"percolate": {"field": "q", "document": {"text": text}}}})
            self.assertEqual(st, 200, r)
            return [h["_id"] for h in r["hits"]["hits"]]
        self.assertIn(self.sid, hits("the orchard gate code is blue heron"))
        self.assertNotIn(self.sid, hits("compost drainage gravel"))

    def check_ask(self, **kw):
        from living_context.model.budget import Budget
        from living_context.model.cache import DictStore
        from living_context.model.port import ModelPort
        from living_context.model.stub import StubBackend
        plog = self.plog()
        plog.record_digest(self.sid, "actortok", "stub-1", ["base"], "garden", [self.offers()[0]])
        res = Resolver(Store(), ["base"], lambda lid: False, lambda ev: None)
        lk = GatewayLookup(res, Store(), plog)
        ptrs = [encode_pointer(Pointer(KIND_LEAF, 1, LEAF_B, 1)), encode_pointer(Pointer(KIND_LEAF, 1, LEAF_R, 1))]
        with tempfile.TemporaryDirectory() as d:
            port = ModelPort(StubBackend(), Budget(Path(d) / "b", 100), LAT, DictStore())
            out = lk.ask(self.sid, ["base"], ptrs, "body", actor="actortok", port=port, tenant=T, cls="base", **kw)
        self.assertEqual((out["status"], out["relied"]), ("ok", [ptrs[0]]), "ask served a leaf outside the read set")
        row = self.row("base", LEAF_B, key="read-base")
        self.assertTrue(row["asked"] and row["opened"], "ask did not log the served leaf as opened and asked")

    def test_ask_logs_served_leaves_as_opened_and_asked_and_skips_unreadable_ones(self):
        self.check_ask()

    def test_broken_ask_that_does_not_log_is_red(self):
        with self.assertRaises(AssertionError) as cm:
            self.check_ask(broken_not_logged=True)
        self.assertIn("did not log", str(cm.exception))


if __name__ == "__main__":
    unittest.main()
