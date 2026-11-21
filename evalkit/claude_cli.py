"""The capped real-model port: the headless Claude Code CLI on the subscription (BUILD-PLAN Step 23; EXPERIMENT 10, X5, X7).

It lives in `evalkit/` and not in `living_context/` because the product package may not import `subprocess` (AC-17,
tests/unit/test_no_model_calls.py); only the evaluation kit calls a model. It offers the same `call(task, inputs, ctx=)`
interface as the stub port, so `Recall` and `build_tree` run over it unchanged.

Rules enforced here, each with a test that uses a FAKE runner (no process is spawned by the tests):
  * the durable counter is charged BEFORE the process is spawned, a failed or malformed call still counts, and at the cap the
    port refuses without spawning anything (call cap + 1 is refused);
  * a prompt carrying a gold-style id or a registered forbidden marker is refused before the counter is touched;
  * no cache sits in front of the counter, so one process and one counter file decide every call;
  * the CLI runs from an EMPTY working directory, no tools, no slash commands or skills, no MCP, no session persistence,
    hooks disabled, instruction files and auto-memory off, and its prompt is sent on stdin (never argv);
  * a throttle response stops the stage (`StagePaused`) and is recorded; it is not scored as a model failure.

Authentication is whatever the host's CLI already holds (a stored subscription login). Nothing here reads, copies or prints a
credential; the child inherits the environment unchanged except for the switches named in `CHILD_ENV`.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from living_context.graph.edges import TYPES as EDGE_TYPES
from living_context.model.budget import Budget, BudgetExceeded
from living_context.model.port import SCHEMA, CallContext, CallResult, PortRefused, Task, schema_ok

from .dataset import DatasetError, assert_model_safe

PROMPT_VERSION = "v1-real"
CHILD_ENV = {"CLAUDE_CODE_DISABLE_CLAUDE_MDS": "1", "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1", "DISABLE_AUTOUPDATER": "1"}
SYSTEM_PROMPT = ("You are one component of a memory-retrieval pipeline. Reply with exactly one JSON object and nothing "
                 "else: no prose, no code fences. The JSON must match the schema stated in the user message.")
THROTTLE = re.compile(r"rate.?limit|usage limit|overloaded|too many requests|\b429\b|\b529\b", re.I)


# An allowance refusal (weekly, session or usage limit) does not clear by waiting a few minutes: it is a hard stop, never retried.
LIMIT = re.compile(r"hit your (?:\w+ )*limit|(?:weekly|daily|monthly|session|5.?hour|usage|plan|credit) limit"
                   r"|resets? (?:at|in|on) |out of (?:extra )?usage|credit balance", re.I)
MAX_CLI_FAILURE_STREAK = 5      # consecutive non-zero exits of the CLI (any text) stop the stage: something is wrong, do not burn the cap


class StageLimited(Exception):
    """The allowance is exhausted or the CLI keeps failing: the stage stops with its state file. Never retried, never scored."""


class AllowanceRefused(StageLimited):
    """An allowance or usage-limit response (LIMIT). `ResilientPort` with an adaptive concurrency halves and backs off a bounded number
    of times before it stops the stage; a fixed concurrency stops at once."""


class StagePaused(Exception):
    """A throttle response: the stage pauses, the event is recorded, and it is not a scored failure. `retry_after_s` carries an
    API's Retry-After (seconds), which the back-off honours as a floor."""
    def __init__(self, msg: str = "", retry_after_s: Optional[float] = None):
        super().__init__(msg)
        self.retry_after_s = retry_after_s


class Throttled(Exception):
    pass


# ---- prompts --------------------------------------------------------------------------------------------------------
def _j(x: Any) -> str:
    return json.dumps(x, ensure_ascii=False, separators=(",", ":"))


