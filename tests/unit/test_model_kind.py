"""The `muse` model kind (evalkit.model_kind) with a FAKE MusePort (no key, no network): the guards never open the key file, the port
is built from --key-file/--model/--base-url/--api-timeout, one pacer per run spaces requests at --rpm, a 429 or 503 pauses with
Retry-After as the back-off floor and is not a failure of the streak, a quota refusal stops the stage at once, token counts are in
every call row, and the S6-H1 stage gives the same ledger at 1 and 4 workers, with the model recorded in the stamp and the ledger."""
import argparse
import builtins
import io
import json
import os
import sys
import tempfile
import threading
import types
import unittest
from pathlib import Path
from unittest import mock

from evalkit import claude_cli as cc, model_kind as mk, run_h1, run_s6h1 as rs
from evalkit.s2_ports import Journal, ResilientPort
from living_context.model.budget import Budget
from living_context.model.port import Task
from tests.security.test_run_s2_es import Runner
from tests.unit import test_run_h1_guards as hg, test_run_s6h1 as t6
from tests.unit.test_parallel import in_threads, route_inputs, seeded_node_ids

MODEL = "meta/muse-spark-1.3-contributor"


def key_file(mode=0o600):
    p = Path(tempfile.mkdtemp()) / "muse.key"
    p.write_text("NOT-A-KEY-MARKER")
    os.chmod(p, mode)
    return str(p)


def margs(**kw):
    a = dict(model_kind="muse", model=MODEL, provider="openrouter", key_file=key_file(), key_secret_project="", key_secret_name="",
             base_url="", rpm=140, api_timeout=30.0, binary="")
    a.update(kw)
    return argparse.Namespace(**a)


class FakeMuse:
    """Answers through the stage test Runner (the same outputs as the CLI kind); scripted HTTP outcomes by call number."""
    def __init__(self, *, throttle=(), unavailable=(), quota=(), error=(), mismatch=(), retry_after=None):
        self.runner, self.n, self.lock = Runner(), 0, threading.Lock()
        self.throttle, self.unavailable, self.quota, self.error = set(throttle), set(unavailable), set(quota), set(error)
        self.mismatch = set(mismatch)
        self.retry_after, self.systems = retry_after, []

    def call(self, prompt, system=None):
        with self.lock:
            self.n += 1
            k = self.n
            self.systems.append(system)
        if k in self.throttle:
            return {"text": None, "status": 429, "throttled": True, "retry_after_s": self.retry_after}
        if k in self.unavailable:
            return {"text": None, "status": 503, "retry_after_s": self.retry_after}
        if k in self.quota:
            return {"text": None, "status": 402, "limited": True, "error": "billing_error"}
        if k in self.mismatch:
            return {"text": "{}", "status": 200, "model": "some/other-model", "error": "model_mismatch"}
        if k in self.error:
            return {"text": None, "status": 500, "error": "server_error"}
        with self.lock:
            ro = self.runner(prompt)
        return {"text": json.loads(ro.stdout)["result"], "status": 200, "input_tokens": 1200, "output_tokens": 40,
                "reasoning_tokens": 300, "latency_s": 0.01, "model": MODEL}


def port_over(tmp, fake, *, concurrency=1, sleep=None, pacer=None, cap=1000, **kw):
    tmp = Path(tmp)
    b = Budget(tmp / "counter", cap)
    inner = cc.CappedPort(mk.MuseRunner(fake), b, tmp / "calls.jsonl", model_id=MODEL, argv_record=mk.argv_record(margs()), pacer=pacer)
    delays = []
    port = ResilientPort(inner, b, Journal(tmp / "journal.jsonl"), tmp / "STOP", sleep=sleep or delays.append, base_delay_s=1.0,
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
                         (dict(key_file=key_file(0o644)), "mode 0600"), (dict(rpm=141), "--rpm"), (dict(rpm=0), "--rpm"),
                         (dict(base_url="http://api.example"), "https"), (dict(model=""), "--model"),
                         (dict(provider="other"), "--provider")):
            why = mk.check(margs(**kw))
            self.assertTrue(why and all(word in w for w in why), (kw, why))
        self.assertTrue(mk.check(margs(model_kind="other")))
        self.assertEqual(mk.check(margs(key_file="", key_secret_project="p", key_secret_name="n")), [])

    def test_the_stage_runners_take_the_muse_kind_and_refuse_a_resume_across_kinds(self):
        a = rs.parser().parse_args(["--dataset", "d", "--es-url", "u", "--es-ca", "c", "--es-pw-file", "p", "--run-dir", "r",
                                    "--stamp-registry", "s", "--embed-dir", "e", "--model", MODEL, "--rates-from", "x",
                                    "--mem-proof", "m", "--approved-cap", "1", "--approval-ref", "a", "--commit", "c",
                                    "--model-kind", "muse", "--key-file", "k", "--rpm", "70"])
        self.assertEqual((a.model_kind, a.provider, a.key_file, a.rpm, a.base_url, a.binary), ("muse", "openrouter", "k", 70, "", ""))
        d = tempfile.mkdtemp()
        (Path(d) / "STAMP.json").write_text(json.dumps({"stage": "S3", "commit": "abc1234", "cap": 6000, "n_questions": 102,
                                                         "a0_fields": "strong", "shuffled_n": 50}))
        why = run_h1.check_prereqs(hg.args(stage="S3", n_questions=102, shuffled_n=50, run_dir=d, resume=True, a0_fields="strong",
                                           **vars(margs())), embed=hg.RealEmbed())
        self.assertTrue(any("model kind or model differs" in w for w in why), why)


