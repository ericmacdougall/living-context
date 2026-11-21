"""GoodQA #16441: the Muse Spark HTTPS port (evalkit/muse_api.py). No network: every request goes to a stub server on 127.0.0.1
(loopback test mode, which sends a fixed dummy key) or to a patched `_open` that records the request without sending it. The fake
keys below are recognisable strings in temporary files; the tests assert they appear in no exception, repr, argv or RunOut."""
import json
import os
import re
import tempfile
import threading
import time
import traceback
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from evalkit import muse_api as ma
from evalkit.claude_cli import LIMIT, THROTTLE, AllowanceRefused, CappedPort, StageLimited, StagePaused
from living_context.model.budget import Budget
from living_context.model.port import CallContext, Task

META_KEY = "LLM_4242_MUSEFAKEKEYdoNotLeak0123456789abcdef"
OR_KEY = "sk-or-v1-" + "fa1e" * 16
KEYS = {"meta": META_KEY, "openrouter": OR_KEY}
MODELS = {"meta": "muse-spark-1.3-contributor", "openrouter": "meta/muse-spark-1.3-contributor"}
PATHS = {"meta": "/v1/chat/completions", "openrouter": "/api/v1/chat/completions"}
SECRETS = (META_KEY, OR_KEY, ma.LOOPBACK_DUMMY_KEY)
CTX = CallContext("t", "base", "px", ("base",))
JUDGE = {"query": "q", "text": "t"}


def leaks(*things) -> list:
    return [s for s in SECRETS for t in things if s in str(t)]


def exc_leaks(e: BaseException) -> list:
    """The secrets in an exception's text, repr, args, chained exceptions and the locals of every frame of its traceback."""
    found, seen, stack = [], set(), [e]
    while stack:
        x = stack.pop()
        if x is None or id(x) in seen:
            continue
        seen.add(id(x))
        found += leaks(str(x), repr(x), repr(x.args))
        tb = x.__traceback__
        while tb is not None:
            found += leaks(*[repr(v) for v in tb.tb_frame.f_locals.values()])
            tb = tb.tb_next
        stack += [x.__context__, x.__cause__]
    return found


class Stub:
    """A chat-completions stub on 127.0.0.1. `reply(path, headers, body) -> (status, headers, bytes[, delay_s])`."""

    def __init__(self, reply):
        self.reply, self.requests, self._lock = reply, [], threading.Lock()
        stub = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _any(self):
                n = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(n) if n else b""
                try:
                    body = json.loads(raw) if raw else None
                except ValueError:
                    body = None
                with stub._lock:
                    stub.requests.append({"path": self.path, "headers": dict(self.headers), "body": body, "method": self.command})
                out = stub.reply(self.path, self.headers, body)
                status, hdrs, data = out[:3]
                if len(out) > 3:
                    time.sleep(out[3])
                try:
                    self.send_response(status)
                    for k, v in (hdrs or {}).items():
                        self.send_header(k, v)
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                except (BrokenPipeError, ConnectionResetError):
                    pass

            do_POST = do_GET = _any

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.server.daemon_threads = True
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *a):
        self.server.shutdown()
        self.server.server_close()


def completion(text, model, *, prompt_tokens=120, cached=20, completion_tokens=30, extra=None):
    b = {"id": "gen-1", "object": "chat.completion", "created": 1, "model": model,
         "choices": [{"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": text, "refusal": None}}],
         "usage": {"prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens, "total_tokens": prompt_tokens + completion_tokens,
                   "prompt_tokens_details": {"cached_tokens": cached}, "completion_tokens_details": {"reasoning_tokens": 7}}}
    b.update(extra or {})
    return json.dumps(b).encode()


def err_body(status, *, etype="server_error", code=None, metadata=None, message=None):
    # the stub plants every secret in the server's own text: none of it may come back out of the port
    e = {"message": message or ("leak? %s %s %s" % SECRETS), "type": etype, "param": None, "code": code if code is not None else status}
    if metadata:
        e["metadata"] = metadata
    return json.dumps({"error": e}).encode()