def render(task: str, i: Mapping[str, Any]) -> str:
    """The user message of one call. Inputs are exactly what the stub backend receives; labels c1..cN come from the caller."""
    if task == "route":
        return ("TASK route. Below are a prompt and the child entries of one index holder. Decide which children may be "
                "relevant to the prompt.\nPrompt: %s\nChildren (JSON): %s\nReturn {\"selected\": [labels, at most %d], "
                "\"reasons\": {label: a reason of at most 12 words in your own words, never quoting the child text}, "
                "\"confidence\": {label: integer 0 to 3}}. Use only the labels shown. Select none if none may be relevant."
                % (i["query"], _j(i["children"]), int(i.get("b_fanout", 6))))
    if task == "build_l1":
        leaves = [{"label": "c%d" % n, "title": l.get("title", ""), "text": l["text"]} for n, l in enumerate(i["leaves"], 1)]
        return ("TASK build_l1. Below are %d leaves of memory text. For each leaf, in the same order, give a short gist "
                "(one or two sentences), up to 3 short facts and up to 4 entity terms; then write one summary of 2 to 4 "
                "sentences that covers the topics of all the leaves.\nLeaves (JSON): %s\nReturn {\"entries\": [{\"label\": "
                "\"c1\", \"gist\": str, \"facts\": [str], \"entities\": [str]}, ...one per leaf in order], \"summary\": str}."
                % (len(leaves), _j(leaves)))
    if task == "build_upper":
        return ("TASK build_upper. Below are the summaries of the child nodes of an index node. Write one summary of 2 to 4 "
                "sentences that covers their topics.\nChildren (JSON): %s\nReturn {\"summary\": str}." % _j(i["children"]))
    if task == "organise":
        return ("TASK organise. Below are pointers (opaque strings) with the path of the holders that named them and a score. "
                "Group them into a short outline. Copy each pointer exactly.\nPointers (JSON): %s\nReturn {\"outline\": "
                "[{\"label\": a label of at most 12 words, \"pointers\": [pointer strings]}]}." % _j(i["pointers"]))
    if task == "type_edges":
        return ("TASK type_edges. Item c0 and items c1..cN follow. For each item that is related to c0, give the relation "
                "type, one of %s. Omit unrelated items.\nItems (JSON): %s\nReturn {\"edges\": [{\"src\": label, \"dst\": label, "
                "\"type\": type}]}." % (sorted(EDGE_TYPES), _j(i["items"])))
    if task == "judge_relevance":
        return ("TASK judge_relevance. Is the text relevant to the query, that is, does it help answer it?\nQuery: %s\n"
                "Text: %s\nReturn {\"score\": number from 0 to 1, \"relevant\": true or false}." % (i["query"], i["text"]))
    if task == "query_variants":
        return ("TASK query_variants. Rewrite the query three ways that keep its meaning.\nQuery: %s\nReturn "
                "{\"variants\": [three strings]}." % i["query"])
    if task == "synthetic_queries":
        return ("TASK synthetic_queries. Write three different questions that the text below would help answer.\nText: %s\n"
                "Return {\"queries\": [three strings]}." % i["text"])
    if task in ("ask", "reader_answer"):
        return ("TASK %s. Answer the question using only the context.\nQuestion: %s\nContext (JSON): %s\nReturn "
                "{\"answer\": str}." % (task, i["question"], _j(i["context"])))
    if task == "answer_judge":
        return ("TASK answer_judge. Does the answer agree with the reference answer?\nReference: %s\nAnswer: %s\nReturn "
                "{\"correct\": true or false}." % (i["gold"], i["answer"]))
    raise PortRefused("unknown task")


