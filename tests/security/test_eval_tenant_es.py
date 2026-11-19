"""The H1 data path on a real Elasticsearch (BUILD-PLAN Step 20: whitelist and hygiene; Steps 6, 7, 9, 10 wired together).

Needs LC_TEST_ES_URL, LC_TEST_CA, LC_TEST_PW (the module FAILS without them). Dataset-shaped records carrying a marker in every
non-whitelisted field and a `has_answer` flag are parsed, ingested through the real writer, built, published and recalled; every
Elasticsearch document and every captured prompt is scanned for the markers. Broken control: a write that puts a marker in
`source_ref` must be found by the same scan."""
import json
import os
import re
import tempfile
import unittest
from pathlib import Path

from evalkit import arms, dataset as ds, fixture, stats
from evalkit.eval_tenant import CLS, EvalTenant
from evalkit.score import question_scores
from living_context.model.budget import Budget
from living_context.model.cache import DictStore
from living_context.model.port import ModelPort
from living_context.model.stub import StubBackend
from living_context.policy import ClassLattice

URL, CA, PW = (os.environ.get(k) for k in ("LC_TEST_ES_URL", "LC_TEST_CA", "LC_TEST_PW"))
T = "evh1"
MARK = "ZZMARKER"
GOLD_ID = re.compile(r"\banswer" r"_[0-9a-f]{6,}")


class Spy(StubBackend):
    def __init__(self):
        super().__init__()
        self.prompts = []

    def run(self, task, inputs):
        self.prompts.append((task, json.dumps(inputs, sort_keys=True)))
        return super().run(task, inputs)


def raw_records(fx):
    """The fixture as dataset records: every original field present, a marker in each non-whitelisted one, has_answer on gold."""
    out = []
    for q in fx.questions:
        gold = set(q.gold_session_ids)
        out.append({"question_id": q.qid, "question_type": MARK + "type", "question": q.question, "answer": MARK + "answer",
                    "question_date": MARK + "date", "answer_session_ids": list(gold),
                    "haystack_dates": [MARK + "d"] * len(q.sessions), "haystack_session_ids": [s for s, _ in q.sessions],
                    "haystack_sessions": [[{"role": r, "content": c, "has_answer": sid in gold} for r, c in t] for sid, t in q.sessions]})
    return out


def scan(admin, needle) -> int:
    """Count documents of the tenant whose JSON contains the needle (a string or a compiled regex)."""
    hits = 0
    st, rows = admin.request("GET", f"/_cat/indices/lc-{T}-*?format=json&h=index")
    for row in rows:
        admin.request("POST", f"/{row['index']}/_refresh")
        st, r = admin.request("POST", f"/{row['index']}/_search", {"size": 10000, "query": {"match_all": {}}})
        for h in r["hits"]["hits"]:
            blob = json.dumps(h["_source"])
            hits += bool(needle.search(blob)) if hasattr(needle, "search") else needle in blob
    return hits


class EvalTenantOnElasticsearch(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not (URL and CA and PW):
            raise RuntimeError("LC_TEST_ES_URL, LC_TEST_CA and LC_TEST_PW must point at a scratch cluster")
        cls.tmp = tempfile.TemporaryDirectory()
        cls.fx = fixture.make("advantage", 8)
        qs = [ds.parse_record(r) for r in raw_records(cls.fx)]
        cls.t = EvalTenant(URL, CA, PW, T, Path(cls.tmp.name))
        seen = {}
        for q in qs:
            for sid, turns in q.sessions:
                seen.setdefault(ds.turns_hash(turns), turns)
        cls.written = cls.t.ingest(list(seen.items()))
        cls.spy = Spy()
        cls.port = ModelPort(cls.spy, Budget(Path(cls.tmp.name) / "b", 10 ** 7), ClassLattice([CLS], []), DictStore())
        cls.tree = cls.t.build_and_publish(cls.port)

    @classmethod
    def tearDownClass(cls):
        cls.t.close()
        from evalkit.eval_tenant import reset_tenant
        reset_tenant(cls.t.admin, T)
        cls.tmp.cleanup()

    def test_every_leaf_is_in_elasticsearch_through_the_real_writer(self):
        self.assertGreater(self.written, 100)
        self.assertEqual(self.t.leaf_count_in_es(), self.written)
        self.assertEqual(len(self.t.leaves), self.written)

    def test_no_marker_and_no_gold_flag_or_id_in_any_document_and_the_scan_can_fail(self):
        self.assertEqual(scan(self.t.admin, MARK), 0)
        self.assertEqual(scan(self.t.admin, "has_answer"), 0)
        self.assertEqual(scan(self.t.admin, GOLD_ID), 0)
        # control: an adapter that copies a non-whitelisted field into source_ref is caught by the same scan
        res = self.t.writer.remember("eval", CLS, "a control leaf", source_ref={"session": "sx", "leak": MARK}, refresh=True)
        self.assertTrue(res.ok)
        self.assertGreater(scan(self.t.admin, MARK), 0)

    def test_source_refs_hold_only_the_opaque_session_reference(self):
        st, r = self.t.admin.request("POST", f"/lc-{T}-src/_search", {"size": 5, "query": {"match_all": {}}})
        for h in r["hits"]["hits"]:
            ref = h["_source"].get("source_ref", {})
            if ref.get("leak"):
                continue   # the control leaf of the test above
            self.assertEqual(sorted(ref), ["session"])
            self.assertRegex(ref["session"], r"^s[0-9a-f]{12}$")

    def test_captured_prompts_carry_no_marker_and_no_gold_id(self):
        self.assertTrue(self.spy.prompts)
        for task, blob in self.spy.prompts:
            self.assertNotIn(MARK, blob, task)
            self.assertIsNone(GOLD_ID.search(blob), task)
            self.assertNotIn("has_answer", blob, task)

    def test_the_arms_run_end_to_end_on_the_real_stack(self):
        """Pipeline sanity, not a win: Elasticsearch also matches the title and entities fields, which carry each leaf's topic
        terms, so flat search is not length-handicapped here and the text-only planted advantage of the in-memory gate does
        not transfer. What must hold: no failed recall, the tree finds the gold, and every arm scores."""
        direct = self.t.direct()
        view = self.t.view()
        self.assertEqual(view.gen, 1)
        tr = arms.TreeRetriever(self.port, view, T, CLS, "px", self.t.consts)
        outs = {"A0": {}, "A1": {}, "A1-tree-only": {}}
        gold = {}
        for q in self.fx.questions:
            o = arms.run_question(q.question, direct, tr, self.t.world, beta=0.0)
            for a in outs:
                outs[a][q.qid] = o[a]
            gold[q.qid] = [ds.turns_hash(dict(q.sessions)[q.gold_session_ids[0]])]
        sc = {a: question_scores(o, gold) for a, o in outs.items()}
        mean = lambda d: sum(d[0].values()) / len(d[0])
        self.assertEqual(tr.failures, 0)
        self.assertEqual({a: v[1] for a, v in sc.items()}, {"A0": 0, "A1": 0, "A1-tree-only": 0})
        self.assertGreaterEqual(mean(sc["A1-tree-only"]), 0.75, {a: mean(v) for a, v in sc.items()})
        self.assertGreaterEqual(mean(sc["A1"]), 0.75)
        print("real-stack means:", {a: round(mean(v), 3) for a, v in sc.items()})


if __name__ == "__main__":
    unittest.main()
