"""S6-H1 question loop and H1 decision. The loop runs on the in-memory corpus with the stub model behind a counter (no Elasticsearch, no
real model); the decision is checked on hand-built ledgers whose answer is known: the registered rule (a), (b), (c), the outcome
words, the SHUFFLED void reading, the primary denominator (DEV-EXPOSED and abstention out), and clustering by content."""
import tempfile
import unittest
from pathlib import Path

from evalkit import arms, corpus, dataset as ds, direct50_offline as d50, fixture, s2_stage, s6h1_stage as st, stats
from living_context.config import Constants
from living_context.model.budget import Budget
from living_context.model.cache import DictStore
from living_context.model.port import ModelPort
from living_context.model.stub import StubBackend
from living_context.policy import ClassLattice
from living_context.recall.view import MemView
from living_context.structure import build

C = Constants()
FX = fixture.make("advantage", 8, mates=10)
MC = corpus.MemCorpus(FX.sessions)
HASHES = {sid: ds.turns_hash(t) for q in FX.questions for sid, t in q.sessions}


def port(d, cap=10 ** 6):
    return ModelPort(StubBackend(), Budget(Path(d) / "counter", cap), ClassLattice(["base"], []), DictStore())


def fresh_from_fixture():
    qs = list(FX.questions)
    ab = ds.Question("zz_abs", qs[0].qtype, "nothing about this", "", (), qs[0].sessions)
    qs.append(ab)
    ids = [q.qid for q in FX.questions]
    return ds.FreshSet(tuple(qs), tuple(ids[1:]), tuple(ids), (ids[0],), ("zz_abs",), 0)


