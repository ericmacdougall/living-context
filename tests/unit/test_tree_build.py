"""BUILD-PLAN Step 9: bulk tree build, one family, one class (AC-4). Pure, stub model only."""
import random
import tempfile
import unittest
from pathlib import Path

from living_context.config import Constants
from living_context.embed import stub
from living_context.ingest import title
from living_context.model.budget import Budget
from living_context.model.cache import DictStore
from living_context.model.port import ModelPort
from living_context.model.stub import StubBackend
from living_context.policy import ClassLattice
from living_context.structure import build, cluster

C = Constants()
LAT = ClassLattice(["base"], [])


def fixture(n=2000, topics=100, seed=7):
    rng = random.Random(seed)
    vocab = [[f"w{t}x{j}" .replace("0", "o").replace("1", "i").replace("2", "t").replace("3", "h").replace("4", "f")
              .replace("5", "v").replace("6", "s").replace("7", "e").replace("8", "g").replace("9", "n") + "ab" for j in range(10)]
             for t in range(topics)]
    out = []
    for i in range(n):
        words = rng.sample(vocab[i % topics], 6) + ["shared", "common", "noise"]
        text = " ".join(words) + "."
        out.append(build.LeafRec(f"{i + 1:032x}", 1, text, title.build_title(text), stub.embed(text), 20 + (i % 7)))
    return out


def port(tmp):
    return ModelPort(StubBackend(), Budget(Path(tmp) / "b", 10_000), LAT, DictStore())


class TreeBuild(unittest.TestCase):
    leaves = fixture()
    _default = None

    def build(self, **kw):
        if not kw and TreeBuild._default is not None:
            return TreeBuild._default
        with tempfile.TemporaryDirectory() as d:
            t = build.build_tree(self.leaves, port(d), "tena", "base", **kw)
        if not kw:
            TreeBuild._default = t
        return t

    def check_shape(self, tree):
        nodes = list(tree.nodes.values())
        over = [n for n in nodes if n.child_count > C.f_max]
        self.assertEqual(over, [], "a node has more than F_MAX children")
        for level in {n.level for n in nodes}:
            small = [n for n in nodes if n.level == level and n.child_count < C.f_min]
            self.assertLessEqual(len(small), 1, f"more than one node under F_MIN at level {level}")
        self.assertLessEqual(len(tree.top), C.f_max, "the top level has more than F_MAX nodes")

    def test_two_thousand_leaves_respect_the_fan_out_limits(self):
        tree = self.build()
        self.check_shape(tree)
        self.assertEqual(sorted({n.level for n in tree.nodes.values()}), [1, 2])
        self.assertEqual(tree.levels, 2)
        self.assertEqual(len(tree.top), 8)

    def test_broken_no_index_of_indexes_gives_a_125_node_top(self):
        tree = self.build(index_of_indexes=False)
        self.assertEqual(len(tree.top), 125)
        with self.assertRaises(AssertionError) as cm:
            self.check_shape(tree)
        self.assertIn("top level has more than F_MAX", str(cm.exception))

    def test_broken_unbalanced_clustering_overfills_a_node(self):
        tree = self.build(grouper=cluster.kmeans_groups)
        with self.assertRaises(AssertionError) as cm:
            self.check_shape(tree)
        self.assertTrue("more than F_MAX children" in str(cm.exception) or "under F_MIN" in str(cm.exception))

    def test_sizes_and_totals_equal_the_sum_of_leaf_sizes(self):
        tree = self.build()
        want = sum(l.size_tokens for l in self.leaves)
        self.assertEqual(sum(tree.nodes[r].total_tokens for r in tree.top), want)
        for n in tree.nodes.values():
            self.assertEqual(n.total_tokens, sum(c["total_tokens"] for c in n.children))
        l1 = [n for n in tree.nodes.values() if n.level == 1]
        self.assertEqual(sorted(c["child_id"] for n in l1 for c in n.children), sorted(l.leaf_id for l in self.leaves))

    def test_every_title_is_code_built_and_lineage_is_set(self):
        tree = self.build()
        for n in tree.nodes.values():
            self.assertEqual(n.title, title.build_title(n.summary))
            self.assertFalse(any(ch.isdigit() for ch in n.title))
            self.assertTrue(n.lin_leaf_ids or n.lin_nodes)
        up = [n for n in tree.nodes.values() if n.level == 2]
        self.assertTrue(all(n.lin_nodes and not n.lin_leaf_ids for n in up))  # upper nodes reach leaves transitively

    def test_a_single_upper_node_is_dropped_and_a_failed_call_aborts(self):
        small = [l for l in self.leaves[:200]]
        with tempfile.TemporaryDirectory() as d:
            tree = build.build_tree(small, port(d), "tena", "base")
        self.assertEqual(tree.levels, 1)  # 13 level-1 nodes fit the top
        self.assertEqual(len(tree.top), 13)

        class Boom(StubBackend):
            def _build_l1(self, i):
                raise RuntimeError("down")

        with tempfile.TemporaryDirectory() as d:
            p = ModelPort(Boom(), Budget(Path(d) / "b", 100), LAT, DictStore())
            with self.assertRaises(build.BuildFailed):
                build.build_tree(small, p, "tena", "base")

    def test_groups_are_balanced_to_within_one(self):
        g = cluster.balanced_groups([l.embedding for l in self.leaves[:50]], 16)
        self.assertEqual(sorted(len(x) for x in g), [12, 12, 13, 13])


if __name__ == "__main__":
    unittest.main()
