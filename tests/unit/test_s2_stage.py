"""The S2 question loop end to end on the in-memory corpus with a FAKE port (stub answers behind the real charge-before-call counter):
a stopped or killed run resumes to the same result as an uninterrupted one without charging any call twice, the cap ends the stage
without recording a half question, and the per-arm files carry the registered row schema. No Elasticsearch, no model."""
import json
import tempfile
import unittest
from pathlib import Path

from evalkit import arms, corpus, dataset as ds, fixture, s2_stage
from evalkit.s2_ports import Journal, ResilientPort, StageStopped
from evalkit.score import REQUIRED_FIELDS
from living_context.config import Constants
from living_context.model.budget import Budget, BudgetExceeded
from living_context.model.port import CallResult
from living_context.model.stub import StubBackend
from living_context.recall.view import MemView
from living_context.structure import build

C = Constants()
FX = fixture.make("advantage", 6, mates=10)
MC = corpus.MemCorpus(FX.sessions)
HASHES = {sid: ds.turns_hash(t) for q in FX.questions for sid, t in q.sessions}


class Kill(BaseException):
    pass


class Inner:
    def __init__(self, budget, kill_after_charge=None):
        self.b, self.backend, self.n, self.kill_after = budget, StubBackend(), 0, kill_after_charge

    def call(self, task, inputs, *, ctx=None):
        self.b.charge()
        self.n += 1
        if self.kill_after is not None and self.b.count() == self.kill_after:
            raise Kill()
        return CallResult(True, self.backend.run(task.value, inputs))


def _tree():
    with tempfile.TemporaryDirectory() as d:
        from living_context.model.cache import DictStore
        from living_context.model.port import ModelPort
        from living_context.policy import ClassLattice
        return build.build_tree(MC.leaves, ModelPort(StubBackend(), Budget(Path(d) / "b", 10 ** 6), ClassLattice(["base"], []), DictStore()),
                                "t", "base", consts=C)


TREE = _tree()      # a resumed real run reuses the published tree (its node ids are stored in Elasticsearch), so the tests do too


def run(tmp, *, cap=10 ** 6, stop_after=None, kill_after_charge=None):
    """One process: the question loop over the published tree. Returns (state, port, ledger, budget)."""
    tmp = Path(tmp)
    b = Budget(tmp / "counter", cap)
    j = Journal(tmp / "journal.jsonl")
    port = ResilientPort(Inner(b, kill_after_charge), b, j, tmp / "STOP", sleep=lambda s: None)
    led = s2_stage.Ledger(tmp / "done.jsonl")
    state = "COMPLETE"
    try:
        view = MemView(TREE)
        direct = s2_stage.Recorder(corpus.MemDirect(MC.leaves))
        tr = s2_stage.Recorder(arms.TreeRetriever(port, view, "t", "base", "px", C, workers=1))
        sh = s2_stage.Recorder(arms.TreeRetriever(port, arms.PermutedView(view), "t", "base", "px", C, workers=1))

        def progress(msg):
            if stop_after is not None and len(led.rows) >= stop_after:
                (tmp / "STOP").write_text("")
        s2_stage.run_questions(FX.questions, HASHES, direct, tr, sh, MC.world, led, stop=port.stop_requested, progress=progress)
    except StageStopped:
        state = "STOPPED"
    except (BudgetExceeded, s2_stage.CapReached):
        state = "CAP_REACHED"
    return state, port, led, b


def result_view(led):
    return {k: ({a: v["sessions"] for a, v in r["arms"].items()}, r["sources"]) for k, r in led.rows.items()}


