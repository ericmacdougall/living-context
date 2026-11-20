"""SEC-A1, A3 and A4 on a real Elasticsearch (BUILD-PLAN Step 10; EXPERIMENT 9.2): non-interference between the corpus C0 (no
restricted leaves) and C1 (C0 plus restricted leaves close to base leaves, here with their own tree). A `base`-only reader
runs the same queries on both through the real entry (direct search plus the pinned tree view plus the fan-out) with the
deterministic stub. Leaves are written by the real writer (ids are made deterministic so the digests can be byte-identical).

  A1  digests identical (broken: a base tree built over base + restricted leaves and filtered at read)
  A3  request plan identical and never naming a class outside the read set (broken: search every class, filter afterwards)
  A4  `lookup` of a restricted pointer and of a random nonexistent pointer are one identical answer (broken: no class check)

The number of queries and of restricted leaves are parsed from docs/EXPERIMENT.md section 9.2, never held here; every probe
asserts that it executed exactly that many checks. Needs LC_TEST_ES_URL, LC_TEST_CA, LC_TEST_PW (fails, never skips)."""
import json
import os
import random
import re
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from living_context.embed import stub
from living_context.gateway.es import EsClient
from living_context.gateway.handler import EsGate
from living_context.gateway.provision import provision_tenant
from living_context.ids import KIND_LEAF, Pointer, encode_pointer
from living_context.ingest import title
from living_context.ingest.subjects import Guard, TokenSource
from living_context.ingest.writer import Writer
from living_context.keystore import Keystore
from living_context.model.budget import Budget
from living_context.model.cache import DictStore
from living_context.model.port import ModelPort
from living_context.model.stub import StubBackend
from living_context.policy import ClassLattice, Principal, Role
from living_context.recall.direct import DirectSearch
from living_context.recall.entry import recall_digest
from living_context.recall.fanout import Recall
from living_context.recall.lookup import EsLeafStore, Resolver
from living_context.recall.view import EsView
from living_context.structure import build
from living_context.structure.publish import Publisher
from tools import a7_timing
from tests.unit.test_tree_build import fixture

URL, CA, PW = (os.environ.get(k) for k in ("LC_TEST_ES_URL", "LC_TEST_CA", "LC_TEST_PW"))
LAT = ClassLattice(["base", "restricted"], [("restricted", "base")])
_SPEC = " ".join((Path(__file__).resolve().parents[2] / "docs" / "EXPERIMENT.md").read_text().split())
N_QUERIES = int(re.search(r"runs the same (\d+) queries", _SPEC).group(1))
N_RESTRICTED = int(re.search(r"C0 plus (\d+) restricted leaves", _SPEC).group(1))
NB = 120
N_TIMING = int(re.search(r"([\d,]+) interleaved requests per corpus", _SPEC).group(1).replace(",", ""))
TIMING_BUDGET_S = 150.0   # the largest N that fits the time budget is run and stated; the registered N is N_TIMING


def counter_ids(start):
    state = {"n": start}

    def nxt(*a, **k):
        state["n"] += 1
        return f"{state['n']:032x}"
    return nxt


class Recording:
    def __init__(self, inner, log):
        self._inner, self._log = inner, log

    def request(self, method, path, body=None, **kw):
        norm = lambda x: re.sub(r"secc(0|1m?)", "tenant", x)   # the two corpora are two tenants; everything else must match
        self._log.append((method, norm(path), norm(json.dumps(body, sort_keys=True))))
        return self._inner.request(method, path, body, **kw)


class MixedViewFilteredAtRead:
    """BROKEN A1 control: the base tree was built over base AND restricted leaves; the restricted leaf entries are dropped when
    the view is read, but the summaries, gists, sizes and totals above them still include the restricted content."""

    def __init__(self, inner, hidden):
        self._i, self._hidden, self.gen = inner, hidden, inner.gen

    def top(self):
        return self._i.top()

    def entries(self, ref):
        return [e for e in self._i.entries(ref) if e["child_id"] not in self._hidden]