class Port(unittest.TestCase):
    def test_the_port_is_built_from_one_key_source_the_provider_model_and_timeout_with_one_pacer_at_rpm(self):
        made = []
        fake_mod = types.SimpleNamespace(MusePort=lambda **kw: made.append(kw) or FakeMuse())
        import evalkit
        for a, src in ((margs(rpm=70, api_timeout=45.0), "file"),
                       (margs(rpm=70, api_timeout=45.0, key_file="", key_secret_project="P", key_secret_name="N"), "secret")):
            with mock.patch.dict(sys.modules, {"evalkit.muse_api": fake_mod}), \
                    mock.patch.object(evalkit, "muse_api", fake_mod, create=True):
                p = mk.capped_port(a, Path(tempfile.mkdtemp()), Budget(Path(tempfile.mkdtemp()) / "counter", 10))
            want = {"model": MODEL, "provider": "openrouter", "base_url": None, "timeout": 45.0}
            want.update({"key_file": a.key_file} if src == "file" else {"key_secret_project": "P", "key_secret_name": "N"})
            self.assertEqual(made[-1], want)
            self.assertEqual(p._pacer.rpm, 70)
            self.assertEqual(p._argv, ["muse", "provider=openrouter", "model=" + MODEL, "effort=provider-default", "rpm=70"])
            self.assertFalse(any(x in " ".join(p._argv) for x in ("P", "N", a.key_file or "\0")))   # no key source recorded

    def test_an_answer_records_its_token_counts_and_the_system_prompt_is_sent(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake = FakeMuse()
            b, port, _ = port_over(tmp, fake)
            self.assertTrue(port.call(Task.ROUTE, route_inputs(1)).ok)
            r = rows(tmp)[0]
            self.assertEqual((r["input_tokens"], r["output_tokens"], r["reasoning_tokens"], r["http_status"]), (1200, 40, 300, 200))
            self.assertEqual((r["model_reported"], r["schema_ok"]), (MODEL, True))
            self.assertEqual(fake.systems, [cc.SYSTEM_PROMPT])

    def test_a_429_or_503_backs_off_at_least_retry_after_and_is_asked_again(self):
        for kind in ("throttle", "unavailable"):
            with tempfile.TemporaryDirectory() as tmp:
                b, port, delays = port_over(tmp, FakeMuse(**{kind: (1,)}, retry_after=42.0))
                self.assertTrue(port.call(Task.ROUTE, route_inputs(1)).ok)
                self.assertEqual(delays, [42.0])                        # base delay 1 s; Retry-After is the floor
                self.assertEqual(b.count(), 2)
                self.assertTrue(rows(tmp)[0]["throttled"])

    def test_throttles_never_count_as_failures_of_the_streak(self):
        with tempfile.TemporaryDirectory() as tmp:          # 6 calls, each throttled once then answered: more than the streak of 5
            b, port, _ = port_over(tmp, FakeMuse(throttle=(1, 3, 5, 7, 9, 11)))
            self.assertTrue(all(port.call(Task.ROUTE, route_inputs(i)).ok for i in range(6)))
        with tempfile.TemporaryDirectory() as tmp:          # 5 429s in a row on one call, no answer between, do not stop the stage
            b, port, _ = port_over(tmp, FakeMuse(throttle=range(1, 6)), max_retries=6)
            self.assertTrue(port.call(Task.ROUTE, route_inputs(1)).ok)
        with tempfile.TemporaryDirectory() as tmp:          # 5 real API errors in a row still stop the stage
            b, port, _ = port_over(tmp, FakeMuse(error=range(1, 100)))
            with self.assertRaises(cc.StageLimited):
                for i in range(10):
                    port.call(Task.ROUTE, route_inputs(i))

    def test_a_reported_model_mismatch_stops_the_stage_at_once_and_is_not_retried(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake = FakeMuse(mismatch=(2,))
            b, port, delays = port_over(tmp, fake, concurrency=4, adaptive=True, start_concurrency=4)
            self.assertTrue(port.scoped("q").call(Task.ROUTE, route_inputs(1)).ok)
            with self.assertRaises(cc.StageLimited) as e:
                port.scoped("q").call(Task.ROUTE, route_inputs(2))
            self.assertNotIsInstance(e.exception, cc.AllowanceRefused)
            self.assertEqual((b.count(), delays, fake.n), (2, [], 2))
            self.assertEqual(rows(tmp)[1]["error"], "model_mismatch")

    def test_a_quota_refusal_stops_the_stage_at_once_even_at_adaptive_concurrency(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake = FakeMuse(quota=(1,))
            b, port, delays = port_over(tmp, fake, concurrency=4, adaptive=True, start_concurrency=4)
            with self.assertRaises(cc.StageLimited) as e:
                port.scoped("q").call(Task.ROUTE, route_inputs(1))
            self.assertNotIsInstance(e.exception, cc.AllowanceRefused)             # not the retried kind
            self.assertEqual((b.count(), delays, fake.n), (1, [], 1))
            self.assertTrue(rows(tmp)[0]["limited"])

    def test_one_pacer_spaces_every_request_of_the_run_before_its_charge(self):
        t, slept = [0.0], []

        def sleep(s):
            slept.append(round(s, 6))
        pacer = cc.Pacer(60, clock=lambda: t[0], sleep=sleep)
        with tempfile.TemporaryDirectory() as tmp:
            b, port, _ = port_over(tmp, FakeMuse(), concurrency=4, pacer=pacer)
            errs = in_threads([lambda w=w: port.scoped("q%d" % w).call(Task.ROUTE, route_inputs(w)) for w in range(4)])
            self.assertEqual(errs, [None] * 4)
            self.assertEqual(b.count(), 4)
        self.assertEqual(sorted(slept), [1.0, 2.0, 3.0])           # 60 per minute: starts 1 s apart, shared across the 4 workers


@mock.patch.object(rs, "EXPECTED_LEAVES", 1)
class Stage(unittest.TestCase):
    def go(self, n):
        base, s3, man, run = t6.setup_dirs()
        a = t6.args(base, s3, run, concurrency=n, concurrency_reason="test" if n > 2 else "", **vars(margs(rpm=10 ** 6)))
        with seeded_node_ids():
            rc = rs.run(a, embedder=t6.RealEmbed(), runner=mk.MuseRunner(FakeMuse()), loader=lambda _a: t6.rel.QS,
                        tenant_factory=t6.factory(run), registered_pool=t6.POOL, registered_fresh=t6.rel.REG, manifest_path=man)
        self.assertEqual(rc, 0)
        return run

    def test_the_muse_kind_gives_the_same_ledger_at_one_and_four_workers_and_records_the_model(self):
        r1, r4 = self.go(1), self.go(4)
        self.assertEqual((r4 / "done.jsonl").read_bytes(), (r1 / "done.jsonl").read_bytes())
        led = [json.loads(l) for l in (r1 / "done.jsonl").read_text().splitlines()]
        self.assertTrue(led and all((x["model_kind"], x["model"], x["effort"]) == ("muse", MODEL, mk.EFFORT) for x in led))
        stamp = json.loads((r1 / "STAMP.json").read_text())
        self.assertEqual((stamp["model_kind"], stamp["provider"], stamp["model"], stamp["effort"]), ("muse", "openrouter", MODEL, mk.EFFORT))
        self.assertFalse(any(k.startswith("key") for k in stamp))
        self.assertEqual(json.loads((r1 / "STATE.json").read_text())["models_reported"], [MODEL])
        calls = [json.loads(l) for l in (r1 / "calls.jsonl").read_text().splitlines()]
        self.assertTrue(all(c["reasoning_tokens"] == 300 for c in calls))

    def test_a_prebuild_on_one_kind_is_refused_to_a_stamped_run_on_the_other(self):
        base, s3, man, run = t6.setup_dirs()
        (run / "PREBUILD.json").write_text(json.dumps(dict(rs._prebuild_now(t6.args(base, s3, run, **vars(margs())), rs.fz.ROOT))))
        why = rs.check_prereqs(t6.args(base, s3, run), embed=t6.RealEmbed(), manifest_path=man, mem_available=lambda: 64000.0)
        self.assertTrue(any("early build's model_kind" in w for w in why), why)


if __name__ == "__main__":
    unittest.main()
