"""Ordered concurrency for a stage: run independent items on a few workers, keep every output in the items' own order.

The stage outputs must not depend on which call finished first (EXPERIMENT 3.7: a run is reproducible from its seed and commit). So the
work of item i may finish at any time, but its result is COMMITTED (a ledger row, a tree node) only after the results of every item
before it, by the calling thread: the ledger file is always a prefix of the seeded order, whatever the concurrency. A worker failure, a
`False` answer from `ok`, or the stop check ends the submission of new items; the items already running finish, the completed prefix is
committed, and the earliest failure in item order is raised (or, for a `False` answer, returned last). Items that finished after the
failure point are not committed: their calls are in the run journal, so a resume answers them from it without charging again.

Concurrency is a run parameter with a registered default (DESIGN 13: "the model port has one global concurrency (2 on the
subscription)"); a value above the default needs a recorded reason (`check_concurrency`)."""
from __future__ import annotations

import concurrent.futures as cf
from typing import Any, Callable, Dict, List, Optional, Sequence

REGISTERED_CONCURRENCY = 2


class Stopped(Exception):
    """The stop check was true before an item was submitted."""


def check_concurrency(value: int, reason: Optional[str]) -> List[str]:
    """Refusal reasons for a requested concurrency: at least 1; above the registered default only with a recorded reason."""
    out = []
    if not isinstance(value, int) or value < 1:
        out.append("concurrency must be a whole number of at least 1")
    elif value > REGISTERED_CONCURRENCY and not (reason or "").strip():
        out.append("concurrency %d is above the registered %d: give --concurrency-reason (it is recorded in the stamp)"
                   % (value, REGISTERED_CONCURRENCY))
    return out


def run_ordered(items: Sequence[Any], work: Callable[[Any], Any], commit: Callable[[int, Any, Any], None], *, workers: int,
                stop: Callable[[], bool] = lambda: False, ok: Callable[[Any], bool] = lambda r: True,
                stop_exc: Callable[[], BaseException] = lambda: Stopped("stop requested")) -> bool:
    """Runs `work(item)` with at most `workers` in flight and calls `commit(index, item, result)` strictly in item order. Returns True
    when every item was committed and False after a result that `ok` rejected (committed, and nothing after it). Raises the earliest
    worker exception in item order, or `stop_exc()` when the stop check ended the submission."""
    if workers < 1:
        raise ValueError("workers must be at least 1")
    results: Dict[int, Any] = {}
    errors: Dict[int, BaseException] = {}
    nxt_commit = 0
    halted = stopped = False
    rejected: Optional[int] = None

    def drain_ready() -> None:
        nonlocal nxt_commit, halted, rejected
        while rejected is None and nxt_commit in results:
            r = results.pop(nxt_commit)
            commit(nxt_commit, items[nxt_commit], r)
            if not ok(r):
                rejected, halted = nxt_commit, True
            nxt_commit += 1

    with cf.ThreadPoolExecutor(max_workers=workers) as ex:
        inflight: Dict[cf.Future, int] = {}
        i = 0
        while True:
            while not halted and i < len(items) and len(inflight) < workers:
                if stop():
                    halted = stopped = True
                    break
                inflight[ex.submit(work, items[i])] = i
                i += 1
            if not inflight:
                break
            done, _ = cf.wait(list(inflight), return_when=cf.FIRST_COMPLETED)
            for f in done:
                k = inflight.pop(f)
                e = f.exception()
                if e is not None:
                    errors[k] = e
                    halted = True
                else:
                    results[k] = f.result()
                    if not ok(results[k]):
                        halted = True
            drain_ready()
    drain_ready()
    if rejected is not None:
        return False
    if errors:
        raise errors[min(errors)]
    if stopped:
        raise stop_exc()
    return True


def ordered_map(workers: int) -> Callable[[Callable, Sequence], list]:
    """A `pmap` for `build_tree`: answers in item order, stopping after the first failed answer (`.ok` false) or exception."""
    def pmap(fn: Callable, items: Sequence) -> list:
        out: list = []
        run_ordered(items, fn, lambda i, x, r: out.append(r), workers=workers, ok=lambda r: bool(getattr(r, "ok", True)))
        return out
    return pmap
