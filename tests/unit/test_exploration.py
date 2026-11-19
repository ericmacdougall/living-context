"""Exploration slots (BUILD-PLAN Step 11): after the ranked list, readable classes only, never the last L insert batches,
and a logged propensity that matches the real inclusion frequency. Each broken variant must turn its check red."""
import collections
import random
import unittest

from living_context.feedback.explore import Candidate, place, slots_for
from living_context.feedback.log import Offer

RANKED = [Offer(f"{i:032x}", 1, "base", ("tree",), i) for i in range(9)]
POOL = [Candidate(f"{100 + i:032x}", 1, "base" if i % 5 else "restricted", batch=i // 4) for i in range(60)]
KW = dict(readable_classes=["base"], current_batch=14, lag=5, share=0.1)


class Exploration(unittest.TestCase):
    def run_once(self, seed=1, **flags):
        return place(RANKED, POOL, rng=random.Random(seed), **{**KW, **flags})

    def check_rules(self, **flags):
        out = self.run_once(**flags)
        ex = [o for o in out if o.exploration]
        self.assertEqual(len(out), 10)
        self.assertEqual(len(ex), 1, "one slot in ten")
        self.assertTrue(all(o.rank > max(r.rank for r in out if not r.exploration) for o in ex), "an exploration slot sits before the ranked list")
        bybatch = {c.leaf_id: c for c in POOL}
        for o in ex:
            self.assertEqual(bybatch[o.leaf_id].cls, "base", "an exploration slot offers a class the reader cannot read")
            self.assertLessEqual(bybatch[o.leaf_id].batch, 14 - 5, "an exploration slot offers a leaf of the last L insert batches")

    def test_the_rules_hold_for_many_seeds(self):
        for seed in range(200):
            out = self.run_once(seed)
            self.assertEqual([o.leaf_id for o in out[:9]], [r.leaf_id for r in RANKED])
        self.check_rules()

    def test_broken_recent_leaves_included_is_red(self):
        with self.assertRaises(AssertionError) as cm:
            for seed in range(100):
                self.check_rules_seed(seed, broken_include_recent=True)
        self.assertIn("last L insert batches", str(cm.exception))

    def test_broken_any_class_is_red(self):
        with self.assertRaises(AssertionError) as cm:
            for seed in range(100):
                self.check_rules_seed(seed, broken_any_class=True)
        self.assertIn("cannot read", str(cm.exception))

    def test_broken_slot_before_the_ranked_list_is_red(self):
        with self.assertRaises(AssertionError) as cm:
            self.check_rules(broken_before_ranked=True)
        self.assertIn("before the ranked list", str(cm.exception))

    def check_rules_seed(self, seed, **flags):
        out = self.run_once(seed, **flags)
        by = {c.leaf_id: c for c in POOL}
        for o in out:
            if o.exploration:
                self.assertEqual(by[o.leaf_id].cls, "base", "an exploration slot offers a class the reader cannot read")
                self.assertLessEqual(by[o.leaf_id].batch, 14 - 5, "an exploration slot offers a leaf of the last L insert batches")

    # ---- the propensity ----------------------------------------------------------------------------------------
    def inclusion(self, **flags):
        n, hits, prop = 4000, collections.Counter(), {}
        for seed in range(n):
            for o in self.run_once(seed, **flags):
                if o.exploration:
                    hits[o.leaf_id] += 1
                    prop[o.leaf_id] = o.propensity
        return n, hits, prop

    def check_propensity(self, **flags):
        n, hits, prop = self.inclusion(**flags)
        for leaf, p in prop.items():
            freq = hits[leaf] / n
            self.assertAlmostEqual(freq, p, delta=0.012, msg=f"logged propensity {p:.3f} but the leaf was shown with frequency {freq:.3f}")

    def test_the_logged_propensity_matches_the_inclusion_frequency(self):
        self.check_propensity()

    def test_broken_propensity_one_is_red(self):
        with self.assertRaises(AssertionError) as cm:
            self.check_propensity(broken_propensity_one=True)
        self.assertIn("logged propensity", str(cm.exception))

    def test_slot_count_is_a_share_of_the_final_digest(self):
        self.assertEqual([slots_for(n, 0.1) for n in (4, 9, 18, 27)], [0, 1, 2, 3])


if __name__ == "__main__":
    unittest.main()
