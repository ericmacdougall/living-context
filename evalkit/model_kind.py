"""The model kind of a stage (`--model-kind`): which real model a stage's capped port reaches, built in one place for S2/S3
(`run_h1`/`run_s2`) and S6-H1 (`run_s6h1`, early build and question run).

  * `claude`: the headless Claude Code CLI on the subscription (`claude_cli.CliRunner`), as registered.
  * `muse`: Meta's Muse Spark through `evalkit.muse_api.MusePort` (docs/MUSE-PORT.md) at `--provider` openrouter (pinned to Meta,
    no fallback, in the port) or meta. The key comes from exactly one source, named at run time only: `--key-file` (here only its
    existence, type, owner and mode are checked; the port reads it) or `--key-secret-project` with `--key-secret-name` (a Secret
    Manager secret the port fetches inside this process). Nothing here opens, prints or copies a key. `--reasoning-effort` and
    `--max-tokens` are registered run arguments with no default in code: cost, latency and whether reasoning leaves room for the
    answer depend on them, so the stamp, PREBUILD.json and every ledger row record them. Every request is paced by one
    `claude_cli.Pacer` per run at `--rpm` requests per minute (at most `RPM_CEILING`; runs sharing one upstream limit split it).

`MuseRunner` reads the port's envelope (not its exit code or stderr) into `claude_cli.ApiReply`, so `CappedPort` pauses on a
throttle with Retry-After as the back-off floor, stops the stage at once on a billing refusal or a model mismatch, and counts any
other error, an answer cut short by `finish_reason` length included, as a failed call and never as an answer.

Both kinds go through the same `claude_cli.CappedPort` (counter charged before the call, call rows, prompt refusals, scoring) and
the same `s2_ports.ResilientPort` (journal, stop file, adaptive concurrency, back-off), so the stage outputs do not depend on the
kind's pacing or concurrency."""
from __future__ import annotations

import json
import os
import shutil
import stat
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, List, Optional

from . import claude_cli

KINDS = ("claude", "muse")
PROVIDERS = ("openrouter", "meta")
RPM_CEILING = 140                               # registered: under the presumed upstream limit of 150 requests per minute
EFFORTS = ("minimal", "low", "medium", "high", "xhigh", "max")     # muse_api.REASONING_EFFORT ("none" is a 400 on Spark)
UNFINISHED = ("length", "content_filter")       # an answer that stopped for these reasons is a failed call, never an answer
IDENTITY = ("model_kind", "model", "provider", "reasoning_effort", "max_tokens")   # what makes two runs the same model's
API_TIMEOUT_S = 240.0


class PortUnavailable(Exception):
    """The model port could not be built (bad settings or key source); raised before any stamp, charge or call."""


def _set(x) -> bool:
    return bool(str(x or "").strip())


def check(a) -> List[str]:
    """Reasons the model kind's settings must not start a stage (no call; no key is read)."""
    kind = getattr(a, "model_kind", "claude")
    why: List[str] = []
    if kind not in KINDS:
        return ["the stage uses a real model port (--model-kind %s)" % " or ".join(KINDS)]
    if kind == "claude":                    # the registered kind: its guards are unchanged (the runner needs --binary and --model)
        return why
    model = str(getattr(a, "model", "") or "")
    if not _set(model):
        why.append("--model (the requested model id) is required for --model-kind muse")
    if getattr(a, "provider", "") not in PROVIDERS:
        why.append("--provider must be one of %s" % ", ".join(PROVIDERS))
    kf = getattr(a, "key_file", "")
    proj, name = getattr(a, "key_secret_project", ""), getattr(a, "key_secret_name", "")
    if _set(kf) == (_set(proj) or _set(name)):
        why.append("exactly one key source: --key-file, or --key-secret-project with --key-secret-name")
    elif not _set(kf) and not (_set(proj) and _set(name)):
        why.append("--key-secret-project and --key-secret-name go together")
    elif _set(kf):
        try:
            st = os.lstat(kf)
        except OSError:
            why.append("the --key-file does not exist")
        else:
            if not stat.S_ISREG(st.st_mode):
                why.append("the --key-file is not a regular file")
            if st.st_mode & 0o077:
                why.append("the --key-file must be readable by its owner only (mode 0600)")
            if hasattr(os, "getuid") and st.st_uid != os.getuid():
                why.append("the --key-file must be owned by the user running the stage")
    effort = str(getattr(a, "reasoning_effort", "") or "")
    if effort not in EFFORTS:
        why.append("--reasoning-effort is a registered run argument for --model-kind muse: one of %s" % ", ".join(EFFORTS))
    elif effort == "max" and model.endswith("muse-spark-1.3-contributor"):
        why.append("--reasoning-effort max is not available on muse-spark-1.3-contributor")
    mt = getattr(a, "max_tokens", 0)
    if not (isinstance(mt, int) and not isinstance(mt, bool) and mt >= 1):
        why.append("--max-tokens (the output limit, reasoning included) is a registered run argument for --model-kind muse")
    rpm = getattr(a, "rpm", RPM_CEILING)
    if not (isinstance(rpm, int) and not isinstance(rpm, bool) and 1 <= rpm <= RPM_CEILING):
        why.append("--rpm must be 1 to %d (runs on one upstream limit split it)" % RPM_CEILING)
    if _set(getattr(a, "base_url", "")) and not str(a.base_url).startswith("https://"):
        why.append("--base-url must be https")
    return why


