"""Run control for a long stage, with a FAKE inner port (no process, no model): resume without repeating a charged call, a stop file,
throttle back-off that never retries past the cap, and retry of failed build calls only."""
import json
import tempfile
import unittest
from pathlib import Path

from evalkit import s2_ports
from evalkit.s2_ports import Journal, ResilientPort, StagePaused, StageStopped
from living_context.model.budget import Budget, BudgetExceeded
from living_context.model.port import CallResult, Task

J = {"query": "q", "text": "t"}
BL = {"leaves": [{"text": "a", "title": "t"}]}


class Kill(BaseException):
    """A process kill: not an Exception, so nothing in the port swallows it."""


class Inner:
    """Charges the durable counter BEFORE acting, like the real port; `script` maps call number to an action."""
    def __init__(self, budget, script=None, default=None):
        self.b, self.script, self.default, self.calls = budget, script or {}, default, 0
        self.on_charge = lambda n: None             # set by ResilientPort, called right after the charge (as CappedPort does)

    def call(self, task, inputs, *, ctx=None):
        self.calls += 1
        act = self.script.get(self.calls, self.default)
        if act == "kill_before_charge":
            raise Kill()
        self.on_charge(self.b.charge())
        if act == "kill_after_charge":
            raise Kill()
        if act == "throttle":
            raise StagePaused("throttled")
        if act == "fail":
            return CallResult(False, error="schema")
        return CallResult(True, {"score": 0.5, "relevant": True, "n": self.calls})


def make(tmp, cap=100, script=None, default=None, **kw):
    b = Budget(Path(tmp) / "counter", cap)
    j = Journal(Path(tmp) / "journal.jsonl")
    inner = Inner(b, script, default)
    delays = []
    p = ResilientPort(inner, b, j, Path(tmp) / "STOP", sleep=delays.append, **kw)
    return p, inner, b, delays


class TornTail(unittest.TestCase):
    """A stop in the middle of a write (killed, disk full) tears only the last line; a resume must not append onto it."""

    def test_a_torn_last_journal_line_is_set_aside_and_no_call_is_charged_twice(self):
        with tempfile.TemporaryDirectory() as d:
            p, inner, b, _ = make(d)
            for q in "ab":
                p.call(Task.JUDGE_RELEVANCE, {"query": q, "text": "t"})
            j = Path(d) / "journal.jsonl"
            with open(j, "a") as f:
                f.write('{"t": "pending", "key": "half-writ')            # the write that never finished
            p2, inner2, b2, _ = make(d)
            self.assertTrue(all(p2.call(Task.JUDGE_RELEVANCE, {"query": q, "text": "t"}).ok for q in "abc"))
            self.assertEqual((b2.count(), inner2.calls), (3, 1))
            p3, inner3, b3, _ = make(d)                                  # a second resume sees every row of the first
            self.assertTrue(all(p3.call(Task.JUDGE_RELEVANCE, {"query": q, "text": "t"}).ok for q in "abc"))
            self.assertEqual((b3.count(), inner3.calls, p3.replayed), (3, 0, 3))
            self.assertEqual([x.read_text() for x in Path(d).glob("journal.jsonl.torn.*")], ['{"t": "pending", "key": "half-writ'])

    def test_a_last_line_without_its_newline_keeps_both_rows(self):
        with tempfile.TemporaryDirectory() as d:
            j = Path(d) / "journal.jsonl"
            j.write_text(json.dumps({"t": "result", "key": "k:0", "ok": True, "output": {}, "error": ""}))     # no newline
            Journal(j).append({"t": "result", "key": "k:1", "ok": True, "output": {}, "error": ""})
            self.assertEqual(sorted(Journal(j).results), ["k:0", "k:1"])

    def test_an_unreadable_line_before_the_last_refuses_the_resume(self):
        with tempfile.TemporaryDirectory() as d:
            j = Path(d) / "journal.jsonl"
            j.write_text('{"t": "result", "key": "k:0", "ok": true}\n{"t": "res\n{"t": "result", "key": "k:1", "ok": true}\n')
            with self.assertRaises(s2_ports.JournalCorrupt):
                Journal(j)


