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
from living_context.model.port import CallContext, CallResult, PortRefused, Task

from . import claude_cli
from .claude_cli import AllowanceRefused, StageLimited, StagePaused

__all__ = ["Journal", "JournalCorrupt", "NotAdmitted", "UNSCOPED_REFUSAL", "journal_unscoped", "unscoped_keys", "read_resumable", "Tally", "ResilientPort", "ScopedPort", "ScopeRequired", "StageStopped", "StagePaused", "StageLimited", "BudgetExceeded"]


class StageStopped(Exception):
    """The stop file exists: the stage stops cleanly and is resumable."""


class NotAdmitted(Exception):
    """Raised inside the inner port when the shared halt gate refuses a call before its charge (review F1); `ResilientPort` journals
    the call as uncharged and raises the halt itself (`exc`), so it is never mistaken for the inner port's own refusal or throttle."""
    def __init__(self, exc: BaseException):
        super().__init__(str(exc))
        self.exc = exc


class JournalCorrupt(Exception):
    """A line before the last one of an append-only file does not parse: rows after it would be hidden, so nothing resumes on it."""


def read_resumable(path: Path) -> List[dict]:
    """The rows of an append-only JSON-lines file that a run appends to and a resume reads back (the journal, the ledger).

    An interrupted write (the process ended, the disk filled) can tear only the last line. That torn tail is moved aside to
    `<name>.torn.<k>` and cut from the file, and a last line without its newline gets one, so the next append starts on a clean
    line and nothing the resumed run writes is merged into a line no later load can parse. A line that does not parse before the
    last one raises `JournalCorrupt`."""
    path = Path(path)
    if not path.exists():
        return []
    raw = path.read_bytes()
    rows: List[dict] = []
    pos = 0
    segs = raw.split(b"\n")
    for i, seg in enumerate(segs):
        if seg.strip():
            try:
                row = json.loads(seg)
            except ValueError:
                if any(s.strip() for s in segs[i + 1:]):
                    raise JournalCorrupt("%s: line %d does not parse and rows follow it; refusing to resume on it" % (path.name, i + 1))
                k = 0
                while path.with_name("%s.torn.%d" % (path.name, k)).exists():
                    k += 1
                path.with_name("%s.torn.%d" % (path.name, k)).write_bytes(raw[pos:])
                with open(path, "r+b") as f:
                    f.truncate(pos)
                    f.flush()
                    os.fsync(f.fileno())
                return rows
            if isinstance(row, dict):
                rows.append(row)
        pos += len(seg) + 1
    if raw and not raw.endswith(b"\n"):         # complete, but its newline was never written
        with open(path, "ab") as f:
            f.write(b"\n")
            f.flush()
            os.fsync(f.fileno())
    return rows


class Journal:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.results: Dict[str, dict] = {}
        self.tail: Dict[str, dict] = {}         # the last row of every key
        self.rows = 0
        self.last: Optional[dict] = None
        self._lock = threading.Lock()
        for row in read_resumable(self.path):  # a torn last line is set aside; an unreadable earlier one refuses
            self.rows += 1
            self.last = row
            if "key" in row:
                self.tail[row["key"]] = row
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
            if "key" in row:
                self.tail[row["key"]] = row
            if row.get("t") == "result":
                self.results[row["key"]] = row


class ScopeRequired(Exception):
    """A call without a scope where every call needs one (more than one worker, or a port that requires scopes), or a journal with
    unscoped keys under a port that requires scopes: the journal key would depend on the worker count or interleaving."""


UNSCOPED_REFUSAL = ("the journal holds %d call(s) journaled without a question or build scope (an older commit at one worker): a "
                    "resume keys every call by its question or build group at any worker count, so it would ask answered calls "
                    "again and could replace their answers; refused, with no migration (start a new run directory)")


def unscoped_keys(keys) -> int:
    """How many journal keys carry no scope (`<sha>:<n>`; a scoped key is `<scope>|<sha>:<n>`)."""
    return sum(1 for k in keys if "|" not in str(k))


