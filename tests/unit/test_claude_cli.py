"""BUILD-PLAN Step 23 failing-first checks for the capped real-model port, with a FAKE runner: no process is spawned and no model
is called by this file. The first real invocation is counter call 1 of the S1 canary, run by evalkit/canary.py."""
import json
import re
import tempfile
import unittest
from pathlib import Path

from evalkit import claude_cli as cc
from evalkit.arms import TreeRetriever
from evalkit import corpus, fixture
from living_context.config import Constants
from living_context.model.budget import Budget, BudgetExceeded
from living_context.model.port import CallContext, PortRefused, Task
from living_context.model.stub import StubBackend
from living_context.recall.view import MemView
from living_context.structure import build

CTX = CallContext("t", "base", "px", ("base",))


def envelope(obj, **kw):
    e = {"type": "result", "is_error": False, "result": json.dumps(obj), "num_turns": 1, "duration_ms": 900,
         "usage": {"input_tokens": 120, "output_tokens": 30, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0},
         "modelUsage": {"model-x": {}}}
    e.update(kw)
    return json.dumps(e)


class Fake:
    """A runner that records the prompts it is given and answers from a script."""
    def __init__(self, reply=None):
        self.prompts, self.reply = [], reply

    def __call__(self, prompt):
        self.prompts.append(prompt)
        r = self.reply(prompt) if callable(self.reply) else self.reply
        return r if isinstance(r, cc.RunOut) else cc.RunOut(0, r, "", 0.5)


def port(tmp, runner, cap=25, **kw):
    return cc.CappedPort(runner, Budget(Path(tmp) / "b", cap), Path(tmp) / "calls.jsonl", **kw)


JUDGE = envelope({"score": 1, "relevant": True})