class Resume(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.full_dir = tempfile.mkdtemp()
        cls.state, cls.port, cls.led, cls.b = run(cls.full_dir)
        cls.total = cls.b.count()

    def test_uninterrupted_run_completes(self):
        self.assertEqual((self.state, len(self.led.rows)), ("COMPLETE", len(FX.questions)))
        self.assertGreater(self.total, 20)

    def test_stop_then_resume_equals_the_uninterrupted_run_and_charges_nothing_twice(self):
        d = tempfile.mkdtemp()
        st, p1, led1, b1 = run(d, stop_after=2)
        self.assertEqual((st, len(led1.rows)), ("STOPPED", 2))
        part = b1.count()
        self.assertLess(part, self.total)
        Path(d, "STOP").unlink()
        st2, p2, led2, b2 = run(d)
        self.assertEqual(st2, "COMPLETE")
        self.assertEqual(b2.count(), self.total)                        # the same number of charged calls as one uninterrupted run
        self.assertEqual(result_view(led2), result_view(self.led))
        self.assertEqual(p2.replayed, 0)                                # the stop fell between questions: completed ones are skipped by the ledger
        self.assertEqual(p2.fresh, self.total - part)
        j = Journal(Path(d) / "journal.jsonl")
        self.assertEqual(len(j.results), self.total)                    # one result row per charged call, no key twice

    def test_a_kill_in_the_middle_of_a_question_does_not_repeat_the_charged_call(self):
        d = tempfile.mkdtemp()
        k = self.total - 10
        with self.assertRaises(Kill):
            run(d, kill_after_charge=k)
        st, p2, led2, b2 = run(d)
        self.assertEqual((st, p2.lost), ("COMPLETE", 1))
        j = Journal(Path(d) / "journal.jsonl")
        self.assertEqual(len(j.results), b2.count())                    # every charge has exactly one outcome row: none was paid for twice
        self.assertEqual(len(led2.rows), len(FX.questions))

    def test_cap_ends_the_stage_without_recording_a_half_question(self):
        d = tempfile.mkdtemp()
        cap = self.total - 5
        st, p, led, b = run(d, cap=cap)
        self.assertEqual(st, "CAP_REACHED")
        self.assertEqual(b.count(), cap)
        self.assertLess(len(led.rows), len(FX.questions))
        done_now = set(led.rows)
        st2, p2, led2, b2 = run(d, cap=cap)                             # resuming under the same cap changes nothing and charges nothing
        self.assertEqual((st2, b2.count(), set(led2.rows) == done_now), ("CAP_REACHED", cap, True))

    def test_the_build_phase_replays_from_the_journal_without_a_charge(self):
        d = Path(tempfile.mkdtemp())
        b = Budget(d / "counter", 10 ** 6)
        port = ResilientPort(Inner(b), b, Journal(d / "journal.jsonl"), d / "STOP", sleep=lambda s: None)
        t1 = build.build_tree(MC.leaves, port, "t", "base", consts=C)
        charged = b.count()
        self.assertEqual(charged, t1.calls)
        b2 = Budget(d / "counter", 10 ** 6)
        port2 = ResilientPort(Inner(b2), b2, Journal(d / "journal.jsonl"), d / "STOP", sleep=lambda s: None)
        t2 = build.build_tree(MC.leaves, port2, "t", "base", consts=C)
        self.assertEqual((b2.count(), port2.replayed, t2.calls), (charged, charged, t1.calls))
        self.assertEqual(sorted(n.summary for n in t1.nodes.values()), sorted(n.summary for n in t2.nodes.values()))

    def test_arm_files_have_the_registered_schema_and_opaque_content_only(self):
        out = Path(tempfile.mkdtemp())
        qids = [q.qid for q in FX.questions]
        means = s2_stage.write_arm_results(out, self.led, qids, commit="abc1234", stage="S2", pool="p", counter=self.total, cache_hits=0,
                                           a0_fields=("text", "title", "entities"), clusters=FX.clusters, resamples=200)
        self.assertEqual(set(means), set(s2_stage.ARMS_H1))
        for arm in s2_stage.ARMS_H1:
            doc = json.loads((out / ("arm_%s.json" % arm)).read_text())
            for f in REQUIRED_FIELDS:
                self.assertIn(f, doc)
            self.assertEqual(len(doc["rows"]), len(FX.questions))
            blob = json.dumps(doc)
            self.assertNotIn("q000", blob)                                # question ids are not in the file
            self.assertEqual(doc["a0_fields"], ["text", "title", "entities"])
        cmp_ = s2_stage.comparisons(self.led, qids, FX.clusters, resamples=200)
        self.assertIn("A1_minus_A0", cmp_)
        self.assertIn("SHUFFLED_control", cmp_)

    def test_summaries_count_calls_by_kind(self):
        calls = Path(tempfile.mkdtemp()) / "calls.jsonl"
        calls.write_text("\n".join(json.dumps({"task": t, "strict_ok": ok, "latency_s": 2.0, "input_tokens": 10, "output_tokens": 5})
                                   for t, ok in (("route", True), ("route", True), ("build_l1", False))) + "\n")
        s = s2_stage.summaries(calls, Path(self.full_dir) / "journal.jsonl")
        self.assertEqual((s["calls"], s["invalid_or_failed"], s["by_task"]["route"]["calls"]), (3, 1, 2))
        self.assertGreater(s["max_calls_in_any_5h_window"], 0)


if __name__ == "__main__":
    unittest.main()