class Resume(unittest.TestCase):
    def test_resume_replays_without_charging_or_spawning(self):
        with tempfile.TemporaryDirectory() as d:
            p, inner, b, _ = make(d)
            first = [p.call(Task.JUDGE_RELEVANCE, {"query": "q%d" % i, "text": "t"}).output for i in range(5)]
            self.assertEqual(b.count(), 5)
            p2, inner2, b2, _ = make(d)
            again = [p2.call(Task.JUDGE_RELEVANCE, {"query": "q%d" % i, "text": "t"}).output for i in range(5)]
            self.assertEqual((again, b2.count(), inner2.calls, p2.replayed), (first, 5, 0, 5))

    def test_the_same_prompt_twice_in_one_run_is_two_calls_not_one_cached_answer(self):
        with tempfile.TemporaryDirectory() as d:
            p, inner, b, _ = make(d)
            a, c = p.call(Task.JUDGE_RELEVANCE, J), p.call(Task.JUDGE_RELEVANCE, J)
            self.assertEqual((b.count(), a.output["n"], c.output["n"]), (2, 1, 2))
            p2, inner2, b2, _ = make(d)
            self.assertEqual([p2.call(Task.JUDGE_RELEVANCE, J).output["n"] for _ in range(2)], [1, 2])
            self.assertEqual((b2.count(), inner2.calls), (2, 0))

    def test_a_kill_after_the_charge_is_not_repeated_it_is_a_counted_lost_call(self):
        with tempfile.TemporaryDirectory() as d:
            p, inner, b, _ = make(d, script={3: "kill_after_charge"})
            p.call(Task.JUDGE_RELEVANCE, {"query": "a", "text": "t"})
            p.call(Task.JUDGE_RELEVANCE, {"query": "b", "text": "t"})
            with self.assertRaises(Kill):
                p.call(Task.JUDGE_RELEVANCE, {"query": "c", "text": "t"})
            self.assertEqual(b.count(), 3)
            p2, inner2, b2, _ = make(d)
            for q in "ab":
                self.assertTrue(p2.call(Task.JUDGE_RELEVANCE, {"query": q, "text": "t"}).ok)
            lost = p2.call(Task.JUDGE_RELEVANCE, {"query": "c", "text": "t"})
            self.assertEqual((lost.ok, lost.error, p2.lost, b2.count(), inner2.calls), (False, "lost_in_flight", 1, 3, 0))
            self.assertTrue(p2.call(Task.JUDGE_RELEVANCE, {"query": "d", "text": "t"}).ok)    # new work goes on
            self.assertEqual(b2.count(), 4)

    def test_a_kill_before_the_charge_is_simply_asked_again(self):
        with tempfile.TemporaryDirectory() as d:
            p, inner, b, _ = make(d, script={2: "kill_before_charge"})
            p.call(Task.JUDGE_RELEVANCE, {"query": "a", "text": "t"})
            with self.assertRaises(Kill):
                p.call(Task.JUDGE_RELEVANCE, {"query": "b", "text": "t"})
            self.assertEqual(b.count(), 1)
            p2, inner2, b2, _ = make(d)
            p2.call(Task.JUDGE_RELEVANCE, {"query": "a", "text": "t"})
            self.assertTrue(p2.call(Task.JUDGE_RELEVANCE, {"query": "b", "text": "t"}).ok)
            self.assertEqual((b2.count(), p2.lost, inner2.calls), (2, 0, 1))

    def test_broken_variant_a_port_without_the_journal_charges_again_on_rerun(self):
        with tempfile.TemporaryDirectory() as d:
            b = Budget(Path(d) / "counter", 100)
            for _ in range(2):
                inner = Inner(b)
                for i in range(3):
                    inner.call(Task.JUDGE_RELEVANCE, {"query": "q%d" % i, "text": "t"})
            self.assertEqual(b.count(), 6)     # the property the journal exists to prevent (the tests above hold it at 3)

    def test_a_torn_last_journal_line_is_ignored(self):
        with tempfile.TemporaryDirectory() as d:
            p, inner, b, _ = make(d)
            p.call(Task.JUDGE_RELEVANCE, J)
            with open(Path(d) / "journal.jsonl", "a") as f:
                f.write('{"t": "pend')
            p2, inner2, b2, _ = make(d)
            self.assertTrue(p2.call(Task.JUDGE_RELEVANCE, J).ok)
            self.assertEqual((b2.count(), inner2.calls), (1, 0))


class Cap(unittest.TestCase):
    def test_the_cap_is_a_hard_stop_and_nothing_retries_past_it(self):
        with tempfile.TemporaryDirectory() as d:
            p, inner, b, delays = make(d, cap=3, default="throttle", max_retries=50)
            with self.assertRaises(BudgetExceeded):
                p.call(Task.JUDGE_RELEVANCE, J)
            self.assertEqual((b.count(), inner.calls, len(delays)), (3, 4, 3))     # 3 charged attempts, the 4th refused, no sleep after it

    def test_call_cap_plus_one_is_refused(self):
        with tempfile.TemporaryDirectory() as d:
            p, inner, b, _ = make(d, cap=2)
            for q in "ab":
                p.call(Task.JUDGE_RELEVANCE, {"query": q, "text": "t"})
            with self.assertRaises(BudgetExceeded):
                p.call(Task.JUDGE_RELEVANCE, {"query": "c", "text": "t"})
            self.assertEqual(b.count(), 2)


