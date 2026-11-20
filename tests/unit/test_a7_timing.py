"""A7 timing harness (tools/a7_timing.py): the statistic is right, a delayed corpus is flagged, identical corpora are not, and
the interleaving alternates the two sides."""
import random
import unittest

from tools import a7_timing as a7


class Harness(unittest.TestCase):
    def test_ks_statistic_matches_hand_computed_values(self):
        self.assertEqual(a7.ks_statistic([1, 2, 3], [1, 2, 3]), 0.0)
        self.assertEqual(a7.ks_statistic([1, 2, 3], [4, 5, 6]), 1.0)
        self.assertAlmostEqual(a7.ks_statistic([1, 2, 3, 4], [3, 4, 5, 6]), 0.5)
        self.assertAlmostEqual(a7.ks_statistic([1, 1, 2], [1, 2, 2]), 1 / 3, places=9)
        with self.assertRaises(ValueError):
            a7.ks_statistic([], [1])

    def test_the_median_difference_is_relative(self):
        self.assertAlmostEqual(a7.median_difference([10, 10, 10], [9, 9, 9]), 0.1)
        self.assertEqual(a7.median_difference([0, 0], [0, 0]), 0.0)

    def sample(self, scale, seed):
        rng = random.Random(seed)
        return [scale * rng.gauss(10, 1) for _ in range(2000)]

    def test_identical_corpora_are_not_flagged_and_a_delayed_corpus_is(self):
        self.assertFalse(a7.compare(self.sample(1, 1), self.sample(1, 2))["disclosed_timing_channel"])
        self.assertTrue(a7.compare(self.sample(1, 1), self.sample(1.05, 2))["disclosed_timing_channel"])

    def test_broken_comparison_that_looks_at_the_median_only_misses_a_spread_difference(self):
        rng = random.Random(3)
        a = [rng.gauss(10, 0.2) for _ in range(2000)]
        b = [rng.gauss(10, 3.0) for _ in range(2000)]   # same median, a very different distribution
        self.assertLess(a7.median_difference(a, b), a7.MEDIAN_LIMIT, "premise: the medians agree")
        self.assertTrue(a7.compare(a, b)["disclosed_timing_channel"], "the KS statistic must catch the spread")

    def test_the_two_sides_are_interleaved(self):
        order = []
        ta, tb = a7.measure(lambda i: order.append(("a", i)), lambda i: order.append(("b", i)), 3)
        self.assertEqual(order, [("a", 0), ("b", 0), ("a", 1), ("b", 1), ("a", 2), ("b", 2)])
        self.assertEqual((len(ta), len(tb)), (3, 3))


if __name__ == "__main__":
    unittest.main()
