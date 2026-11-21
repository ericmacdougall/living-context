"""Memory pools: the object, the grants and effective access (POOLS 1, 2.1, 2.2, 2.4; pure, no I/O).

A pool belongs to exactly one access class (reading B of POOLS 1.1): the class is the pool's sensitivity label, the pool is the unit
a session is granted. Effective access is the conjunction of the pool grant and the class rules of DESIGN 4 (`decide`).
Pool ids are 16 random lowercase alphanumerics, never reused. Labels live only in the card, restricted to `[a-z0-9 _-]{1,32}`,
unique per tenant after a confusables skeleton.

Grants carry a per-tenant monotone `counter`: a removal journalled at counter `c` removes only grants made at counter `<= c`, so a
grant re-made later (a higher counter) is never confused with the removed one when the stores are replayed after a restart (2.5).
"""
from __future__ import annotations

import re
import secrets
import string
import threading
from dataclasses import dataclass, replace
from typing import Callable, Iterable, Mapping, Optional

from .classes import ClassLattice
from .decide import Principal, decide
from .roles import Op

QUERY, SAVE, UPDATE, RETRACT, REVIEW = "query", "save", "update", "retract", "review"
OPERATIONS = frozenset({QUERY, SAVE, UPDATE, RETRACT, REVIEW})
DEFAULT_GRANT = frozenset({QUERY, SAVE})          # a pool's default grants (2.1); the rest is granted explicitly
ACTIVE, DETACHED, DELETING = "active", "detached", "deleting"
STATES = frozenset({ACTIVE, DETACHED, DELETING})
KINDS = frozenset({"project", "team", "person"})
POOL_ID = re.compile(r"^[a-z0-9]{16}$")
LABEL = re.compile(r"^[a-z0-9 _-]{1,32}$")
_ALPHABET = string.ascii_lowercase + string.digits
UNAVAILABLE = {"ok": False, "error": "unavailable"}   # the one shape of every pool-related refusal (2.7)


class PoolError(ValueError):
    pass


def new_pool_id(rand: Callable[[int], int] = secrets.randbelow) -> str:
    return "".join(_ALPHABET[rand(len(_ALPHABET))] for _ in range(16))


_CONFUSABLE = str.maketrans({"0": "o", "1": "l", "5": "s", "3": "e", "4": "a", "7": "t", "8": "b"})


def skeleton(label: str) -> str:
    """The confusables skeleton of a label: case, separators and look-alike digits folded away."""
    return re.sub(r"[ _-]", "", label.lower()).translate(_CONFUSABLE)


@dataclass(frozen=True)
class Pool:
    id: str
    tenant: str
    cls: str
    kind: str
    label: str
    card: str = ""
    private: bool = False
    state: str = ACTIVE
    state_counter: int = 0


