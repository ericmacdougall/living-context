"""The `muse` model kind (evalkit.model_kind) over the REAL `muse_api.MusePort` against a stub OpenRouter on 127.0.0.1 (the port's
loopback test mode with a fake key; no network, no real key). The guards never open the key file and require the registered
reasoning effort and output limit; a port that cannot be built refuses the stage before any stamp or charge; one pacer per run
spaces requests at --rpm; a 429, a 503 or OpenRouter's in-flight 402 pauses with Retry-After as the back-off floor and is not a
failure of the streak; a 402 or OpenRouter's 403 budget refusal and a reported model mismatch stop the stage at once, unretried;
an answer cut by `finish_reason` length is a failed call, never an answer; every call row records prompt, completion and
reasoning tokens, the reported model and the upstream provider; and the S6-H1 stage gives the same ledger at 1 and 4 workers,
with the model's identity in the stamp and every ledger row."""
import argparse
import builtins
import io
import json
import os
import shutil
import sys
import tempfile
import threading
import types
import unittest
from pathlib import Path
from unittest import mock

from evalkit import claude_cli as cc, model_kind as mk, muse_api as ma, run_h1, run_s6h1 as rs
from evalkit.s2_ports import Journal, ResilientPort
from living_context.model.budget import Budget
from living_context.model.port import Task
from tests.security.test_run_s2_es import Runner
from tests.unit import test_run_h1_guards as hg, test_run_s6h1 as t6
from tests.unit.test_muse_api import OR_KEY, Stub, completion, err_body
from tests.unit.test_parallel import in_threads, route_inputs, seeded_node_ids

MODEL = "meta/muse-spark-1.3-contributor"


def key_file(text="NOT-A-KEY-MARKER", mode=0o600):
    p = Path(tempfile.mkdtemp()) / "muse.key"
    p.write_text(text)
    os.chmod(p, mode)
    return str(p)


def margs(**kw):
    a = dict(model_kind="muse", model=MODEL, provider="openrouter", key_file=key_file(), key_secret_project="", key_secret_name="",
             base_url="", reasoning_effort="low", max_tokens=4096, rpm=140, api_timeout=30.0, binary="")
    a.update(kw)
    return argparse.Namespace(**a)


class Upstream:
    """OpenRouter's chat completions on 127.0.0.1: answers each prompt as the stage test Runner would (the same outputs as the CLI
    kind), with scripted outcomes by request number."""
    KINDS = ("throttle", "unavailable", "inflight", "quota", "budget", "error", "mismatch", "length")

    def __init__(self, retry_after=None, **script):
        assert set(script) <= set(self.KINDS), script
        self.runner, self.n, self.lock = Runner(), 0, threading.Lock()
        self.script = {k: set(script.get(k, ())) for k in self.KINDS}
        self.retry_after, self.systems, self.bodies = retry_after, [], []

    def has(self, kind, k):
        return k in self.script[kind]

    def reply(self, path, headers, body):
        with self.lock:
            self.n += 1
            k = self.n
            self.systems.append(body["messages"][0]["content"])
            self.bodies.append({x: body.get(x) for x in ("model", "max_tokens", "reasoning_effort", "provider")})
        ra = {"Retry-After": str(self.retry_after)} if self.retry_after is not None else {}
        if self.has("throttle", k):
            return 429, ra, err_body(429, etype="rate_limit_error")
        if self.has("unavailable", k):
            return 503, ra, err_body(503)
        if self.has("inflight", k):
            return 402, ra, err_body(402, metadata={"limit_source": "openrouter_in_flight_budget"})
        if self.has("quota", k):
            return 402, {}, err_body(402, etype="billing_error")
        if self.has("budget", k):
            return 403, {}, err_body(403, metadata={"limit_source": "openrouter_key_limit"})
        if self.has("error", k):
            return 500, {}, err_body(500)
        with self.lock:
            text = json.loads(self.runner(body["messages"][1]["content"]).stdout)["result"]
        extra = {"provider": "Meta"}
        if self.has("length", k):           # a parseable answer cut short: must never count as an answer
            extra["choices"] = [{"index": 0, "finish_reason": "length", "message": {"role": "assistant", "content": text}}]
        return 200, {}, completion(text, "some/other-model" if self.has("mismatch", k) else MODEL, prompt_tokens=1200, cached=200,
                                   completion_tokens=340, extra=extra)