# ---- consumer-strict validity (beyond the port's top-level type check) ---------------------------------------------
def strict_problems(task: str, i: Mapping[str, Any], out: Mapping[str, Any]) -> List[str]:
    """What the consuming code relies on, checked per task. An empty list means the output is usable as is."""
    p: List[str] = []
    words = lambda s: len(re.findall(r"[A-Za-z0-9]+", s))
    if task == "build_l1":
        n = len(i["leaves"])
        ents = out["entries"]
        if len(ents) != n:
            p.append("entry count %d != leaf count %d" % (len(ents), n))
        for k, e in enumerate(ents):
            if not (isinstance(e, dict) and isinstance(e.get("gist"), str) and isinstance(e.get("facts"), list)
                    and isinstance(e.get("entities"), list)):
                p.append("entry %d malformed" % k)
            elif not all(isinstance(x, str) for x in e["facts"] + e["entities"]):
                p.append("entry %d has a non-string fact or entity" % k)
        if not out["summary"].strip():
            p.append("empty summary")
    elif task == "build_upper" and not out["summary"].strip():
        p.append("empty summary")
    elif task == "route":
        shown = {c["label"] for c in i["children"]}
        sel = out["selected"]
        if any(l not in shown for l in sel):
            p.append("a selected label was not presented")
        if len(sel) > int(i.get("b_fanout", 6)):
            p.append("more than b_fanout labels selected")
        for l in sel:
            if words(str(out["reasons"].get(l, ""))) > 12:
                p.append("reason over 12 words for %s" % l)
            c = out["confidence"].get(l)
            if not (isinstance(c, int) and not isinstance(c, bool) and 0 <= c <= 3):
                p.append("confidence for %s not an integer 0 to 3" % l)
    elif task == "organise":
        have = {x["pointer"] for x in i["pointers"]}
        for o in out["outline"]:
            if not (isinstance(o, dict) and isinstance(o.get("label"), str) and isinstance(o.get("pointers"), list)):
                p.append("outline entry malformed")
                continue
            if words(o["label"]) > 12:
                p.append("outline label over 12 words")
            if any(x not in have for x in o["pointers"]):
                p.append("an outline pointer was not presented")
    elif task == "type_edges":
        labels = {x["label"] for x in i["items"]}
        for e in out["edges"]:
            if not (isinstance(e, dict) and e.get("src") in labels and e.get("dst") in labels and e.get("type") in EDGE_TYPES):
                p.append("an edge has an unknown label or type")
    elif task == "judge_relevance":
        if not (isinstance(out["score"], float) or isinstance(out["score"], int)) or not 0 <= out["score"] <= 1:
            p.append("score outside 0 to 1")
    elif task in ("query_variants", "synthetic_queries"):
        xs = out.get("variants" if task == "query_variants" else "queries")
        if len(xs) != 3 or not all(isinstance(x, str) and x.strip() for x in xs):
            p.append("expected three non-empty strings")
    elif task in ("ask", "reader_answer") and not out["answer"].strip():
        p.append("empty answer")
    return p


def parse_model_json(text: str) -> Optional[dict]:
    t = text.strip()
    t = re.sub(r"^```(?:json)?\s*|\s*```$", "", t)
    try:
        v = json.loads(t)
    except ValueError:
        m = re.search(r"\{.*\}", t, re.S)
        if not m:
            return None
        try:
            v = json.loads(m.group(0))
        except ValueError:
            return None
    return v if isinstance(v, dict) else None


# ---- the process runner (the only place a model is reached) ---------------------------------------------------------
@dataclass
class RunOut:
    rc: int
    stdout: str
    stderr: str
    latency_s: float


@dataclass
class ApiReply:
    """One HTTP model call (the `muse` model kind; `model_kind.MuseRunner` reads it from `muse_api.MusePort`'s envelope): the answer
    text (None unless the call answered and finished), the token counts the response reported (completion includes reasoning),
    latency, and the outcome: `throttled` (429, 503, OpenRouter's in-flight 402: the stage pauses and backs off, Retry-After as the
    floor), `limited` (a billing, credit or budget refusal, 402 or OpenRouter's 403: the stage stops at once, never retried),
    `model_mismatch` (the response reported another model: the stage stops at once, never retried), or another `error` (a failed
    call, counted in the failure streak; an answer cut by `finish_reason` length is one). `model` and `upstream_provider` are what
    the response reported."""
    text: Optional[str]
    prompt_tokens: int = 0
    completion_tokens: int = 0
    reasoning_tokens: int = 0
    cached_tokens: int = 0
    latency_s: float = 0.0
    throttled: bool = False
    limited: bool = False
    model_mismatch: bool = False
    error: str = ""
    status: Optional[int] = None
    retry_after_s: Optional[float] = None
    model: str = ""
    upstream_provider: str = ""
    finish_reason: str = ""
    cost_usd: Optional[float] = None


class Pacer:
    """Request starts spaced at least 60/rpm seconds apart across every worker of one run (one pacer per run), so the run sits
    under a requests-per-minute limit instead of living on 429s. It only delays: what is asked and answered does not change. With a
    `check` (the run's halt gate), the wait is slept in slices of at most `SLICE_S` and `check` runs before each one, so a stop
    during the wait ends it at once, before any charge (review F1)."""
    SLICE_S = 0.25

    def __init__(self, rpm: float, *, clock: Callable[[], float] = time.monotonic, sleep: Callable[[float], None] = time.sleep):
        if not rpm or rpm <= 0:
            raise ValueError("rpm must be positive")
        self.rpm, self.gap = float(rpm), 60.0 / float(rpm)
        self._clock, self._sleep, self._lock, self._next = clock, sleep, threading.Lock(), None
        self.waited = 0.0

    def wait(self, check: Optional[Callable[[], None]] = None) -> None:
        with self._lock:
            now = self._clock()
            at = now if self._next is None else max(now, self._next)
            self._next = at + self.gap
            self.waited += at - now
        left = at - now
        if check is None:
            if left > 0:
                self._sleep(left)
            return
        while left > 0:
            check()
            s = min(left, self.SLICE_S)
            self._sleep(s)
            left -= s