class KeyDir:
    def __init__(self):
        self._d = tempfile.TemporaryDirectory()
        self.root = Path(self._d.name)

    def write(self, name, text, mode=0o600):
        p = self.root / name
        p.write_bytes(text.encode() if isinstance(text, str) else text)
        os.chmod(p, mode)
        return p

    def close(self):
        self._d.cleanup()


class Base(unittest.TestCase):
    def setUp(self):
        self.kd = KeyDir()
        self.addCleanup(self.kd.close)
        self.files = {p: self.kd.write("%s.key" % p, "%s\n" % k) for p, k in KEYS.items()}

    def port(self, provider, stub, **kw):
        return ma.MusePort(self.files[provider], MODELS[provider], provider=provider, _loopback_test_port=stub.port, **kw)

    def assertClean(self, ro, port=None):
        self.assertEqual(leaks(ro.stdout, ro.stderr, repr(ro)), [])
        if port is not None:
            self.assertEqual(leaks(repr(port), port.argv(), vars(port)), [])


# ---- the key file ------------------------------------------------------------------------------------------------------
class KeyFile(Base):
    def refused(self, path, provider="meta", why=None):
        with self.assertRaises(ma.KeyFileError) as cm:
            ma.MusePort(path, provider=provider)
        self.assertIn(str(path), str(cm.exception))
        if why:
            self.assertIn(why, str(cm.exception))
        self.assertEqual(exc_leaks(cm.exception), [])
        return cm.exception

    def test_missing_file_is_refused_naming_the_path(self):
        self.refused(self.kd.root / "nope.key", why="missing")

    def test_a_mode_wider_than_0600_is_refused(self):
        for mode in (0o644, 0o640, 0o604, 0o660, 0o700):
            with self.subTest(mode=oct(mode)):
                self.refused(self.kd.write("m%o.key" % mode, META_KEY + "\n", mode), why="wider than 0600")

    def test_0600_and_0400_are_accepted(self):
        for mode in (0o600, 0o400):
            with self.subTest(mode=oct(mode)):
                p = ma.MusePort(self.kd.write("ok%o.key" % mode, META_KEY, mode))
                self.assertEqual(leaks(repr(p), p.argv(), vars(p)), [])

    def test_empty_and_comment_only_files_are_refused(self):
        self.refused(self.kd.write("e.key", ""), why="empty")
        self.refused(self.kd.write("c.key", "# a comment\n\n   \n"), why="empty")

    def test_a_symlink_a_directory_and_two_entries_are_refused(self):
        link = self.kd.root / "link.key"
        os.symlink(self.files["meta"], link)
        self.refused(link)
        d = self.kd.root / "dir.key"
        d.mkdir()
        self.refused(d)
        self.refused(self.kd.write("two.key", "%s\n%s\n" % (META_KEY, META_KEY)), why="more than one")

    def test_non_ascii_bytes_around_the_key_are_refused_and_nothing_holds_the_bytes(self):
        e = self.refused(self.kd.write("bin.key", META_KEY.encode() + b"\xff\xfe" + META_KEY.encode()), why="not ASCII")
        self.assertIsNone(e.__context__)

    def test_a_key_with_a_space_or_a_wrong_prefix_is_refused(self):
        self.refused(self.kd.write("sp.key", "LLM_1_abc defghijkl"))
        self.refused(self.kd.write("pre.key", "XYZ_1234567890abcdef"), why="expected prefix")

    def test_a_key_file_for_one_provider_is_refused_for_the_other(self):
        self.refused(self.files["meta"], provider="openrouter", why="sk-or-v1-")
        self.refused(self.files["openrouter"], provider="meta", why="LLM_")

    def test_bare_and_name_value_forms_are_read(self):
        sent = []
        for i, text in enumerate([META_KEY, "MODEL_API_KEY=%s" % META_KEY, "export MODEL_API_KEY='%s'" % META_KEY,
                                  '# key\nMODEL_API_KEY = "%s"\n' % META_KEY]):
            p = ma.MusePort(self.kd.write("f%d.key" % i, text))
            with mock.patch.object(p, "_open", side_effect=lambda req, t: sent.append(req.get_header("Authorization")) or _raise_timeout()):
                ro = p("hello")
            self.assertEqual(ro.rc, 124)
            self.assertClean(ro, p)
        self.assertEqual(sent, ["Bearer " + META_KEY] * 4)


