"""Sessions and the authority-epoch table (DESIGN 4.4, 5.10, 7.1; BUILD-PLAN Step 5).

* A session is minted only by an identity adapter from an authentication proof,
  never from a bare principal id (LAB-R10). The token is `<tenant>.<256 random
  bits>`; only its sha256 is stored. A token whose tenant prefix does not match
  the record it resolves to is refused.
* The epoch table is the gateway's own, in memory. `revoke` flips the epoch
  and discards the principal's unsent buffers under the EXCLUSIVE side of the
  tenant's send fence, in memory only, and returns `denied`; durability
  (journal append and fsync) is reported separately by the caller (Step 15).
* Everything the request may do (tenant, principal, roles, read and write
  sets) comes from the session record, never from the request body.
"""
from __future__ import annotations

import hashlib
import hmac
import os
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Mapping, Optional, Protocol

from ..config import Constants
from ..policy import Principal, Role
from ..policy import pools as pl
from .fence import SendFence

INF = float("inf")


class TokenError(Exception):
    """A typed refusal of a token operation. `code` is one of the POOLS 2.6 and 2.7 answers: `unavailable`, `expired`,
    `scope_widened`, `rate_limited`. A pool-related refusal is always `unavailable` and never says which pool."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


class IdentityAdapter(Protocol):
    def verify(self, tenant: str, proof: bytes) -> Optional[str]:
        """Return the verified principal identifier for this tenant, or None."""


@dataclass
class Record:
    tenant: str
    principal_token: str
    roles: frozenset
    read_set: frozenset
    write_set: frozenset
    own_class: Optional[str]
    epoch: int
    # scoped-token fields (POOLS 2.3); a plain session has `scope = None` and so reaches no pool (least privilege)
    actor: str = "agent"
    root: str = ""                 # hash of the root token of the delegation tree
    parent: str = ""               # hash of the parent token ("" for a root)
    conversation: str = ""
    scope: Optional[Mapping[str, frozenset]] = None
    default_save_pool: Optional[str] = None
    exp: float = INF
    created: float = 0.0
    max_age: float = INF
    hash: str = ""


@dataclass
class Revoked:
    denied_at: float


class EpochTable:
    def __init__(self, fence_for: Callable[[str], SendFence]):
        self._fence_for = fence_for
        self._lock = threading.Lock()
        self._epochs: dict[tuple[str, str], int] = {}
        self._revoked: set[tuple[str, str]] = set()
        self._unsent: dict[tuple[str, str], list] = {}

    def epoch(self, tenant: str, principal: str) -> int:
        with self._lock:
            return self._epochs.get((tenant, principal), 0)

    def is_revoked(self, tenant: str, principal: str) -> bool:
        with self._lock:
            return (tenant, principal) in self._revoked

    def current(self, tenant: str, principal: str, epoch: int) -> bool:
        """The check made at admission, at each wave and under the fence at every hand-off."""
        with self._lock:
            k = (tenant, principal)
            return k not in self._revoked and self._epochs.get(k, 0) == epoch

    def snapshot(self, tenant: str) -> list[tuple[str, int, bool]]:
        with self._lock:
            return sorted((p, e, (t, p) in self._revoked) for (t, p), e in self._epochs.items() if t == tenant)

    def restore(self, tenant: str, principal: str, epoch: int, revoked: bool) -> None:
        """Rebuild one row from the journal at start (no fence: nothing is admitted yet)."""
        with self._lock:
            k = (tenant, principal)
            self._epochs[k] = max(self._epochs.get(k, 0), epoch)
            if revoked:
                self._revoked.add(k)

    def under_fence(self, tenant: str, fn: Callable[[], object]):
        """Run `fn` (one in-memory step, no I/O) under the EXCLUSIVE side of the tenant's send fence."""
        with self._fence_for(tenant).exclusive():
            return fn()

    def register_buffer(self, tenant: str, principal: str, buf: list) -> None:
        with self._lock:
            self._unsent.setdefault((tenant, principal), []).append(buf)

    def bump(self, tenant: str, principal: str, *, revoke: bool = True, in_fence: bool = False) -> Revoked:
        """Flip the epoch (and, for a revoke, mark revoked) and discard unsent buffers: one
        in-memory step under the exclusive side of the tenant's fence, no I/O inside it. `in_fence`: the caller already holds it."""
        import contextlib
        fence = self._fence_for(tenant)
        with (contextlib.nullcontext() if in_fence else fence.exclusive()):
            with self._lock:
                k = (tenant, principal)
                self._epochs[k] = self._epochs.get(k, 0) + 1
                if revoke:
                    self._revoked.add(k)
                for buf in self._unsent.pop(k, []):
                    buf.clear()
        return Revoked(time.time())


