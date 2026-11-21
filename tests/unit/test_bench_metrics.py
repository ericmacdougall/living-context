"""bench/metrics.py on a hand-computed fixture: every expected figure below is worked out in the comments, not copied from the code."""
import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from bench import metrics

PAD = ["p%d" % i for i in range(10)]

DONE = [
    # Q1: gold g1,g2. A0 returns g1 at rank 2 and g2 at rank 4: recall 2/2, precision 2/10, all-gold 1, MRR 1/2.
    #                 A1 returns g1 at rank 1 only:            recall 1/2, precision 1/10, all-gold 0, MRR 1.
    {"qref": "q1", "gold": ["g1", "g2"],
     "arms": {"A0": {"sessions": ["x", "g1", "y", "g2"] + PAD[:6], "failed": False},
              "A1": {"sessions": ["g1"] + PAD[:9], "failed": False}}},
    # Q2: gold g3. A0 misses it: all four figures 0. A1's recall FAILED: scores 0 and is counted, not dropped.
    {"qref": "q2", "gold": ["g3"],
     "arms": {"A0": {"sessions": PAD, "failed": False}, "A1": {"sessions": [], "failed": True}}},
    # Q3: abstention (no gold): not scored for any arm.
    {"qref": "q3", "gold": [], "arms": {"A0": {"sessions": PAD, "failed": False}, "A1": {"sessions": PAD, "failed": False}}},
]
# A0 over Q1,Q2: recall (1+0)/2, precision (0.2+0)/2, all-gold (1+0)/2, MRR (0.5+0)/2.
# A1 over Q1,Q2: recall (0.5+0)/2, precision (0.1+0)/2, all-gold (0+0)/2, MRR (1+0)/2; one failed.

CALLS = [
    {"task": "build_l1", "cost_usd": 0.01, "input_tokens": 100, "cache_read_input_tokens": 50, "output_tokens": 20, "latency_s": 1.0},
    {"task": "build_l1", "cost_usd": 0.02, "input_tokens": 200, "output_tokens": 30, "latency_s": 2.0},
    {"task": "build_l1", "cost_usd": None, "input_tokens": 10, "output_tokens": 5, "latency_s": 3.0},      # cost not reported
    {"task": "holder", "cost_usd": 0.5, "input_tokens": 1000, "cache_creation_input_tokens": 100, "output_tokens": 100, "latency_s": 4.5},
]


def write(path, rows, tail=""):
    Path(path).write_text("".join(json.dumps(r) + "\n" for r in rows) + tail)


class QuestionMetrics(unittest.TestCase):
    def test_hits_and_rank(self):
        m = metrics.question_metrics(["x", "g1", "y", "g2"], ["g1", "g2"])
        self.assertEqual(m, {"recall": 1.0, "precision": 0.2, "all_gold": 1.0, "mrr": 0.5})

    def test_gold_below_the_cut_off_does_not_count(self):
        m = metrics.question_metrics(PAD + ["g1"], ["g1"])
        self.assertEqual(m, {"recall": 0.0, "precision": 0.0, "all_gold": 0.0, "mrr": 0.0})


class ArmMetrics(unittest.TestCase):
    def test_hand_computed_means_with_a_failure_and_an_abstention(self):
        got = metrics.arm_metrics(DONE)
        a0, a1 = got["A0"], got["A1"]
        self.assertEqual((a0["n"], a0["failed"], a1["n"], a1["failed"]), (2, 0, 2, 1))
        self.assertEqual([a0[k] for k in ("recall@10", "precision@10", "all-gold@10", "mrr@10")], [0.5, 0.1, 0.5, 0.25])
        self.assertEqual([a1[k] for k in ("recall@10", "precision@10", "all-gold@10", "mrr@10")], [0.25, 0.05, 0.0, 0.5])

    def test_broken_variant_that_drops_failed_recalls_would_differ(self):
        # the figure with the failed recall dropped (A1 over Q1 only) is 0.5, not the registered 0.25: the test can tell them apart
        self.assertNotEqual(metrics.arm_metrics(DONE[:1])["A1"]["recall@10"], metrics.arm_metrics(DONE)["A1"]["recall@10"])


class CallStats(unittest.TestCase):
    def test_per_task_cost_tokens_latency(self):
        got = metrics.call_stats(CALLS)
        b = got["build_l1"]
        self.assertEqual((b["calls"], b["cost_usd"], b["cost_usd_rows"]), (3, 0.03, 2))
        self.assertEqual((b["input_tokens"], b["output_tokens"]), (100 + 50 + 200 + 10, 55))      # cache tokens count as input
        self.assertEqual((b["latency_median_s"], b["latency_p90_s"]), (2.0, 3.0))
        h = got["holder"]
        self.assertEqual((h["calls"], h["cost_usd"], h["input_tokens"], h["latency_median_s"]), (1, 0.5, 1100, 4.5))


class Cli(unittest.TestCase):
    def run_main(self, *argv):
        out = io.StringIO()
        with redirect_stdout(out):
            rc = metrics.main(list(argv))
        return rc, out.getvalue()

    def test_json_output_reads_done_and_calls_and_ignores_a_torn_last_line(self):
        with tempfile.TemporaryDirectory() as d:
            write(Path(d) / "done.jsonl", DONE, tail='{"qref": "q4", "gold": ["g')            # an interrupted run's torn row
            write(Path(d) / "calls.jsonl", CALLS)
            rc, text = self.run_main(d, "--json")
            doc = json.loads(text)
            self.assertEqual(rc, 0)
            self.assertEqual(doc["arms"]["A0"]["n"], 2)
            self.assertEqual(sorted(doc["calls_by_task"]), ["build_l1", "holder"])
            rc, text = self.run_main(d)
            self.assertIn("recall@10", text)
            self.assertIn("build_l1", text)
            self.assertIn("(cost on 2 of 3 calls)", text)

    def test_calls_are_optional_and_a_missing_ledger_is_refused(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(self.run_main(d)[0], 2)
            write(Path(d) / "done.jsonl", DONE)
            rc, text = self.run_main(d, "--json")
            self.assertEqual((rc, json.loads(text)["calls_by_task"]), (0, None))

    def test_a_bad_line_before_the_end_is_an_error(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "done.jsonl").write_text('{"qref": "q1"\n' + json.dumps(DONE[0]) + "\n")
            with self.assertRaises(ValueError):
                self.run_main(d)


if __name__ == "__main__":
    unittest.main()
