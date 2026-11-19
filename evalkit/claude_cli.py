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


class StagePaused(Exception):
    """A throttle response: the stage pauses, the event is recorded, and it is not a scored failure."""


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
                 model_id: str = "", argv_record: Sequence[str] = ()):
        self._run, self._budget, self._log = runner, budget, Path(log_path)
        self._forbidden, self.model_id, self._argv = tuple(forbidden), model_id, list(argv_record)
        self._lock = threading.Lock()
        self.port_calls = 0
        self.spawned = 0
        self.rows: List[dict] = []
        self._log.parent.mkdir(parents=True, exist_ok=True)

    def _check_prompt(self, prompt: str) -> None:
        try:
            assert_model_safe(prompt)
        except DatasetError:
            raise PortRefused("a gold-style id is in the prompt")
        if any(m and m in prompt for m in self._forbidden):
            raise PortRefused("a forbidden marker is in the prompt")

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
        with self._lock:                      # one call at a time: one process, one counter
            self.port_calls += 1
            n = self._budget.charge()         # BEFORE the process; raises BudgetExceeded at the cap, nothing is spawned
            self.spawned += 1
            ro = self._run(prompt)
            row = {"n": n, "task": name, "prompt_chars": len(prompt), "prompt_sha": hashlib.sha256(prompt.encode()).hexdigest()[:16],
                   "latency_s": round(ro.latency_s, 3), "rc": ro.rc, "stdout_chars": len(ro.stdout), "argv": self._argv,
                   "prompt_version": PROMPT_VERSION}
            if THROTTLE.search(ro.stderr or "") or (ro.rc != 0 and THROTTLE.search(ro.stdout or "")):
                row.update(throttled=True, error="throttled")
                self._append(row)
                raise StagePaused("throttle response at call %d" % n)
            env = self._envelope(ro.stdout)
            row.update(self._usage(env))
            text = env.get("result") if isinstance(env, dict) else None
            out = parse_model_json(text) if isinstance(text, str) else None
            if out is not None and name == "judge_relevance" and isinstance(out.get("score"), int) and not isinstance(out.get("score"), bool):
                out["score"] = float(out["score"])    # the port's schema wants a float; an integer 0 or 1 is the same number
            row["parse_ok"] = out is not None
            row["schema_ok"] = bool(out is not None and schema_ok(name, out))
            probs = strict_problems(name, inputs, out) if row["schema_ok"] else ["not schema valid"]
            row["strict_ok"] = not probs
            row["strict_problems"] = probs[:5]
            if ro.rc != 0 or (isinstance(env, dict) and env.get("is_error")):
                row["error"] = "cli_error" if ro.rc != 0 else "is_error"
            self._append(row)
            if ro.rc != 0 or not row["schema_ok"]:
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