def record(a) -> dict:
    """The model's identity, recorded in the stamp, PREBUILD.json and every ledger row (never a key source): two runs with a
    different record are runs of different models."""
    kind = getattr(a, "model_kind", "claude")
    rec = {"model_kind": kind, "model": getattr(a, "model", "")}
    if kind == "muse":
        rec.update(provider=a.provider, reasoning_effort=a.reasoning_effort, max_tokens=a.max_tokens)
    return rec


def differs(old: dict, a) -> List[str]:
    """The identity fields in which an earlier stamp or early build `old` is not this run's. A record written before the kind
    existed reads as the claude kind, and a field it does not hold is not compared (a muse record always holds every field)."""
    now = record(a)
    return [k for k in IDENTITY if (k == "model_kind" or k in old) and old.get(k, "claude") != now.get(k, "claude")]


@dataclass
class Prepared:
    """The stage's model runner, built once before any stamp: what CappedPort is given, and what the stamp records of it."""
    runner: Any
    model_id: str
    argv: List[str]
    pacer: Optional[claude_cli.Pacer] = None
    port: dict = field(default_factory=dict)


def _num(v) -> int:
    return int(v) if isinstance(v, (int, float)) and not isinstance(v, bool) and v >= 0 else 0


def reply_from(out: claude_cli.RunOut) -> claude_cli.ApiReply:
    """`muse_api.MusePort`'s RunOut as an ApiReply, from the envelope's own flags (docs/MUSE-PORT.md). Every field of the `muse`
    block is optional (the port may withhold it)."""
    try:
        env = json.loads(out.stdout or "")
    except ValueError:
        env = None
    if not isinstance(env, dict):
        return claude_cli.ApiReply(text=None, error="timeout" if out.rc == 124 else "malformed_envelope",
                                   latency_s=float(out.latency_s or 0))
    m = env.get("muse") if isinstance(env.get("muse"), dict) else {}
    u = env.get("usage") if isinstance(env.get("usage"), dict) else {}
    error = str(env.get("error") or "")
    throttled, limited = bool(env.get("throttled")), bool(env.get("limited"))
    answered = out.rc == 0 and not env.get("is_error") and not throttled and not limited and isinstance(env.get("result"), str)
    finish = m.get("finish_reason") if isinstance(m.get("finish_reason"), str) else ""
    text = env["result"] if answered else None
    if answered and finish in UNFINISHED:
        text, error = None, "finish_" + finish
    if text is None and not error:
        error = "timeout" if out.rc == 124 else "no_answer"
    mismatch = error == "model_mismatch"
    model = env.get("model") if isinstance(env.get("model"), str) else ""
    if mismatch and not model and isinstance(m.get("response_model"), str):
        model = m["response_model"]
    ra = m.get("retry_after_s")
    cost = env.get("total_cost_usd")
    return claude_cli.ApiReply(
        text=text, prompt_tokens=_num(m.get("prompt_tokens")), completion_tokens=_num(m.get("completion_tokens")),
        reasoning_tokens=_num(m.get("reasoning_tokens")), cached_tokens=_num(u.get("cache_read_input_tokens")),
        latency_s=float(out.latency_s or 0), throttled=throttled, limited=limited, model_mismatch=mismatch, error=error,
        status=m.get("http_status") if isinstance(m.get("http_status"), int) else None,
        retry_after_s=float(ra) if isinstance(ra, (int, float)) and not isinstance(ra, bool) else None, model=model,
        upstream_provider=m.get("upstream_provider") if isinstance(m.get("upstream_provider"), str) else "",
        finish_reason=finish, cost_usd=float(cost) if isinstance(cost, (int, float)) and not isinstance(cost, bool) else None)


