"""Legal hold (DESIGN 14.1: "legal hold wins: the erasure is queued and the delay is written in the receipt"; E5).

A hold names a scope: a subject token or a leaf id. While a hold covers a request, the erasure is QUEUED: no tombstone is
appended, nothing is published and nothing is deleted (a hold means the content must be kept). The queue and the holds are
durable (a small JSON file replaced atomically, fsynced, directory fsynced), so a queued erasure survives a gateway restart.
When the last hold covering a queued request is released, the erasure runs and its receipt states the delay (request to the
start of the erasure). `honour_holds=False` is the BROKEN variant used by the E5 test: the request runs at once."""
from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Optional

from .executor import Executor, Receipt


@dataclass
class Outcome:
    queued: bool
    receipt: Optional[Receipt] = None
    delay_s: float = 0.0


def _keys(subject: str, leaf_ids: Iterable[str]) -> set:
    return ({f"s:{subject}"} if subject else set()) | {f"l:{x}" for x in leaf_ids}


class HoldRegistry:
    def __init__(self, path: Path, *, clock: Callable[[], float] = time.time, honour_holds: bool = True):
        self._p, self._clock, self._honour = Path(path), clock, honour_holds
        self._p.parent.mkdir(parents=True, exist_ok=True)
        self._state = json.loads(self._p.read_text()) if self._p.exists() else {"holds": [], "queue": []}

    def _save(self) -> None:
        tmp = self._p.with_suffix(".new")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(json.dumps(self._state, sort_keys=True))
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, self._p)
        dfd = os.open(self._p.parent, os.O_RDONLY)
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)

    def holds(self) -> set:
        return set(self._state["holds"])

    def queued(self) -> list:
        return [dict(q) for q in self._state["queue"]]

    def place(self, *, subject: str = "", leaf_ids: Iterable[str] = ()) -> None:
        self._state["holds"] = sorted(self.holds() | _keys(subject, leaf_ids))
        self._save()

    def covered(self, subject: str, leaf_ids: Iterable[str]) -> bool:
        return self._honour and bool(self.holds() & _keys(subject, leaf_ids))

    def enqueue(self, subject: str, leaf_ids: Iterable[str]) -> None:
        self._state["queue"].append({"subject": subject, "leaf_ids": sorted(set(leaf_ids)), "requested_at": self._clock()})
        self._save()

    def release(self, *, subject: str = "", leaf_ids: Iterable[str] = ()) -> list:
        """Drop the hold and return (and remove from the queue) the requests that no remaining hold covers, oldest first."""
        self._state["holds"] = sorted(self.holds() - _keys(subject, leaf_ids))
        due = [q for q in self._state["queue"] if not self.covered(q["subject"], q["leaf_ids"])]
        self._state["queue"] = [q for q in self._state["queue"] if q not in due]
        self._save()
        return sorted(due, key=lambda q: q["requested_at"])


def request_erasure(ex: Executor, reg: HoldRegistry, subject: str, *, leaf_ids: Iterable[str] = (),
                    apply_guard: Callable[[], None] = lambda: None) -> Outcome:
    """File an erasure. A held scope is queued (nothing is published); otherwise it runs at once."""
    leaf_ids = list(leaf_ids)
    if reg.covered(subject, leaf_ids):
        reg.enqueue(subject, leaf_ids)
        return Outcome(queued=True)
    return Outcome(queued=False, receipt=ex.erase_subject(subject, item_leaves=leaf_ids, apply_guard=apply_guard))


def release_hold(ex: Executor, reg: HoldRegistry, *, subject: str = "", leaf_ids: Iterable[str] = (),
                 apply_guard: Callable[[], None] = lambda: None) -> list:
    """Release a hold: every queued erasure it was the last obstacle to runs now, with the delay in its receipt."""
    out = []
    for q in reg.release(subject=subject, leaf_ids=leaf_ids):
        delay = max(0.0, reg._clock() - q["requested_at"])
        rc = ex.erase_subject(q["subject"], item_leaves=q["leaf_ids"], apply_guard=apply_guard)
        rc.hold_delay_s = delay
        out.append(Outcome(queued=False, receipt=rc, delay_s=delay))
    return out