def _raise_timeout():
    raise TimeoutError("stub")


# ---- the host --------------------------------------------------------------------------------------------------------
class Host(Base):
    BAD = {
        "meta": ["http://api.meta.ai/v1", "https://evil.example/v1", "https://api.meta.ai.evil.example/v1",
                 "https://api.meta.ai@evil.example/v1", "https://user@api.meta.ai/v1", "https://api.meta.ai:8443/v1",
                 "https://api.meta.ai./v1", "https://127.0.0.1/v1", "http://127.0.0.1:1/v1", "https://api.meta.ai/v2",
                 "https://api.meta.ai/v1?x=1", "https://api.meta.ai/v1#f", "https://openrouter.ai/api/v1", "not a url", "ftp://api.meta.ai/v1"],
        "openrouter": ["https://api.meta.ai/v1", "http://openrouter.ai/api/v1", "https://openrouter.ai.evil.example/api/v1",
                       "https://openrouter.ai/v1", "https://evil.example/api/v1"],
    }

    def test_a_foreign_or_plain_http_base_url_is_refused_before_any_request(self):
        for provider, urls in self.BAD.items():
            for url in urls:
                with self.subTest(provider=provider, url=url):
                    with mock.patch.object(ma.MusePort, "_open", side_effect=AssertionError("a request was made")):
                        with self.assertRaises(ma.MuseConfigError) as cm:
                            ma.MusePort(self.files[provider], base_url=url, provider=provider)
                    self.assertEqual(exc_leaks(cm.exception), [])

    def test_the_documented_base_url_is_accepted(self):
        for provider, url in (("meta", "https://api.meta.ai/v1"), ("meta", "https://API.meta.ai:443/v1/"),
                              ("openrouter", "https://openrouter.ai/api/v1")):
            with self.subTest(url=url):
                ma.MusePort(self.files[provider], base_url=url, provider=provider)

    def test_an_unknown_provider_and_a_loopback_with_a_base_url_are_refused(self):
        with self.assertRaises(ma.MuseConfigError):
            ma.MusePort(self.files["meta"], provider="other")
        with self.assertRaises(ma.MuseConfigError):
            ma.MusePort(self.files["meta"], base_url="https://api.meta.ai/v1", _loopback_test_port=8080)

    def test_real_mode_sends_the_file_key_only_to_the_selected_providers_host(self):
        for provider in ("meta", "openrouter"):
            with self.subTest(provider=provider):
                p = ma.MusePort(self.files[provider], provider=provider)
                seen = []

                def fake_open(req, timeout):
                    seen.append((req.full_url, req.get_header("Authorization"), json.loads(req.data)))
                    raise TimeoutError("not sent")
                with mock.patch.object(p, "_open", side_effect=fake_open):
                    ro = p("hello")
                url, auth, body = seen[0]
                host = ma.PROVIDERS[provider].host
                self.assertEqual(url, "https://%s%s" % (host, PATHS[provider]))
                self.assertEqual(auth, "Bearer " + KEYS[provider])
                self.assertEqual(body["model"], MODELS[provider])
                if provider == "openrouter":
                    self.assertEqual(body["provider"], {"order": ["meta"], "only": ["meta"], "allow_fallbacks": False,
                                                        "require_parameters": True})
                    self.assertNotIn("models", body)
                    self.assertNotIn("route", body)
                else:
                    self.assertNotIn("provider", body)
                self.assertClean(ro, p)

    def test_a_key_file_swapped_after_construction_is_not_sent_to_the_other_host(self):
        p = ma.MusePort(self.files["openrouter"], provider="openrouter")
        self.files["openrouter"].write_text(META_KEY + "\n")          # a Meta key now sits in the OpenRouter port's key file
        with mock.patch.object(p, "_open", side_effect=AssertionError("a request was made")):
            ro = p("hello")
        self.assertEqual((ro.rc, json.loads(ro.stdout)["error"]), (1, "key_file_unavailable"))
        self.assertClean(ro, p)

    def test_the_opener_has_no_proxy_and_no_redirect_handler(self):
        p = ma.MusePort(self.files["meta"])
        kinds = [type(h) for h in p._opener().handlers]
        import urllib.request as ur
        self.assertFalse(any(issubclass(k, (ur.ProxyHandler, ur.HTTPRedirectHandler, ur.HTTPHandler)) and k is not ur.HTTPSHandler
                             for k in kinds))
        self.assertIn(ur.HTTPSHandler, kinds)


