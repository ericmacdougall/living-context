"""The per-call pool authorization (POOLS 2.4 checks 1 to 4, 2.7): one function, memory only, no Elasticsearch read.

`authorize(...)` resolves the token (exp, individual revocation, epoch, root and parent: `Sessions.resolve`), refuses a token with no
scope (a plain session reaches no pool: least privilege), and for EVERY pool named checks that the pool is active, in the token's scope
with each needed operation, held now in the principal's live grant table, and that its class is in the principal's read set (query,
retract, review) or write set (save, update). A call naming several pools is checked whole: if one fails, the whole call is refused
and nothing is built. Every refusal is the same `None`, from which the caller returns the one `unavailable` answer.

The send hand-off repeats checks 1 to 4 (2.4: "for every pool whose content the response carries"). What a response carries is
collected while the request runs: `carried()` opens a collector for one request (the gateway's `serve`), every successful `authorize`
adds its pools and operations to it (union across calls), and a call that checks pools only to choose among them (pool_query's G)
passes `carry=False` and then `carry(...)` the pools it actually reads (S). The server re-runs the collected needs under the shared side
of the send fence at every hand-off (`recheck`, through `PoolTools.send_ok`).

Visibility (review F16, round 4). The operations that return pools without reading their content (pool_list, pool_describe) pass the
ONE live visibility check, `sees`, at the call (`view`), and every answer that names a pool carries the pseudo-need `VIEW` for it (the
pool_create receipt too, whose call is the administrator check, `sees`'s administrator lane), which the hand-off re-runs through `sees`
like any content need. `sees` is POOLS 2.7 applied to a pool's directory entry (id, label, card, class,
kind, private flag, state), which is administrator text visible to grantees (4.1):
* a scoped token sees a pool when SOME operation of its scope on that pool passes check 4 now (active, in scope, held in the live grant
  table, class in the read or write set);
* a plain session of a tenant administrator sees every pool of its tenant, in any state (the administrator detaches, reattaches, grants
  and deletes them);
* any other plain session sees a pool when SOME operation passes check 4 now without a token scope (active, live grant, class).
`VIEW` is not an operation (`pools.OPERATIONS`): it is never granted, minted or delegated, only checked.
"""
from __future__ import annotations

import contextlib
import contextvars
from dataclasses import dataclass
from typing import Iterable, Iterator, Mapping, Optional

from ..policy import ClassLattice, Principal, Role
from ..policy import pools as pl
from .sessions import Record, Sessions

MAX_POOLS_PER_CALL = 32
VIEW = "view"   # the right to see a pool's directory entry; a pseudo-need, never an operation (see the module docstring)
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


def sees(sessions: Sessions, directory: pl.PoolDirectory, lattices: Mapping[str, ClassLattice], rec: Record,
         pool_id: object) -> Optional[pl.Pool]:
    """The one live visibility check (VIEW): the pool when `rec` may see its directory entry now, else None (the same None for a pool
    that does not exist). Memory only: the pool directory and the grant table under their own leaf locks."""
    if not isinstance(pool_id, str) or rec is None:
        return None
    lattice = lattices.get(rec.tenant)
    if lattice is None:
        return None
    p = sessions.principal(rec)
    if rec.scope is not None:
        ops, scope = rec.scope.get(pool_id, frozenset()), rec.scope
    elif Role.TENANT_ADMIN in p.roles and p.tenant == rec.tenant:
        return directory.get(rec.tenant, pool_id)
    else:
        ops, scope = pl.OPERATIONS, None
    for op in sorted(ops):
        pool = pl.pool_allows(directory, lattice, p, pool_id, op, scope)
        if pool is not None:
            return pool
    return None


def view(sessions: Sessions, directory: pl.PoolDirectory, lattices: Mapping[str, ClassLattice], rec: Record,
         pool_ids: Iterable[object], *, carry_needs: bool = True) -> dict:
    """Pool id to pool, for every pool of `pool_ids` that `rec` sees now (`sees`), in the order given. Each one is carried with `VIEW`,
    so the hand-off re-checks it. No cap: a listing names as many pools as the caller sees."""
    got = {}
    for pid in pool_ids:
        pool = sees(sessions, directory, lattices, rec, pid)
        if pool is not None:
            got[pid] = pool
    if carry_needs and got:
        carry({pid: {VIEW} for pid in got})
    return got


def recheck(sessions: Sessions, directory: pl.PoolDirectory, lattices: Mapping[str, ClassLattice], token: str,
            needs: Mapping[str, Iterable[str]]) -> bool:
    """The send hand-off (POOLS 2.4, under the shared side of the send fence, before every chunk): checks 1 to 3 for `token`, then for
    every carried pool its content operations through `authorize` (whole, no cap) and its `VIEW` through `sees`. An operation that is
    neither is refused. Memory only; any failure is False and aborts the whole answer."""
    rec = sessions.resolve(token)
    if rec is None:
        return False
    content: dict = {}
    for pid, ops in needs.items():
        ops = set(ops)
        if not ops or not ops <= (pl.OPERATIONS | {VIEW}):
            return False
        if VIEW in ops and sees(sessions, directory, lattices, rec, pid) is None:
            return False
        if ops - {VIEW}:
            content[pid] = ops - {VIEW}
    return not content or authorize(sessions, directory, lattices, token, content, carry_needs=False, limit=None) is not None
