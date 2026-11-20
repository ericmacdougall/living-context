"""BUILD-PLAN Step 18 on the stub, no Elasticsearch: the reverse audit.

  * a planted mis-filed leaf whose two past queries were logged in different cycles and not offered by the tree gets a miss rate of
    1 with both queries listed and is flagged (broken: candidates of the current cycle only, so support never reaches 2; broken:
    support threshold 5; broken: audit against synthetic queries only misses the real query);
  * the audit's judge calls per cycle never exceed N_AUDIT (broken: uncapped, counted);
  * with synthetic queries off, a leaf with no search history is never re-filed (broken: synthetic on re-files it);
  * the priority rule sees no gold; the Wilson interval is right."""
import unittest

from living_context.config import Constants
from living_context.embed import stub as embed_stub
from living_context.feedback import reverse_audit as ra

LEAVES = {"L1": "orchard keeper lantern tonight", "L2": "zebra quokka marathon", "H": "volcano ferry timetable"}


def percolate(leaf, queries):
    words = set(leaf.split())
    return {qid: 1.0 for qid, q in queries.items() if set(q.split()) <= words}


def knn(leaf, queries):
    v = embed_stub.embed(leaf)
    return {qid: c for qid, q in queries.items() if (c := embed_stub.cosine(v, embed_stub.embed(q))) >= 0.99}


def tree_offered(sid, leaf):
    return leaf != "L1"          # the tree keeps missing L1 for the searches that match it


class ReverseAudit(unittest.TestCase):
    def two_cycles(self, **kw):
        collect_kw = {k: kw.pop(k) for k in ("broken_current_cycle_only",) if k in kw}
        ledger, last = ra.Ledger(), None
        judge = lambda sid, leaf: True
        for cycle, (qid, q) in enumerate([("q1", "orchard keeper"), ("q2", "keeper lantern")], start=1):
            ra.collect(ledger, LEAVES, {qid: q}, cycle, percolate=percolate, knn=knn, offered_by_tree=tree_offered, **collect_kw)
            last = ra.run_cycle(ledger, judge, **kw)
        return ledger, last

    def check_flagged(self, **kw):
        _, res = self.two_cycles(**kw)
        r = res.reports["L1"]
        self.assertTrue(r.flagged, f"the planted mis-filed leaf was not flagged (missed {r.missed} of {r.relevant})")
        self.assertEqual((r.miss_rate, r.missed_searches), (1.0, ("q1", "q2")))
        self.assertGreater(r.interval[0], 0.3)
        self.assertFalse(res.reports["L2"].flagged if "L2" in res.reports else False)

    def test_a_leaf_missed_for_two_searches_of_different_cycles_is_flagged_with_both_listed(self):
        self.check_flagged()

    def test_broken_candidates_of_the_current_cycle_only_never_reach_the_support(self):
        with self.assertRaises(AssertionError) as cm:
            self.check_flagged(broken_current_cycle_only=True)
        self.assertIn("was not flagged", str(cm.exception))

    def test_broken_support_threshold_of_five_never_flags_it(self):
        with self.assertRaises(AssertionError) as cm:
            self.check_flagged(s_min=5)
        self.assertIn("was not flagged", str(cm.exception))

    def test_broken_audit_against_synthetic_queries_only_misses_the_real_query(self):
        with self.assertRaises(AssertionError) as cm:
            self.check_flagged(broken_synthetic_only=True)
        self.assertIn("was not flagged", str(cm.exception))

    def calls(self, **kw):
        ledger = ra.Ledger()
        for i in range(150):
            for j in range(5):
                ledger.add(f"leaf{i:03d}", ra.Cand(f"s{i}-{j}", 1.0 - j / 10, 1, offered_by_tree=False))
        return ra.run_cycle(ledger, lambda sid, leaf: True, **kw)

    def check_cap(self, **kw):
        res = self.calls(**kw)
        self.assertLessEqual(res.judge_calls, Constants().n_audit, f"the cycle made {res.judge_calls} judge calls, over N_AUDIT")
        self.assertLessEqual(len(res.audited), Constants().n_audit)

    def test_the_judge_calls_of_a_cycle_never_exceed_n_audit(self):
        self.check_cap()

    def test_broken_uncapped_audit_is_red_and_counted(self):
        with self.assertRaises(AssertionError) as cm:
            self.check_cap(broken_uncapped=True)
        self.assertIn("over N_AUDIT", str(cm.exception))

    def check_no_synthetic(self, **kw):
        ledger = ra.Ledger()
        ra.add_synthetic(ledger, "H", [("syn1", False), ("syn2", False), ("syn3", False)], 1)   # H has no search history at all
        res = ra.run_cycle(ledger, lambda sid, leaf: True, **kw)
        self.assertFalse(res.reports["H"].flagged, "a leaf with no search history was re-filed on synthetic queries")

    def test_with_synthetic_queries_off_a_leaf_with_no_history_is_never_refiled(self):
        self.check_no_synthetic()

    def test_broken_synthetic_on_refiles_it(self):
        with self.assertRaises(AssertionError) as cm:
            self.check_no_synthetic(use_synthetic=True)
        self.assertIn("no search history", str(cm.exception))

    def test_an_unlabelled_pair_is_unknown_not_irrelevant_and_the_priority_sees_no_gold(self):
        ledger = ra.Ledger()
        ledger.add("A", ra.Cand("s1", 0.9, 1, offered_by_tree=False))
        ledger.add("A", ra.Cand("s2", 0.8, 1, offered_by_tree=False))
        ledger.add("B", ra.Cand("s3", 0.99, 1, offered_by_tree=False))
        self.assertEqual(sorted(["B", "A"], key=lambda l: ra.priority(ledger, l)), ["A", "B"])   # more misses first, then score
        res = ra.run_cycle(ledger, lambda sid, leaf: None)
        self.assertEqual((res.reports["A"].relevant, res.reports["A"].flagged), (0, False))

    def test_the_wilson_interval(self):
        self.assertEqual(ra.wilson(0, 0), (0.0, 1.0))
        lo, hi = ra.wilson(2, 2)
        self.assertAlmostEqual(lo, 0.3424, places=3)
        self.assertEqual(hi, 1.0)
        lo, hi = ra.wilson(5, 10)
        self.assertAlmostEqual(lo, 0.2366, places=3)
        self.assertAlmostEqual(hi, 0.7634, places=3)


if __name__ == "__main__":
    unittest.main()