# ---- responses -------------------------------------------------------------------------------------------------------
class Responses(Base):
    def run_one(self, provider, reply, **kw):
        with Stub(reply) as s:
            p = self.port(provider, s, **kw)
            ro = p("judge this")
        self.assertClean(ro, p)
        return ro, json.loads(ro.stdout), s

    @staticmethod
    def each():
        return ("meta", "openrouter")          # a plain tuple: a subTest inside a generator turns a failure into GeneratorExit noise

    def test_a_normal_answer_maps_to_the_envelope_with_token_counts(self):
        for pv in self.each():
            ro, env, s = self.run_one(pv, lambda *a: (200, {}, completion('{"score": 1}', MODELS[pv])), max_tokens=500)
            self.assertEqual((ro.rc, ro.stderr, env["is_error"], env["result"]), (0, "", False, '{"score": 1}'))
            self.assertEqual(env["usage"], {"input_tokens": 100, "cache_read_input_tokens": 20, "cache_creation_input_tokens": 0,
                                            "output_tokens": 30})
            self.assertEqual((env["model"], env["throttled"], env["limited"], env["error"]), (MODELS[pv], False, False, None))
            self.assertEqual((env["muse"]["reasoning_tokens"], env["muse"]["finish_reason"]), (7, "stop"))
            self.assertGreater(ro.latency_s, 0)
            req = s.requests[0]
            self.assertEqual((len(s.requests), req["path"]), (1, PATHS[pv]))
            self.assertEqual(req["headers"]["Authorization"], "Bearer " + ma.LOOPBACK_DUMMY_KEY)   # never the file's key
            self.assertEqual(req["body"]["messages"][1], {"role": "user", "content": "judge this"})
            self.assertEqual(req["body"][ma.PROVIDERS[pv].max_tokens_field], 500)

    def test_429_sets_throttled_with_retry_after(self):
        for pv in self.each():
            ro, env, _ = self.run_one(pv, lambda *a: (429, {"Retry-After": "7"}, err_body(429, etype="rate_limit_error",
                                                                                           code="rate_limit_exceeded")))
            self.assertEqual((ro.rc, env["throttled"], env["limited"], env["is_error"], env["muse"]["retry_after_s"]), (0, True, False, True, 7))
            self.assertTrue(THROTTLE.search(ro.stderr) and not LIMIT.search(ro.stderr))

    def test_503_overloaded_is_a_throttle(self):
        for pv in self.each():
            ro, env, _ = self.run_one(pv, lambda *a: (503, {"Retry-After": "60"}, err_body(503, code="service_overloaded")))
            self.assertEqual((ro.rc, env["throttled"], env["muse"]["http_status"]), (0, True, 503))

    def test_402_billing_sets_limited(self):
        for pv in self.each():
            ro, env, _ = self.run_one(pv, lambda *a: (402, {}, err_body(402, etype="billing_error", code="billing_not_configured")))
            self.assertEqual((ro.rc, env["limited"], env["throttled"]), (1, True, False))
            self.assertTrue(LIMIT.search(ro.stderr))

    def test_openrouter_in_flight_budget_402_is_a_throttle_and_a_budget_403_is_a_limit(self):
        ro, env, _ = self.run_one("openrouter", lambda *a: (402, {"Retry-After": "3"}, err_body(
            402, metadata={"limit_source": "openrouter_in_flight_budget"})))
        self.assertEqual((ro.rc, env["throttled"], env["limited"]), (0, True, False))
        ro, env, _ = self.run_one("openrouter", lambda *a: (403, {}, err_body(403, metadata={"limit_source": "openrouter_key_limit"})))
        self.assertEqual((ro.rc, env["limited"]), (1, True))
        ro, env, _ = self.run_one("meta", lambda *a: (403, {}, err_body(403, etype="invalid_request_error")))
        self.assertEqual((ro.rc, env["limited"], env["error"]), (1, False, "http_403"))

    def test_5xx_malformed_json_and_missing_choices_are_errors(self):
        cases = [(lambda *a: (500, {}, err_body(500)), "http_500"), (lambda *a: (502, {}, b"<html>bad gateway</html>"), "http_502"),
                 (lambda *a: (200, {}, b"{not json " + META_KEY.encode()), "malformed_json"),
                 (lambda *a: (200, {}, json.dumps({"id": "x", "model": "m", "usage": {}}).encode()), "missing_choices"),
                 (lambda *a: (200, {}, json.dumps({"choices": [], "model": "m"}).encode()), "missing_choices"),
                 (lambda *a: (200, {}, json.dumps({"choices": [{"index": 0}], "model": "m"}).encode()), "missing_message")]
        for pv in self.each():
            for reply, error in cases:
                ro, env, _ = self.run_one(pv, reply)
                self.assertEqual((ro.rc, env["is_error"], env["error"], env["throttled"], env["limited"]), (1, True, error, False, False))
                self.assertFalse(THROTTLE.search(ro.stderr) or LIMIT.search(ro.stderr))

    def test_an_error_inside_a_200_body_is_an_error_and_its_code_is_mapped(self):
        ok = lambda pv, **x: completion("partial", MODELS[pv], extra=x)
        ro, env, _ = self.run_one("openrouter", lambda *a: (200, {}, ok("openrouter", error={"code": 502, "message": OR_KEY})))
        self.assertEqual((ro.rc, env["error"], env["muse"]["embedded"]), (1, "provider_error", True))
        body = json.loads(ok("openrouter"))
        body["choices"][0].update(finish_reason="error", error={"code": 429, "message": "upstream"})
        ro, env, _ = self.run_one("openrouter", lambda *a: (200, {}, json.dumps(body).encode()))
        self.assertEqual((ro.rc, env["throttled"]), (0, True))
        body["choices"][0].update(error={"code": 402, "message": "credits"})
        ro, env, _ = self.run_one("openrouter", lambda *a: (200, {}, json.dumps(body).encode()))
        self.assertEqual((ro.rc, env["limited"]), (1, True))
        body["choices"][0].pop("error")
        ro, env, _ = self.run_one("openrouter", lambda *a: (200, {}, json.dumps(body).encode()))
        self.assertEqual((ro.rc, env["error"]), (1, "provider_error"))

    def test_a_response_naming_a_different_model_is_an_error(self):
        for pv in self.each():
            for other in ("muse-spark-1.3", "meta/muse-spark-1.2-contributor", "", None, META_KEY):
                ro, env, _ = self.run_one(pv, lambda *a: (200, {}, completion("x", other)))
                self.assertEqual((ro.rc, env["error"], env["result"]), (1, "model_mismatch", ""))

    def test_openrouters_pinned_snapshot_name_is_accepted_and_reported(self):
        snap = "meta/muse-spark-1.3-contributor-20260902"
        ro, env, _ = self.run_one("openrouter", lambda *a: (200, {}, completion("x", snap)))
        self.assertEqual((ro.rc, env["model"], env["modelUsage"]), (0, snap, {snap: {}}))
        ro, env, _ = self.run_one("meta", lambda *a: (200, {}, completion("x", snap)))      # not an alias on Meta's own API
        self.assertEqual(env["error"], "model_mismatch")

    def test_null_content_and_length_stop_are_an_empty_answer_not_a_crash(self):
        body = json.loads(completion("x", MODELS["meta"]))
        body["choices"][0].update(finish_reason="length", message={"role": "assistant", "content": None, "refusal": None})
        ro, env, _ = self.run_one("meta", lambda *a: (200, {}, json.dumps(body).encode()))
        self.assertEqual((ro.rc, env["is_error"], env["result"], env["muse"]["finish_reason"], env["muse"]["content_null"]),
                         (0, False, "", "length", True))

    def test_a_timeout_is_rc_124(self):
        for pv in self.each():
            ro, env, _ = self.run_one(pv, lambda *a: (200, {}, completion("late", MODELS[pv]), 1.5), timeout=0.3)
            self.assertEqual((ro.rc, env["error"]), (124, "timeout"))

    def test_a_redirect_is_refused_and_not_followed(self):
        for pv in self.each():
            for code in (301, 302, 307, 308):
                def reply(path, headers, body, code=code):
                    if path.endswith("/elsewhere"):
                        return 200, {}, completion("followed", MODELS[pv])
                    return code, {"Location": "/elsewhere"}, b""
                ro, env, s = self.run_one(pv, reply)
                self.assertEqual((ro.rc, env["error"], env["muse"]["http_status"]), (1, "redirect_refused", code))
                self.assertEqual([r["path"] for r in s.requests], [PATHS[pv]])

    def test_server_text_that_looks_like_a_throttle_does_not_make_an_error_a_throttle(self):
        reply = lambda *a: (400, {}, err_body(400, etype="rate_limit_error", code="usage limit hit your limit",
                                              message="rate limit usage limit credit balance 429"))
        ro, env, _ = self.run_one("meta", reply)
        self.assertEqual((ro.rc, env["throttled"], env["limited"], env["error"]), (1, False, False, "http_400"))
        self.assertFalse(THROTTLE.search(ro.stdout) or LIMIT.search(ro.stdout) or THROTTLE.search(ro.stderr) or LIMIT.search(ro.stderr))

    def test_secrets_the_server_echoes_in_any_field_are_scrubbed(self):
        # the stub echoes both fake file keys and the dummy into the answer text, the id, the model and the error identifiers
        echo = " ".join(SECRETS)
        for pv in self.each():
            cases = [
                lambda *a: (200, {}, completion("answer " + echo, MODELS[pv], extra={"id": OR_KEY})),
                lambda *a: (200, {}, completion("x", OR_KEY)),
                lambda *a: (500, {}, err_body(500, etype=META_KEY, code=OR_KEY)),
                lambda *a: (429, {"Retry-After": "1"}, err_body(429, etype=OR_KEY, code=META_KEY)),
                lambda *a: (402, {}, err_body(402, etype=META_KEY, code=ma.LOOPBACK_DUMMY_KEY)),
                lambda *a: (200, {}, completion("x", MODELS[pv], extra={"error": {"code": 502, "type": META_KEY}})),
            ]
            for reply in cases:
                ro, env, _ = self.run_one(pv, reply)          # run_one asserts no secret in stdout, stderr, repr or argv
            ro, env, _ = self.run_one(pv, cases[0])
            self.assertEqual(env["result"], "answer " + " ".join([ma.REDACTED] * 3))

    def test_every_fixed_error_phrase_is_inert(self):
        for err in ("redirect_refused", "response_too_large", "malformed_json", "missing_choices", "missing_message", "provider_error",
                    "model_mismatch", "timeout", "connection_error", "key_file_unavailable", "internal_error", "http_400", "http_500"):
            self.assertFalse(THROTTLE.search(ma.MSG_ERROR % err) or LIMIT.search(ma.MSG_ERROR % err), err)
        self.assertTrue(THROTTLE.search(ma.MSG_THROTTLED % 429) and not LIMIT.search(ma.MSG_THROTTLED % 429))
        self.assertTrue(LIMIT.search(ma.MSG_LIMITED % 402))

    def test_bad_parameters_are_refused(self):
        f = self.files["meta"]
        for kw in ({"max_tokens": 0}, {"max_tokens": True}, {"reasoning_effort": "none"}, {"reasoning_effort": "max"},
                   {"response_format": "json_schema"}, {"timeout": 0}, {"model": ""}, {"model": "a b"}):
            with self.subTest(kw=kw), self.assertRaises(ma.MuseConfigError):
                ma.MusePort(f, **kw)