class Throttle(unittest.TestCase):
    def test_exponential_back_off_then_success(self):
        with tempfile.TemporaryDirectory() as d:
            p, inner, b, delays = make(d, script={1: "throttle", 2: "throttle"}, base_delay_s=30, max_delay_s=100)
            self.assertTrue(p.call(Task.JUDGE_RELEVANCE, J).ok)
            self.assertEqual((delays, b.count(), p.throttled), ([30, 60], 3, 2))     # each throttled attempt is a charged call

    def test_delay_is_capped(self):
        with tempfile.TemporaryDirectory() as d:
            p, inner, b, delays = make(d, script={i: "throttle" for i in range(1, 6)}, base_delay_s=30, max_delay_s=100, max_retries=5)
            p.call(Task.JUDGE_RELEVANCE, J)
            self.assertEqual(delays, [30, 60, 100, 100, 100])

    def test_exhausted_retries_pause_the_stage_and_resume_asks_again(self):
        with tempfile.TemporaryDirectory() as d:
            p, inner, b, delays = make(d, default="throttle", max_retries=2)
            with self.assertRaises(StagePaused):
                p.call(Task.JUDGE_RELEVANCE, J)
            self.assertEqual(b.count(), 3)
            p2, inner2, b2, _ = make(d)           # a calmer inner this time
            self.assertTrue(p2.call(Task.JUDGE_RELEVANCE, J).ok)
            self.assertEqual((b2.count(), p2.lost), (4, 0))


class Stop(unittest.TestCase):
    def test_stop_file_stops_before_the_next_charge(self):
        with tempfile.TemporaryDirectory() as d:
            p, inner, b, _ = make(d)
            p.call(Task.JUDGE_RELEVANCE, J)
            Path(d, "STOP").write_text("")
            with self.assertRaises(StageStopped):
                p.call(Task.JUDGE_RELEVANCE, {"query": "x", "text": "t"})
            self.assertEqual((b.count(), inner.calls), (1, 1))
            p2, inner2, b2, _ = make(d)       # replay of an already journaled call needs no model, so it is not stopped
            self.assertTrue(p2.call(Task.JUDGE_RELEVANCE, J).ok)
            Path(d, "STOP").unlink()
            self.assertTrue(p2.call(Task.JUDGE_RELEVANCE, {"query": "x", "text": "t"}).ok)


class FailedCalls(unittest.TestCase):
    def test_failed_build_call_is_retried_as_new_charged_calls_up_to_the_limit(self):
        with tempfile.TemporaryDirectory() as d:
            p, inner, b, _ = make(d, script={1: "fail", 2: "fail"})
            self.assertTrue(p.call(Task.BUILD_L1, BL).ok)
            self.assertEqual(b.count(), 3)
            p2, inner2, b2, _ = make(d, default="fail")
            self.assertFalse(p2.call(Task.BUILD_L1, {"leaves": [{"text": "zz", "title": "t"}]}).ok)
            self.assertEqual(b2.count(), 3 + 3)       # 1 + failure_retries(2) attempts, then the failure is returned

    def test_failed_route_call_is_not_retried(self):
        with tempfile.TemporaryDirectory() as d:
            p, inner, b, _ = make(d, default="fail")
            self.assertFalse(p.call(Task.ROUTE, {"query": "q", "children": [], "b_fanout": 6}).ok)
            self.assertEqual(b.count(), 1)

    def test_failures_replay_as_failures(self):
        with tempfile.TemporaryDirectory() as d:
            p, inner, b, _ = make(d, default="fail")
            p.call(Task.ROUTE, {"query": "q", "children": [], "b_fanout": 6})
            p2, inner2, b2, _ = make(d)
            r = p2.call(Task.ROUTE, {"query": "q", "children": [], "b_fanout": 6})
            self.assertEqual((r.ok, r.error, b2.count(), inner2.calls), (False, "schema", 1, 0))


if __name__ == "__main__":
    unittest.main()


class Scope(unittest.TestCase):
    """S6-H1 scopes the journal per question: a resume that skips completed questions must not answer a later question's prompt with an
    earlier question's journaled answer to an IDENTICAL prompt (an organise prompt carries no query text, so two questions can render
    the same one). Unscoped keys (S2, S3) are unchanged."""

    def test_an_identical_prompt_in_a_skipped_question_is_not_replayed_for_a_later_one(self):
        with tempfile.TemporaryDirectory() as d:
            p, inner, b, _ = make(d)
            for ref in ("qa", "qb"):                     # first run: both questions ask the same prompt once
                p.scope = ref
                p.call(Task.JUDGE_RELEVANCE, J)
            self.assertEqual(b.count(), 2)
            p2, inner2, b2, _ = make(d)                  # resumed run: qa is complete and skipped; qb is asked again
            p2.scope = "qb"
            out = p2.call(Task.JUDGE_RELEVANCE, J)
            self.assertEqual((p2.replayed, b2.count(), out.output["n"]), (1, 2, 2))      # qb's own answer, not qa's
            p2.scope = "qc"                              # a new question with the same prompt is a new, charged call
            p2.call(Task.JUDGE_RELEVANCE, J)
            self.assertEqual((p2.replayed, b2.count()), (1, 3))

    def test_without_a_scope_the_keys_are_the_unscoped_ones(self):
        with tempfile.TemporaryDirectory() as d:
            p, _, _, _ = make(d)
            p.call(Task.JUDGE_RELEVANCE, J)
            self.assertTrue(all("|" not in k for k in p.journal.results))
