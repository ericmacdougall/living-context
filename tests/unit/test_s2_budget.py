"""The S2 call-count arithmetic against the real builder and the real fan-out (stub model): the predicted build calls, levels and top
width equal what `build_tree` does, and a recall's holder and organise calls stay inside the predicted band."""
import random
import tempfile
import unittest
from pathlib import Path

from evalkit import arms, corpus, fixture, s2_budget
from living_context.config import Constants
from living_context.model.budget import Budget
from living_context.model.cache import DictStore
from living_context.model.port import ModelPort
from living_context.model.stub import StubBackend
from living_context.policy import ClassLattice
from living_context.recall.view import MemView
from living_context.structure import build
from living_context.structure.build import LeafRec

C = Constants()


def port(d):
    return ModelPort(StubBackend(), Budget(Path(d) / "b", 10 ** 8), ClassLattice(["base"], []), DictStore())


def leaves(n, seed=3):
    rng = random.Random(seed)
    return [LeafRec("%032x" % i, 1, "note %d %s" % (i, " ".join(rng.choice(["alpha", "beta", "gamma", "delta"]) for _ in range(6))),
                    "t%d" % i, [rng.random() for _ in range(8)], 10) for i in range(n)]


class Shape(unittest.TestCase):
    def test_predicted_build_calls_levels_and_top_equal_the_builder(self):
        for n in (1, 5, 16, 17, 100, 383, 384, 385, 400, 1000, 2000):
            with tempfile.TemporaryDirectory() as d:
                t = build.build_tree(leaves(n), port(d), "t", "base", consts=C)
            s = s2 = s2_budget.tree_shape(n, C)
            self.assertEqual(t.calls, s["l1_calls"] + s["upper_calls"], n)
            self.assertEqual((t.levels, len(t.top)), (s2["levels"], s2["top"]), n)

    def test_a_tree_off_by_one_in_the_rule_would_be_caught(self):
        with tempfile.TemporaryDirectory() as d:
            t = build.build_tree(leaves(400), port(d), "t", "base", consts=C)
        wrong = s2_budget.tree_shape(400, Constants(f_target=17))     # a different fan-out gives a different count
        self.assertNotEqual(t.calls, wrong["l1_calls"] + wrong["upper_calls"])

    def test_recall_calls_stay_inside_the_band(self):
        fx = fixture.make("advantage", 12, mates=15)
        mc = corpus.MemCorpus(fx.sessions)
        with tempfile.TemporaryDirectory() as d:
            tree = build.build_tree(mc.leaves, port(d), "t", "base", consts=C)
            shape = s2_budget.tree_shape(len(mc.leaves), C)
            band = s2_budget.recall_band(shape["top"], shape["levels"], C)
            view = MemView(tree)
            from living_context.recall.fanout import Recall
            for q in fx.questions:
                r = Recall(port(d), "t", "base", "px", consts=C, workers=1).recall(fx.queries[q.qid], view)
                self.assertGreaterEqual(r.counters.holder_calls, band["route_min"])
                self.assertLessEqual(r.counters.holder_calls, band["route_max"])
                self.assertLessEqual(r.counters.organise_calls, band["organise_max"])


if __name__ == "__main__":
    unittest.main()
