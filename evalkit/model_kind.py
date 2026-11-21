"""The model kind of a stage (`--model-kind`): which real model a stage's capped port reaches, built in one place for S2/S3
(`run_h1`/`run_s2`) and S6-H1 (`run_s6h1`, early build and question run).

  * `claude`: the headless Claude Code CLI on the subscription (`claude_cli.CliRunner`), as registered.
  * `muse`: Meta's Muse Spark through `evalkit.muse_api.MusePort` (OpenAI-compatible chat completions) at `--provider` openrouter
    (pinned to Meta, no fallback, in the port) or meta. The key comes from exactly one source, named at run time only: `--key-file`
    (only its existence, type, owner and mode are checked here) or `--key-secret-project` with `--key-secret-name` (a Secret
    Manager secret the port reads inside this process). Nothing here opens, prints or copies a key. Every request is paced by one
    `claude_cli.Pacer` per run at `--rpm` requests per minute (at most `RPM_CEILING`; runs sharing one upstream limit split it with
    their own `--rpm`). The reasoning effort is left at the provider's default (no knob). A reported model that is not the
    requested one stops the stage at once (`claude_cli.CappedPort`).

Both kinds go through the same `claude_cli.CappedPort` (counter charged before the call, call rows, prompt refusals, scoring) and
the same `s2_ports.ResilientPort` (journal, stop file, adaptive concurrency, back-off), so the stage outputs do not depend on the
kind's pacing or concurrency."""
from __future__ import annotations

import inspect
import os
import stat
from pathlib import Path
from typing import Any, List, Optional

from . import claude_cli

KINDS = ("claude", "muse")
PROVIDERS = ("openrouter", "meta")
RPM_CEILING = 140                               # registered: under the presumed upstream limit of 150 requests per minute
EFFORT = "provider default"


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
    if not _set(getattr(a, "model", "")):
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
            st = os.stat(kf)
        except OSError:
            why.append("the --key-file does not exist")
        else:
            if not stat.S_ISREG(st.st_mode):
                why.append("the --key-file is not a regular file")
            if st.st_mode & 0o077:
                why.append("the --key-file must be readable by its owner only (mode 0600)")
            if hasattr(os, "getuid") and st.st_uid != os.getuid():
                why.append("the --key-file must be owned by the user running the stage")
    rpm = getattr(a, "rpm", RPM_CEILING)
    if not (isinstance(rpm, int) and not isinstance(rpm, bool) and 1 <= rpm <= RPM_CEILING):
        why.append("--rpm must be 1 to %d (runs on one upstream limit split it)" % RPM_CEILING)
    if _set(getattr(a, "base_url", "")) and not str(a.base_url).startswith("https://"):
        why.append("--base-url must be https")
    return why


def record(a) -> dict:
    """What the stamp, PREBUILD.json and the ledger record about the model (never a key source)."""
    kind = getattr(a, "model_kind", "claude")
    rec = {"model_kind": kind, "model": a.model}
    if kind == "muse":
        rec.update(provider=a.provider, effort=EFFORT, rpm=getattr(a, "rpm", RPM_CEILING))
    return rec


def argv_record(a) -> List[str]:
    """The invocation recorded in every call row (the CLI's argv, or the API's fixed parameters; never a key source)."""
    if getattr(a, "model_kind", "claude") == "claude":
        return claude_cli.argv_for(a.binary, a.model)
    return ["muse", "provider=" + a.provider, "model=" + a.model, "effort=" + EFFORT.replace(" ", "-"),
            "rpm=%d" % getattr(a, "rpm", RPM_CEILING)]