class Cap(unittest.TestCase):
    def test_call_26_is_refused_without_spawning_and_every_call_was_counted_before(self):
        with tempfile.TemporaryDirectory() as d:
            r = Fake(JUDGE)
            p = port(d, r, cap=25)
            for _ in range(25):
                self.assertTrue(p.call(Task.JUDGE_RELEVANCE, {"query": "q", "text": "t"}, ctx=CTX).ok)
            with self.assertRaises(BudgetExceeded):
                p.call(Task.JUDGE_RELEVANCE, {"query": "q", "text": "t"}, ctx=CTX)
            self.assertEqual((len(r.prompts), p.spawned, p.budget_count if hasattr(p, "budget_count") else Budget(Path(d) / "b", 25).count()), (25, 25, 25))

    def test_a_crash_inside_the_runner_still_counts(self):
        with tempfile.TemporaryDirectory() as d:
            def boom(_):
                raise RuntimeError("process died")
            p = port(d, boom)
            with self.assertRaises(RuntimeError):
                p.call(Task.JUDGE_RELEVANCE, {"query": "q", "text": "t"}, ctx=CTX)
            self.assertEqual(Budget(Path(d) / "b", 25).count(), 1)

    def test_a_malformed_reply_is_a_typed_failure_and_counts(self):
        with tempfile.TemporaryDirectory() as d:
            p = port(d, Fake("not json at all"))
            res = p.call(Task.JUDGE_RELEVANCE, {"query": "q", "text": "t"}, ctx=CTX)
            self.assertEqual((res.ok, res.error), (False, "parse"))
            self.assertEqual(Budget(Path(d) / "b", 25).count(), 1)
            res2 = port(d, Fake(envelope({"score": "high"}))).call(Task.JUDGE_RELEVANCE, {"query": "q", "text": "t"}, ctx=CTX)
            self.assertEqual((res2.ok, res2.error), (False, "schema"))

    def test_broken_charging_after_the_call_would_not_count_a_crash(self):
        # the control: the counter file must already say 1 while the runner is running
        with tempfile.TemporaryDirectory() as d:
            seen = []
            def spy(_):
                seen.append(Budget(Path(d) / "b", 25).count())
                return cc.RunOut(0, JUDGE, "", 0.1)
            port(d, spy).call(Task.JUDGE_RELEVANCE, {"query": "q", "text": "t"}, ctx=CTX)
            self.assertEqual(seen, [1])

    def test_a_gold_style_id_or_a_forbidden_marker_is_refused_before_the_counter(self):
        with tempfile.TemporaryDirectory() as d:
            r = Fake(JUDGE)
            p = port(d, r, forbidden=["ZZMARKER"])
            with self.assertRaises(PortRefused):
                p.call(Task.JUDGE_RELEVANCE, {"query": "see " + "answer" + "_4be1b6b4_2", "text": "t"}, ctx=CTX)
            with self.assertRaises(PortRefused):
                p.call(Task.JUDGE_RELEVANCE, {"query": "q", "text": "has ZZMARKER inside"}, ctx=CTX)
            self.assertEqual((r.prompts, Budget(Path(d) / "b", 25).count()), ([], 0))

    def test_a_throttle_pauses_the_stage_is_recorded_and_is_not_a_scored_failure(self):
        with tempfile.TemporaryDirectory() as d:
            p = port(d, Fake(cc.RunOut(1, "", "API Error: 429 rate limit reached", 0.2)))
            with self.assertRaises(cc.StagePaused):
                p.call(Task.JUDGE_RELEVANCE, {"query": "q", "text": "t"}, ctx=CTX)
            self.assertTrue(p.rows[-1]["throttled"])
            self.assertEqual(Budget(Path(d) / "b", 25).count(), 1)     # the attempt counted
            self.assertNotIn("strict_ok", p.rows[-1])

    def test_an_allowance_limit_refusal_is_a_hard_stop_not_a_throttle_and_not_a_scored_failure(self):
        refusal = json.dumps({"type": "result", "is_error": True, "result": "You've hit your weekly limit \u00b7 resets Nov 24 at 8am"})
        for out in (cc.RunOut(1, refusal, "", 0.2), cc.RunOut(1, "", "Claude usage limit reached", 0.2), cc.RunOut(0, refusal, "", 0.2)):
            with tempfile.TemporaryDirectory() as d:
                p = port(d, Fake(out))
                with self.assertRaises(cc.StageLimited):
                    p.call(Task.JUDGE_RELEVANCE, {"query": "q", "text": "t"}, ctx=CTX)
                self.assertTrue(p.rows[-1]["limited"])
                self.assertEqual(Budget(Path(d) / "b", 25).count(), 1)

    def test_model_text_that_mentions_a_limit_is_not_a_refusal(self):
        ok = envelope({"score": 1, "relevant": True}, result=json.dumps({"score": 1, "relevant": True, "note": "weekly limit"}))
        with tempfile.TemporaryDirectory() as d:
            self.assertTrue(port(d, Fake(ok)).call(Task.JUDGE_RELEVANCE, {"query": "q", "text": "t"}, ctx=CTX).ok)

    def test_a_streak_of_cli_failures_stops_the_stage_and_a_success_resets_it(self):
        with tempfile.TemporaryDirectory() as d:
            seq = [cc.RunOut(1, "", "boom", 0.1)] * (cc.MAX_CLI_FAILURE_STREAK - 1) + [cc.RunOut(0, JUDGE, "", 0.1)]
            r = Fake(lambda _p: seq.pop(0) if seq else cc.RunOut(1, "", "boom", 0.1))
            p = port(d, r)
            for _ in range(cc.MAX_CLI_FAILURE_STREAK):
                p.call(Task.JUDGE_RELEVANCE, {"query": "q", "text": "t"}, ctx=CTX)
            with self.assertRaises(cc.StageLimited):
                for _ in range(cc.MAX_CLI_FAILURE_STREAK):
                    p.call(Task.JUDGE_RELEVANCE, {"query": "q", "text": "t"}, ctx=CTX)


class Invocation(unittest.TestCase):
    def test_argv_is_frozen_has_no_prompt_and_no_bare(self):
        a = cc.argv_for("claude", "sonnet")
        self.assertEqual(a[:4], ["claude", "-p", "--model", "sonnet"])
        for flag in ("--no-session-persistence", "--disable-slash-commands", "--strict-mcp-config", "--tools"):
            self.assertIn(flag, a)
        self.assertEqual(a[a.index("--tools") + 1], "")
        self.assertNotIn("--bare", a)
        self.assertIn('{"disableAllHooks":true}', a)
        self.assertEqual(a[-2:], ["--system-prompt", cc.SYSTEM_PROMPT])
        prompt = cc.render("judge_relevance", {"query": "UNIQUEQUERY", "text": "x"})
        self.assertNotIn("UNIQUEQUERY", " ".join(a))
        self.assertIn("UNIQUEQUERY", prompt)

    def test_the_working_directory_must_be_empty(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "CLAUDE.md").write_text("x")
            with self.assertRaises(PortRefused):
                cc.CliRunner("claude", "sonnet", Path(d))
            with tempfile.TemporaryDirectory() as e:
                self.assertEqual(cc.CliRunner("claude", "sonnet", Path(e) / "w").argv()[0], "claude")