class Sessions:
    def __init__(self, adapters: dict[str, IdentityAdapter], epochs: EpochTable,
                 principal_token: Callable[[str, str], str],
                 grants: Callable[[str, str], Optional[Principal]], *, broken_epoch_only_at_mint: bool = False,
                 consts: Optional[Constants] = None, clock: Callable[[], float] = time.time,
                 token_persist: Optional[Callable[[str, dict], None]] = None, directory: Optional[pl.PoolDirectory] = None,
                 broken_scope_not_attenuated: bool = False, broken_no_live_cap: bool = False,
                 broken_delegate_depth: bool = False, broken_revoke_by_epoch: bool = False,
                 broken_renew_keeps_removed: bool = False, broken_ttl_not_capped: bool = False,
                 broken_no_expiry: bool = False):
        """`grants(tenant, principal_token)` returns the stored grants (roles, sets) or None."""
        self._c, self._clock, self._token_persist, self._dir = consts or Constants(), clock, token_persist, directory
        self._revoked_tokens: dict[str, float] = {}    # token hash -> the revoked token's `exp` (POOLS 2.5)
        self._mints: dict[tuple[str, str], list[float]] = {}
        self._bt = dict(attenuate=broken_scope_not_attenuated, live=broken_no_live_cap, depth=broken_delegate_depth,
                        by_epoch=broken_revoke_by_epoch, renew_keeps=broken_renew_keeps_removed, ttl=broken_ttl_not_capped,
                        no_exp=broken_no_expiry)
        self._adapters = adapters
        self._epochs = epochs
        self._principal_token = principal_token
        self._grants = grants
        self._by_hash: dict[str, Record] = {}
        self._mint_only = broken_epoch_only_at_mint   # BROKEN variant: the epoch is checked when the session is minted, never again
        self._lock = threading.Lock()

    @staticmethod
    def _hash(token: str) -> str:
        return hashlib.sha256(token.encode()).hexdigest()

    def mint(self, tenant: str, proof: bytes) -> Optional[str]:
        adapter = self._adapters.get(tenant)
        who = adapter.verify(tenant, proof) if adapter else None
        if who is None:
            return None
        ptoken = self._principal_token(tenant, who)
        g = self._grants(tenant, ptoken)
        if g is None or self._epochs.is_revoked(tenant, ptoken):
            return None
        token = f"{tenant}.{os.urandom(32).hex()}"
        rec = Record(tenant, ptoken, g.roles, g.read_set, g.write_set, g.own_class,
                     self._epochs.epoch(tenant, ptoken))
        with self._lock:
            self._by_hash[self._hash(token)] = rec
        return token

    def resolve(self, token: str) -> Optional[Record]:
        """The record for a live session, or None (unknown, forged prefix, revoked, stale epoch)."""
        if not isinstance(token, str) or "." not in token:
            return None
        prefix, _, rest = token.partition(".")
        if len(rest) != 64:
            return None
        with self._lock:
            rec = self._by_hash.get(self._hash(token))
        if rec is None or rec.tenant != prefix:
            return None
        if not hmac.compare_digest(self._hash(token), self._hash(token)):  # constant-shape path
            return None
        if not self._mint_only and not self._epochs.current(rec.tenant, rec.principal_token, rec.epoch):
            return None
        if rec.scope is not None or rec.exp != INF:
            now = self._clock()
            for h in (rec.hash, rec.parent, rec.root):   # check 1 for the token, check 3 for a delegated one (2.4)
                if not h:
                    continue
                with self._lock:
                    r = self._by_hash.get(h)
                    gone = h in self._revoked_tokens
                if gone or r is None or (r.exp <= now and not self._bt["no_exp"]):
                    return None
        return rec

    def principal(self, rec: Record) -> Principal:
        return Principal(rec.tenant, rec.principal_token, rec.roles, rec.read_set, rec.write_set, rec.own_class)

    # ---- scoped tokens (POOLS 2.3, 2.5, 2.6) -----------------------------------------------------------------------------
    @staticmethod
    def token_id(rec: Record) -> str:
        """A non-secret handle of a token (the first 16 hex characters of its stored hash), for revocation by an administrator."""
        return rec.hash[:16]

    def _live_count(self, tenant: str, principal: str, now: float) -> int:
        with self._lock:
            return sum(1 for r in self._by_hash.values() if r.tenant == tenant and r.principal_token == principal and r.exp > now)

    def _rate_ok(self, tenant: str, principal: str, now: float) -> bool:
        with self._lock:
            w = [t for t in self._mints.get((tenant, principal), []) if t > now - 3600]
            if len(w) >= self._c.mint_rate_per_h:
                self._mints[(tenant, principal)] = w
                return False
            w.append(now)
            self._mints[(tenant, principal)] = w
            return True

    def _persist(self, rec: Record) -> None:
        if self._token_persist:
            try:
                self._token_persist(rec.hash, {"tenant": rec.tenant, "principal": rec.principal_token, "actor": rec.actor,
                                               "root": rec.root, "parent": rec.parent, "conversation": rec.conversation,
                                               "scope": {k: sorted(v) for k, v in (rec.scope or {}).items()},
                                               "default_save_pool": rec.default_save_pool or "", "exp": rec.exp,
                                               "created": rec.created, "max_age": rec.max_age, "epoch": rec.epoch,
                                               "roles": sorted(r.value for r in rec.roles), "read": sorted(rec.read_set),
                                               "write": sorted(rec.write_set), "own_class": rec.own_class or ""})
            except Exception:
                pass   # best effort: the journal, not this copy, is what a revoke relies on

    def _live_scope(self, tenant: str, principal: str, requested: Mapping[str, frozenset]) -> Optional[dict]:
        """`requested` intersected with the live grant table; None when any named pool is not held (the whole mint is refused with
        the one `unavailable`)."""
        out = {}
        for pid, ops in requested.items():
            held = self._dir.live_ops(tenant, principal, pid) if self._dir is not None else frozenset()
            pool = self._dir.get(tenant, pid) if self._dir is not None else None
            if pool is None or pool.state != pl.ACTIVE or not ops or not set(ops) <= pl.OPERATIONS or not set(ops) <= held:
                return None
            out[pid] = frozenset(ops)
        return out

    def mint_scoped(self, tenant: str, proof: bytes, *, conversation: str, scope: Mapping[str, frozenset],
                    default_save_pool: Optional[str] = None, ttl_s: Optional[float] = None, human: bool = False) -> str:
        """A root scoped token for a harness, from an authentication proof (never a bare principal id). `scope` is pool id to
        operations and is never wider than the principal's live grants. `human` is a harness-side argument, never read from a
        request, and is honoured only for an identity adapter that declares the capability (POOLS 2.3)."""
        adapter = self._adapters.get(tenant)
        who = adapter.verify(tenant, proof) if adapter else None
        if who is None or not isinstance(conversation, str) or not conversation or len(conversation) > 128:
            raise TokenError("unavailable")
        ptoken = self._principal_token(tenant, who)
        g = self._grants(tenant, ptoken)
        if g is None or self._epochs.is_revoked(tenant, ptoken):
            raise TokenError("unavailable")
        live = self._live_scope(tenant, ptoken, {k: frozenset(v) for k, v in scope.items()})
        if live is None:
            raise TokenError("unavailable")   # a pool not held, not active or not existing: one answer (2.7)
        if default_save_pool is not None and pl.SAVE not in live.get(default_save_pool, frozenset()):
            raise TokenError("unavailable")
        now = self._clock()
        if not self._bt["live"] and self._live_count(tenant, ptoken, now) >= self._c.tok_live_max:
            raise TokenError("rate_limited")
        if not self._rate_ok(tenant, ptoken, now):
            raise TokenError("rate_limited")
        ttl = min(float(ttl_s if ttl_s is not None else self._c.token_ttl_s), self._c.token_ttl_s)
        token = f"{tenant}.{os.urandom(32).hex()}"
        h = self._hash(token)
        rec = Record(tenant, ptoken, g.roles, g.read_set, g.write_set, g.own_class, self._epochs.epoch(tenant, ptoken),
                     actor="human" if human and getattr(adapter, "human_presence", False) else "agent", root=h, parent="",
                     conversation=conversation, scope=live, default_save_pool=default_save_pool, exp=now + ttl, created=now,
                     max_age=now + self._c.token_max_age_s, hash=h)
        with self._lock:
            self._by_hash[h] = rec
        self._persist(rec)
        return token

    def delegate(self, parent_token: str, scope: Mapping[str, frozenset], ttl_s: float) -> str:
        """Attenuation only (2.6): a subset of the parent's scope, per pool a subset of its operations, a lifetime no later than the
        parent's, actor `agent`, depth 1. A harness operation, never a model-facing tool; the child token is returned to the harness
        process only."""
        parent = self.resolve(parent_token)
        if parent is None or parent.scope is None:
            raise TokenError("unavailable")
        if parent.parent and not self._bt["depth"]:
            raise TokenError("scope_widened")   # a child cannot delegate
        now = self._clock()
        child_scope: dict = {}
        for pid, ops in scope.items():
            ops = frozenset(ops)
            if not self._bt["attenuate"] and not ops <= parent.scope.get(pid, frozenset()):
                raise TokenError("scope_widened")
            child_scope[pid] = ops
        if not self._bt["ttl"] and now + float(ttl_s) > parent.exp:
            raise TokenError("scope_widened")
        if not self._bt["live"] and self._live_count(parent.tenant, parent.principal_token, now) >= self._c.tok_live_max:
            raise TokenError("rate_limited")
        if not self._rate_ok(parent.tenant, parent.principal_token, now):
            raise TokenError("rate_limited")
        token = f"{parent.tenant}.{os.urandom(32).hex()}"
        h = self._hash(token)
        rec = Record(parent.tenant, parent.principal_token, parent.roles, parent.read_set, parent.write_set, parent.own_class,
                     parent.epoch, actor="agent", root=parent.root or parent.hash, parent=parent.hash,
                     conversation=parent.conversation, scope=child_scope,
                     default_save_pool=parent.default_save_pool if parent.default_save_pool in child_scope else None,
                     exp=now + float(ttl_s), created=now, max_age=parent.max_age, hash=h)
        with self._lock:
            self._by_hash[h] = rec
        self._persist(rec)
        return token

    def renew(self, token: str) -> float:
        """Extend `exp` up to `max_age` in place (the string does not change) if the epoch is unchanged and the token is live, and
        re-intersect the scope with the live grant table: a pool whose grant was removed drops out and the rest continues. An empty
        result is refused with `expired`. Never widens."""
        rec = self.resolve(token)
        if rec is None or rec.scope is None or rec.parent:
            raise TokenError("expired")
        now = self._clock()
        keep = {}
        for pid, ops in rec.scope.items():
            if self._bt["renew_keeps"]:
                keep[pid] = ops
                continue
            pool = self._dir.get(rec.tenant, pid) if self._dir else None
            held = self._dir.live_ops(rec.tenant, rec.principal_token, pid) if self._dir else frozenset()
            ok = ops & held
            if pool is not None and pool.state == pl.ACTIVE and ok:
                keep[pid] = frozenset(ok)
        if not keep:
            raise TokenError("expired")
        with self._lock:
            rec.scope = keep
            if rec.default_save_pool not in keep or pl.SAVE not in keep.get(rec.default_save_pool, frozenset()):
                rec.default_save_pool = None
            rec.exp = min(now + self._c.token_ttl_s, rec.max_age)
        self._persist(rec)
        return rec.exp

    def find_by_id(self, token_id: str) -> Optional[str]:
        """The hash of the one token whose id (the first 16 hex characters of its hash) is `token_id`, or None."""
        if not isinstance(token_id, str) or len(token_id) < 16:
            return None
        with self._lock:
            hit = [h for h in self._by_hash if h.startswith(token_id)]
        return hit[0] if len(hit) == 1 else None

    def revoke_hash(self, h: str) -> Optional[tuple[str, float]]:
        """Mark one token revoked (in memory, to run under the send fence; the caller journals it and returns `denied`). Children die
        with it by check 3. A revoked token cannot renew. Returns (hash, exp), or None when there is no such token."""
        with self._lock:
            rec = self._by_hash.get(h)
            if rec is None:
                return None
            if self._bt["by_epoch"]:   # BROKEN variant: no per-token record, the principal's epoch moves instead
                self._epochs.bump(rec.tenant, rec.principal_token, revoke=False, in_fence=True)
            else:
                self._revoked_tokens[rec.hash] = rec.exp
            return rec.hash, rec.exp

    def restore_token_revoke(self, h: str, exp: float) -> None:
        with self._lock:
            self._revoked_tokens[h] = float(exp)

    def revoked_snapshot(self, now: Optional[float] = None) -> list[tuple[str, float]]:
        """Token-revoke records still needed: a record is kept until its `exp` passes (2.5)."""
        now = self._clock() if now is None else now
        with self._lock:
            return sorted((h, e) for h, e in self._revoked_tokens.items() if e > now)

    def restore_token(self, h: str, d: Mapping) -> None:
        """A token record read back from the authz copy at start-up (memory only)."""
        with self._lock:
            self._by_hash[h] = Record(d["tenant"], d["principal"], frozenset(Role(x) for x in d.get("roles", [])),
                                      frozenset(d.get("read", [])), frozenset(d.get("write", [])), d.get("own_class") or None,
                                      int(d["epoch"]), actor=d.get("actor", "agent"), root=d.get("root", ""), parent=d.get("parent", ""),
                                      conversation=d.get("conversation", ""),
                                      scope={k: frozenset(v) for k, v in d.get("scope", {}).items()},
                                      default_save_pool=d.get("default_save_pool") or None, exp=float(d["exp"]),
                                      created=float(d.get("created", 0)), max_age=float(d.get("max_age", INF)), hash=h)

    def live_token_count(self, tenant: str, principal: str) -> int:
        return self._live_count(tenant, principal, self._clock())