# ---- through the real CappedPort ---------------------------------------------------------------------------------------
class ThroughCappedPort(Base):
    def capped(self, d, stub, provider="meta"):
        runner = self.port(provider, stub)
        return CappedPort(runner, Budget(Path(d) / "counter", 50), Path(d) / "calls.jsonl", model_id=runner.model,
                          argv_record=runner.argv())

    def test_success_throttle_limit_and_failure_streak_read_as_the_cli_path_does(self):
        script = []
        ans = json.dumps({"score": 1, "relevant": True})

        def reply(*a):
            kind = script.pop(0)
            if kind == "ok":
                return 200, {}, completion(ans, MODELS["meta"])
            if kind == "429":
                return 429, {"Retry-After": "1"}, err_body(429, etype="rate_limit_error")
            if kind == "402":
                return 402, {}, err_body(402, etype="billing_error")
            return 500, {}, err_body(500)
        with tempfile.TemporaryDirectory() as d, Stub(reply) as s:
            p = self.capped(d, s)
            script[:] = ["ok"]
            res = p.call(Task.JUDGE_RELEVANCE, JUDGE, ctx=CTX)
            self.assertTrue(res.ok)
            self.assertEqual((p.rows[-1]["input_tokens"], p.rows[-1]["output_tokens"], p.rows[-1]["cache_read_input_tokens"]), (100, 30, 20))
            script[:] = ["429"] * 6                       # a burst of throttles must not trip the five-failure streak
            for _ in range(6):
                with self.assertRaises(StagePaused):
                    p.call(Task.JUDGE_RELEVANCE, JUDGE, ctx=CTX)
                self.assertTrue(p.rows[-1].get("throttled"))
            script[:] = ["402"]
            with self.assertRaises(AllowanceRefused):
                p.call(Task.JUDGE_RELEVANCE, JUDGE, ctx=CTX)
            self.assertTrue(p.rows[-1].get("limited"))
            script[:] = ["500"] * 5                       # real failures still stop the stage
            for i in range(4):
                self.assertFalse(p.call(Task.JUDGE_RELEVANCE, JUDGE, ctx=CTX).ok)
            with self.assertRaises(StageLimited):
                p.call(Task.JUDGE_RELEVANCE, JUDGE, ctx=CTX)
            log = (Path(d) / "calls.jsonl").read_text()
            self.assertEqual(leaks(log), [])


# ---- threads ---------------------------------------------------------------------------------------------------------
class Threads(Base):
    def test_16_threads_each_get_their_own_answer(self):
        def reply(path, headers, body):
            tag = re.search(r"tag-(\d+)", body["messages"][1]["content"]).group(1)
            return 200, {}, completion("answer-%s" % tag, MODELS["meta"]), (int(tag) % 5) * 0.02
        with Stub(reply) as s:
            p = self.port("meta", s)
            out, errs = {}, []

            def work(i):
                try:
                    for j in range(3):
                        n = i * 10 + j
                        ro = p("question tag-%d" % n)
                        out[n] = json.loads(ro.stdout)["result"]
                except Exception as e:                    # pragma: no cover - reported below
                    errs.append(e)
            ts = [threading.Thread(target=work, args=(i,)) for i in range(16)]
            for t in ts:
                t.start()
            for t in ts:
                t.join()
        self.assertEqual(errs, [])
        self.assertEqual(out, {i * 10 + j: "answer-%d" % (i * 10 + j) for i in range(16) for j in range(3)})
        self.assertEqual(len(s.requests), 48)


if __name__ == "__main__":
    unittest.main()