class Strict(unittest.TestCase):
    def test_consumer_level_problems_are_found_where_the_port_schema_passes(self):
        i = {"leaves": [{"text": "a", "title": "t"}, {"text": "b", "title": "u"}]}
        ok = {"entries": [{"label": "c1", "gist": "g", "facts": ["f"], "entities": ["e"]}] * 2, "summary": "s"}
        self.assertEqual(cc.strict_problems("build_l1", i, ok), [])
        short = dict(ok, entries=ok["entries"][:1])
        self.assertTrue(any("entry count" in p for p in cc.strict_problems("build_l1", i, short)))
        ri = {"query": "q", "b_fanout": 2, "children": [{"label": "c1"}, {"label": "c2"}]}
        bad = {"selected": ["c1", "c9"], "reasons": {"c1": "one two three four five six seven eight nine ten eleven twelve thirteen"},
               "confidence": {"c1": 5, "c9": 1}}
        got = " ".join(cc.strict_problems("route", ri, bad))
        for part in ("not presented", "over 12 words", "0 to 3"):
            self.assertIn(part, got)
        oi = {"pointers": [{"pointer": "p1", "path": "a", "score": 1}]}
        self.assertTrue(cc.strict_problems("organise", oi, {"outline": [{"label": "x", "pointers": ["p2"]}]}))
        self.assertEqual(cc.strict_problems("organise", oi, {"outline": [{"label": "x", "pointers": ["p1"]}]}), [])
        self.assertTrue(cc.strict_problems("type_edges", {"items": [{"label": "c0"}, {"label": "c1"}]},
                                           {"edges": [{"src": "c0", "dst": "c1", "type": "friends"}]}))


class FakeModel:
    """Answers the four H1 tasks from the prompt text, through the stub's own logic, to prove Recall and build_tree run unchanged over the capped port."""
    def __init__(self):
        self.stub = StubBackend()

    def __call__(self, prompt):
        task = re.match(r"TASK (\w+)", prompt).group(1)
        pj = lambda tag: json.loads(re.search(tag + r" \(JSON\): (.*)\n", prompt).group(1))
        if task == "route":
            q = re.search(r"Prompt: (.*)\n", prompt).group(1)
            out = self.stub.run("route", {"query": q, "children": pj("Children"), "b_fanout": 6})
        elif task == "build_l1":
            out = self.stub.run("build_l1", {"leaves": [{"text": l["text"], "title": l["title"]} for l in pj("Leaves")]})
        elif task == "build_upper":
            out = self.stub.run("build_upper", {"children": pj("Children")})
        elif task == "organise":
            out = self.stub.run("organise", {"pointers": pj("Pointers")})
        else:
            raise AssertionError(task)
        return envelope(out)


class OverRecall(unittest.TestCase):
    def test_build_and_recall_run_unchanged_over_the_capped_port_and_the_cap_is_enforced(self):
        fx = fixture.make("advantage", 4)
        mc = corpus.MemCorpus(fx.sessions)
        c = Constants()
        with tempfile.TemporaryDirectory() as d:
            p = port(d, Fake(FakeModel()), cap=10_000)
            tree = build.build_tree(mc.leaves, p, "t", "base", consts=c)
            self.assertTrue(tree.nodes)
            tr = TreeRetriever(p, MemView(tree), "t", "base", "px", c)
            out = tr(fx.queries["q000"])
            self.assertTrue(out.ok and out.ids)
            used = p.spawned
            small = port(d + "x", Fake(FakeModel()), cap=2)
            tr2 = TreeRetriever(small, MemView(tree), "t", "base", "px", c)
            res = tr2(fx.queries["q000"])
            self.assertFalse(res.ok)
            self.assertLessEqual(small.spawned, 2)
            self.assertGreater(used, 2)


if __name__ == "__main__":
    unittest.main()
