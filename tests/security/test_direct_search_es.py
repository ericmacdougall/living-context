"""BUILD-PLAN Step 7 on a real Elasticsearch: direct leaf search (AC-7). Fails, never skips, without a cluster.

The leaves are written by the real writer; the embedding is the stub, with two leaves given crafted
vectors so that one needle is reachable only lexically and the other only by vector.
"""
import os
import tempfile
import unittest
from pathlib import Path

from living_context.embed import stub
from living_context.gateway.es import EsClient
from living_context.gateway.handler import EsGate
from living_context.gateway.provision import provision_tenant
from living_context.ingest.subjects import Guard, TokenSource
from living_context.ingest.writer import Writer
from living_context.keystore import Keystore
from living_context.policy import ClassLattice, Principal, Role
from living_context.recall.direct import BM25_ONLY, DirectSearch, HYBRID, KNN_ONLY

URL, CA, PW = (os.environ.get(k) for k in ("LC_TEST_ES_URL", "LC_TEST_CA", "LC_TEST_PW"))
HAVE = bool(URL and CA and PW)
T = "srch"
LAT = ClassLattice(["base", "restricted"], [("restricted", "base")])

QUERY_SEM = "marsupial habitat"          # the semantic needle shares no word with this query
SEM_TEXT = "completely different words about nothing"
QUERY_LEX = "xylophonic"                 # the lexical needle's vector is far from this query's
LEX_TEXT = "the xylophonic quartet rehearsed"
CUSTOM = {SEM_TEXT: stub.embed(QUERY_SEM), LEX_TEXT: stub.embed("zzz qqq vvv")}


def emb(text):
    return CUSTOM.get(text) or stub.embed(text)


def reset(admin):
    st, rows = admin.request("GET", "/_cat/indices/lc-*?format=json&h=index")
    for row in rows if st == 200 and isinstance(rows, list) else []:
        if row["index"].startswith(f"lc-{T}-"):
            admin.request("DELETE", "/" + row["index"])


class Recording:
    """Wraps a client and records every path it is asked to touch (the request plan)."""

    def __init__(self, inner, log):
        self._inner, self._log = inner, log

    def request(self, method, path, body=None, **kw):
        self._log.append((method, path, body))
        return self._inner.request(method, path, body, **kw)


class DirectSearchOnElasticsearch(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not HAVE:
            raise RuntimeError("LC_TEST_ES_URL, LC_TEST_CA and LC_TEST_PW must point at a scratch cluster")
        cls.tmp = tempfile.TemporaryDirectory()
        root = Path(cls.tmp.name)
        cls.admin = EsClient(URL, CA, PW, scheme="basic", user="elastic")
        reset(cls.admin)
        r = provision_tenant(cls.admin, T, ["base", "restricted"], key_dir=root / "keys", data_root=root / "data",
                             journal_size=65536)
        cls.gate = EsGate(URL, CA, {T: r["keys"]})
        ks = Keystore(root / "ks" / f"{T}.db", T)
        cls.ks = ks
        w = Writer(lambda: cls.gate.client(T, "ingest"), ks, T, Guard(TokenSource(ks)), embed=emb)
        words = "garden river stone market winter engine paper window forest anchor candle meadow".split()
        for i in range(60):
            text = f"{words[i % 12]} {words[(i * 5 + 1) % 12]} {words[(i * 7 + 2) % 12]} note {i}"
            assert w.remember("tok", "base", text).ok
        cls.lex = w.remember("tok", "base", LEX_TEXT)
        cls.sem = w.remember("tok", "base", SEM_TEXT)
        cls.secret = w.remember("tok", "restricted", "restricted xylophonic ledger marsupial habitat")
        assert cls.lex.ok and cls.sem.ok and cls.secret.ok
        cls.base_only = Principal(T, "p-base", frozenset([Role.READER]), frozenset(["base"]), frozenset())
        cls.both = Principal(T, "p-both", frozenset([Role.READER]), frozenset(["base", "restricted"]), frozenset())

    @classmethod
    def tearDownClass(cls):
        if HAVE:
            reset(cls.admin)
            cls.ks.close()
            cls.tmp.cleanup()

    def searcher(self, **kw):
        log = []
        ds = DirectSearch(lambda c: Recording(self.gate.client(T, f"read-{c}"), log), T, embed=emb, **kw)
        return ds, log

    def ids(self, hits):
        return [h.doc_id for h in hits]

    # ---- both needles in the fused top 10 -------------------------------------------------------
    def check_both_needles(self, mode):
        ds, _ = self.searcher(mode=mode)
        lex = self.ids(ds.search(self.base_only, LAT, QUERY_LEX, size=10))
        sem = self.ids(ds.search(self.base_only, LAT, QUERY_SEM, size=10))
        self.assertIn(f"{self.lex.leaf_id}:1", lex, "the lexical-only needle is missing from the fused top 10")
        self.assertIn(f"{self.sem.leaf_id}:1", sem, "the semantic-only needle is missing from the fused top 10")

    def test_lexical_only_and_semantic_only_needles_are_both_in_the_fused_top_10(self):
        self.check_both_needles(HYBRID)

    def test_broken_bm25_only_search_misses_the_semantic_needle(self):
        with self.assertRaises(AssertionError) as cm:
            self.check_both_needles(BM25_ONLY)
        self.assertIn("semantic-only needle", str(cm.exception))

    def test_broken_knn_only_search_misses_the_lexical_needle(self):
        with self.assertRaises(AssertionError) as cm:
            self.check_both_needles(KNN_ONLY)
        self.assertIn("lexical-only needle", str(cm.exception))

    # ---- no cross-class index touched ----------------------------------------------------------------
    def check_plan_never_names_the_restricted_index(self, make):
        ds, log = make()
        hits = ds.search(self.base_only, LAT, "xylophonic marsupial habitat", size=10)
        touched = [path for _, path, body in log] + [str(body) for _, _, body in log]
        self.assertFalse(any("restricted" in t for t in touched), "the request plan names a restricted index")
        self.assertTrue(hits)
        return ds, hits

    def test_base_reader_never_touches_the_restricted_index(self):
        ds, hits = self.check_plan_never_names_the_restricted_index(self.searcher)
        self.assertEqual(ds.plan, [f"/_msearch:lc-{T}-base-leaf"])
        self.assertTrue(all(h.cls == "base" for h in hits))
        ds2, log2 = self.searcher()
        both = ds2.search(self.both, LAT, "xylophonic marsupial habitat", size=10)
        self.assertIn("restricted", {h.cls for h in both})  # the wider reader does reach it (control)

    def test_broken_query_everything_and_filter_after_is_caught(self):
        class FilterAfter(DirectSearch):
            def classes_for(self, p, lattice, wanted=None):
                return sorted(lattice.classes)  # query every class, drop hits the reader may not see afterwards

        def make():
            log = []
            ds = FilterAfter(lambda c: Recording(self.gate.client(T, "read-base"), log), T, embed=emb)
            return ds, log
        with self.assertRaises(AssertionError) as cm:
            self.check_plan_never_names_the_restricted_index(make)
        self.assertIn("names a restricted index", str(cm.exception))


if __name__ == "__main__":
    unittest.main()
