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
from typing import Callable, Optional, Protocol

from ..policy import Principal, Role
from .fence import SendFence


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

    def register_buffer(self, tenant: str, principal: str, buf: list) -> None:
        with self._lock:
            self._unsent.setdefault((tenant, principal), []).append(buf)

    def bump(self, tenant: str, principal: str, *, revoke: bool = True) -> Revoked:
        """Flip the epoch (and, for a revoke, mark revoked) and discard unsent buffers: one
        in-memory step under the exclusive side of the tenant's fence, no I/O inside it."""
        fence = self._fence_for(tenant)
        with fence.exclusive():
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
                 grants: Callable[[str, str], Optional[Principal]]):
        """`grants(tenant, principal_token)` returns the stored grants (roles, sets) or None."""
        self._adapters = adapters
        self._epochs = epochs
        self._principal_token = principal_token
        self._grants = grants
        self._by_hash: dict[str, Record] = {}
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
        if not self._epochs.current(rec.tenant, rec.principal_token, rec.epoch):
            return None
        return rec

    def principal(self, rec: Record) -> Principal:
        return Principal(rec.tenant, rec.principal_token, rec.roles, rec.read_set, rec.write_set, rec.own_class)
