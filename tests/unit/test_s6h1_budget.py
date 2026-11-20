"""The S6-H1 call estimate (EXPERIMENT 10.3; cap 35,574): rates measured from earlier stages' logs, build exact for the leaf count, recalls
at the code bound for the top, retry allowances; the stage is refused when the top exceeds the cap. Synthetic logs shaped like S2's
measured figures (route 3,522, organise 502, build 399, 60 tree recalls, no failure, no throttle). No model, no dataset."""
import json
import math
import tempfile
import unittest
from pathlib import Path

from evalkit import s6h1_budget as bud


def logs(route=3522, organise=502, build=399, questions=30, shuffled=True, build_failed=0, throttled=0):
    d = Path(tempfile.mkdtemp())
    rows = [{"task": "route"}] * route + [{"task": "organise"}] * organise + [{"task": "build_l1", "schema_ok": True}] * (build - build_failed)
    rows += [{"task": "build_l1", "error": "cli_error", "schema_ok": False}] * build_failed
    rows += [{"task": "route", "throttled": True, "error": "throttled"}] * throttled
    (d / "calls.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
    arms = {"A0": {}, "A1": {}, "A1-tree-only": {}}
    if shuffled:
        arms["SHUFFLED"] = {}
    (d / "done.jsonl").write_text("".join(json.dumps({"qref": "q%d" % i, "arms": arms}) + "\n" for i in range(questions)))
    return d


def wilson_upper(k, n, z=1.959964):
    p = k / n
    den = 1 + z * z / n
    return min(1.0, (p + z * z / (2 * n)) / den + z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / den)


class Rates(unittest.TestCase):
    def test_rates_from_logs_count_shuffled_as_a_second_recall(self):
        r = bud.measured_rates([logs()])
        self.assertEqual(r["recalls"], 60)
        self.assertAlmostEqual(r["per_recall"], 4024 / 60)
        r1 = bud.measured_rates([logs(shuffled=False)])
        self.assertEqual(r1["recalls"], 30)

    def test_failures_and_throttles_are_counted(self):
        r = bud.measured_rates([logs(build_failed=4, throttled=9)])
        self.assertEqual((r["build_failed"], r["throttled"]), (4, 9))
        self.assertAlmostEqual(r["build_fail_upper"], wilson_upper(4, 399))


class Estimate(unittest.TestCase):
    def test_the_whole_pool_estimate_by_hand(self):
        r = bud.measured_rates([logs()])
        e = bud.estimate(78_700, r)
        # by hand: 4,919 level-1 groups; 308 then 20 upper nodes (top 20, three levels); one more possible upper level of 2
        self.assertEqual((e["tree"]["l1_calls"], e["tree"]["upper_calls"], e["tree"]["top"]), (4919, 328, 20))
        self.assertEqual(e["build_calls"], 5247)
        self.assertEqual(e["recalls"], 312 + 50)
        self.assertEqual(e["per_recall_code_bound"], 24 + 36 + 12)
        base = 5249 + 362 * 72
        top = base + math.ceil(2 * 5249 * wilson_upper(0, 399)) + math.ceil(base * wilson_upper(0, 4423))
        self.assertEqual(e["top"], top)
        self.assertEqual(e["expected"], round(5247 + 362 * 4024 / 60))
        self.assertLessEqual(e["top"], bud.APPROVED_CAP)
        self.assertEqual(bud.refusal(e, bud.APPROVED_CAP), [])

    def test_the_top_never_uses_a_rate_below_the_code_bound(self):
        r = bud.measured_rates([logs(route=600, organise=0)])        # 10 calls per recall measured
        e = bud.estimate(78_700, r)
        self.assertGreaterEqual(e["top"], e["build_calls_top"] + e["recalls"] * 72)

    def test_a_measured_rate_above_the_bound_raises_the_top(self):
        r = bud.measured_rates([logs(route=4800, organise=600)])     # 90 per recall
        e = bud.estimate(78_700, r)
        self.assertGreaterEqual(e["top"], e["build_calls_top"] + e["recalls"] * 90)

    def test_a_top_over_the_cap_is_refused(self):
        r = bud.measured_rates([logs()])
        e = bud.estimate(78_700, r)
        self.assertTrue(bud.refusal(e, e["top"] - 1))
        self.assertEqual(bud.refusal(e, e["top"]), [])

    def test_a_larger_pool_or_measured_failures_can_exceed_the_cap(self):
        e = bud.estimate(200_000, bud.measured_rates([logs()]))
        self.assertTrue(bud.refusal(e, bud.APPROVED_CAP))
        e2 = bud.estimate(78_700, bud.measured_rates([logs(build_failed=120, throttled=1500)]))
        self.assertTrue(bud.refusal(e2, bud.APPROVED_CAP))


if __name__ == "__main__":
    unittest.main()