class MuseRunner:
    """The runner the capped port calls with one prompt: asks the port (with the registered system prompt when its call takes one)
    and maps its reply (an object or a dict) onto `claude_cli.ApiReply`. Missing fields read as zero, false or empty."""
    def __init__(self, port: Any):
        self.port = port
        fn = port.call if hasattr(port, "call") else port
        try:
            takes_system = "system" in inspect.signature(fn).parameters
        except (TypeError, ValueError):
            takes_system = False
        self._fn, self._takes_system = fn, takes_system

    def __call__(self, prompt: str) -> claude_cli.ApiReply:
        r = self._fn(prompt, system=claude_cli.SYSTEM_PROMPT) if self._takes_system else self._fn(prompt)
        if isinstance(r, claude_cli.ApiReply):
            return r
        g = lambda k, d=None: (r.get(k, d) if isinstance(r, dict) else getattr(r, k, d))
        status = g("status")
        err = str(g("error", "") or "")
        return claude_cli.ApiReply(text=g("text"), input_tokens=int(g("input_tokens", 0) or 0),
                                   output_tokens=int(g("output_tokens", 0) or 0), reasoning_tokens=int(g("reasoning_tokens", 0) or 0),
                                   latency_s=float(g("latency_s", 0.0) or 0.0), throttled=bool(g("throttled", False)),
                                   retryable=bool(g("retryable", status in (503, 504))), limited=bool(g("limited", False)),
                                   model_mismatch=bool(g("model_mismatch", False)) or "model_mismatch" in err, error=err,
                                   status=status, retry_after_s=g("retry_after_s"), model=str(g("model", "") or ""))


def port_kwargs(a) -> dict:
    """The arguments `muse_api.MusePort` is built with (by keyword): one key source, the provider, the requested model, the base URL
    (empty: the port's default for the provider) and the request timeout."""
    kw = {"model": a.model, "provider": a.provider, "base_url": getattr(a, "base_url", "") or None,
          "timeout": float(getattr(a, "api_timeout", 120.0))}
    if _set(getattr(a, "key_file", "")):
        kw["key_file"] = a.key_file
    else:
        kw.update(key_secret_project=a.key_secret_project, key_secret_name=a.key_secret_name)
    return kw


def make_runner(a, run_dir: Path, *, runner=None):
    """(runner, argv record, pacer) for the stage's capped port. Tests pass a fake runner; with the muse kind its pacer is kept."""
    kind = getattr(a, "model_kind", "claude")
    pacer: Optional[claude_cli.Pacer] = claude_cli.Pacer(getattr(a, "rpm", RPM_CEILING)) if kind == "muse" else None
    if runner is not None:
        return runner, list(getattr(runner, "argv", lambda: [])()), pacer
    if kind == "claude":
        r = claude_cli.CliRunner(a.binary, a.model, Path(run_dir) / "cwd")
        return r, r.argv(), None
    from . import muse_api                              # the port module (feat/muse-port); imported only for this kind
    return MuseRunner(muse_api.MusePort(**port_kwargs(a))), argv_record(a), pacer


def capped_port(a, run_dir: Path, budget, *, runner=None) -> claude_cli.CappedPort:
    r, argv, pacer = make_runner(a, run_dir, runner=runner)
    return claude_cli.CappedPort(r, budget, Path(run_dir) / "calls.jsonl", model_id=a.model, argv_record=argv, pacer=pacer)


def add_arguments(ap, *, default_kind: str) -> None:
    """The model-kind options, shared by the stage runners. Key sources are run-time values only."""
    ap.add_argument("--provider", default="openrouter", choices=list(PROVIDERS), help="--model-kind muse: the API provider")
    ap.add_argument("--key-file", default="", help="--model-kind muse: an API key file (mode 0600), read by the port only")
    ap.add_argument("--key-secret-project", default="", help="--model-kind muse: the Secret Manager project of the key")
    ap.add_argument("--key-secret-name", default="", help="--model-kind muse: the Secret Manager secret of the key")
    ap.add_argument("--base-url", default="", help="--model-kind muse: the API base URL (default: the port's for the provider)")
    ap.add_argument("--rpm", type=int, default=RPM_CEILING,
                    help="--model-kind muse: requests per minute for this run, at most %d; runs on one limit split it" % RPM_CEILING)
    ap.add_argument("--api-timeout", type=float, default=120.0, help="--model-kind muse: seconds per request")
