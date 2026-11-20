"""The per-call pool authorization (POOLS 2.4 checks 1 to 4, 2.7): one function, memory only, no Elasticsearch read.

`authorize(...)` resolves the token (exp, individual revocation, epoch, root and parent: `Sessions.resolve`), refuses a token with no
scope (a plain session reaches no pool: least privilege), and for EVERY pool named checks that the pool is active, in the token's scope
with each needed operation, held now in the principal's live grant table, and that its class is in the principal's read set (query,
retract, review) or write set (save, update). A call naming several pools is checked whole: if one fails, the whole call is refused
and nothing is built. Every refusal is the same `None`, from which the caller returns the one `unavailable` answer.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Mapping, Optional

from ..policy import ClassLattice, Principal
from ..policy import pools as pl
from .sessions import Record, Sessions

MAX_POOLS_PER_CALL = 32


@dataclass(frozen=True)
class PoolAuth:
    rec: Record
    principal: Principal
    pools: Mapping[str, pl.Pool]


def authorize(sessions: Sessions, directory: pl.PoolDirectory, lattices: Mapping[str, ClassLattice], token: str,
              needs: Mapping[str, Iterable[str]], *, broken_scope_not_checked: bool = False,
              broken_whole_call: bool = False, broken_state_from_mint: bool = False) -> Optional[PoolAuth]:
    """`needs`: pool id to the operations the call needs on it. The `broken_*` flags are for the tests' broken variants only: the
    token scope not consulted; a partial answer when some pools fail; the pool state not re-read."""
    rec = sessions.resolve(token)
    if rec is None or rec.scope is None:
        return None
    lattice = lattices.get(rec.tenant)
    if lattice is None or not needs or len(needs) > MAX_POOLS_PER_CALL:
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
    return PoolAuth(rec, p, got) if got else None