class SecA(unittest.TestCase):
    corpora = {}

    @classmethod
    def setUpClass(cls):
        if not (URL and CA and PW):
            raise RuntimeError("LC_TEST_ES_URL, LC_TEST_CA and LC_TEST_PW must point at a scratch cluster")
        cls.tmp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.tmp.name)
        cls.admin = EsClient(URL, CA, PW, scheme="basic", user="elastic")
        cls.base_texts = [l.text for l in fixture(n=NB, topics=10, seed=7)]
        cls.restr_texts = [l.text for l in fixture(n=N_RESTRICTED, topics=10, seed=99)]
        cls.build_corpus("secc0", restricted=False)
        cls.build_corpus("secc1", restricted=True)
        cls.build_corpus("secc1m", restricted=True, mixed=True)
        rng = random.Random(5)
        words = [w for t in cls.base_texts for w in t.rstrip(".").split()]
        cls.queries = [" ".join(rng.sample(rng.choice(cls.base_texts).rstrip(".").split(), 3) + [rng.choice(words)])
                       for _ in range(N_QUERIES)]
        cls.reader = Principal("secc0", "p-base", frozenset([Role.READER]), frozenset(["base"]), frozenset())

    @classmethod
    def tearDownClass(cls):
        st, rows = cls.admin.request("GET", "/_cat/indices/lc-*?format=json&h=index")
        for row in rows if st == 200 and isinstance(rows, list) else []:
            if row["index"].startswith("lc-secc") or row["index"] == "lc-sys-registry":
                cls.admin.request("DELETE", "/" + row["index"])
        for c in cls.corpora.values():
            c["ks"].close()
        cls.tmp.cleanup()

    @classmethod
    def build_corpus(cls, t, *, restricted, mixed=False):
        st, rows = cls.admin.request("GET", "/_cat/indices/lc-*?format=json&h=index")
        for row in rows if st == 200 and isinstance(rows, list) else []:
            if row["index"].startswith(f"lc-{t}-"):
                cls.admin.request("DELETE", "/" + row["index"])
        r = provision_tenant(cls.admin, t, ["base", "restricted"], key_dir=cls.root / t / "keys", data_root=cls.root / t / "data",
                             journal_size=65536)
        gate = EsGate(URL, CA, {t: r["keys"]})
        ks = Keystore(cls.root / f"{t}.db", t)
        with mock.patch("living_context.ingest.writer.new_leaf_id", counter_ids(0)):
            w = Writer(lambda: gate.client(t, "ingest"), ks, t, Guard(TokenSource(ks)))
            recs = []
            for i, text in enumerate(cls.base_texts):
                res = w.remember("tok", "base", text, key=f"b{i}")
                assert res.ok
                recs.append(build.LeafRec(res.leaf_id, 1, text, title.build_title(text), stub.embed(text), 20 + i % 7))
            rrecs = []
            for i, text in enumerate(cls.restr_texts if restricted else []):
                res = w.remember("tok", "restricted", text, key=f"r{i}")
                assert res.ok
                rrecs.append(build.LeafRec(res.leaf_id, 1, text, title.build_title(text), stub.embed(text), 20 + i % 7))
        with tempfile.TemporaryDirectory() as d, mock.patch("living_context.structure.build.new_node_id", counter_ids(10**6)):
            port = ModelPort(StubBackend(), Budget(Path(d) / "b", 10**7), LAT, DictStore())
            base_tree = build.build_tree(recs + (rrecs if mixed else []), port, t, "base")
            if restricted and not mixed:
                rtree = build.build_tree(rrecs, port, t, "restricted")
        Publisher(lambda: gate.client(t, "structure"), t, "base").publish(base_tree, watermark=NB)
        if restricted and not mixed:
            Publisher(lambda: gate.client(t, "structure"), t, "restricted").publish(rtree, watermark=NB)
        cls.admin.request("POST", f"/lc-{t}-*/_refresh")
        cls.corpora[t] = {"gate": gate, "ks": ks, "restricted_ids": [x.leaf_id for x in rrecs]}

    # ---- the real entry for the base-only reader --------------------------------------------------------------------
    def run_queries(self, t, *, mixed=False, post_filter=False):
        c = self.corpora[t]
        plans, digests = [], []
        with tempfile.TemporaryDirectory() as d:
            port = ModelPort(StubBackend(), Budget(Path(d) / "b", 10**8), LAT, DictStore())
            for q in self.queries:
                log = []
                client = lambda cls_: Recording(c["gate"].client(t, f"read-{cls_}"), log)
                if post_filter:  # BROKEN A3: every class is searched with a key that may read it, and filtered afterwards
                    wide = Principal(t, "p-wide", frozenset([Role.READER]), frozenset(["base", "restricted"]), frozenset())
                    direct = DirectSearch(client, t)
                    hits = [h for h in direct.search(wide, LAT, q, size=20) if h.cls == "base"][:10]
                    digests.append(hits)
                    plans.append(log)
                    continue
                direct = DirectSearch(client, t)
                view = lambda cls_, c_=client: EsView(c_(cls_), t, cls_, "topic")
                if mixed:
                    hidden = set(self.corpora[t]["restricted_ids"])
                    view_for = lambda cls_: MixedViewFilteredAtRead(view(cls_), hidden)
                else:
                    view_for = view
                p = Principal(t, "p-base", frozenset([Role.READER]), frozenset(["base"]), frozenset())
                dig = recall_digest(p, LAT, q, direct=direct, view_for=view_for,
                                    recall_for=lambda cls_: Recall(port, t, cls_, "p-base"))
                digests.append(json.dumps(dig, sort_keys=True))
                plans.append(log)
        return digests, plans

    @classmethod
    def names(cls, plan):
        return [f"{m} {p} {b}" for m, p, b in plan]   # the index of an _msearch sub-search is in its body

    # ---- A1 -------------------------------------------------------------------------------------------------------
    def check_a1(self, c1):
        d0, _ = self.run_queries("secc0")
        d1, _ = self.run_queries(c1, mixed=(c1 == "secc1m"))
        executed = 0
        for q, a, b in zip(self.queries, d0, d1):
            executed += 1
            self.assertEqual(a, b, f"A1: the digest differs between C0 and C1 for {q!r}")
        self.assertEqual(executed, N_QUERIES)

    def test_a1_digests_are_byte_identical_with_and_without_restricted_leaves(self):
        self.check_a1("secc1")

    def test_a1_broken_a_mixed_summary_filtered_at_read_is_red(self):
        with self.assertRaises(AssertionError) as cm:
            self.check_a1("secc1m")
        self.assertIn("A1: the digest differs", str(cm.exception))

    # ---- A3 -------------------------------------------------------------------------------------------------------
    def check_a3(self, **kw):
        _, p0 = self.run_queries("secc0")
        _, p1 = self.run_queries("secc1", **kw)
        executed = 0
        for q, a, b in zip(self.queries, p0, p1):
            executed += 1
            if not kw:
                self.assertEqual(a, b, f"A3: the request plan differs between C0 and C1 for {q!r}")
            for line in self.names(b):
                self.assertNotIn("-restricted-", line, f"A3: the plan names a class outside the read set ({line})")
        self.assertEqual(executed, N_QUERIES)

    def test_a3_the_request_plan_is_identical_and_touches_only_the_read_set(self):
        self.check_a3()

    def test_a3_broken_post_filter_instead_of_pre_filter_is_red(self):
        with self.assertRaises(AssertionError) as cm:
            self.check_a3(post_filter=True)
        self.assertIn("A3: the plan names a class outside the read set", str(cm.exception))

    # ---- A4 -------------------------------------------------------------------------------------------------------
    def check_a4(self, **kw):
        c = self.corpora["secc1"]
        store = EsLeafStore(c["gate"].client("secc1", "ingest"), "secc1")
        res = Resolver(store, ["base"], lambda leaf_id: False, lambda d: None, **kw)
        ptr = lambda lid: encode_pointer(Pointer(KIND_LEAF, 1, lid, 1))
        rng = random.Random(11)
        executed = 0
        for lid in c["restricted_ids"]:
            executed += 1
            forbidden = json.dumps(res.resolve(ptr(lid)), sort_keys=True)
            missing = json.dumps(res.resolve(ptr(f"{rng.getrandbits(128):032x}")), sort_keys=True)
            self.assertEqual(forbidden, missing, "A4: a forbidden pointer is distinguishable from a nonexistent one")
        self.assertEqual(executed, N_RESTRICTED)
        base_id = f"{1:032x}"
        self.assertEqual(res.resolve(ptr(base_id))["status"], "ok")  # control: a readable leaf resolves

    def test_a4_forbidden_and_nonexistent_pointers_get_one_identical_answer(self):
        self.check_a4()

    def test_a4_broken_no_class_check_is_red(self):
        with self.assertRaises(AssertionError) as cm:
            self.check_a4(check_class=False)
        self.assertIn("A4: a forbidden pointer is distinguishable", str(cm.exception))

    # ---- A7: wall-clock timing, REPORTED and never gating (EXPERIMENT 9.2; a failure is a disclosed timing channel, DESIGN 20) ----
    def test_a7_timing_of_c0_against_c1_and_of_the_a4_pair_is_reported(self):
        with tempfile.TemporaryDirectory() as d:
            port = ModelPort(StubBackend(), Budget(Path(d) / "b", 10**9), LAT, DictStore())

            def one(t):
                c = self.corpora[t]

                def run(i):
                    client = lambda cls_: c["gate"].client(t, f"read-{cls_}")
                    p = Principal(t, "p-base", frozenset([Role.READER]), frozenset(["base"]), frozenset())
                    recall_digest(p, LAT, self.queries[i % len(self.queries)], direct=DirectSearch(client, t),
                                  view_for=lambda cls_: EsView(client(cls_), t, cls_, "topic"),
                                  recall_for=lambda cls_: Recall(port, t, cls_, "p-base"))
                return run

            t0, ta, tb, n = time.time(), [], [], 0
            fa, fb = one("secc0"), one("secc1")
            while n < N_TIMING and time.time() - t0 < TIMING_BUDGET_S:
                a, b = a7_timing.measure(fa, fb, 1)
                ta += a
                tb += b
                n += 1
        res = a7_timing.compare(ta, tb)
        c = self.corpora["secc1"]
        store = EsLeafStore(c["gate"].client("secc1", "ingest"), "secc1")
        resolver = Resolver(store, ["base"], lambda leaf_id: False, lambda d: None)
        ptr = lambda lid: encode_pointer(Pointer(KIND_LEAF, 1, lid, 1))
        rng = random.Random(13)
        forb = lambda i: resolver.resolve(ptr(c["restricted_ids"][i % len(c["restricted_ids"])]))
        miss = lambda i: resolver.resolve(ptr(f"{rng.getrandbits(128):032x}"))
        pa, pb = a7_timing.measure(forb, miss, min(N_TIMING, 1000))
        pair = a7_timing.compare(pa, pb)
        print(f"A7 (reported, not gating): C0 vs C1 {res['n_a']} interleaved requests per corpus of the registered {N_TIMING}: {res}; "
              f"A4 pair {pair['n_a']} requests: {pair}", flush=True)
        self.assertGreaterEqual(res["n_a"], 100, "the timing run was too short to report anything")
        self.assertEqual(res["n_a"], res["n_b"])


if __name__ == "__main__":
    unittest.main()