class Live:
    """A stub upstream and the real port over it (test-only loopback mode, never a runner flag)."""
    def __init__(self, upstream, **port_kw):
        self.up, self.stub = upstream, Stub(upstream.reply)
        self.stub.__enter__()
        self.key = key_file(OR_KEY)
        kw = dict(model=MODEL, provider="openrouter", max_tokens=4096, reasoning_effort="low", timeout=30.0)
        kw.update(port_kw)
        self.runner = mk.MuseRunner(ma.MusePort(self.key, _loopback_test_port=self.stub.port, **kw))

    def close(self):
        self.stub.__exit__()


def port_over(tc, tmp, upstream, *, concurrency=1, sleep=None, pacer=None, cap=1000, **kw):
    live = Live(upstream)
    tc.addCleanup(live.close)
    tmp = Path(tmp)
    b = Budget(tmp / "counter", cap)
    p = mk.capped_port(margs(), tmp, b, runner=live.runner)
    p._pacer = pacer                    # the run's pacer is tested on its own; unpaced otherwise
    delays = []
    port = ResilientPort(p, b, Journal(tmp / "journal.jsonl"), tmp / "STOP", sleep=sleep or delays.append, base_delay_s=1.0,
                         concurrency=concurrency, **kw)
    return b, port, delays


def rows(tmp):
    return [json.loads(l) for l in (Path(tmp) / "calls.jsonl").read_text().splitlines()]


