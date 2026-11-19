"""The S2 body (`run_s2.run`) on a real Elasticsearch with a FAKE model runner: no model is called by this file. Needs LC_TEST_ES_URL,
LC_TEST_CA, LC_TEST_PW (skipped without them). The questions are a synthetic fixture, the embedder is a stand-in that carries the real
embedder's identity but returns the hashed stub's vectors, and the runner answers each prompt from a script.

Shown here: a stop file ends the run cleanly (exit 3), a resume attaches to the built tenant and finishes (exit 0) with no call charged
twice and no model reached for what was already paid for; the cap ends a run (exit 4) at exactly the cap; throttle responses back off
and the stage completes; the per-arm files exist."""
import argparse
import json
import os
import re
import tempfile
import unittest
from pathlib import Path

from evalkit import claude_cli as cc, fixture, dataset as ds, run_h1, run_s2
from evalkit.s2_ports import Journal
from living_context.embed import e5, stub

URL, CA, PW = (os.environ.get(k) for k in ("LC_TEST_ES_URL", "LC_TEST_CA", "LC_TEST_PW"))


class StandIn:
    is_real, model_id = True, e5.MODEL_ID
    def passage(self, t): return stub.embed(t)
    query = passage


def envelope(obj):
    return json.dumps({"type": "result", "is_error": False, "result": json.dumps(obj), "num_turns": 1, "duration_ms": 10,
                       "usage": {"input_tokens": 10, "output_tokens": 5, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0},
                       "modelUsage": {"m": {}}})


def block(prompt, marker):
    m = re.search(re.escape(marker) + r" (.*?)\nReturn", prompt, re.S)
    return json.loads(m.group(1))


class Runner:
    """Answers by task from the prompt; optionally creates the stop file at call `stop_at`, throttles the calls in `throttle`."""
    def __init__(self, stop_at=None, stop_file=None, throttle=()):
        self.n, self.stop_at, self.stop_file, self.throttle = 0, stop_at, stop_file, set(throttle)
        self.prompts = []

    def argv(self):
        return ["fake"]

    def __call__(self, prompt):
        self.n += 1
        self.prompts.append(prompt)
        if self.stop_at and self.n == self.stop_at:
            Path(self.stop_file).write_text("")
        if self.n in self.throttle:
            return cc.RunOut(1, "", "rate limit exceeded", 0.01)
        if "TASK route" in prompt:
            kids = block(prompt, "Children (JSON):")
            lab = kids[0]["label"]
            out = {"selected": [lab], "reasons": {lab: "looks related"}, "confidence": {lab: 2}}
        elif "TASK build_l1" in prompt:
            n = len(block(prompt, "Leaves (JSON):"))
            out = {"entries": [{"label": "c%d" % (i + 1), "gist": "a gist", "facts": ["a fact"], "entities": ["thing"]} for i in range(n)],
                   "summary": "a summary of topics"}
        elif "TASK build_upper" in prompt:
            out = {"summary": "an upper summary"}
        elif "TASK organise" in prompt:
            ptr = [x["pointer"] for x in block(prompt, "Pointers (JSON):")]
            out = {"outline": [{"label": "group", "pointers": ptr}]}
        else:
            raise AssertionError("unexpected task")
        return cc.RunOut(0, envelope(out), "", 0.01)


FX = fixture.make("advantage", 4, mates=10)
HASHES = {sid: ds.turns_hash(t) for q in FX.questions for sid, t in q.sessions}


def loader(a):
    return FX.questions, HASHES, FX.clusters


def args(run_dir, **kw):
    base = dict(stage="S2", dataset="", es_url=URL, es_ca=CA, es_pw_file=PW, tenant="s2test", run_dir=str(run_dir), n_questions=4,
                splits=["pilot"], model_kind="claude", binary="fake", model="m", approved_cap=100000, approval_ref="test", commit="abc1234",
                beta=None, resume=False, embed_dir="", threads=1, a0_fields="strong", max_retries=2, base_delay=0.0)
    base.update(kw)
    return argparse.Namespace(**base)


@unittest.skipUnless(URL and CA and PW, "needs a scratch Elasticsearch")
class S2Body(unittest.TestCase):
    def go(self, d, runner, **kw):
        return run_s2.run(args(d, **kw), run_h1.pick_questions, embedder=StandIn(), runner=runner, loader=loader)

    def state(self, d):
        return json.loads((Path(d) / "STATE.json").read_text())

    def test_complete_run_writes_the_arm_files(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(self.go(d, Runner()), 0)
            self.assertEqual(self.state(d)["state"], "COMPLETE")
            for arm in ("A0", "A1", "A1-tree-only", "SHUFFLED"):
                doc = json.loads((Path(d) / "results" / ("arm_%s.json" % arm)).read_text())
                self.assertEqual(doc["n"], 4)
                self.assertEqual(doc["call_counter"], self.state(d)["counter"])
            self.assertTrue((Path(d) / "results" / "comparisons.json").exists())
            summ = json.loads((Path(d) / "results" / "summaries.json").read_text())
            self.assertEqual(summ["calls"], self.state(d)["counter"])
            self.assertEqual(summ["failure_rate"], 0.0)

    def test_stop_then_resume_finishes_without_a_repeated_charge(self):
        with tempfile.TemporaryDirectory() as d:
            r1 = Runner(stop_at=20, stop_file=Path(d) / "STOP")
            self.assertEqual(self.go(d, r1), 3)
            st = self.state(d)
            self.assertEqual((st["state"], st["counter"]), ("STOPPED", 20))
            ours = lambda: [w for w in run_h1.check_prereqs(args(d, resume=True), embed=StandIn()) if "30 pilot" not in w]   # the test pool has 4
            self.assertTrue(any("stop file" in w for w in ours()))      # the stop file blocks the resume
            (Path(d) / "STOP").unlink()
            self.assertEqual(ours(), [])
            r2 = Runner()
            self.assertEqual(self.go(d, r2, resume=True), 0)
            st2 = self.state(d)
            self.assertEqual(st2["state"], "COMPLETE")
            self.assertEqual(st2["counter"], 20 + r2.n)                  # every call after the stop is new work
            j = Journal(Path(d) / "journal.jsonl")
            self.assertEqual(len(j.results), st2["counter"])             # one outcome per charged call: none twice
            self.assertEqual(st2["lost_in_flight"], 0)

    def test_cap_stops_the_run_at_exactly_the_cap(self):
        with tempfile.TemporaryDirectory() as d:
            r = Runner()
            self.assertEqual(self.go(d, r, approved_cap=25), 4)
            st = self.state(d)
            self.assertEqual((st["state"], st["counter"], r.n), ("CAP_REACHED", 25, 25))

    def test_throttling_backs_off_and_the_stage_completes(self):
        with tempfile.TemporaryDirectory() as d:
            r = Runner(throttle=(3, 4))
            self.assertEqual(self.go(d, r), 0)
            st = self.state(d)
            self.assertEqual(st["throttled"], 2)
            self.assertEqual(st["counter"], r.n)                          # each throttled attempt was a charged call

    def test_throttling_past_the_retries_pauses_and_never_past_the_cap(self):
        with tempfile.TemporaryDirectory() as d:
            r = Runner(throttle=range(1, 1000))
            self.assertEqual(self.go(d, r, max_retries=2), 75)
            self.assertEqual(self.state(d)["counter"], 3)


if __name__ == "__main__":
    unittest.main()
