"""Tuning kit: statistics, signals, stage registry and adapters, traces, pool and conversation generators, external-set loader.
No Elasticsearch, no model."""
import json
import math
import random
import tempfile
import unittest
from pathlib import Path

import numpy as np

from tunekit import baseline, external, pool_reference, signals, stages, stats, synth, synth_conv, synth_pools, traces


class Stats(unittest.TestCase):
    def test_wilson_known_values(self):
        p, lo, hi = stats.wilson(5, 10)
        self.assertAlmostEqual(lo, 0.2366, 3)
        self.assertAlmostEqual(hi, 0.7634, 3)
        self.assertAlmostEqual(stats.wilson(0, 10)[2], 0.2775, 3)
        self.assertTrue(math.isnan(stats.wilson(0, 0)[0]))

    def test_auc_perfect_inverse_and_ties(self):
        self.assertEqual(stats.auc([0, 0, 1, 1], [0, 0, 1, 1]), 1.0)
        self.assertEqual(stats.auc([1, 1, 0, 0], [0, 0, 1, 1]), 0.0)
        self.assertEqual(stats.auc([1, 1, 1, 1], [0, 0, 1, 1]), 0.5)

    def test_auc_report_refuses_small_samples_and_separates_signal_from_noise(self):
        rng = np.random.default_rng(1)
        y = (rng.random(600) < 0.3).astype(int)
        good = y + rng.normal(0, 0.5, 600)
        noise = rng.normal(0, 1, 600)
        cl = [i % 40 for i in range(600)]
        g = stats.auc_report(good, y, cl, resamples=300)
        n = stats.auc_report(noise, y, cl, resamples=300)
        self.assertGreater(g["lo"], 0.7)
        self.assertLessEqual(n["lo"], 0.5)
        self.assertGreaterEqual(n["hi"], 0.5)
        small = stats.auc_report(good[:40], y[:40], cl[:40], resamples=50)
        self.assertIsNone(small["auc"])
        self.assertIn("NOT ESTABLISHED", small["reading"])
        # broken variant: shuffled labels must turn the "good" check red
        sh = stats.auc_report(good, rng.permutation(y), cl, resamples=300)
        self.assertLess(sh["lo"], 0.6)

    def test_cluster_bootstrap_is_deterministic(self):
        x = np.arange(100.0)
        a = stats.cluster_bootstrap(np.mean, np.arange(100) // 5, [x], resamples=200)
        b = stats.cluster_bootstrap(np.mean, np.arange(100) // 5, [x], resamples=200)
        self.assertEqual(a, b)
        self.assertEqual(a[3], 20)


class Signals(unittest.TestCase):
    def test_features_as_adaptive_defines_them(self):
        bm = [f"b{i}" for i in range(10)]
        kn = bm[:4] + [f"k{i}" for i in range(6)]
        cos = [0.9 - 0.01 * i for i in range(10)]
        f = signals.query_features(bm, kn, cos, bm + kn, {d: hash(d) % 3 for d in bm + kn})
        self.assertEqual(f["agree"], 0.4)
        self.assertAlmostEqual(f["margin"], (0.9 - 0.81) / 0.9)
        self.assertLessEqual(f["spread"], 3)
        self.assertTrue(signals.flat_sufficient({"agree": 0.4, "margin": 0.1, "spread": 6}))
        self.assertFalse(signals.flat_sufficient({"agree": 0.39, "margin": 0.1, "spread": 6}))
        self.assertFalse(signals.flat_sufficient({"agree": 0.4, "margin": 0.09, "spread": 6}))
        self.assertFalse(signals.flat_sufficient({"agree": 0.4, "margin": 0.1, "spread": 7}))
        self.assertFalse(signals.flat_sufficient({"agree": 1, "margin": float("nan"), "spread": 1}))   # a failed feature escalates

    def test_memory_signals_count_what_adaptive_counts(self):
        c = synth.generate(synth.knobs_at(500, 3, 0.5))
        s = signals.memory_signals(c.leaves)
        self.assertEqual(s["leaf_count"], 500)
        self.assertGreater(s["token_total"], 500 * 10)
        multi = {}
        for L in c.leaves:
            multi[L["session"]] = multi.get(L["session"], 0) + 1
        self.assertEqual(s["session_count"], sum(1 for v in multi.values() if v >= 2))

    def test_predictive_power_finds_a_planted_signal_and_not_a_shuffled_one(self):
        rng = random.Random(5)
        rows = []
        for i in range(800):
            fail = rng.random() < 0.25
            rows.append({"hit10": not fail, "agree": rng.choice([0.0, 0.1, 0.2]) if fail else rng.choice([0.6, 0.7, 0.8]),
                         "margin": rng.random() * 0.3, "spread": rng.randrange(1, 9), "cluster": i % 50})
        p = signals.predictive_power(rows, resamples=200)
        self.assertGreater(p["agree (low = hard)"]["lo"], 0.9)
        self.assertLess(p["spread (high = hard)"]["lo"], 0.6)       # noise
        ys = [r["hit10"] for r in rows]
        rng.shuffle(ys)
        for r, y in zip(rows, ys):
            r["hit10"] = y
        q = signals.predictive_power(rows, resamples=200)
        self.assertLess(q["agree (low = hard)"]["lo"], 0.6)


class Baseline(unittest.TestCase):
    def test_score_ranks_gold_stale_and_multiple_gold(self):
        doc_ids = [f"d{i}" for i in range(6)]
        qs = [{"qid": 0, "gold": [3], "stale": [2]}, {"qid": 1, "gold": [1, 4], "stale": []}, {"qid": 2, "gold": [5], "stale": []}]
        mk = lambda fused, bm=(), kn=(): baseline.QueryRow(-1, list(fused), list(bm), list(kn), [], [], 1.0)
        rows = [mk(["d2", "d3"]), mk(["d0", "d4", "d1"], bm=["d4"]), mk(["d0"])]
        out = baseline.score(qs, rows, doc_ids)
        self.assertEqual([o["rank"] for o in out], [2, 2, None])
        self.assertTrue(out[0]["stale_first"])               # the superseded leaf outranks the current one
        self.assertTrue(out[1]["hit5"] and out[1]["bm25_hit10"] and not out[1]["knn_hit10"])
        self.assertFalse(out[2]["hit10"])
        self.assertEqual(out[1]["rr"], 0.5)


class ExactKnn(unittest.TestCase):
    def setUp(self):
        rng = np.random.default_rng(3)
        P = rng.normal(size=(60, 8))
        self.P = P / np.linalg.norm(P, axis=1, keepdims=True)
        q = rng.normal(size=(2, 8))
        self.Q = q / np.linalg.norm(q, axis=1, keepdims=True)
        self.ids = [f"d{i}" for i in range(60)]

    def rows(self, shuffle=False, bias=0.0):
        out = []
        for i in range(2):
            cos = self.P @ self.Q[i]
            order = list(np.argsort(-cos)[:10])
            if shuffle:
                order = list(np.argsort(-cos)[20:30])
            out.append(baseline.QueryRow(i, [], [], [self.ids[j] for j in order], [float(cos[j]) + bias for j in order], [], 1.0))
        return out

    def test_exact_agreement_is_detected_and_a_broken_index_is_not_hidden(self):
        ok = baseline.exact_knn_check(None, self.P, self.ids, self.Q, self.rows(), n=2)
        self.assertEqual(ok["mean_top10_overlap_with_exact"], 1.0)
        self.assertLess(ok["max_abs_cosine_error"], 1e-9)
        bad = baseline.exact_knn_check(None, self.P, self.ids, self.Q, self.rows(shuffle=True), n=2)
        self.assertLess(bad["mean_top10_overlap_with_exact"], 0.2)
        off = baseline.exact_knn_check(None, self.P, self.ids, self.Q, self.rows(bias=0.05), n=2)
        self.assertAlmostEqual(off["max_abs_cosine_error"], 0.05, places=6)


class Stages(unittest.TestCase):
    def test_every_stage_is_listed_with_a_status(self):
        self.assertEqual([s.id for s in stages.STAGES], ["Z1", "Z2", "Z3", "Z4", "Z5", "Z6"])
        self.assertEqual({s.status for s in stages.STAGES}, {"RUNS", "PARTIAL", "WAITS"})

    def test_adapters_fail_loudly_while_product_code_is_missing(self):
        for key in ("adaptive_signals", "adaptive_machine", "pool_select", "pool_merge", "inject", "rollover"):
            if stages.product_available(key):
                continue      # the product exists: the adapter would import it, which is its job
            with self.assertRaises(stages.ProductCodeMissing):
                stages.require_product(key)
        if not stages.product_available("pool_select"):
            with self.assertRaises(stages.ProductCodeMissing):
                stages.SelectionAdapter().select(None)
        if not stages.product_available("adaptive_machine"):
            with self.assertRaises(stages.ProductCodeMissing):
                stages.MachineAdapter().run_trace([], {})

    def test_n_floor_is_f_max_times_f_target(self):
        from living_context.config import Constants
        c = Constants()
        self.assertEqual(stages.n_floor(c.f_max, c.f_target), 384)


class Traces(unittest.TestCase):
    def test_determinism_and_shapes(self):
        for name, fn in traces.ALL.items():
            self.assertEqual(fn(3), fn(3), name)
        g = traces.growth(1, n0=100, n_end=20000, hours=200)
        self.assertEqual(g[0].leaf_count, 100)
        self.assertAlmostEqual(g[-1].leaf_count, 20000, delta=2)
        self.assertEqual(sum(len(t.units) for t in traces.bulk_import(1)), 0)
        self.assertEqual(traces.bulk_import(1)[-1].leaf_count, 30000)

    def test_stationary_noise_stays_below_the_placeholder_gate_and_flood_is_one_principal(self):
        noise = traces.stationary_noise(2)
        self.assertLess(traces.hard_rate(noise, window=2000), 0.2)
        fl = traces.flooding_principal(2)
        self.assertGreater(traces.hard_rate(fl), 0.8)                         # the flooder makes the pool look hard...
        self.assertLess(traces.hard_rate(fl, drop_principal=0), 0.3)          # ...and leave-one-out removes it

    def test_oscillation_crosses_the_gate_both_ways(self):
        t = traces.oscillating(1, hours=24 * 8, period=48)
        a = traces.hard_rate(t[:48], window=500)
        b = traces.hard_rate(t[48:96], window=500)
        self.assertLess(a, 0.2)
        self.assertGreater(b, 0.2)


class Pools(unittest.TestCase):
    def test_world_is_deterministic_and_ground_truth_is_consistent(self):
        k = synth_pools.PoolKnobs(seed=3, n_pools=6, n_decoys=3, n_leak=1, leaves_per_pool=120, n_questions=200)
        a, b = synth_pools.generate(k), synth_pools.generate(k)
        self.assertEqual(a.manifest, b.manifest)
        self.assertEqual([q["text"] for q in a.questions], [q["text"] for q in b.questions])
        by = {p["id"]: p for p in a.pools}
        self.assertEqual(a.manifest["roles"], {"real": 6, "decoy": 3, "leak": 1})
        for q in a.questions:
            for pid, ids in q["gold"].items():
                self.assertTrue(by[pid]["granted"])
                self.assertEqual(by[pid]["role"], "real")
                self.assertTrue(all(0 <= i < len(by[pid]["leaves"]) for i in ids))
            if q["kind"] == "none":
                self.assertEqual(q["answer_pools"], [])
            if q["kind"] == "two":
                self.assertEqual(len(q["answer_pools"]), 2)
        self.assertTrue(any(q["kind"] == "two" for q in a.questions) and any(q["kind"] == "none" for q in a.questions))
        leak = [p for p in a.pools if p["role"] == "leak"][0]
        self.assertFalse(leak["granted"])
        self.assertTrue(any(L.get("copy_of") for L in leak["leaves"]))          # the ungranted pool holds copies of real facts

    def test_gold_leaf_answers_the_question_in_its_pool(self):
        w = synth_pools.generate(synth_pools.PoolKnobs(seed=4, n_pools=5, n_decoys=2, n_leak=0, leaves_per_pool=150, n_questions=100,
                                                      none_frac=0.0, two_pool_frac=0.2))
        by = {p["id"]: p for p in w.pools}
        for q in w.questions:
            for pid, ids in q["gold"].items():
                leaf = by[pid]["leaves"][ids[0]]
                phrase = q["text"].split("What is the ")[1].split(" for ")[0]
                self.assertTrue(any(phrase == f for concept in synth.ATTRS for f in concept))
                ent = q["text"].rstrip("?").split(" for ")[1].split(" regarding ")[0]
                self.assertTrue(set(ent.split()) <= set(leaf["text"].replace(",", " ").replace(".", " ").replace("(", " ").replace(")", " ").replace(":", " ").split()))

    def test_domains_keep_invented_vocabularies_apart(self):
        a = synth.generate(synth.knobs_at(100, 1, 0.0, domain=1))
        b = synth.generate(synth.knobs_at(100, 1, 0.0, domain=2))
        va = {w for t in a.topics for w in t}
        vb = {w for t in b.topics for w in t}
        self.assertEqual(va & vb - set(synth.COMMON), set())

    def test_reference_selection_follows_the_documented_rule(self):
        rng = np.random.default_rng(0)
        cards = {f"p{i}": rng.normal(size=8) for i in range(10)}
        cards = {k: v / np.linalg.norm(v) for k, v in cards.items()}
        q = cards["p3"]
        self.assertEqual(pool_reference.select(q, cards, ["p1", "p2", "p3"]), ["p1", "p2", "p3"])           # |G0| <= P_ALL: all
        sel = pool_reference.select(q, cards, cards, p_max=3, tau_card=-1)
        self.assertEqual(sel[0], "p3")
        self.assertEqual(len(sel), 3)
        self.assertEqual(pool_reference.select(q, cards, cards, tau_card=2.0), ["p3"])                      # none clears: the top one
        self.assertEqual(pool_reference.select(q, cards, cards, hint=["p5"]), ["p5"])
        with self.assertRaises(pool_reference.NotGranted):
            pool_reference.select(q, cards, ["p1", "p2"], hint=["p9"])


class PoolRun(unittest.TestCase):
    class Fake:
        """The hashed stub embedder (a lexical bag of words): enough to exercise the runner; not the pinned model."""
        def passages(self, texts):
            from living_context.embed import stub
            return np.array([stub.embed(t) for t in texts])
        queries = passages

    def test_runner_counts_hits_and_never_selects_an_ungranted_pool(self):
        from tunekit import run_pools
        rows, cs = run_pools.run(self.Fake(), [1], [(6, 3, 0.6)], (1, 8), (-1.0,))
        by = {(r["p_max"]): r for r in rows}
        self.assertGreaterEqual(by[8]["answer_pool_in_S"], by[1]["answer_pool_in_S"])     # a wider selection never hits less
        self.assertGreater(by[8]["answer_pool_in_S"], 0.9)                                  # lexical cards: the answer pool is found
        self.assertEqual({r["ungranted_selected"] for r in rows}, {0})
        self.assertEqual(by[1]["mean_pools_queried"], 1.0)

    def test_runner_goes_red_when_cards_are_shuffled(self):
        from tunekit import run_pools

        class Shuffled(self.Fake):
            def passages(self, texts):
                v = super().passages(texts)
                return v[np.random.default_rng(1).permutation(len(v))]
        rows, _ = run_pools.run(Shuffled(), [1], [(6, 3, 0.6)], (1,), (-1.0,))
        self.assertLess(rows[0]["answer_pool_in_S"], 0.5)


class Conversations(unittest.TestCase):
    def test_planted_events_and_offers(self):
        c = synth.generate(synth.knobs_at(1500, 2, 0.5))
        convs = synth_conv.generate(c, 7, n_convs=30, turns=25)
        again = synth_conv.generate(c, 7, n_convs=30, turns=25)
        self.assertEqual([x.turns for x in convs], [x.turns for x in again])
        kinds = {e["kind"] for cv in convs for e in cv.events}
        self.assertTrue({"shift", "return", "new_entity"} <= kinds, kinds)
        cur = {L["id"] for L in c.leaves if L["kind"] != "old"}
        for cv in convs:
            for t in cv.turns:
                if t["events"]:
                    self.assertTrue(t["offer"])
                    self.assertTrue(set(t["offer"]) <= cur)
                if "shift" in t["events"]:
                    self.assertTrue(all(c.leaves[i]["topic"] == t["topic"] for i in t["offer"]))
            for e in cv.events:
                self.assertIn(e["kind"], cv.turns[e["i"]]["events"])


class External(unittest.TestCase):
    def test_beir_like_fixture(self):
        with tempfile.TemporaryDirectory() as d:
            r = Path(d)
            (r / "corpus.jsonl").write_text("\n".join(json.dumps({"_id": f"D{i}", "title": f"T{i}", "text": f"body {i}"}) for i in range(5)))
            (r / "queries.jsonl").write_text("\n".join(json.dumps({"_id": f"Q{i}", "text": f"query {i}"}) for i in range(4)))
            (r / "qrels.tsv").write_text("query-id\tcorpus-id\tscore\nQ0\tD1\t1\nQ0\tD2\t0\nQ1\tD3\t2\nQ2\tD9\t1\n")
            c = external.load_beir_like(r)
        self.assertEqual(len(c.leaves), 5)
        self.assertEqual([q["gold"] for q in c.questions], [[1], [3]])          # Q2's only relevant doc is absent, Q3 has none: dropped
        self.assertEqual(c.manifest["questions_dropped_no_gold"], 2)


class D1(unittest.TestCase):
    """TUNING D1 / Z3's derivation check: at or below N_FLOOR a bulk build has no upper level; one leaf above it has one. Uses the
    product's own build_tree with the stub model (no real model call)."""

    def build(self, n):
        from living_context.embed import stub
        from living_context.ingest import title
        from living_context.model.budget import Budget
        from living_context.model.cache import DictStore
        from living_context.model.port import ModelPort
        from living_context.model.stub import StubBackend
        from living_context.policy import ClassLattice
        from living_context.structure import build
        c = synth.generate(synth.knobs_at(n, 1, 0.5))
        leaves = [build.LeafRec(f"{i + 1:032x}", 1, L["text"], title.build_title(L["text"]), stub.embed(L["text"]), 20) for i, L in enumerate(c.leaves)]
        with tempfile.TemporaryDirectory() as d:
            port = ModelPort(StubBackend(), Budget(Path(d) / "b", 10_000), ClassLattice(["base"], []), DictStore())
            return build.build_tree(leaves, port, "tena", "base")

    def test_no_upper_level_at_or_below_n_floor_and_one_above(self):
        floor = stages.n_floor()
        at, above = self.build(floor), self.build(floor + 1)
        self.assertEqual(at.levels, 1)
        self.assertLessEqual(len(at.top), 24)
        self.assertGreaterEqual(above.levels, 2)
        for n in (8, 100):
            self.assertEqual(self.build(n).levels, 1)


if __name__ == "__main__":
    unittest.main()
