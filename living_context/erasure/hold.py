"""Legal hold (DESIGN 14.1: "legal hold wins: the erasure is queued and the delay is written in the receipt"; E5).

A hold names a scope: a subject token or a leaf id. While a hold covers a request, the erasure is QUEUED: no tombstone is
appended, nothing is published and nothing is deleted (a hold means the content must be kept). The queue and the holds are
durable (a small JSON file replaced atomically, fsynced, directory fsynced), so a queued erasure survives a gateway restart.
When the last hold covering a queued request is released, the erasure runs and its receipt states the delay (request to the
start of the erasure). A queued request leaves the queue only after it has run to its receipt (`complete`): a crash or any
failure other than `Held` between the two leaves it queued and it runs again (an erasure is idempotent: a repeat finds an empty
closure). The `Executor` takes the registry at construction and checks it under the gate in every public erasure entry point
(subject, item, relabel); there is no argument that skips it. A restore's replay re-applies a tombstone already published, so a
hold does not apply to it. `honour_holds=False` is the BROKEN variant used by the E5 test: the request runs at once."""
from __future__ import annotations

import json
import os
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Optional

from .executor import Executor, Held, Receipt


@dataclass
class Outcome:
    queued: bool
    receipt: Optional[Receipt] = None
    delay_s: float = 0.0


def _keys(subject: str, leaf_ids: Iterable[str]) -> set:
    return ({f"s:{subject}"} if subject else set()) | {f"l:{x}" for x in leaf_ids}


class HoldRegistry:
    def __init__(self, path: Optional[Path] = None, *, clock: Callable[[], float] = time.time, honour_holds: bool = True):
        """`path=None` keeps the registry in memory (tests and tools that need no durability); a gateway uses a path."""
        self._p, self._clock, self._honour = (Path(path) if path is not None else None), clock, honour_holds
        if self._p is not None:
            self._p.parent.mkdir(parents=True, exist_ok=True)
        self._state = json.loads(self._p.read_text()) if self._p is not None and self._p.exists() else {"holds": [], "queue": []}
        for q in self._state["queue"]:
            q.setdefault("id", uuid.uuid4().hex)
            q.setdefault("kind", "erase")

    def _save(self) -> None:
        if self._p is None:
            return
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

    def covered(self, subject: str, leaf_ids: Iterable[str], tokens: Iterable[str] = ()) -> bool:
        """`tokens`: the subject tokens of the leaves the closure reaches (a hold on a person covers an item of theirs)."""
        return self._honour and bool(self.holds() & (_keys(subject, leaf_ids) | {f"s:{t}" for t in tokens}))

    def enqueue(self, subject: str, leaf_ids: Iterable[str], identifiers: Iterable[str] = (), bare_tokens: Iterable[str] = (), *,
                kind: str = "erase", cls: str = "") -> str:
        qid = uuid.uuid4().hex
        self._state["queue"].append({"id": qid, "kind": kind, "cls": cls, "subject": subject, "leaf_ids": sorted(set(leaf_ids)),
                                     "identifiers": sorted(set(identifiers)), "bare_tokens": sorted(set(bare_tokens)),
                                     "requested_at": self._clock()})
        self._save()
        return qid

    def release(self, *, subject: str = "", leaf_ids: Iterable[str] = ()) -> list:
        """Drop the hold and return the queued requests that no remaining hold covers, oldest first. They STAY queued until the
        caller reports each one done with `complete`: a failure between the two loses nothing."""
        self._state["holds"] = sorted(self.holds() - _keys(subject, leaf_ids))
        self._save()
        return sorted((dict(q) for q in self._state["queue"] if not self.covered(q["subject"], q["leaf_ids"])),
                      key=lambda q: q["requested_at"])

    def complete(self, qid: str) -> None:
        """A queued request has run to its receipt: only now does it leave the queue."""
        self._state["queue"] = [q for q in self._state["queue"] if q["id"] != qid]
        self._save()


def request_erasure(ex: Executor, subject: str, *, leaf_ids: Iterable[str] = (), identifiers: Iterable[str] = (),
                    bare_tokens: Iterable[str] = (), apply_guard: Callable[[], None] = lambda: None) -> Outcome:
    """File an erasure. A held scope is queued (nothing is published); otherwise it runs at once. The registry is the executor's."""
    reg, leaf_ids = ex.holds, list(leaf_ids)
    if reg.covered(subject, leaf_ids):
        reg.enqueue(subject, leaf_ids, identifiers, bare_tokens)
        return Outcome(queued=True)
    try:
        return Outcome(queued=False, receipt=ex.erase_subject(subject, item_leaves=leaf_ids, identifiers=identifiers,
                                                              bare_tokens=bare_tokens, apply_guard=apply_guard))
    except Held:   # a hold on something the closure reaches, found under the gate before anything was published
        reg.enqueue(subject, leaf_ids, identifiers, bare_tokens)
        return Outcome(queued=True)


def request_relabel(ex: Executor, leaf_ids: Iterable[str], old_cls: str, *, apply_guard: Callable[[], None] = lambda: None) -> Outcome:
    """File an upward relabel (the erasure of the old-class copy). Held: queued and nothing changes, the caller writes no new
    version until the released request returns its receipt (ASSUMPTION, design-readings)."""
    leaf_ids = list(leaf_ids)
    if ex.holds.covered("", leaf_ids):
        ex.holds.enqueue("", leaf_ids, kind="relabel", cls=old_cls)
        return Outcome(queued=True)
    try:
        return Outcome(queued=False, receipt=ex.relabel_erase(leaf_ids, old_cls, apply_guard=apply_guard))
    except Held:
        ex.holds.enqueue("", leaf_ids, kind="relabel", cls=old_cls)
        return Outcome(queued=True)


def release_hold(ex: Executor, *, subject: str = "", leaf_ids: Iterable[str] = (),
                 apply_guard: Callable[[], None] = lambda: None, _broken_pop_first: bool = False) -> list:
    """Release a hold: every queued request it was the last obstacle to runs now, with the delay in its receipt. A request
    leaves the queue only after its receipt exists; any failure but `Held` propagates and leaves it queued."""
    out, reg = [], ex.holds
    for q in reg.release(subject=subject, leaf_ids=leaf_ids):
        if _broken_pop_first:   # BROKEN variant (the previous behaviour): the request leaves the queue before it has run
            reg.complete(q["id"])
        delay = max(0.0, reg._clock() - q["requested_at"])
        try:
            if q["kind"] == "relabel":
                rc = ex.relabel_erase(q["leaf_ids"], q["cls"], apply_guard=apply_guard)
            else:
                rc = ex.erase_subject(q["subject"], item_leaves=q["leaf_ids"], identifiers=q.get("identifiers", []),
                                      bare_tokens=q.get("bare_tokens", []), apply_guard=apply_guard)
        except Held:   # another hold still covers what the closure reaches: the request stays queued, with its original time
            continue
        rc.hold_delay_s = delay
        reg.complete(q["id"])
        out.append(Outcome(queued=False, receipt=rc, delay_s=delay))
    return out