def journal_unscoped(path: Path) -> int:
    """Unscoped keys in a journal file, read without changing it (a torn last row is skipped); 0 when there is none."""
    path = Path(path)
    if not path.exists():
        return 0
    keys = set()
    for line in path.read_text().splitlines():
        try:
            r = json.loads(line)
        except ValueError:
            continue
        if isinstance(r, dict) and r.get("key") is not None:
            keys.add(r["key"])
    return unscoped_keys(keys)


class ScopedPort:
    """The port as one question (or one build group) sees it: every call carries the scope into the journal key."""

    def __init__(self, port: "ResilientPort", scope: str):
        self._port, self.scope = port, scope

    def call(self, task: Task, inputs: Mapping[str, Any], *, ctx: Optional[CallContext] = None) -> CallResult:
        return self._port.call(task, inputs, ctx=ctx, scope=self.scope)

    def stop_requested(self) -> bool:
        return self._port.stop_requested()


INVALID_ANSWERS = ("parse", "schema")             # the model answered, but not in the task's schema


class Tally:
    """One recall's view of the port that counts its failed calls (each becomes the product's own fallback inside a finished
    question): `timeouts`, `errors` (any other failed call: a process or API error, a call lost in flight) and `invalid` (an
    answer outside the schema). The ledger records them per question and arm, so a result's dependence on concurrency through
    timeouts and errors under load is measured, not assumed (review finding F7)."""
    def __init__(self, port):
        self._port = port
        self.failed: Dict[str, int] = {}

    def call(self, task: Task, inputs: Mapping[str, Any], **kw) -> CallResult:
        res = self._port.call(task, inputs, **kw)
        if not res.ok:
            k = res.error or "error"
            self.failed[k] = self.failed.get(k, 0) + 1
        return res

    def stop_requested(self) -> bool:
        return self._port.stop_requested()

    def counts(self) -> dict:
        f = self.failed
        return {"timeouts": f.get("timeout", 0), "invalid": sum(f.get(k, 0) for k in INVALID_ANSWERS),
                "errors": sum(v for k, v in f.items() if k != "timeout" and k not in INVALID_ANSWERS)}


