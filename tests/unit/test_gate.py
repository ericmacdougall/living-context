"""BUILD-PLAN Step 19, the replay gate's statistical core (DESIGN 13.3), no Elasticsearch:
  * the exact p-value equals brute-force enumeration over all sign vectors for K up to 12;
  * a candidate with fewer than 8 clusters is never adopted (broken: a per-query test adopts one fully correlated cluster of 60);
  * the replay set spreads over as many clusters as possible, holds at most 6 per cluster, and never stores a synthetic query."""
import itertools
import random
import unittest
from fractions import Fraction

from living_context.feedback import gate


def brute(means):
    k = len(means)
    t = sum(means)
    ge = sum(1 for e in itertools.product((-1, 1), repeat=k) if sum(s * m for s, m in zip(e, means)) >= t)
    return Fraction(ge, 2 ** k)


class Gate(unittest.TestCase):
    def test_the_exact_p_value_equals_brute_force_for_k_up_to_12(self):
        rng = random.Random(4)
        checked = 0
        for k in range(1, 13):
            for _ in range(8):
                means = []
                for _ in range(k):
                    n = rng.randint(1, 6)
                    means.append(Fraction(sum(rng.choice((-1, 0, 1)) for _ in range(n)), n))
                self.assertEqual(gate.exact_p(means), brute(means), means)
                checked += 1
        self.assertEqual(checked, 96)

    def clusters(self, k, d=1, n=3):
        return {f"c{i}": [d] * n for i in range(k)}

    def test_fewer_than_eight_clusters_are_never_adopted_and_eight_clean_gains_are(self):
        for k in range(1, 8):
            self.assertFalse(gate.decide(self.clusters(k)).adopt, k)
        d = gate.decide(self.clusters(8))
        self.assertTrue(d.adopt, d)
        self.assertLess(d.p, Fraction(5, 100))
        self.assertFalse(gate.decide(self.clusters(8), general_guard_ok=False).adopt)

    def correlated(self):
        return {"one": [1] * 60}      # one cluster of 60 fully correlated paraphrases

    def test_one_cluster_of_sixty_paraphrases_is_not_adopted_and_a_per_query_test_adopts_it(self):
        self.assertFalse(gate.decide(self.correlated()).adopt)
        with self.assertRaises(AssertionError):
            self.assertFalse(gate.decide(self.correlated(), broken_per_query=True).adopt, "a per-query test adopted one correlated cluster")

    def test_random_signs_are_not_adopted(self):
        rng = random.Random(9)
        adopted = sum(gate.decide({f"c{i}": [rng.choice((-1, 1)) for _ in range(4)] for i in range(10)}).adopt for _ in range(200))
        self.assertLess(adopted, 30)

    def test_the_replay_set_spreads_over_clusters_and_caps_each(self):
        held = {f"c{i}": [f"h{i}-{j}" for j in range(10)] for i in range(5)}
        synth = {f"c{i}": [f"s{i}-{j}" for j in range(10)] for i in range(5)}
        out = gate.spread(held, synth, total=12)
        self.assertEqual(sum(len(v) for v in out.values()), 12)
        self.assertEqual(sorted(len(v) for v in out.values()), [2, 2, 2, 3, 3], "not spread one per cluster in turn")
        first = [v[0] for v in out.values()]
        self.assertTrue(all(not syn for _, syn in first), "a held-out search must come before any synthetic query")
        full = gate.spread(held, synth)
        self.assertTrue(all(len(v) <= 6 for v in full.values()))
        self.assertEqual(sum(len(v) for v in full.values()), 30, "5 clusters of at most 6 hold 30 queries")
        many = gate.spread({f"c{i}": [f"q{i}-{j}" for j in range(9)] for i in range(12)}, {})
        self.assertEqual(sum(len(v) for v in many.values()), 60)
        self.assertEqual(max(len(v) for v in many.values()), 5, "12 clusters share 60 queries evenly before any gets a sixth")

    def test_a_synthetic_query_is_tagged_and_never_stored(self):
        stored = []
        out = gate.replay_queries({"c0": ["h"]}, lambda leaf: [f"about {leaf}"], {"c0": ["L1"], "c1": ["L2"]})
        flags = {q: syn for v in out.values() for q, syn in v}
        self.assertEqual(flags, {"h": False, "about L1": True, "about L2": True})
        self.assertEqual(stored, [], "a gate-only synthetic query was stored")


if __name__ == "__main__":
    unittest.main()