class Guards(unittest.TestCase):
    def test_the_key_file_is_checked_by_metadata_only_and_never_opened(self):
        a = margs()
        opened = []
        real_open, real_io_open = builtins.open, io.open

        def spy(opener):
            def o(file, *x, **k):
                if isinstance(file, (str, os.PathLike)) and os.fspath(file) == a.key_file:
                    opened.append(file)
                return opener(file, *x, **k)
            return o
        with mock.patch("builtins.open", spy(real_open)), mock.patch("io.open", spy(real_io_open)):
            self.assertEqual(mk.check(a), [])
        self.assertEqual(opened, [])

    def test_each_muse_setting_is_refused_for_its_own_reason(self):
        for kw, word in ((dict(key_file=""), "exactly one key source"), (dict(key_file="/nonexistent/k"), "does not exist"),
                         (dict(key_secret_project="p", key_secret_name="n"), "exactly one key source"),
                         (dict(key_file="", key_secret_project="p"), "go together"),
                         (dict(key_file="", key_secret_name="n"), "go together"),
                         (dict(key_file=key_file(mode=0o644)), "mode 0600"), (dict(rpm=141), "--rpm"), (dict(rpm=0), "--rpm"),
                         (dict(base_url="http://api.example"), "https"), (dict(model=""), "--model"),
                         (dict(provider="other"), "--provider"), (dict(reasoning_effort=""), "--reasoning-effort"),
                         (dict(reasoning_effort="none"), "--reasoning-effort"), (dict(reasoning_effort="max"), "not available"),
                         (dict(max_tokens=0), "--max-tokens"), (dict(max_tokens=True), "--max-tokens")):
            why = mk.check(margs(**kw))
            self.assertTrue(why and all(word in w for w in why), (kw, why))
        self.assertTrue(mk.check(margs(model_kind="other")))
        self.assertEqual(mk.check(margs(key_file="", key_secret_project="p", key_secret_name="n")), [])
        self.assertEqual(mk.EFFORTS, ma.REASONING_EFFORT)                   # one list of efforts, the port's

    def test_the_stage_runners_take_the_muse_kind_and_refuse_a_resume_across_kinds_or_settings(self):
        req = ["--dataset", "d", "--es-url", "u", "--es-ca", "c", "--es-pw-file", "p", "--run-dir", "r", "--stamp-registry", "s",
               "--embed-dir", "e", "--model", MODEL, "--rates-from", "x", "--mem-proof", "m", "--approved-cap", "1",
               "--approval-ref", "a", "--commit", "c", "--model-kind", "muse"]
        a = rs.parser().parse_args(req + ["--key-file", "k", "--rpm", "70", "--reasoning-effort", "low", "--max-tokens", "8192"])
        self.assertEqual((a.model_kind, a.provider, a.key_file, a.rpm, a.base_url, a.binary, a.reasoning_effort, a.max_tokens),
                         ("muse", "openrouter", "k", 70, "", "", "low", 8192))
        bare = rs.parser().parse_args(req)
        self.assertEqual((bare.reasoning_effort, bare.max_tokens), ("", 0))          # no default hidden in code: check refuses
        self.assertTrue(any("--reasoning-effort" in w for w in mk.check(bare)) and any("--max-tokens" in w for w in mk.check(bare)))
        d = tempfile.mkdtemp()
        old = {"stage": "S3", "commit": "abc1234", "cap": 6000, "n_questions": 102, "a0_fields": "strong", "shuffled_n": 50}

        def why_with(stamp):
            (Path(d) / "STAMP.json").write_text(json.dumps(stamp))
            return run_h1.check_prereqs(hg.args(stage="S3", n_questions=102, shuffled_n=50, run_dir=d, resume=True, a0_fields="strong",
                                                **vars(margs())), embed=hg.RealEmbed())
        self.assertTrue(any("model_kind differs" in w for w in why_with(old)))                # a claude stamp
        same = dict(old, **mk.record(margs()))
        self.assertFalse(any("differs from the stamp" in w for w in why_with(same)), why_with(same))
        for k, v in (("reasoning_effort", "high"), ("max_tokens", 2048), ("provider", "meta")):
            self.assertTrue(any("%s differs" % k in w for w in why_with(dict(same, **{k: v}))), k)
        stub = run_h1.check_prereqs(hg.args(stage="DEV-STUB", **vars(margs())))
        self.assertTrue(any("only in S2 or S3" in w for w in stub), stub)


