"""BUILD-PLAN Step 20 / Step 10 stub gate for H1: the arms, the scorers, the registered statistics and every broken variant.

Planted-ranking worlds test the machinery (fusion, session mapping, intervals, outcome words) with exact rankings; the text
fixtures run the real builder and fan-out with the stub and show SHUFFLED-SUMMARIES falling. Each `broken` check must be red."""
import unittest

from evalkit import arms, fixture, memrun, stats
from evalkit.arms import ArmOut, TreeOut, World
from evalkit.score import ResultError, check_result, question_scores, write_result
from living_context.config import Constants

N = 40


def planted(kind: str, n: int = N, gold_tokens: int = 1000, decoy_tokens: int = 1000, seed: int = 3):
    """Exact rankings. Leaf ids L<q>_<k>; every leaf is its own session. `advantage`: the direct list buries gold below the
    window (so only the tree finds it, at rank 2) for 3 of every 4 questions; `null`: both lists have gold at rank 1."""
    leaf_session, tokens, gold, ranks = {}, {}, {}, {}
    for q in range(n):
        qid = f"q{q:03d}"
        g = f"g{q}"
        decoys_d = [f"d{q}_{k}" for k in range(70)]
        decoys_t = [f"t{q}_{k}" for k in range(3)]
        for lid in [g] + decoys_d + decoys_t:
            leaf_session[lid] = "S" + lid
            tokens["S" + lid] = gold_tokens if lid == g else decoy_tokens
        gold[qid] = ["S" + g]
        buried = (q % 4 != 0) and kind == "advantage"
        if kind == "null":
            direct, tree = [g] + decoys_d, [g] + decoys_t
        elif buried:
            direct, tree = decoys_d + [g], [decoys_t[0], g, decoys_t[1], decoys_t[2]]     # gold at direct rank 71
        else:
            direct, tree = [g] + decoys_d, [g] + decoys_t
        ranks[qid] = (direct, tree)
    return World(leaf_session, tokens), gold, ranks


def run_planted(world, gold, ranks, beta=0.0):
    outs = {a: {} for a in ("A0", "A0+LEN", "A1", "A1-tree-only")}
    src = {}
    for qid, (direct, tree) in ranks.items():
        scored = [(l, 1.0 / (60 + i + 1)) for i, l in enumerate(direct)]
        o = arms.run_question(qid, lambda _q, s=scored: s, lambda _q, t=tree: TreeOut(list(t)), world, beta=beta)
        for a in outs:
            outs[a][qid] = o[a]
        src[qid] = arms.source_of_gold(o["A1"], tree, [l for l, _ in scored], gold[qid], world)
    return outs, src


class Machinery(unittest.TestCase):
    clusters = {f"q{q:03d}": f"c{q // 2}" for q in range(N)}

    def scores(self, outs, gold):
        return {a: question_scores(o, gold)[0] for a, o in outs.items()}

    def decision(self, world, gold, ranks, beta=0.0):
        outs, src = run_planted(world, gold, ranks, beta)
        sc = self.scores(outs, gold)
        a = stats.clustered_bootstrap(stats.paired_differences(sc["A1"], sc["A0"]), self.clusters, resamples=2000)
        b = stats.clustered_bootstrap(stats.paired_differences(sc["A1"], sc["A0+LEN"]), self.clusters, resamples=2000)
        k = sum(1 for s in src.values() if "tree_only" in s.values())
        n = sum(1 for s in src.values() if s)
        return sc, a, b, stats.decide_h1(a, b, k, n)

    def test_planted_advantage_a1_beats_a0_and_a0_len_and_h1_holds(self):
        w, g, r = planted("advantage")
        sc, a, b, dec = self.decision(w, g, r)
        mean = lambda d: sum(d.values()) / len(d)
        self.assertEqual(mean(sc["A0"]), 0.25)          # gold is found by flat search for 1 question in 4
        self.assertEqual(mean(sc["A1"]), 1.0)
        self.assertGreater(a.lo, 0.0)
        self.assertGreater(b.lo, 0.0)
        self.assertEqual(dec.word, stats.HOLDS, dec)
        self.assertGreater(dec.tree_only_share[2], 0.0)

    def test_null_a1_does_not_beat_a0(self):
        w, g, r = planted("null")
        _, a, b, dec = self.decision(w, g, r)
        self.assertEqual((a.point, b.point), (0.0, 0.0))
        self.assertNotIn(dec.word, (stats.HOLDS, stats.BELOW_MIN))

    def test_a_length_prior_that_explains_the_gain_makes_a0_len_equal_a1_and_h1_not_hold(self):
        # gold sessions are 100 times longer than the others: A0+LEN lifts them into the window top, A1's edge over it vanishes
        w, g, r = planted("advantage", gold_tokens=100000, decoy_tokens=1000)
        # direct rank 71 is outside the window, so LEN cannot see it: put the gold inside the window at rank 40
        r = {q: ([*d[:40], d[-1], *d[40:-1]] if d[-1].startswith("g") and d[0] != d[-1] else d, t) for q, (d, t) in r.items()}
        sc, a, b, dec = self.decision(w, g, r, beta=0.016)
        self.assertGreater(a.lo, 0.0)        # (a) holds
        self.assertLessEqual(b.lo, 0.0)      # (b) does not
        self.assertNotIn(dec.word, (stats.HOLDS, stats.BELOW_MIN))

    def test_tree_only_gain_with_no_tree_only_source_is_not_established(self):
        a = stats.Interval(0.3, 0.2, 0.4, 40, 20, "x")
        self.assertEqual(stats.decide_h1(a, a, 0, 30).word, stats.NOT_ESTABLISHED)
        self.assertEqual(stats.decide_h1(a, a, 12, 30).word, stats.HOLDS)


