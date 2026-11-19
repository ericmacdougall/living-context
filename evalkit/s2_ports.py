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
from .claude_cli import StagePaused

__all__ = ["Journal", "ResilientPort", "StageStopped", "StagePaused", "BudgetExceeded"]


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


class ResilientPort:
    def __init__(self, inner, budget: Budget, journal: Journal, stop_file: Path, *, max_retries: int = 3, base_delay_s: float = 30.0,
                 max_delay_s: float = 600.0, sleep: Callable[[float], None] = time.sleep,
                 render: Callable[[str, Mapping[str, Any]], str] = claude_cli.render,
                 retry_failed: tuple = ("build_l1", "build_upper"), failure_retries: int = 2,
                 clock: Callable[[], float] = time.time):
        self._inner, self._budget, self.journal, self._stop = inner, budget, journal, Path(stop_file)
        self._retries, self._base, self._max = max_retries, base_delay_s, max_delay_s
        self._sleep, self._render, self._clock = sleep, render, clock
        self._retry_failed, self._fail_retries = tuple(retry_failed), failure_retries
        self._occ: Dict[str, int] = {}
        self._lock = threading.Lock()
        self.replayed = self.fresh = self.lost = self.throttled = 0
        self.delays: List[float] = []
        # the one possibly-lost call: the journal ends with a pending row and the counter moved past what it recorded
        last = journal.last
        self._lost_key = last["key"] if last and last.get("t") == "pending" and budget.count() > last.get("count_before", 0) else None

    def stop_requested(self) -> bool:
        return self._stop.exists()

    def call(self, task: Task, inputs: Mapping[str, Any], *, ctx: Optional[CallContext] = None) -> CallResult:
        """One logical call. A failed BUILD call is asked again, as a new charged call with its own journal key, up to
        `failure_retries` times, because a failed build call aborts the whole generation (DESIGN 7.6); a failed route or organise
        call is the product's own fallback path and is never retried."""
        tries = 0
        while True:
            res = self._attempt(task, inputs, ctx)
            if res.ok or task.value not in self._retry_failed or tries >= self._fail_retries:
                return res
            tries += 1

    def _attempt(self, task: Task, inputs: Mapping[str, Any], ctx: Optional[CallContext]) -> CallResult:
        with self._lock:                      # one call at a time, as the inner port already requires
            name = task.value
            sha = hashlib.sha256(self._render(name, inputs).encode()).hexdigest()[:32]
            n = self._occ.get(sha, 0)
            self._occ[sha] = n + 1
            key = "%s:%d" % (sha, n)
            row = self.journal.results.get(key)
            if row is not None:
                self.replayed += 1
                return CallResult(bool(row["ok"]), row.get("output"), row.get("error") or "")
            if self._lost_key == key:
                self._lost_key = None
                self.lost += 1
                self.journal.append({"t": "result", "key": key, "task": name, "ok": False, "output": None, "error": "lost_in_flight",
                                     "ts": self._clock()})
                return CallResult(False, error="lost_in_flight")
            attempt = 0
            while True:
                if self.stop_requested():
                    raise StageStopped("stop file present")
                self.journal.append({"t": "pending", "key": key, "task": name, "count_before": self._budget.count(),
                                     "ts": self._clock()})
                try:
                    res = self._inner.call(task, inputs, ctx=ctx)
                except StagePaused:
                    self.throttled += 1
                    self.journal.append({"t": "throttled", "key": key, "task": name, "attempt": attempt, "ts": self._clock()})
                    attempt += 1
                    if attempt > self._retries:
                        raise
                    delay = min(self._max, self._base * (2 ** (attempt - 1)))
                    self.delays.append(delay)
                    self._sleep(delay)
                    continue
                # BudgetExceeded and refusals propagate: nothing was charged beyond the cap, nothing is retried
                self.fresh += 1
                self.journal.append({"t": "result", "key": key, "task": name, "ok": res.ok, "output": res.output, "error": res.error,
                                     "ts": self._clock()})
                return res
