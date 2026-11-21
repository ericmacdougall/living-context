"""The S6-H1 memory-proof tool at SMALL scale (the full-scale run, about 85,000 leaves and 11 GB, is an operator step on a quiet run
host; docs/RUN-S6H1.md): the report is a valid `--mem-proof` for the code under test, its tree is the one the count rule predicts, and
the runner's proof check accepts it only when it covers the expected pool."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from evalkit import run_s6h1 as rs, s2_budget, s6h1_freeze as fz, s6h1_mem as mem
from living_context.config import Constants


class SmallScale(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.rep = mem.measure(600, 384, 400)

    def test_the_report_names_the_code_and_the_pool_shape(self):
        r = self.rep
        self.assertEqual((r["leaves"], r["dim"], r["tool"]), (600, 384, "evalkit.s6h1_mem"))
        self.assertEqual(r["code_sha256"], fz.code_digest())
        self.assertGreater(r["peak_rss_mb"], 0)

    def test_the_tree_is_the_one_the_count_rule_predicts(self):
        shape = s2_budget.tree_shape(600, Constants())
        self.assertEqual(self.rep["build_calls"], shape["l1_calls"] + shape["upper_calls"])
        self.assertEqual(self.rep["level1_groups"], shape["l1_calls"])
        self.assertEqual(self.rep["cluster_n"][0], 600)

    def test_the_report_records_the_clusterer_workers_it_ran_with(self):
        self.assertEqual(self.rep["cluster_workers"], 1)
        from living_context.structure import cluster
        orig = cluster.balanced_groups
        with mock.patch.object(cluster, "balanced_groups", side_effect=lambda v, f, **kw: orig(v, f, **kw)) as spy:
            rep = mem.measure(300, 384, 200, cluster_workers=3)
        self.assertEqual(rep["cluster_workers"], 3)
        self.assertEqual({c.kwargs.get("workers") for c in spy.call_args_list}, {3})

    def test_the_runner_accepts_the_report_only_when_it_covers_the_expected_pool(self):
        p = Path(tempfile.mkdtemp()) / "proof.json"
        p.write_text(json.dumps(self.rep))
        with self.assertRaises(ValueError):
            rs.mem_proof(p)                                  # 600 < 78,700 expected leaves
        with mock.patch.object(rs, "EXPECTED_LEAVES", 600):
            self.assertEqual(rs.mem_proof(p)["leaves"], 600)


if __name__ == "__main__":
    unittest.main()