class Scorers(unittest.TestCase):
    def test_recall_counts_distinct_sessions_not_leaves(self):
        world = World({f"a{i}": "SA" for i in range(10)} | {"b": "SB"}, {"SA": 10, "SB": 10})
        ranked = [f"a{i}" for i in range(10)] + ["b"]
        self.assertEqual(arms.recall_at_k(arms.distinct_sessions(ranked, world), ["SB"]), 1.0)
        self.assertEqual(arms.recall_at_k(arms.distinct_sessions(ranked, world, broken_leaf_level=True), ["SB"]), 0.0,
                         "the leaf-level variant should have missed the second session")

    def test_gold_is_the_content_hash_not_the_session_id(self):
        # the same conversation under two ids: scoring on ids misses the twin, scoring on content hashes does not
        world = World({"leaf": "HASH"}, {"HASH": 5})
        got = arms.distinct_sessions(["leaf"], world)
        self.assertEqual(arms.recall_at_k(got, ["HASH"]), 1.0)
        self.assertEqual(arms.recall_at_k(got, ["twin-id"]), 0.0)

    def test_abstention_questions_are_excluded_and_a_failed_call_scores_zero_and_is_counted(self):
        outs = {"q1": ArmOut([], ["S1"]), "q2": ArmOut([], [], failed=True), "q3_abs": ArmOut([], ["S9"])}
        gold = {"q1": ["S1"], "q2": ["S2"], "q3_abs": []}
        sc, failed = question_scores(outs, gold, {"q3_abs"})
        self.assertEqual((sc, failed), ({"q1": 1.0, "q2": 0.0}, 1))
        self.assertIn("q3_abs", question_scores(outs, gold, {"q3_abs"}, broken_include_abstention=True)[0])
        self.assertNotIn("q2", question_scores(outs, gold, {"q3_abs"}, broken_skip_failed=True)[0])

    def test_a_budget_refusal_is_a_failed_recall_for_a1_and_does_not_touch_a0(self):
        fx = fixture.make("advantage", 6)
        r = memrun.run_fixture(fx, cap=0)
        self.assertEqual(r["failed"]["A1"], 6)
        self.assertEqual(sum(r["scores"]["A1"].values()), 0.0)
        self.assertEqual(r["failed"]["A0"], 0)
        self.assertGreater(r["tree_failures"], 0)

    def test_result_row_missing_a_required_field_is_refused(self):
        row = dict(commit="c", stage="S0", arm="A1", pool="fixture", n=3, metric="recall@10", interval_method="m", seed=1,
                   call_counter=0, product_cache_hits=0, routing="agent-routed")
        check_result(row)
        for f in list(row):
            with self.assertRaises(ResultError, msg=f):
                check_result({k: v for k, v in row.items() if k != f})
        with self.assertRaises(ResultError):
            check_result(dict(row, routing="elsewhere"))
        import os, tempfile
        with tempfile.TemporaryDirectory() as d:
            write_result(os.path.join(d, "r.json"), row, [{"qid": "x", "recall": 1.0}])
            with self.assertRaises(ResultError):
                write_result(os.path.join(d, "bad.json"), {k: v for k, v in row.items() if k != "seed"})
            self.assertFalse(os.path.exists(os.path.join(d, "bad.json")))


