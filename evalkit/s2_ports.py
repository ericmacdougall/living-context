"""Run-control around the capped model port for a long stage (EXPERIMENT 10, S2): resumability, a stop file and throttle back-off.

`ResilientPort` wraps a port with the interface `call(task, inputs, ctx=)` whose every real call is charged BEFORE it is made by a
durable counter (`claude_cli.CappedPort`; in tests a fake with the same contract). It adds, and nothing else:

  * a durable JOURNAL (append-only JSON lines, fsynced) of every call outcome, keyed by the sha256 of the rendered prompt plus the
    number of times that prompt has been asked in this run. A killed run resumes over the same journal: a call whose outcome is
    journaled is answered from the journal, is not charged and is not spawned. A call that was charged but whose outcome was lost
    in the kill (a `pending` row that is the last row of the file while the counter moved past the value it recorded) is NOT
    repeated: it is answered as a typed failure `lost_in_flight` and counted, because repeating it would charge twice. The stage's
    failure rate then includes it, as the registered rule for any failed call (EXPERIMENT 5.2).
    This is a run journal of one stamped run, not an evaluation cache: it answers only the same prompt in the same run, never
    across arms or runs, and is deleted with the run directory (EXPERIMENT 10.1, review 2 F10 withdrew the shared cache);
  * a retry of a failed BUILD call (new charged call, own key), because one failed build call aborts the generation;
  * a STOP file: when it exists the next call raises `StageStopped` before anything is charged;
  * THROTTLE BACK-OFF: a throttle response (which the inner port already charged and recorded) is retried after an exponentially
    growing delay, each retry charged like any call. Never past the cap: at the cap the counter raises `BudgetExceeded` and that
    propagates at once, with no sleep. After `max_retries` consecutive throttles the stage pauses (`StagePaused`) with the journal
    consistent, and a later resume asks the same call again.

CONCURRENCY (the registered model-port concurrency, DESIGN 13: "2 on the subscription"): several workers may call one ResilientPort at
once. Each worker calls through a SCOPED view (`scoped(ref)`: one question, or one build group), and journal occurrences are counted
per scope, so a key never depends on how the workers interleave; above one worker an unscoped call is refused. Every call waits at one
shared GATE before anything is charged: while any worker is backing off from a throttle the others wait instead of spending charged
retries, and once any worker hits the stop file, the cap, an allowance refusal or its last throttle retry, every other worker raises
the same stop before its next charge. At most `concurrency` calls are in the inner port at a time. A resume after a kill with several
calls in flight answers every call whose last journal row is `pending` as `lost_in_flight` when the counter moved past the earliest
of them (never a second charge; at most `concurrency - 1` of those may in fact never have been charged, which then counts as an
extra failure, disclosed in the run's summary).
"""
from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional

from living_context.model.budget import Budget, BudgetExceeded
from living_context.model.port import CallContext, CallResult, Task

from . import claude_cli
from .claude_cli import StageLimited, StagePaused

__all__ = ["Journal", "ResilientPort", "ScopedPort", "ScopeRequired", "StageStopped", "StagePaused", "StageLimited", "BudgetExceeded"]


class StageStopped(Exception):
    """The stop file exists: the stage stops cleanly and is resumable."""


class Journal:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.results: Dict[str, dict] = {}
        self.rows = 0
        self.last: Optional[dict] = None
        self._lock = threading.Lock()
        if self.path.exists():
            for line in self.path.read_text().splitlines():
                if not line.strip():
                    continue
                try:
                    row = json.loads(line)
                except ValueError:      # a torn last line from a kill: nothing after it can exist
                    break
                self.rows += 1
                self.last = row
                if row.get("t") == "result":
                    self.results[row["key"]] = row

    def append(self, row: dict) -> None:
        with self._lock:
            with open(self.path, "a") as f:
                f.write(json.dumps(row, sort_keys=True) + "\n")
                f.flush()
                os.fsync(f.fileno())
            self.rows += 1
            self.last = row
            if row.get("t") == "result":
                self.results[row["key"]] = row