class AdaptiveSlots:
    """In-flight slots whose limit moves between 1 and `ceiling`: it starts at `start` (a ramp), halves on a throttle or an allowance
    refusal, and doubles (up to the ceiling) after a clean window of max(8, 2 x limit) answered calls since the last change. A fixed
    limit (`adaptive=False`) never moves."""
    def __init__(self, ceiling: int, *, adaptive: bool = False, start: Optional[int] = None):
        self.ceiling, self.adaptive = int(ceiling), bool(adaptive)
        self.limit = max(1, min(self.ceiling, int(start))) if (adaptive and start) else self.ceiling
        self._inflight, self._clean = 0, 0
        self._cv = threading.Condition()
        self.changes: List[tuple] = []          # (old, new, reason)

    def acquire(self) -> None:
        with self._cv:
            while self._inflight >= self.limit:
                self._cv.wait(0.5)
            self._inflight += 1

    def release(self) -> None:
        with self._cv:
            self._inflight -= 1
            self._cv.notify_all()

    def _set(self, new: int, why: str) -> None:
        if new != self.limit:
            self.changes.append((self.limit, new, why))
            self.limit = new
        self._clean = 0
        self._cv.notify_all()

    def clean(self) -> None:
        with self._cv:
            if not self.adaptive:
                return
            self._clean += 1
            if self.limit < self.ceiling and self._clean >= max(8, 2 * self.limit):
                self._set(min(self.ceiling, 2 * self.limit), "clean window")

    def halve(self, why: str) -> None:
        with self._cv:
            if self.adaptive:
                self._set(max(1, self.limit // 2), why)


RETRY_AFTER_CEILING_S = 3600.0     # the longest provider-requested wait honoured in a run; above it the stage stops resumably (ASSUMPTION)


class ResilientPort:
    def __init__(self, inner, budget: Budget, journal: Journal, stop_file: Path, *, max_retries: int = 3, base_delay_s: float = 30.0,
                 max_delay_s: float = 600.0, sleep: Optional[Callable[[float], None]] = None,
                 retry_after_ceiling_s: float = RETRY_AFTER_CEILING_S,
                 render: Callable[[str, Mapping[str, Any]], str] = claude_cli.render,
                 retry_failed: tuple = ("build_l1", "build_upper"), failure_retries: int = 2,
                 clock: Callable[[], float] = time.time, concurrency: int = 1, adaptive: bool = False,
                 start_concurrency: Optional[int] = None, require_scope: bool = False):
        if int(concurrency) < 1:
            raise ValueError("concurrency must be at least 1")
        self._inner, self._budget, self.journal, self._stop = inner, budget, journal, Path(stop_file)
        self._retries, self._base, self._max = max_retries, base_delay_s, max_delay_s
        # `sleep` is a test hook; by default a back-off waits at the halt gate, so a halt or the stop file ends it at once
        self._sleep, self._render, self._clock = sleep, render, clock
        self._ra_ceiling = float(retry_after_ceiling_s)
        self._retry_failed, self._fail_retries = tuple(retry_failed), failure_retries
        self.concurrency = int(concurrency)
        self._occ: Dict[str, int] = {}
        # optional default journal scope for a single worker (kept for callers that set it between questions); with several workers
        # every call must come through `scoped(ref)` instead, because one shared attribute is one shared occurrence key
        self.scope: Optional[str] = None
        self._lock = threading.Lock()                           # occurrence counts, lost keys and the statistics below
        self.slots = AdaptiveSlots(self.concurrency, adaptive=adaptive, start=start_concurrency)
        if hasattr(inner, "row_extra"):                         # every call row records the effective concurrency
            inner.row_extra = lambda: {"concurrency": self.slots.limit}
        self._gate = threading.Condition()                      # the shared back-off and halt gate
        self._backing_off = 0
        self._halt: Optional[BaseException] = None
        self.replayed = self.fresh = self.lost = self.throttled = self.limited = 0
        # allowance refusals since the last answered call, across every worker: the stage halts when they pass max_retries (or one
        # call's own refusals do), so several workers refused together never multiply the charged retries (a per-call count allowed
        # workers x retries). Throttles stay per call: a burst of 429s in flight together halves the limit and goes on.
        self._refused = 0
        self.delays: List[float] = []
        self._tls = threading.local()                           # the journal key of the call this thread is making
        if hasattr(inner, "on_charge"):                         # the inner port reports each charge, in the charging thread
            inner.on_charge = self._charged
        if hasattr(inner, "admit"):                             # the inner port asks the gate again after its pacer, before its charge
            inner.admit, inner.on_halt = self._admit, self.halt
        if hasattr(inner, "on_outcome"):                        # throttles and refusals are decided under the inner charge lock
            inner.on_outcome = self._on_outcome
        self._lost_keys = self._lost_in_flight(journal)
        # the stage runners key every call by its question or build group at every worker count (review F2): one key scheme, so a
        # resume with another worker count replays what was answered; a journal written under another scheme is refused here
        self.require_scope = bool(require_scope)
        if self.require_scope and unscoped_keys(journal.tail):
            raise ScopeRequired(UNSCOPED_REFUSAL % unscoped_keys(journal.tail))

    def _charged(self, n: int) -> None:
        key = getattr(self._tls, "key", None)
        if key is not None:
            self.journal.append({"t": "charged", "key": key, "n": n, "ts": self._clock()})

    def _bump(self) -> int:
        with self._lock:
            self._refused += 1
            return self._refused

    @staticmethod
    def _lost_in_flight(journal: Journal) -> set:
        """Keys whose last journal row is `charged`: the call was charged and the run ended before its outcome was written. They are
        answered as lost (never asked again: the charge is spent). Nothing else is lost: a key whose last row is `pending` was never
        charged (it waited for a slot or the pacer, or was refused before the charge, which also writes `uncharged`) and is asked
        again. A kill between the charge and its journal row (an fsync apart) asks that one call again, charged and counted twice;
        a journal written before charges were journaled has no lost keys, and its pending calls are asked again the same way."""
        return {k for k, r in journal.tail.items() if r.get("t") == "charged"}

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

    def _halted(self) -> Optional[BaseException]:
        """Under the gate: the halt in force (a fresh exception of its type), the stop file making one, or None."""
        h = self._halt
        if h is not None:
            err = type(h)(str(h))
            if getattr(h, "retry_after_s", None) is not None:
                err.retry_after_s = h.retry_after_s       # every halted worker reports the provider's requested wait
            return err
        if self.stop_requested():
            self._halt = StageStopped("stop file present")
            self._gate.notify_all()
            return StageStopped("stop file present")
        return None

    def _wait_gate(self, inner: bool = False) -> bool:
        """Before every charge: no halt, no stop file, nobody backing off. Inside the inner port (`inner`) a refusal is NotAdmitted.
        True when it waited for a back-off."""
        waited = False
        with self._gate:
            while True:
                err = self._halted()
                if err is not None:
                    raise NotAdmitted(err) if inner else err
                if self._backing_off == 0:
                    return waited
                waited = True
                self._gate.wait(0.5)

    def _ceiling_pause(self, retry_after: float) -> StagePaused:
        when = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + float(retry_after)))
        return StagePaused("the provider asked for a wait of %.0f s, above the ceiling of %.0f s: the stage stops here; resume after %s"
                           % (float(retry_after), self._ra_ceiling, when), retry_after_s=float(retry_after))

    def _decide(self, kind: str, exc: BaseException, attempts: int) -> Optional[BaseException]:
        """THE place a throttle (`kind` "throttle") or an allowance refusal ("refusal") becomes a halt of the stage: returns the halt
        (published to every worker) or None. `attempts` counts this call's outcomes so far, this one included. A throttle halts on a
        requested wait above the ceiling or past `max_retries`; a refusal at a fixed concurrency at once, at an adaptive one past
        `max_retries` refusals (its own, or all workers' since the last answer)."""
        if kind == "throttle":
            ra = getattr(exc, "retry_after_s", None)
            h = self._ceiling_pause(ra) if ra is not None and float(ra) > self._ra_ceiling else exc if attempts > self._retries else None
        else:
            h = exc if (not self.slots.adaptive or max(attempts, self._bump()) > self._retries) else None
        if h is not None:
            self.halt(h)
        else:
            self._begin_backoff()
        return h

    def _begin_backoff(self) -> None:
        """A back-off of this thread's call is published here, when it is decided (under the inner charge lock when the inner port
        reports outcomes, review F3 of goodqa#16562): no worker charges from then until `_end_backoff`."""
        with self._gate:
            self._backing_off += 1
        self._tls.backoff = True

    def _end_backoff(self) -> None:
        """Ends the back-off this thread's call published, if any (after its wait, or on any exception before it)."""
        if getattr(self._tls, "backoff", False):
            self._tls.backoff = False
            with self._gate:
                self._backing_off -= 1
                self._gate.notify_all()

    def _on_outcome(self, kind: str, exc: BaseException) -> None:
        """Called by the inner port under its charge lock (review F1 of goodqa#16562): the decision is made and published before any
        other worker can pass its last admission check; the handler below reuses it."""
        self._tls.decision = (self._decide(kind, exc, getattr(self._tls, "attempt", 0) + 1),)

    def _decision(self, kind: str, exc: BaseException, attempts: int) -> Optional[BaseException]:
        d, self._tls.decision = getattr(self._tls, "decision", None), None
        return d[0] if d is not None else self._decide(kind, exc, attempts)     # an inner port without the hook

    def _backoff_wait(self, delay: float) -> None:
        """The back-off, at the halt gate: a halt or the stop file ends it at once (the throttled call is asked again on resume)."""
        if self._sleep is not None:
            self._sleep(delay)
            return
        end = time.monotonic() + delay
        with self._gate:
            while True:
                err = self._halted()
                if err is not None:
                    raise err
                left = end - time.monotonic()
                if left <= 0:
                    return
                self._gate.wait(min(left, 0.5))

    def _admit(self, block: bool) -> bool:
        """The inner port's admission (review F1): after its pacer wait (`block`, waiting out a back-off too: True when it did),
        during that wait and immediately before its charge (never blocking: it holds the charge lock then). Not blocking, True
        means a back-off is in force (review F3 of goodqa#16562): the inner port releases its lock, waits it out, takes a new pacer
        turn and asks again, uncharged meanwhile."""
        if block:
            return self._wait_gate(inner=True)
        with self._gate:
            err = self._halted()
            backing_off = self._backing_off > 0
        if err is not None:
            raise NotAdmitted(err)
        return backing_off

    def call(self, task: Task, inputs: Mapping[str, Any], *, ctx: Optional[CallContext] = None, scope: Optional[str] = None) -> CallResult:
        """One logical call. A failed BUILD call is asked again, as a new charged call with its own journal key, up to
        `failure_retries` times, because a failed build call aborts the whole generation (DESIGN 7.6); a failed route or organise
        call is the product's own fallback path and is never retried."""
        scope = scope if scope is not None else self.scope
        if scope is None and (self.concurrency > 1 or self.require_scope):
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
            self._tls.backoff = False
            try:
                self._wait_gate()
                self.slots.acquire()
                try:
                    self._wait_gate()                 # a halt or back-off may have started while this worker waited for a slot
                    self.journal.append({"t": "pending", "key": key, "task": name, "count_before": self._budget.count(),
                                         "concurrency": self.slots.limit, "ts": self._clock()})
                    self._tls.key, self._tls.attempt, self._tls.decision = key, attempt, None
                    try:
                        res = self._inner.call(task, inputs, ctx=ctx)
                    except NotAdmitted as e:
                        # halted or stopped while it waited in the pacer: never charged, never sent; a resume asks it again
                        self.journal.append({"t": "uncharged", "key": key, "task": name, "error": "halted", "ts": self._clock()})
                        raise e.exc from None
                    except PortRefused as e:
                        # a refused prompt is refused before the charge: close the key, so a resume asks it (and refuses it) again
                        self.journal.append({"t": "uncharged", "key": key, "task": name, "error": "PortRefused", "ts": self._clock()})
                        raise
                    except AllowanceRefused as e:
                        with self._lock:
                            self.limited += 1
                        self.journal.append({"t": "limited", "key": key, "task": name, "attempt": attempt, "ts": self._clock()})
                        attempt += 1
                        if self._decision("refusal", e, attempt) is not None:
                            raise                     # a fixed concurrency stops at once; an adaptive one after its bounded retries
                        self.slots.halve("allowance refusal")
                        paused, retry_after = True, None
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
                        retry_after = getattr(e, "retry_after_s", None)
                        h = self._decision("throttle", e, attempt)   # above the ceiling, or past max_retries (per call): a halt
                        if h is e:
                            raise
                        if h is not None:
                            raise h from None         # the ceiling pause, with no retry spent
                        self.slots.halve("throttle")
                        paused = True
                    except BudgetExceeded as e:
                        # nothing was charged beyond the cap and nothing is retried; no other worker charges after it
                        self.journal.append({"t": "uncharged", "key": key, "task": name, "error": "BudgetExceeded", "ts": self._clock()})
                        self.halt(e)
                        raise
                    else:
                        paused = False
                    finally:
                        self._tls.key = None
                finally:
                    self.slots.release()
                if paused:
                    # the throttled call was charged once by the inner port; the retry below is one new charged call. Every other
                    # worker waits at the gate meanwhile, so a throttle never multiplies into charged retries across workers
                    # only the exponential back-off is capped; Retry-After is a minimum, honoured in full up to the ceiling above
                    delay = max(min(self._max, self._base * (2 ** (attempt - 1))), float(retry_after or 0))
                    # the back-off was published when it was decided (`_decide`); the finally below ends it
                    with self._lock:
                        self.delays.append(delay)
                    self._backoff_wait(delay)
                    continue
            finally:
                self._end_backoff()       # ends the back-off this try published: after its wait, or on an exception
            with self._lock:
                self.fresh += 1
                self._refused = 0
            self.slots.clean()
            self.journal.append({"t": "result", "key": key, "task": name, "ok": res.ok, "output": res.output, "error": res.error,
                                 "ts": self._clock()})
            return res
