"""The LoCoMo report regenerates its tables from a run directory alone: hand-computed means on a four-question synthetic run, the
failed-recall rule, the paired differences, the A0 equality check and the completion counts."""
import json
import re
import tempfile
import unittest
from pathlib import Path

from evalkit import locomo_report as rep

L0 = ["L%d" % i for i in range(6)]
M = ["M%d" % i for i in range(3)]
SESS0 = dict(zip(L0, ["D1", "D1", "D2", "D2", "D3", "D3"]))
SESS1 = dict(zip(M, ["D1", "D1", "D2"]))
QS = {
    "c0#0": dict(conv=0, category=1, gold_leaves=["L2"], gold_sessions=["D2"], cluster="c0|D2"),
    "c0#1": dict(conv=0, category=2, gold_leaves=["L0", "L4"], gold_sessions=["D1", "D3"], cluster="c0|D1"),
    "c1#0": dict(conv=1, category=4, gold_leaves=["M2"], gold_sessions=["D2"], cluster="c1|D2"),
    "c1#1": dict(conv=1, category=4, gold_leaves=["M0"], gold_sessions=["D1"], cluster="c1|D1"),
}
A0 = {"c0#0": ["L0", "L2", "L1"], "c0#1": L0, "c1#0": ["M0", "M1", "M2"], "c1#1": ["M2", "M1"]}
A1 = {"c0#0": ["L2", "L0"], "c0#1": L0, "c1#0": ["M2"], "c1#1": ["M0"]}
TREE = {"c0#0": ["L4"], "c0#1": ["L0", "L4"], "c1#0": ["M2"], "c1#1": []}