class ScopeRequired(Exception):
    """More than one worker and a call without a scope: the journal key would depend on worker interleaving."""


class ScopedPort:
    """The port as one question (or one build group) sees it: every call carries the scope into the journal key."""

    def __init__(self, port: "ResilientPort", scope: str):
        self._port, self.scope = port, scope

    def call(self, task: Task, inputs: Mapping[str, Any], *, ctx: Optional[CallContext] = None) -> CallResult:
        return self._port.call(task, inputs, ctx=ctx, scope=self.scope)

    def stop_requested(self) -> bool:
        return self._port.stop_requested()


class ResilientPort:
    def __init__(self, inner, budget: Budget, journal: Journal, stop_file: Path, *, max_retries: int = 3, base_delay_s: float = 30.0,
                 max_delay_s: float = 600.0, sleep: Callable[[float], None] = time.sleep,
                 render: Callable[[str, Mapping[str, Any]], str] = claude_cli.render,
                 retry_failed: tuple = ("build_l1", "build_upper"), failure_retries: int = 2,
                 clock: Callable[[], float] = time.time, concurrency: int = 1):
        if int(concurrency) < 1:
            raise ValueError("concurrency must be at least 1")
        self._inner, self._budget, self.journal, self._stop = inner, budget, journal, Path(stop_file)
        self._retries, self._base, self._max = max_retries, base_delay_s, max_delay_s
        self._sleep, self._render, self._clock = sleep, render, clock
        self._retry_failed, self._fail_retries = tuple(retry_failed), failure_retries
        self.concurrency = int(concurrency)
        self._occ: Dict[str, int] = {}
        # optional default journal scope for a single worker (kept for callers that set it between questions); with several workers
        # every call must come through `scoped(ref)` instead, because one shared attribute is one shared occurrence key
        self.scope: Optional[str] = None
        self._lock = threading.Lock()                           # occurrence counts, lost keys and the statistics below
        self._slots = threading.BoundedSemaphore(self.concurrency)
        self._gate = threading.Condition()                      # the shared back-off and halt gate
        self._backing_off = 0
        self._halt: Optional[BaseException] = None
        self.replayed = self.fresh = self.lost = self.throttled = self.limited = 0
        self.delays: List[float] = []
        self._lost_keys = self._lost_in_flight(journal, budget.count())

    @staticmethod
    def _lost_in_flight(journal: Journal, count: int) -> set:
        """Keys whose last journal row is `pending`: charged or about to be when the run was killed. They are answered as lost (never
        asked again) when the counter moved past the earliest of them; with one worker this is exactly the single tail row rule."""
        last: Dict[str, dict] = {}
        if journal.path.exists():
            for line in journal.path.read_text().splitlines():
                try:
                    row = json.loads(line)
                except ValueError:
                    break
                if "key" in row:
                    last[row["key"]] = row
        pend = {k: r for k, r in last.items() if r.get("t") == "pending"}
        if not pend or count <= min(r.get("count_before", 0) for r in pend.values()):
            return set()
        return set(pend)

    def scoped(self, scope: str) -> ScopedPort:
        return ScopedPort(self, str(scope))

    def stop_requested(self) -> bool:
        return self._stop.exists()

    def halt(self, exc: BaseException) -> None:
        """Every worker raises `exc`'s type before its next charge (the first halt wins)."""
        with self._gate:
            if self._halt is None:
                self._halt = exc
            self._gate.notify_all()

    def _raise_halt(self) -> None:
        h = self._halt
        if h is not None:
            raise type(h)(str(h))

    def _wait_gate(self) -> None:
        """Before every charge: no halt, no stop file, nobody backing off."""
        with self._gate:
            while True:
                self._raise_halt()
                if self.stop_requested():
                    err = StageStopped("stop file present")
                    if self._halt is None:
                        self._halt = err
                    self._gate.notify_all()
                    raise err
                if self._backing_off == 0:
                    return
                self._gate.wait(0.5)

    def call(self, task: Task, inputs: Mapping[str, Any], *, ctx: Optional[CallContext] = None, scope: Optional[str] = None) -> CallResult:
        """One logical call. A failed BUILD call is asked again, as a new charged call with its own journal key, up to
        `failure_retries` times, because a failed build call aborts the whole generation (DESIGN 7.6); a failed route or organise
        call is the product's own fallback path and is never retried."""
        scope = scope if scope is not None else self.scope
        if scope is None and self.concurrency > 1:
            raise ScopeRequired("a call without a scope at concurrency %d" % self.concurrency)
        tries = 0
        while True:
            res = self._attempt(task, inputs, ctx, scope)
            if res.ok or task.value not in self._retry_failed or tries >= self._fail_retries:
                return res
            tries += 1

    def _key(self, name: str, inputs: Mapping[str, Any], scope: Optional[str]) -> str:
        sha = hashlib.sha256(self._render(name, inputs).encode()).hexdigest()[:32]
        if scope is not None:
            sha = "%s|%s" % (scope, sha)
        with self._lock:
            n = self._occ.get(sha, 0)
            self._occ[sha] = n + 1
        return "%s:%d" % (sha, n)

    def _attempt(self, task: Task, inputs: Mapping[str, Any], ctx: Optional[CallContext], scope: Optional[str]) -> CallResult:
        name = task.value
        key = self._key(name, inputs, scope)
        row = self.journal.results.get(key)
        if row is not None:
            with self._lock:
                self.replayed += 1
            return CallResult(bool(row["ok"]), row.get("output"), row.get("error") or "")
        with self._lock:
            lost = key in self._lost_keys
            if lost:
                self._lost_keys.discard(key)
                self.lost += 1
        if lost:
            self.journal.append({"t": "result", "key": key, "task": name, "ok": False, "output": None, "error": "lost_in_flight",
                                 "ts": self._clock()})
            return CallResult(False, error="lost_in_flight")
        attempt = 0
        while True:
            self._wait_gate()
            with self._slots:
                self._wait_gate()                 # a halt or back-off may have started while this worker waited for a slot
                self.journal.append({"t": "pending", "key": key, "task": name, "count_before": self._budget.count(),
                                     "ts": self._clock()})
                try:
                    res = self._inner.call(task, inputs, ctx=ctx)
                except StageLimited as e:
                    # an allowance refusal or a failing CLI: the refused call is not answered, so a resume asks it again (it is charged
                    # again, honestly); nothing is retried here, and no other worker charges after it
                    with self._lock:
                        self.limited += 1
                    self.journal.append({"t": "limited", "key": key, "task": name, "ts": self._clock()})
                    self.halt(e)
                    raise
                except StagePaused as e:
                    with self._lock:
                        self.throttled += 1
                    self.journal.append({"t": "throttled", "key": key, "task": name, "attempt": attempt, "ts": self._clock()})
                    attempt += 1
                    if attempt > self._retries:
                        self.halt(e)
                        raise
                    paused = True
                except BudgetExceeded as e:
                    # nothing was charged beyond the cap and nothing is retried; no other worker charges after it
                    self.halt(e)
                    raise
                else:
                    paused = False
            if paused:
                # the throttled call was charged once by the inner port; the retry below is one new charged call. Every other
                # worker waits at the gate meanwhile, so a throttle never multiplies into charged retries across workers
                delay = min(self._max, self._base * (2 ** (attempt - 1)))
                with self._gate:
                    self._backing_off += 1
                try:
                    with self._lock:
                        self.delays.append(delay)
                    self._sleep(delay)
                finally:
                    with self._gate:
                        self._backing_off -= 1
                        self._gate.notify_all()
                continue
            with self._lock:
                self.fresh += 1
            self.journal.append({"t": "result", "key": key, "task": name, "ok": res.ok, "output": res.output, "error": res.error,
                                 "ts": self._clock()})
            return res