class MuseRunner:
    """The runner CappedPort calls with one prompt: asks `muse_api.MusePort` (which sends the registered system prompt itself) and
    reads its envelope with `reply_from`. `model`, `argv()` and `record()` are the port's."""
    def __init__(self, port: Any):
        self.port = port
        self.model = port.model

    def argv(self) -> List[str]:
        return list(self.port.argv())

    def record(self) -> dict:
        return dict(self.port.record()) if hasattr(self.port, "record") else {}

    def __call__(self, prompt: str) -> claude_cli.ApiReply:
        return reply_from(self.port(prompt))


def port_kwargs(a) -> dict:
    """The keywords `muse_api.MusePort` is built with: one key source, the provider, the requested model, the base URL (empty: the
    provider's), the request timeout and the registered reasoning effort and output limit."""
    kw = {"model": a.model, "provider": a.provider, "base_url": getattr(a, "base_url", "") or None,
          "timeout": float(getattr(a, "api_timeout", API_TIMEOUT_S)), "max_tokens": a.max_tokens,
          "reasoning_effort": a.reasoning_effort}
    if _set(getattr(a, "key_file", "")):
        kw["key_file"] = a.key_file
    else:
        kw.update(key_secret=(a.key_secret_project, a.key_secret_name), gcloud=shutil.which("gcloud"))
    return kw


def prepare(a, run_dir: Path, *, runner=None) -> Prepared:
    """Build the stage's runner before anything is stamped; a port that cannot be built raises PortUnavailable. Tests pass a fake
    runner (with the muse kind its pacer is kept)."""
    kind = getattr(a, "model_kind", "claude")
    pacer = claude_cli.Pacer(getattr(a, "rpm", RPM_CEILING)) if kind == "muse" else None
    if runner is not None:
        rec = runner.record() if hasattr(runner, "record") else {}
        return Prepared(runner, getattr(runner, "model", a.model), list(getattr(runner, "argv", lambda: [])()), pacer, rec)
    if kind == "claude":
        r = claude_cli.CliRunner(a.binary, a.model, Path(run_dir) / "cwd")
        return Prepared(r, a.model, r.argv())
    from . import muse_api                              # imported only for this kind
    try:
        r = MuseRunner(muse_api.MusePort(**port_kwargs(a)))
    except muse_api.MuseConfigError as e:              # the port's messages name no key, project or secret
        raise PortUnavailable("the model port could not be built: %s" % e) from None
    return Prepared(r, r.model, r.argv(), pacer, r.record())


def stamp_record(a, prepared: Optional[Prepared] = None) -> dict:
    """What the stamp and PREBUILD.json record of the model: its identity, the run's pacing and timeout, and the port's fixed
    request fields (no key, path, project or secret name)."""
    rec = record(a)
    if getattr(a, "model_kind", "claude") == "muse":
        rec.update(rpm=getattr(a, "rpm", RPM_CEILING), api_timeout_s=float(getattr(a, "api_timeout", API_TIMEOUT_S)))
        if prepared is not None and prepared.port:
            rec["port"] = prepared.port
    return rec


def capped_port(a, run_dir: Path, budget, *, runner=None, prepared: Optional[Prepared] = None) -> claude_cli.CappedPort:
    p = prepared if prepared is not None else prepare(a, run_dir, runner=runner)
    return claude_cli.CappedPort(p.runner, budget, Path(run_dir) / "calls.jsonl", model_id=p.model_id, argv_record=p.argv, pacer=p.pacer)


def add_arguments(ap, *, default_kind: str) -> None:
    """The model-kind options, shared by the stage runners. Key sources are run-time values only."""
    ap.add_argument("--provider", default="openrouter", choices=list(PROVIDERS), help="--model-kind muse: the API provider")
    ap.add_argument("--key-file", default="", help="--model-kind muse: an API key file (mode 0600), read by the port only")
    ap.add_argument("--key-secret-project", default="", help="--model-kind muse: the Secret Manager project of the key")
    ap.add_argument("--key-secret-name", default="", help="--model-kind muse: the Secret Manager secret of the key")
    ap.add_argument("--base-url", default="", help="--model-kind muse: the API base URL (default: the port's for the provider)")
    ap.add_argument("--reasoning-effort", default="", help="--model-kind muse, required: one of %s (registered)" % ", ".join(EFFORTS))
    ap.add_argument("--max-tokens", type=int, default=0,
                    help="--model-kind muse, required: the output limit per call, reasoning included (registered)")
    ap.add_argument("--rpm", type=int, default=RPM_CEILING,
                    help="--model-kind muse: requests per minute for this run, at most %d; runs on one limit split it" % RPM_CEILING)
    ap.add_argument("--api-timeout", type=float, default=API_TIMEOUT_S, help="--model-kind muse: seconds per request")