class Port(unittest.TestCase):
    def test_the_port_is_built_from_one_key_source_the_registered_settings_and_one_pacer_at_rpm(self):
        made = []

        class FakePort:
            model = MODEL
            def __init__(self, **kw): made.append(kw)
            def argv(self): return ["muse-fake", "{}"]
            def record(self): return {"provider": "openrouter"}
        fake_mod = types.SimpleNamespace(MusePort=FakePort, MuseConfigError=ma.MuseConfigError)
        import evalkit
        for a, src in ((margs(rpm=70, api_timeout=45.0), "file"),
                       (margs(rpm=70, api_timeout=45.0, key_file="", key_secret_project="P", key_secret_name="N"), "secret")):
            with mock.patch.dict(sys.modules, {"evalkit.muse_api": fake_mod}), \
                    mock.patch.object(evalkit, "muse_api", fake_mod, create=True):
                p = mk.capped_port(a, Path(tempfile.mkdtemp()), Budget(Path(tempfile.mkdtemp()) / "counter", 10))
            want = {"model": MODEL, "provider": "openrouter", "base_url": None, "timeout": 45.0, "max_tokens": 4096,
                    "reasoning_effort": "low"}
            want.update({"key_file": a.key_file} if src == "file" else {"key_secret": ("P", "N"), "gcloud": shutil.which("gcloud")})
            self.assertEqual(made[-1], want)
            self.assertEqual((p._pacer.rpm, p._argv, p.model_id), (70, ["muse-fake", "{}"], MODEL))

    def test_the_real_port_is_recorded_without_its_key_source_and_a_bad_key_refuses_before_any_stamp(self):
        a = margs(key_file=key_file(OR_KEY))
        p = mk.prepare(a, Path(tempfile.mkdtemp()))
        rec = mk.stamp_record(a, p)
        self.assertEqual((rec["port"]["provider"], rec["port"]["reasoning_effort"], rec["port"]["max_tokens"], rec["rpm"]),
                         ("openrouter", "low", 4096, 140))
        text = json.dumps(rec) + json.dumps(p.argv)
        self.assertFalse(any(x in text for x in (a.key_file, OR_KEY)), text)
        with self.assertRaises(mk.PortUnavailable) as e:
            mk.prepare(margs(), Path(tempfile.mkdtemp()))                  # a file without an OpenRouter key
        self.assertNotIn("NOT-A-KEY-MARKER", str(e.exception))
        base, s3, man, run = t6.setup_dirs()
        rc = rs.run(t6.args(base, s3, run, **vars(margs())), embedder=t6.RealEmbed(), loader=lambda _a: t6.rel.QS,
                    tenant_factory=t6.factory(run), registered_pool=t6.POOL, registered_fresh=t6.rel.REG, manifest_path=man)
        self.assertEqual((rc, json.loads((run / "STATE.json").read_text())["state"]), (2, "REFUSED"))
        self.assertFalse(any((run / f).exists() for f in ("STAMP.json", "PREBUILD.json", "counter", "calls.jsonl")))

    def test_an_answer_records_its_tokens_model_and_upstream_and_sends_the_registered_settings(self):
        with tempfile.TemporaryDirectory() as tmp:
            up = Upstream()
            b, port, _ = port_over(self, tmp, up)
            self.assertTrue(port.call(Task.ROUTE, route_inputs(1)).ok)
            r = rows(tmp)[0]
            self.assertEqual((r["prompt_tokens"], r["completion_tokens"], r["reasoning_tokens"], r["cached_tokens"], r["http_status"]),
                             (1200, 340, 7, 200, 200))
            self.assertEqual((r["model_reported"], r["upstream_provider"], r["finish_reason"], r["schema_ok"]), (MODEL, "Meta", "stop", True))
            self.assertEqual(up.systems, [cc.SYSTEM_PROMPT])
            self.assertEqual((up.bodies[0]["max_tokens"], up.bodies[0]["reasoning_effort"], up.bodies[0]["provider"]["allow_fallbacks"]),
                             (4096, "low", False))

    def test_a_429_503_or_in_flight_402_backs_off_at_least_retry_after_and_is_asked_again(self):
        for kind in ("throttle", "unavailable", "inflight"):
            with tempfile.TemporaryDirectory() as tmp:
                b, port, delays = port_over(self, tmp, Upstream(retry_after=42, **{kind: (1,)}))
                self.assertTrue(port.call(Task.ROUTE, route_inputs(1)).ok, kind)
                self.assertEqual(delays, [42.0], kind)                  # base delay 1 s; Retry-After is the floor
                self.assertEqual(b.count(), 2)
                self.assertTrue(rows(tmp)[0]["throttled"])

    def test_throttles_never_count_as_failures_of_the_streak(self):
        with tempfile.TemporaryDirectory() as tmp:          # 6 calls, each throttled once then answered: more than the streak of 5
            b, port, _ = port_over(self, tmp, Upstream(throttle=(1, 3, 5, 7, 9, 11)))
            self.assertTrue(all(port.call(Task.ROUTE, route_inputs(i)).ok for i in range(6)))
        with tempfile.TemporaryDirectory() as tmp:          # 5 429s in a row on one call, no answer between, do not stop the stage
            b, port, _ = port_over(self, tmp, Upstream(throttle=range(1, 6)), max_retries=6)
            self.assertTrue(port.call(Task.ROUTE, route_inputs(1)).ok)
        with tempfile.TemporaryDirectory() as tmp:          # 5 real API errors in a row still stop the stage
            b, port, _ = port_over(self, tmp, Upstream(error=range(1, 100)))
            with self.assertRaises(cc.StageLimited):
                for i in range(10):
                    port.call(Task.ROUTE, route_inputs(i))

    def test_an_answer_cut_by_the_output_limit_is_a_failed_call_never_an_answer(self):
        with tempfile.TemporaryDirectory() as tmp:
            b, port, _ = port_over(self, tmp, Upstream(length=range(1, 100)))
            r = port.call(Task.ROUTE, route_inputs(1))
            self.assertFalse(r.ok)
            self.assertEqual((rows(tmp)[0]["error"], rows(tmp)[0]["api_error"], rows(tmp)[0]["finish_reason"]),
                             ("api_error", "finish_length", "length"))
            with self.assertRaises(cc.StageLimited):        # and five in a row stop the stage
                for i in range(2, 10):
                    port.call(Task.ROUTE, route_inputs(i))

    def test_a_reported_model_mismatch_stops_the_stage_at_once_and_is_not_retried(self):
        with tempfile.TemporaryDirectory() as tmp:
            up = Upstream(mismatch=(2,))
            b, port, delays = port_over(self, tmp, up, concurrency=4, adaptive=True, start_concurrency=4)
            self.assertTrue(port.scoped("q").call(Task.ROUTE, route_inputs(1)).ok)
            with self.assertRaises(cc.StageLimited) as e:
                port.scoped("q").call(Task.ROUTE, route_inputs(2))
            self.assertNotIsInstance(e.exception, cc.AllowanceRefused)
            self.assertEqual((b.count(), delays, up.n), (2, [], 2))
            self.assertEqual(rows(tmp)[1]["error"], "model_mismatch")

    def test_a_402_or_a_403_budget_refusal_stops_the_stage_at_once_even_at_adaptive_concurrency(self):
        for kind in ("quota", "budget"):
            with tempfile.TemporaryDirectory() as tmp:
                up = Upstream(**{kind: (1,)})
                b, port, delays = port_over(self, tmp, up, concurrency=4, adaptive=True, start_concurrency=4)
                with self.assertRaises(cc.StageLimited) as e:
                    port.scoped("q").call(Task.ROUTE, route_inputs(1))
                self.assertNotIsInstance(e.exception, cc.AllowanceRefused, kind)            # not the retried kind
                self.assertEqual((b.count(), delays, up.n), (1, [], 1), kind)
                self.assertTrue(rows(tmp)[0]["limited"])

    def test_one_pacer_spaces_every_request_of_the_run_before_its_charge(self):
        t, slept = [0.0], []

        def sleep(s):
            slept.append(round(s, 6))
        pacer = cc.Pacer(60, clock=lambda: t[0], sleep=sleep)
        with tempfile.TemporaryDirectory() as tmp:
            b, port, _ = port_over(self, tmp, Upstream(), concurrency=4, pacer=pacer)
            errs = in_threads([lambda w=w: port.scoped("q%d" % w).call(Task.ROUTE, route_inputs(w)) for w in range(4)])
            self.assertEqual(errs, [None] * 4)
            self.assertEqual(b.count(), 4)
        self.assertEqual(sorted(slept), [1.0, 2.0, 3.0])           # 60 per minute: starts 1 s apart, shared across the 4 workers