def argv_for(binary: str, model: str) -> List[str]:
    return [binary, "-p", "--model", model, "--output-format", "json", "--no-session-persistence", "--tools", "",
            "--disable-slash-commands", "--strict-mcp-config", "--settings", '{"disableAllHooks":true}',
            "--system-prompt", SYSTEM_PROMPT]


class CliRunner:
    def __init__(self, binary: str, model: str, workdir: Path, timeout_s: int = 240):
        self.binary, self.model, self.timeout_s = binary, model, timeout_s
        self.workdir = Path(workdir)
        self.workdir.mkdir(parents=True, exist_ok=True)
        if any(self.workdir.iterdir()):
            raise PortRefused("the CLI working directory must be empty (neutral)")

    def argv(self) -> List[str]:
        return argv_for(self.binary, self.model)

    def __call__(self, prompt: str) -> RunOut:
        env = dict(os.environ)
        env.update(CHILD_ENV)
        t0 = time.monotonic()
        try:
            r = subprocess.run(self.argv(), input=prompt, capture_output=True, text=True, cwd=self.workdir, env=env,
                               timeout=self.timeout_s)
        except subprocess.TimeoutExpired:
            return RunOut(124, "", "timeout", time.monotonic() - t0)
        return RunOut(r.returncode, r.stdout, r.stderr[-2000:], time.monotonic() - t0)