def write_run(d: Path, *, failed_tree=()):
    d.mkdir(parents=True, exist_ok=True)
    qs = {}
    for qid, q in QS.items():
        qs[qid] = {"qid": qid, "sample_id": "c%d" % q["conv"], "cluster_conv": "c%d" % q["conv"], "pool_leaves": 6 if q["conv"] == 0 else 3,
                   "gold_turns": [], "n_unresolved": 0, **q}
    (d / "manifest.json").write_text(json.dumps({"questions": qs, "pools": {"0": 6, "1": 3}}))
    (d / "leafmaps").mkdir(exist_ok=True)
    for i, sess in ((0, SESS0), (1, SESS1)):
        (d / "leafmaps" / ("%d.json" % i)).write_text(json.dumps({"leaves": [{"id": k, "session": v, "turns": []} for k, v in sess.items()]}))
    (d / "a0.jsonl").write_text("".join(json.dumps({"qid": q, "leaves": A0[q], "wall_s": 0.05}) + "\n" for q in QS))
    (d / "subset_B1.json").write_text(json.dumps({"order": list(QS), "shuffled": ["c0#0"]}))
    rows = []
    for n, q in enumerate(QS, 1):
        f = q in failed_tree
        arms = {"A0": {"leaves": A0[q], "failed": False}, "A1": {"leaves": [] if f else A1[q], "failed": f},
                "A1-tree-only": {"leaves": [] if f else TREE[q], "failed": f}}
        row = {"qid": q, "order": n, "arms": arms, "tree_wall_s": 10.0 + n, "tree_calls": 4, "tree_call_wall_s": 9.0, "holder_calls": 3,
               "direct_wall_s": 0.1}
        if q == "c0#0":
            arms["SHUFFLED"] = {"leaves": [], "failed": False}
            row["shuffled"] = {"wall_s": 5.0, "calls": 2, "call_wall_s": 4.0}
        rows.append(row)
    (d / "done.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
    (d / "calls.jsonl").write_text("".join(json.dumps({"task": t, "strict_ok": True, "latency_s": 2.0, "input_tokens": 10, "output_tokens": 5}) + "\n"
                                           for t in ("route", "route", "build_l1")))
    (d / "STATE.json").write_text(json.dumps({"state": "COMPLETE", "counter": 3, "cap": 9000, "models_reported": ["m"]}))
    (d / "BUILT_0.json").write_text(json.dumps({"build_calls": 1, "seconds": 3.0, "nodes": 3}))
    (d / "BUILT_1.json").write_text(json.dumps({"build_calls": 1, "seconds": 3.0, "nodes": 4}))      # conv 1 has 4 nodes: its rows (3 holder calls) opened fewer


class T(unittest.TestCase):
    def run_report(self, **kw):
        self._t = tempfile.TemporaryDirectory()
        self.addCleanup(self._t.cleanup)
        d = Path(self._t.name)
        write_run(d, **kw)
        run = rep.Run(d)
        return rep.build(run, 200), run

    def mean(self, res, arm, m, batch="B1"):
        return res["batches"][batch]["arms"][arm][m]["mean"]

    def test_hand_computed_means(self):
        res, _ = self.run_report()
        self.assertAlmostEqual(self.mean(res, "A0", "leaf_r5"), 0.75)                  # misses only c1#1
        self.assertAlmostEqual(self.mean(res, "A0", "mrr10"), (0.5 + 1 + 1 / 3 + 0) / 4)
        self.assertAlmostEqual(self.mean(res, "A1", "mrr10"), 1.0)
        # tree-only: c0#0 misses (0), c0#1 holds both gold leaves (1), c1#0 hits (1), c1#1 returned an empty list (0, not padded)
        self.assertAlmostEqual(self.mean(res, "A1-tree-only", "leaf_r10"), 0.5)
        # session level: every question finds its gold session (c1#1: M2 then M1, and M1 is in D1), unlike the leaf level (0.75)
        self.assertAlmostEqual(self.mean(res, "A0", "sess_r5"), 1.0)
        self.assertAlmostEqual(res["a0_all"]["metrics"]["sess_r5"]["mean"], 1.0)
        self.assertAlmostEqual(res["a0_all"]["metrics"]["leaf_r5"]["mean"], 0.75)

    def test_all_gold_only_over_questions_with_two_gold(self):
        res, _ = self.run_report()
        c = res["batches"]["B1"]["arms"]["A0"]["allgold10"]
        self.assertEqual(c["n"], 1)                                                    # only c0#1 has two gold leaves
        self.assertEqual(c["mean"], 1.0)
        self.assertEqual(res["batches"]["B1"]["arms"]["A1-tree-only"]["allgold10"]["mean"], 1.0)   # its list holds L0 and L4

    def test_paired_difference_and_counts(self):
        res, _ = self.run_report()
        d = res["batches"]["B1"]["paired"]["A1 minus A0"]["mrr10"]
        self.assertAlmostEqual(d["mean"], 1.0 - (0.5 + 1 + 1 / 3 + 0) / 4)
        self.assertEqual(d["n"], 4)
        self.assertEqual(res["batches"]["B1"]["a0_recall_time_equals_a0_all"], {"same": 4, "of": 4})
        self.assertEqual(res["completion"]["b1_completed"], 4)
        self.assertEqual(res["completion"]["shuffled_completed"], 1)

    def test_routing_line_counts_recalls_that_opened_every_node(self):
        res, run = self.run_report()
        self.assertEqual(res["batches"]["B1"]["routing"], {"holder_calls_equal_node_count": 2, "of": 4})     # only the two conv-0 rows
        self.assertIn("Routing: in 2 of 4 recalls", rep.render(res, run))

    def test_a_failed_recall_scores_zero_and_is_counted_not_dropped(self):
        res, _ = self.run_report(failed_tree=("c0#1",))
        b = res["batches"]["B1"]
        self.assertEqual(b["n"], 4)
        self.assertEqual(b["tree_failed_recalls"], 1)
        self.assertAlmostEqual(b["arms"]["A1"]["leaf_r10"]["mean"], (1 + 0 + 1 + 1) / 4)       # c0#1 contributes 0, not skipped

    def test_a0_mismatch_is_reported(self):
        res, run = self.run_report()
        run.a0["c0#0"]["leaves"] = ["L5"]
        res = rep.build(run, 50)
        self.assertEqual(res["batches"]["B1"]["a0_recall_time_equals_a0_all"]["same"], 3)

    def test_markdown_renders_and_has_no_long_dashes(self):
        res, run = self.run_report()
        md = rep.render(res, run)
        self.assertIn("| A0 on all eligible questions: 4 | 4 |", md)
        self.assertIn("B1 (A1 and A1-tree-only on the seeded stratified subset): 4 | 4", md)
        self.assertIsNone(re.search("[–—]", md))
        self.assertIn("chance reference", md)


if __name__ == "__main__":
    unittest.main()
