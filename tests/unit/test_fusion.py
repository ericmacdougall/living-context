"""BUILD-PLAN Step 7 (pure): reciprocal rank fusion with k 60 and window 50."""
import unittest

from living_context.recall.fusion import K_RRF, WINDOW, rrf


class Fusion(unittest.TestCase):
    def test_known_values(self):
        out = dict(rrf([["a", "b"], ["b", "c"]]))
        self.assertAlmostEqual(out["a"], 1 / 61)
        self.assertAlmostEqual(out["b"], 1 / 62 + 1 / 61)
        self.assertAlmostEqual(out["c"], 1 / 62)
        self.assertEqual([i for i, _ in rrf([["a", "b"], ["b", "c"]])], ["b", "a", "c"])

    def test_only_the_window_counts(self):
        long = [f"x{i}" for i in range(80)]
        got = dict(rrf([long]))
        self.assertIn("x49", got)
        self.assertNotIn("x50", got)
        self.assertEqual((K_RRF, WINDOW), (60, 50))

    def test_ties_break_by_id_and_a_repeat_in_one_list_counts_once(self):
        self.assertEqual([i for i, _ in rrf([["b"], ["a"]])], ["a", "b"])
        self.assertAlmostEqual(dict(rrf([["a", "a", "a"]]))["a"], 1 / 61)

    def test_one_list_alone_keeps_its_order(self):
        self.assertEqual([i for i, _ in rrf([["q", "r", "s"]])], ["q", "r", "s"])


if __name__ == "__main__":
    unittest.main()