class Loop(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.d = tempfile.mkdtemp()
        tree = build.build_tree(MC.leaves, port(cls.d + "/b"), "t", "base", consts=C)
        view = MemView(tree)
        p = port(cls.d)
        cls.fs = fresh_from_fixture()
        cls.shuf = st.shuffled_subset(cls.fs, 3)
        cls.led = s2_stage.Ledger(Path(cls.d) / "done.jsonl")
        cls.beta = 0.004
        st.run_questions(cls.fs, HASHES, s2_stage.Recorder(corpus.MemDirect(MC.leaves)),
                         s2_stage.Recorder(arms.TreeRetriever(p, view, "t", "base", "px", C, workers=1)),
                         s2_stage.Recorder(arms.TreeRetriever(p, arms.PermutedView(view), "t", "base", "px", C, workers=1)),
                         MC.world, cls.led, beta=cls.beta, shuffled_qids=cls.shuf)

    def test_every_fresh_question_is_recorded_once_with_its_stratum(self):
        self.assertEqual(len(self.led.rows), len(self.fs.questions))
        strata = {r["stratum"] for r in self.led.rows.values()}
        self.assertEqual(strata, {"primary", "dev_exposed", "abstention"})
        self.assertEqual(self.led.rows[s2_stage.qref("zz_abs")]["gold"], [])

    def test_shuffled_runs_on_the_registered_subset_only(self):
        got = {k for k, r in self.led.rows.items() if "SHUFFLED" in r["arms"]}
        self.assertEqual(got, {s2_stage.qref(q) for q in self.shuf})
        self.assertTrue(set(self.shuf) <= set(self.fs.primary))

    def test_the_top50_direct_list_is_logged_and_rebuilds_a0_and_a0_len(self):
        for r in self.led.rows.values():
            self.assertTrue(0 < len(r["direct50"]) <= 50)
            self.assertTrue(all(len(e) == 4 for e in r["direct50"]))
            self.assertEqual(d50.a0_from_row(r), r["arms"]["A0"]["sessions"])
            self.assertEqual(d50.a0_len_from_row(r, self.beta), r["arms"]["A0+LEN"]["sessions"])

    def test_the_tree_list_is_logged_and_rebuilds_a1(self):
        for r in self.led.rows.values():
            tids = [e[0] for e in r["tree50"]]
            dids = [e[0] for e in r["direct50"]]
            world = arms.World({e[0]: e[1] for e in r["direct50"] + r["tree50"] if e[1]}, {})
            self.assertEqual(arms.distinct_sessions(arms.a1_leaves(tids, dids, "lc"), world), r["arms"]["A1"]["sessions"])

    def test_the_ledger_holds_no_text_and_no_gold_id(self):
        body = (Path(self.d) / "done.jsonl").read_text()
        ds.assert_model_safe(body)
        for q in FX.questions:
            self.assertNotIn(q.question, body)

    def test_the_decision_runs_on_the_real_loop_output(self):
        dec = st.decide(self.led, self.fs, {}, shuffled_qids=self.shuf, resamples=200)
        self.assertIn(dec["word"], (stats.HOLDS, stats.BELOW_MIN, stats.NOT_ESTABLISHED, stats.DID_NOT_HOLD, stats.HARM))
        self.assertEqual(dec["n_primary"], len(self.fs.primary))


# ---- hand-built ledgers --------------------------------------------------------------------------------------------------------
def ledger(rows):
    led = s2_stage.Ledger(Path(tempfile.mkdtemp()) / "done.jsonl")
    for r in rows:
        led.append(r)
    return led


def row(qid, *, a0, a1, alen=None, tree=None, shuffled=None, sources=(), stratum="primary"):
    """Each arm is 1 (gold in the top 10) or 0. `tree` is A1-tree-only."""
    g = ["g_" + qid]
    hit = lambda x: (g if x else ["x_" + qid])
    arms_ = {"A0": {"sessions": hit(a0), "failed": False}, "A1": {"sessions": hit(a1), "failed": False},
             "A0+LEN": {"sessions": hit(a0 if alen is None else alen), "failed": False},
             "A1-tree-only": {"sessions": hit(a1 if tree is None else tree), "failed": False}}
    if shuffled is not None:
        arms_["SHUFFLED"] = {"sessions": hit(shuffled), "failed": False}
    return {"qref": s2_stage.qref(qid), "stratum": stratum, "qtype": "multi-session", "gold_n": 1, "gold": g, "arms": arms_,
            "sources": list(sources), "holder_calls": 50, "fallbacks": 0, "direct50": [], "tree50": []}


def world(n=60, *, a1_wins=40, tree_only=10, alen_like_a1=False, shuffled_drop=True, exposed_bad=0, abstention=0):
    """n primary questions: A1 hits all; A0 misses `a1_wins` of them; `tree_only` of the hits came from the tree only."""
    qids = ["p%03d" % i for i in range(n)]
    rows = []
    for i, q in enumerate(qids):
        a0 = 0 if i < a1_wins else 1
        rows.append(row(q, a0=a0, a1=1, alen=(1 if alen_like_a1 else a0), sources=["tree_only"] if i < tree_only else ["both"],
                        shuffled=(0 if shuffled_drop else 1) if i < 50 else None))
    ex = ["e%03d" % i for i in range(exposed_bad)]
    rows += [row(q, a0=1, a1=0, sources=[], stratum="dev_exposed") for q in ex]
    ab = ["z%03d_abs" % i for i in range(abstention)]
    rows += [dict(row(q, a0=0, a1=0, stratum="abstention"), gold=[], gold_n=0) for q in ab]
    fs = ds.FreshSet(tuple(ds.Question(q, "multi-session", "", "", (), ()) for q in qids + ex + ab), tuple(qids), tuple(qids + ex),
                     tuple(ex), tuple(ab), 0)
    return ledger(rows), fs, qids[:50]


class Decision(unittest.TestCase):
    def dec(self, clusters=None, **kw):
        led, fs, shuf = world(**kw)
        return st.decide(led, fs, clusters or {}, shuffled_qids=shuf, resamples=2000)

    def test_holds_when_a_b_and_c_all_hold(self):
        d = self.dec()
        self.assertEqual(d["word"], stats.HOLDS)
        self.assertGreater(d["a_A1_minus_A0"]["lo"], 0)
        self.assertGreater(d["b_A1_minus_A0_plus_LEN"]["lo"], 0)
        self.assertGreater(d["c_tree_only_share"]["wilson_lo"], 0)

    def test_the_length_control_matching_a1_makes_it_did_not_hold(self):
        d = self.dec(alen_like_a1=True)
        self.assertEqual(d["word_b"], stats.DID_NOT_HOLD)
        self.assertEqual(d["word"], stats.DID_NOT_HOLD)

    def test_no_tree_only_gold_makes_it_not_established(self):
        d = self.dec(tree_only=0)
        self.assertEqual(d["word"], stats.NOT_ESTABLISHED)
        self.assertIn("(c)", d["reason"])

    def test_a_shuffled_control_that_does_not_drop_voids_the_claim(self):
        d = self.dec(shuffled_drop=False)
        self.assertFalse(d["shuffled_control"]["passed"])
        self.assertEqual(d["word_registered_rule"], stats.HOLDS)
        self.assertEqual(d["word"], stats.NOT_ESTABLISHED)

    def test_harm_when_a1_is_worse_than_a0(self):
        led, fs, shuf = world()
        for q in fs.primary:
            r = led.rows[s2_stage.qref(q)]
            r["arms"]["A0"]["sessions"], r["arms"]["A1"]["sessions"] = r["arms"]["A1"]["sessions"], ["x"]
            r["arms"]["A0"]["sessions"] = r["gold"]
        d = st.decide(led, fs, {}, shuffled_qids=shuf, resamples=2000)
        self.assertEqual(d["word_a"], stats.HARM)

    def test_the_primary_denominator_excludes_dev_exposed_and_abstention(self):
        d = self.dec(exposed_bad=6, abstention=3)
        self.assertEqual(d["n_primary"], 60)
        self.assertEqual(d["a_A1_minus_A0"]["n"], 60)
        self.assertEqual(d["sensitivity_299"]["a"]["n"], 66)
        self.assertLess(d["sensitivity_299"]["a"]["point"], d["a_A1_minus_A0"]["point"])
        self.assertEqual(d["abstention_stratum"]["n"], 3)
        self.assertEqual(d["means"]["dev_exposed"]["A1"]["n"], 6)

    def test_intervals_resample_content_clusters(self):
        clusters = {"p%03d" % i: "c%d" % (i // 3) for i in range(60)}
        d = self.dec(clusters=clusters)
        self.assertEqual(d["a_A1_minus_A0"]["clusters"], 20)
        self.assertEqual(d["a_A1_minus_A0"]["resamples"], 2000)

    def test_the_registered_statistics_are_the_defaults(self):
        self.assertEqual((stats.RESAMPLES, stats.SEED, stats.H1_MIN_EFFECT), (10_000, 16096, 0.05))

    def test_a_missing_question_is_refused(self):
        led, fs, shuf = world()
        fs2 = ds.FreshSet(fs.questions, fs.primary + ("absent",), fs.sensitivity, fs.dev_exposed, fs.abstention, 0)
        with self.assertRaises(ValueError):
            st.decide(led, fs2, {}, shuffled_qids=shuf, resamples=200)


if __name__ == "__main__":
    unittest.main()