# ---- the port ---------------------------------------------------------------------------------------------------------
class CappedPort:
    """Same call interface as `ModelPort`; every call goes through the durable counter first."""

    def __init__(self, runner: Callable[[str], RunOut], budget: Budget, log_path: Path, *, forbidden: Sequence[str] = (),
                 model_id: str = "", argv_record: Sequence[str] = (), pacer: Optional[Pacer] = None):
        self._run, self._budget, self._log, self._pacer = runner, budget, Path(log_path), pacer
        self._forbidden, self.model_id, self._argv = tuple(forbidden), model_id, list(argv_record)
        self._lock = threading.Lock()
        self.port_calls = 0
        self._fail_streak = 0
        self.spawned = 0
        self.rows: List[dict] = []
        self.models_reported: set = set()             # the API kind: every model id a response reported
        self.providers_reported: set = set()          # the API kind: every upstream provider a response reported
        self.row_extra: Callable[[], dict] = dict        # set by ResilientPort: the effective concurrency at the time of the call
        self.on_charge: Callable[[int], None] = lambda n: None   # set by ResilientPort: journals the charge of the call in this thread
        # set by ResilientPort, the run's shared halt gate (review F1): `admit(block)` raises when the stage is halted or the stop file
        # is present (with block, it also waits out a back-off); `on_halt(exc)` halts every worker, called under the charge lock by a
        # call that stops the stage, so no worker charges after it
        self.admit: Callable[[bool], Any] = lambda block: False
        self.on_halt: Callable[[BaseException], None] = lambda exc: None
        # set by ResilientPort: a throttle's Retry-After, seen under the charge lock (a wait above the run's ceiling halts there)
        self.on_throttle: Callable[[Optional[float]], None] = lambda retry_after_s: None
        self._log.parent.mkdir(parents=True, exist_ok=True)

    def _check_prompt(self, prompt: str) -> None:
        try:
            assert_model_safe(prompt)
        except DatasetError:
            raise PortRefused("a gold-style id is in the prompt")
        if any(m and m in prompt for m in self._forbidden):
            raise PortRefused("a forbidden marker is in the prompt")

    def _halting(self, exc: BaseException) -> BaseException:
        """A call that stops the stage halts every worker before its lock is released (the caller holds `_lock`)."""
        self.on_halt(exc)
        return exc

    def _append(self, row: dict) -> None:
        self.rows.append(row)
        with open(self._log, "a") as f:
            f.write(json.dumps(row, sort_keys=True) + "\n")
            f.flush()
            os.fsync(f.fileno())

    def call(self, task: Task, inputs: Mapping[str, Any], *, ctx: Optional[CallContext] = None) -> CallResult:
        name = task.value
        prompt = render(name, inputs)
        self._check_prompt(prompt)            # before the counter: a refused prompt costs nothing
        if self._pacer is not None:           # the wait is part of the halt gate: a stop or halt during it ends it, uncharged
            self._pacer.wait(check=lambda: self.admit(False))
        while self.admit(True) and self._pacer is not None:
            # a back-off that began during the wait held this call: its pacer turn is spent, so it takes a new one (the workers a
            # back-off releases together never charge in one unpaced burst)
            self._pacer.wait(check=lambda: self.admit(False))
        with self._lock:                      # the charge and its bookkeeping are atomic; the process itself runs outside the lock
            self.admit(False)                 # immediately before the charge, under the lock every stopping call halts under
            self.port_calls += 1
            try:
                n = self._budget.charge()     # BEFORE the process; raises BudgetExceeded at the cap, nothing is spawned
            except BudgetExceeded as e:
                self.on_halt(e)
                raise
            self.spawned += 1
        self.on_charge(n)                     # journaled before the process: a kill from here on leaves a charged call with no outcome
        ro = self._run(prompt)                # several processes may run at once (the caller bounds how many: `ResilientPort`)
        if isinstance(ro, ApiReply):
            return self._api(name, inputs, prompt, n, ro)
        with self._lock:
            row = {**self.row_extra(), "n": n, "task": name, "prompt_chars": len(prompt), "prompt_sha": hashlib.sha256(prompt.encode()).hexdigest()[:16],
                   "latency_s": round(ro.latency_s, 3), "rc": ro.rc, "stdout_chars": len(ro.stdout), "argv": self._argv,
                   "prompt_version": PROMPT_VERSION}
            env0 = self._envelope(ro.stdout)
            res_text = env0.get("result") if isinstance(env0, dict) and isinstance(env0.get("result"), str) else ""
            refused = ro.rc != 0 or (isinstance(env0, dict) and bool(env0.get("is_error")))
            if refused and LIMIT.search("%s\n%s\n%s" % (ro.stderr or "", res_text, ro.stdout if ro.rc != 0 else "")):
                row.update(limited=True, error="limit")
                self._append(row)
                raise AllowanceRefused("allowance limit response at call %d" % n)
            self._fail_streak = self._fail_streak + 1 if ro.rc != 0 else 0
            if self._fail_streak >= MAX_CLI_FAILURE_STREAK:
                row.update(error="cli_error")
                self._append(row)
                raise self._halting(StageLimited("%d consecutive CLI failures, last at call %d" % (self._fail_streak, n)))
            if THROTTLE.search(ro.stderr or "") or (ro.rc != 0 and THROTTLE.search(ro.stdout or "")):
                row.update(throttled=True, error="throttled")
                self._append(row)
                raise StagePaused("throttle response at call %d" % n)
            env = self._envelope(ro.stdout)
            row.update(self._usage(env))
            text = env.get("result") if isinstance(env, dict) else None
            out = self._judge(name, inputs, text, row)
            if ro.rc != 0 or (isinstance(env, dict) and env.get("is_error")):
                row["error"] = "timeout" if ro.rc == 124 else "cli_error" if ro.rc != 0 else "is_error"
            self._append(row)
            if ro.rc != 0 or not row["schema_ok"]:
                return CallResult(False, error=row.get("error") or ("parse" if out is None else "schema"))
            return CallResult(True, out)

    @staticmethod
    def _judge(name: str, inputs: Mapping[str, Any], text: Any, row: dict) -> Optional[dict]:
        """Parse the reply text and record parse_ok, schema_ok, strict_ok and strict_problems in the row (both model kinds)."""
        out = parse_model_json(text) if isinstance(text, str) else None
        if out is not None and name == "judge_relevance" and isinstance(out.get("score"), int) and not isinstance(out.get("score"), bool):
            out["score"] = float(out["score"])    # the port's schema wants a float; an integer 0 or 1 is the same number
        row["parse_ok"] = out is not None
        row["schema_ok"] = bool(out is not None and schema_ok(name, out))
        probs = strict_problems(name, inputs, out) if row["schema_ok"] else ["not schema valid"]
        row["strict_ok"] = not probs
        row["strict_problems"] = probs[:5]
        return out

    def _api(self, name: str, inputs: Mapping[str, Any], prompt: str, n: int, rep: "ApiReply") -> CallResult:
        """The HTTP model kind: the same charge, row, refusal and scoring rules as the CLI, from an `ApiReply`. A throttle pauses
        (back-off, Retry-After honoured) and is not a failure of the streak; a billing refusal or a model mismatch stops the stage at
        once; any other error, an unfinished answer included, is a failed call of the streak and never an answer."""
        with self._lock:
            row = {**self.row_extra(), "n": n, "task": name, "prompt_chars": len(prompt),
                   "prompt_sha": hashlib.sha256(prompt.encode()).hexdigest()[:16], "latency_s": round(float(rep.latency_s or 0), 3),
                   "argv": self._argv, "prompt_version": PROMPT_VERSION, "http_status": rep.status,
                   "prompt_tokens": int(rep.prompt_tokens or 0), "completion_tokens": int(rep.completion_tokens or 0),
                   "reasoning_tokens": int(rep.reasoning_tokens or 0), "cached_tokens": int(rep.cached_tokens or 0),
                   # the CLI's fields, read by the summaries: input + cache_read = prompt tokens, output = completion (reasoning in)
                   "input_tokens": max(0, int(rep.prompt_tokens or 0) - int(rep.cached_tokens or 0)),
                   "cache_creation_input_tokens": 0, "cache_read_input_tokens": int(rep.cached_tokens or 0),
                   "output_tokens": int(rep.completion_tokens or 0),
                   "model_reported": rep.model or "", "upstream_provider": rep.upstream_provider or "",
                   "finish_reason": rep.finish_reason or "", "cost_usd": rep.cost_usd, "stdout_chars": len(rep.text or "")}
            if rep.model:
                self.models_reported.add(rep.model)
            if rep.upstream_provider:
                self.providers_reported.add(rep.upstream_provider)
            if rep.model_mismatch:
                row.update(limited=True, error="model_mismatch")
                self._append(row)
                raise self._halting(StageLimited("the response reported model %r, not the requested %r, at call %d"
                                                 % (rep.model, self.model_id, n)))
            if rep.limited:
                row.update(limited=True, error="limit")
                self._append(row)
                raise self._halting(StageLimited("quota or billing refusal (HTTP %s) at call %d" % (rep.status, n)))
            if rep.throttled:
                row.update(throttled=True, error="throttled", retry_after_s=rep.retry_after_s)
                self._append(row)
                self.on_throttle(rep.retry_after_s)
                raise StagePaused("throttled (HTTP %s) at call %d" % (rep.status, n), retry_after_s=rep.retry_after_s)
            failed = bool(rep.error) or rep.text is None
            self._fail_streak = self._fail_streak + 1 if failed else 0
            if self._fail_streak >= MAX_CLI_FAILURE_STREAK:
                row.update(error="api_error")
                self._append(row)
                raise self._halting(StageLimited("%d consecutive API failures, last at call %d" % (self._fail_streak, n)))
            out = self._judge(name, inputs, rep.text, row)
            if failed:
                row.update(error="timeout" if rep.error == "timeout" else "api_error", api_error=rep.error or "no_answer")
            self._append(row)
            if failed or not row["schema_ok"]:
                return CallResult(False, error=row.get("error") or ("parse" if out is None else "schema"))
            return CallResult(True, out)

    @staticmethod
    def _envelope(stdout: str) -> dict:
        try:
            v = json.loads(stdout)
        except ValueError:
            return {}
        if isinstance(v, list):    # some versions return an array of events; the last `result` event holds the answer
            for e in reversed(v):
                if isinstance(e, dict) and e.get("type") == "result":
                    return e
            return {}
        return v if isinstance(v, dict) else {}

    @staticmethod
    def _usage(env: Mapping) -> dict:
        u = env.get("usage") or {}
        mu = env.get("modelUsage") or {}
        g = lambda k: int(u.get(k) or 0)
        return {"input_tokens": g("input_tokens"), "cache_creation_input_tokens": g("cache_creation_input_tokens"),
                "cache_read_input_tokens": g("cache_read_input_tokens"), "output_tokens": g("output_tokens"),
                "models_reported": sorted(mu) if isinstance(mu, dict) else [], "num_turns": env.get("num_turns"),
                "duration_ms": env.get("duration_ms"), "duration_api_ms": env.get("duration_api_ms"),
                "total_cost_usd": env.get("total_cost_usd")}