class PoolDirectory:
    """The gateway's own table of pools and pool grants, in memory (no Elasticsearch read on the hot path, 2.4). `persist_*` are
    best-effort writes to the tenant's stores; `journal` is the tenant's epoch journal (the caller wires it, see `gateway.pool_admin`)."""

    def __init__(self, *, rand: Callable[[int], int] = secrets.randbelow):
        self._lock = threading.RLock()
        self._rand = rand
        self._pools: dict[tuple[str, str], Pool] = {}
        self._grants: dict[tuple[str, str, str], tuple[frozenset, int]] = {}   # (tenant, principal, pool) -> (ops, counter)
        self._counters: dict[str, int] = {}
        self._ceilings: dict[str, int] = {}
        self._used_ids: set[tuple[str, str]] = set()

    # ---- counters --------------------------------------------------------------------------------------------------
    def next_counter(self, tenant: str) -> int:
        """The next counter of the tenant. Past the ceiling the caller reserved (`limit_counter`) there is none: a counter is never
        taken that a later process could take again (`gateway.pool_admin.PoolService.reserve_counters`)."""
        with self._lock:
            c = self._counters.get(tenant, 0) + 1
            if c > self._ceilings.get(tenant, c):
                raise PoolError("counter block exhausted")
            self._counters[tenant] = c
            return c

    def observe_counter(self, tenant: str, counter: int) -> None:
        """A counter read back from a store or the journal at start: the next one is above it."""
        with self._lock:
            self._counters[tenant] = max(self._counters.get(tenant, 0), int(counter))

    def counter(self, tenant: str) -> int:
        with self._lock:
            return self._counters.get(tenant, 0)

    def limit_counter(self, tenant: str, ceiling: int) -> None:
        """The tenant's counters stop at `ceiling` (the block this process reserved in the epoch journal)."""
        with self._lock:
            self._ceilings[tenant] = int(ceiling)

    # ---- pools -------------------------------------------------------------------------------------------------------
    def create(self, tenant: str, cls: str, kind: str, label: str, card: str = "") -> Pool:
        if kind not in KINDS or not isinstance(label, str) or not LABEL.match(label) or not isinstance(card, str) or len(card) > 500:
            raise PoolError("invalid pool")
        with self._lock:
            sk = skeleton(label)
            if any(p.tenant == tenant and skeleton(p.label) == sk for p in self._pools.values()):
                raise PoolError("label taken")
            for _ in range(64):
                pid = new_pool_id(self._rand)
                if (tenant, pid) not in self._used_ids:   # never reused, also after a delete
                    break
            else:
                raise PoolError("no unused pool id")
            self._used_ids.add((tenant, pid))
            pool = Pool(pid, tenant, cls, kind, label, card, private=(kind == "person"), state=ACTIVE,
                        state_counter=self.next_counter(tenant))
            self._pools[(tenant, pid)] = pool
            return pool

    def restore(self, pool: Pool) -> None:
        """A pool read back at start-up: memory only."""
        with self._lock:
            self._pools[(pool.tenant, pool.id)] = pool
            self._used_ids.add((pool.tenant, pool.id))
            self.observe_counter(pool.tenant, pool.state_counter)

    def get(self, tenant: str, pool_id: str) -> Optional[Pool]:
        with self._lock:
            return self._pools.get((tenant, pool_id)) if isinstance(pool_id, str) else None

    def set_state(self, tenant: str, pool_id: str, state: str, counter: Optional[int] = None) -> Pool:
        if state not in STATES:
            raise PoolError("invalid state")
        with self._lock:
            p = self._pools[(tenant, pool_id)]
            c = counter if counter is not None else self.next_counter(tenant)
            if counter is not None and c <= p.state_counter:
                return p   # an older record than the state already held (replay of a superseded record)
            self.observe_counter(tenant, c)
            self._pools[(tenant, pool_id)] = p = replace(p, state=state, state_counter=c)
            return p

    def drop(self, tenant: str, pool_id: str) -> None:
        """A deleted pool leaves the table; its id stays used."""
        with self._lock:
            self._pools.pop((tenant, pool_id), None)
            for k in [k for k in self._grants if k[0] == tenant and k[2] == pool_id]:
                del self._grants[k]

    def pools_of(self, tenant: str) -> list[Pool]:
        with self._lock:
            return sorted((p for p in self._pools.values() if p.tenant == tenant), key=lambda p: p.id)

    # ---- grants --------------------------------------------------------------------------------------------------------
    def grant(self, tenant: str, principal: str, pool_id: str, ops: Iterable[str], counter: Optional[int] = None) -> int:
        ops = frozenset(ops)
        if not ops or not ops <= OPERATIONS:
            raise PoolError("invalid operations")
        with self._lock:
            if (tenant, pool_id) not in self._pools:
                raise PoolError("no such pool")
            c = counter if counter is not None else self.next_counter(tenant)
            self.observe_counter(tenant, c)
            have, hc = self._grants.get((tenant, principal, pool_id), (frozenset(), 0))
            if counter is not None and c <= hc:
                return hc   # replay of an older record
            self._grants[(tenant, principal, pool_id)] = (have | ops, c)
            return c

    def remove_grant(self, tenant: str, principal: str, pool_id: str, ops: Optional[Iterable[str]] = None,
                     counter: Optional[int] = None) -> int:
        """Remove `ops` (all of them when None). Returns the removal's counter. Applied at counter `c`, it removes only a grant made
        at a counter `<= c`; with a live call `c` is a fresh counter, so it removes whatever is held."""
        with self._lock:
            c = counter if counter is not None else self.next_counter(tenant)
            self.observe_counter(tenant, c)
            k = (tenant, principal, pool_id)
            have, hc = self._grants.get(k, (frozenset(), 0))
            if hc > c:
                return c   # the grant is newer than this removal: a re-made grant is not the removed one
            left = frozenset() if ops is None else have - frozenset(ops)
            if left:
                self._grants[k] = (left, hc)
            else:
                self._grants.pop(k, None)
            return c

    def restore_grant(self, tenant: str, principal: str, pool_id: str, ops: Iterable[str], counter: int) -> None:
        with self._lock:
            self.observe_counter(tenant, counter)
            self._grants[(tenant, principal, pool_id)] = (frozenset(ops), int(counter))

    def grant_state(self, tenant: str, principal: str, pool_id: str) -> tuple[frozenset, int, int]:
        """In one step: the grant's operations and counter (none: empty, 0), and the tenant's counter now. Every change of the grant made
        at a counter up to the last value is in the first two."""
        with self._lock:
            ops, c = self._grants.get((tenant, principal, pool_id), (frozenset(), 0))
            return ops, c, self._counters.get(tenant, 0)

    def live_ops(self, tenant: str, principal: str, pool_id: str) -> frozenset:
        with self._lock:
            return self._grants.get((tenant, principal, pool_id), (frozenset(), 0))[0]

    def grants_of(self, tenant: str, principal: str) -> dict[str, frozenset]:
        with self._lock:
            return {k[2]: v[0] for k, v in self._grants.items() if k[0] == tenant and k[1] == principal}

    def grant_rows(self, tenant: str) -> list[tuple[str, str, frozenset, int]]:
        with self._lock:
            return sorted((k[1], k[2], v[0], v[1]) for k, v in self._grants.items() if k[0] == tenant)


# ---- effective access (2.4 check 4) --------------------------------------------------------------------------------------

_WRITE_OPS = {SAVE, UPDATE}


def pool_allows(directory: PoolDirectory, lattice: ClassLattice, p: Principal, pool_id: str, op: str,
                scope: Optional[Mapping[str, frozenset]] = None, *, check_state: bool = True) -> Optional[Pool]:
    """The pool when every condition of 2.4 check 4 holds for `op`, else None. `scope` is a token's scope (pool id to operations) when
    the call comes through a scoped token; None means no token scope is applied (a harness-side check). Every refusal is the same
    None: the caller cannot tell a missing pool from a forbidden one."""
    if op not in OPERATIONS:
        return None
    pool = directory.get(p.tenant, pool_id)
    if pool is None or (check_state and pool.state != ACTIVE):
        return None
    if scope is not None and op not in scope.get(pool_id, frozenset()):
        return None
    if op not in directory.live_ops(p.tenant, p.token, pool_id):
        return None
    class_op = Op.REMEMBER if op in _WRITE_OPS else Op.RECALL
    if not decide(p, class_op, tenant=p.tenant, lattice=lattice, cls=pool.cls):
        return None
    return pool
