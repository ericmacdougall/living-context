"""The per-call pool authorization (POOLS 2.4 checks 1 to 4, 2.7): one function, memory only, no Elasticsearch read.

`authorize(...)` resolves the token (exp, individual revocation, epoch, root and parent: `Sessions.resolve`), refuses a token with no
scope (a plain session reaches no pool: least privilege), and for EVERY pool named checks that the pool is active, in the token's scope
with each needed operation, held now in the principal's live grant table, and that its class is in the principal's read set (query,
retract, review) or write set (save, update). A call naming several pools is checked whole: if one fails, the whole call is refused
and nothing is built. Every refusal is the same `None`, from which the caller returns the one `unavailable` answer.

The send hand-off repeats checks 1 to 4 (2.4: "for every pool whose content the response carries"). What a response carries is
collected while the request runs: `carried()` opens a collector for one request (the gateway's `serve`), every successful `authorize`
adds its pools and operations to it (union across calls), and a call that checks pools only to choose among them (pool_query's G)
passes `carry=False` and then `carry(...)` the pools it actually reads (S). The server re-runs `authorize(..., carry=False,
limit=None)` over the collected needs under the shared side of the send fence at every hand-off (`PoolTools.send_ok`).
"""
from __future__ import annotations

import contextlib
import contextvars
from dataclasses import dataclass
from typing import Iterable, Iterator, Mapping, Optional

from ..policy import ClassLattice, Principal
from ..policy import pools as pl
from .sessions import Record, Sessions

MAX_POOLS_PER_CALL = 32
_CARRIED: contextvars.ContextVar = contextvars.ContextVar("lc_pool_needs_carried", default=None)


@contextlib.contextmanager
def carried() -> Iterator[dict]:
    """One request's collector: pool id -> the operations checked on it by every successful `authorize` of the request. Reset on exit,
    so nothing leaks into the next request on the same thread."""
    box: dict = {}
    tok = _CARRIED.set(box)
    try:
        yield box
    finally:
        _CARRIED.reset(tok)


def carry(needs: Mapping[str, Iterable[str]]) -> None:
    """Add pools (and their operations) that the response will carry to the current request's collector (no-op outside one)."""
    box = _CARRIED.get()
    if box is not None:
        for pid, ops in needs.items():
            box.setdefault(pid, set()).update(ops)


@dataclass(frozen=True)
class PoolAuth:
    rec: Record
    principal: Principal
    pools: Mapping[str, pl.Pool]


def authorize(sessions: Sessions, directory: pl.PoolDirectory, lattices: Mapping[str, ClassLattice], token: str,
              needs: Mapping[str, Iterable[str]], *, broken_scope_not_checked: bool = False,
              broken_whole_call: bool = False, broken_state_from_mint: bool = False, carry_needs: bool = True,
              limit: Optional[int] = MAX_POOLS_PER_CALL) -> Optional[PoolAuth]:
    """`needs`: pool id to the operations the call needs on it. On success the pools and operations are added to the request's
    collector (`carry_needs=False`: a check made only to choose pools, or the hand-off's own re-check). `limit` bounds the pools of one
    call; the hand-off re-check passes None, since a query's S is chosen pool by pool and is bounded by the token scope only. The
    `broken_*` flags are for the tests' broken variants only: the token scope not consulted; a partial answer when some pools fail; the
    pool state not re-read."""
    rec = sessions.resolve(token)
    if rec is None or rec.scope is None:
        return None
    lattice = lattices.get(rec.tenant)
    if lattice is None or not needs or (limit is not None and len(needs) > limit):
        return None
    p = sessions.principal(rec)
    got: dict[str, pl.Pool] = {}
    for pid, ops in needs.items():
        ok = isinstance(pid, str)
        pool = None
        if ok:
            for op in ops:
                pool = pl.pool_allows(directory, lattice, p, pid, op, None if broken_scope_not_checked else rec.scope,
                                      check_state=not broken_state_from_mint)
                if pool is None:
                    break
        if pool is None:
            if broken_whole_call:
                continue    # BROKEN variant: the failing pool is skipped and the rest is served
            return None
        got[pid] = pool
    if not got:
        return None
    if carry_needs:
        carry({pid: needs[pid] for pid in got})
    return PoolAuth(rec, p, got)