@mock.patch.object(rs, "EXPECTED_LEAVES", 1)
class Stage(unittest.TestCase):
    def go(self, n):
        base, s3, man, run = t6.setup_dirs()
        a = t6.args(base, s3, run, concurrency=n, concurrency_reason="test" if n > 2 else "", **vars(margs(rpm=10 ** 6)))
        live = Live(Upstream())
        self.addCleanup(live.close)
        with seeded_node_ids():
            rc = rs.run(a, embedder=t6.RealEmbed(), runner=live.runner, loader=lambda _a: t6.rel.QS,
                        tenant_factory=t6.factory(run), registered_pool=t6.POOL, registered_fresh=t6.rel.REG, manifest_path=man)
        self.assertEqual(rc, 0)
        return run, live

    def test_the_muse_kind_gives_the_same_ledger_at_one_and_four_workers_and_records_the_model(self):
        (r1, live), (r4, _) = self.go(1), self.go(4)
        self.assertEqual((r4 / "done.jsonl").read_bytes(), (r1 / "done.jsonl").read_bytes())
        led = [json.loads(l) for l in (r1 / "done.jsonl").read_text().splitlines()]
        ident = ("muse", MODEL, "openrouter", "low", 4096)
        self.assertTrue(led and all(tuple(x[k] for k in mk.IDENTITY) == ident for x in led))
        stamp = json.loads((r1 / "STAMP.json").read_text())
        self.assertEqual(tuple(stamp[k] for k in mk.IDENTITY), ident)
        self.assertEqual((stamp["rpm"], stamp["port"]["provider"], stamp["port"]["reasoning_effort"]), (10 ** 6, "openrouter", "low"))
        self.assertFalse(any(k.startswith("key") for k in stamp))
        self.assertFalse(any(x in (r1 / "STAMP.json").read_text() for x in (live.key, OR_KEY)))
        state = json.loads((r1 / "STATE.json").read_text())
        self.assertEqual((state["models_reported"], state["providers_reported"]), ([MODEL], ["Meta"]))
        calls = [json.loads(l) for l in (r1 / "calls.jsonl").read_text().splitlines()]
        self.assertTrue(calls and all((c["reasoning_tokens"], c["upstream_provider"]) == (7, "Meta") for c in calls))

    def test_an_early_build_on_another_kind_or_setting_is_refused_to_the_stamped_run(self):
        base, s3, man, run = t6.setup_dirs()
        for pre_kw, now_kw, word in (({}, None, "model_kind"), ({}, {"reasoning_effort": "high"}, "reasoning_effort"),
                                     ({}, {"max_tokens": 2048}, "max_tokens")):
            pre = rs._prebuild_now(t6.args(base, s3, run, **vars(margs(**pre_kw))), rs.fz.ROOT)
            (run / "PREBUILD.json").write_text(json.dumps(pre))
            now = t6.args(base, s3, run) if now_kw is None else t6.args(base, s3, run, **vars(margs(**now_kw)))
            why = rs.check_prereqs(now, embed=t6.RealEmbed(), manifest_path=man, mem_available=lambda: 64000.0)
            self.assertTrue(any("early build's %s" % word in w for w in why), (word, why))

    def test_an_early_build_rerun_or_a_stamped_resume_with_another_setting_is_refused(self):
        base, s3, man, run = t6.setup_dirs()
        low = t6.args(base, s3, run, **vars(margs()))
        (run / "PREBUILD.json").write_text(json.dumps(rs._prebuild_now(low, rs.fz.ROOT)))
        self.assertFalse([w for w in rs._prebuild_prereqs(low, run, rs.fz.ROOT) if "early build must continue" in w])
        high = t6.args(base, s3, run, **vars(margs(reasoning_effort="high")))
        why = rs._prebuild_prereqs(high, run, rs.fz.ROOT)
        self.assertTrue(any("same reasoning_effort as PREBUILD.json" in w for w in why), why)
        (run / "PREBUILD.json").unlink()
        stamp = {k: getattr(low, k) for k in ("commit", "a0_fields", "model")}
        stamp.update(mk.stamp_record(low), cap=low.approved_cap, approval_ref=low.approval_ref, release_ref=low.release_ref)
        (run / "STAMP.json").write_text(json.dumps(stamp))
        for a, bad in ((t6.args(base, s3, run, resume=True, **vars(margs())), False),
                       (t6.args(base, s3, run, resume=True, **vars(margs(max_tokens=2048))), True)):
            why = rs._stamped_prereqs(a, run, man, rs.fz.ROOT)
            self.assertEqual(any("max_tokens differs from the stamp" in w for w in why), bad, why)


if __name__ == "__main__":
    unittest.main()