class Statistics(unittest.TestCase):
    def test_clustered_interval_is_wider_than_the_per_question_variant_on_clustered_data(self):
        diffs, clusters = {}, {}
        for c in range(20):
            for j in range(4):
                diffs[f"q{c}_{j}"] = 1.0 if c % 2 == 0 else 0.0     # the four questions of a cluster move together
                clusters[f"q{c}_{j}"] = f"c{c}"
        good = stats.clustered_bootstrap(diffs, clusters, resamples=3000)
        bad = stats.clustered_bootstrap(diffs, clusters, resamples=3000, broken_per_question=True)
        self.assertEqual((good.n_questions, good.n_clusters), (80, 20))
        self.assertEqual(bad.n_clusters, 80)
        self.assertGreater(good.hi - good.lo, 1.5 * (bad.hi - bad.lo))

    def test_constant_difference_has_a_degenerate_interval_and_runs_are_reproducible(self):
        d = {f"q{i}": 0.5 for i in range(10)}
        iv = stats.clustered_bootstrap(d, {}, resamples=500)
        self.assertEqual((iv.point, iv.lo, iv.hi), (0.5, 0.5, 0.5))
        d2 = {f"q{i}": float(i % 3 - 1) for i in range(30)}
        self.assertEqual(stats.clustered_bootstrap(d2, {}, resamples=800), stats.clustered_bootstrap(d2, {}, resamples=800))
        bca = stats.clustered_bootstrap(d2, {}, resamples=2000, bca=True)
        self.assertLessEqual(bca.lo, bca.point)
        self.assertGreaterEqual(bca.hi, bca.point)

    def test_paired_differences_refuse_unequal_question_sets(self):
        with self.assertRaises(stats.StatsError):
            stats.paired_differences({"a": 1.0}, {"b": 1.0})

    def test_wilson_is_refused_for_a_measure_not_declared_binary(self):
        with self.assertRaises(stats.StatsError):
            stats.wilson(stats.Measure("opens_per_recall", "count"), 120, 100)
        lo, hi = stats.wilson(stats.Measure("tree_only", "binary"), 0, 100)
        self.assertEqual(lo, 0.0)
        self.assertAlmostEqual(hi, 3.84 / 103.84, places=3)   # EXPERIMENT 7.4: 3.7% at N = 100

    def test_outcome_words(self):
        m = 0.05
        self.assertEqual(stats.outcome(0.02, 0.08, m), stats.HOLDS)
        self.assertEqual(stats.outcome(0.01, 0.04, m), stats.BELOW_MIN)
        self.assertEqual(stats.outcome(-0.01, 0.03, m), stats.DID_NOT_HOLD)
        self.assertEqual(stats.outcome(-0.02, 0.10, m), stats.NOT_ESTABLISHED)
        self.assertEqual(stats.outcome(-0.10, -0.02, m), stats.HARM)    # HARM before DID NOT HOLD: hi < 0 also gives hi < m
        self.assertEqual(stats.outcome(-0.10, -0.02, m, harm_defined=False), stats.DID_NOT_HOLD)

    def test_the_worse_of_a_and_b_is_the_outcome(self):
        good = stats.Interval(0.3, 0.2, 0.4, 40, 20, "x")
        flat = stats.Interval(0.0, -0.01, 0.02, 40, 20, "x")
        self.assertEqual(stats.decide_h1(good, flat, 10, 30).word, stats.DID_NOT_HOLD)
        self.assertEqual(stats.decide_h1(flat, good, 10, 30).word, stats.DID_NOT_HOLD)


class ShuffledSummaries(unittest.TestCase):
    fx = fixture.make("advantage", 32)

    def test_shuffled_summaries_fall_for_their_reason_and_the_identity_permutation_does_not(self):
        c = Constants()
        r = memrun.run_fixture(self.fx, consts=c)
        self.assertGreaterEqual(r["levels"], 2, "the control needs a tree of at least two levels")
        base, shuf = r["scores"]["A1-tree-only"], r["scores"]["SHUFFLED"]
        clusters = self.fx.clusters
        drop = stats.clustered_bootstrap(stats.paired_differences(base, shuf), clusters, resamples=2000)
        verdict = stats.shuffled_control(drop, min_point=0.30)
        self.assertTrue(verdict.passed, verdict.reason)
        ident = memrun.run_fixture(self.fx, consts=c, shuffle_shift=0)
        nodrop = stats.clustered_bootstrap(stats.paired_differences(ident["scores"]["A1-tree-only"], ident["scores"]["SHUFFLED"]),
                                           clusters, resamples=2000)
        self.assertFalse(stats.shuffled_control(nodrop, min_point=0.30).passed,
                         "an identity permutation must not look like a working control")

    def test_stub_a1_beats_a0_on_the_advantage_text_world_and_not_on_the_null_world(self):
        adv = memrun.run_fixture(self.fx)
        d = stats.paired_differences(adv["scores"]["A1"], adv["scores"]["A0"])
        iv = stats.clustered_bootstrap(d, self.fx.clusters, resamples=2000)
        self.assertGreater(iv.lo, 0.0, iv)
        nul = fixture.make("null", 32)
        r = memrun.run_fixture(nul)
        d0 = stats.paired_differences(r["scores"]["A1"], r["scores"]["A0"])
        iv0 = stats.clustered_bootstrap(d0, nul.clusters, resamples=2000)
        self.assertLessEqual(iv0.lo, 0.0, iv0)


if __name__ == "__main__":
    unittest.main()
