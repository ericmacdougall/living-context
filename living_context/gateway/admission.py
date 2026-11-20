"""Admission control (DESIGN 7.1, 15; LAB-R1, LAB-R5; BUILD-PLAN Step 5).

Pure, thread-safe primitives; the socket server composes them:
  * bytes are bounded BEFORE any parsing (`read_line`), and the number of
    structures is counted on the raw bytes before the parser runs;
  * one semaphore around parse and dispatch;
  * bounded per-tenant slots, queue and open-connection cap, so one tenant's
    flood waits behind its own limits and cannot take another tenant's slots;
  * a separate lane for revoke, one per tenant (its own semaphore); the server grants it only after the session resolves and
    the principal's role allows the operation;
  * lock, cache, rate-limit and idempotency keys embed the tenant in a
    collision-free form (length-prefixed, never a short hash; INV-10, T6).
"""
from __future__ import annotations

import json
import struct
import threading
import time
from typing import Callable, IO

from ..config import Constants


class Refused(Exception):
    """A typed admission refusal; `code` is for the log, the caller sees one shape."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def slot_key(tenant: str, principal: str, key: str) -> bytes:
    """Collision-free key for locks, caches, rate limits and idempotency (INV-10)."""
    out = b""
    for part in (tenant, principal, key):
        raw = part.encode("utf-8")
        out += struct.pack(">I", len(raw)) + raw
    return out


def read_line(stream: IO[bytes], max_line: int) -> bytes:
    """Read one newline-terminated request of at most `max_line` bytes. Nothing is parsed;
    a longer line is refused after reading at most `max_line + 1` bytes."""
    data = stream.readline(max_line + 1)
    if len(data) > max_line:
        raise Refused("too_large")
    return data


def count_structures(raw: bytes, limit: int) -> None:
    """Count `{` and `[` bytes before parsing and refuse past `limit` (over-counts brackets inside
    strings, which only makes the bound stricter). Also bounds nesting depth."""
    n = raw.count(b"{") + raw.count(b"[")
    if n > limit:
        raise Refused("too_many_structures")


def parse(raw: bytes, *, max_structures: int, parse_fn: Callable[[bytes], object] = json.loads):
    count_structures(raw, max_structures)
    try:
        return parse_fn(raw)
    except (ValueError, RecursionError):
        raise Refused("malformed") from None


class Admission:
    """Per-tenant slots, queue and connection caps plus the global parse semaphore."""

    def __init__(self, consts: Constants | None = None, *, slots: int = 4, queue: int = 8,
                 connections: int = 8, revoke_slots: int = 2, parse_slots: int = 1,
                 broken_shared_revoke_lane: bool = False):
        self.consts = consts or Constants()
        self._slots_n, self._queue_n, self._conn_n = slots, queue, connections
        self._lock = threading.Lock()
        self._slots: dict[bytes, threading.Semaphore] = {}
        self._waiting: dict[bytes, int] = {}
        self._conns: dict[bytes, int] = {}
        # its own lane (LAB-R5), one per tenant (DESIGN 15): one tenant's authorised revoke calls never wait for another's.
        # BROKEN variant: one semaphore shared by every tenant
        self._revoke_n, self._revoke_shared = revoke_slots, (threading.Semaphore(revoke_slots) if broken_shared_revoke_lane else None)
        self._revoke: dict[bytes, threading.Semaphore] = {}
        self.parse_semaphore = threading.Semaphore(parse_slots)

    @staticmethod
    def _t(tenant: str) -> bytes:
        return slot_key(tenant, "", "")

    # --- connections ----------------------------------------------------------
    def open_connection(self, tenant: str) -> None:
        k = self._t(tenant)
        with self._lock:
            if self._conns.get(k, 0) >= self._conn_n:
                raise Refused("connection_cap")
            self._conns[k] = self._conns.get(k, 0) + 1

    def close_connection(self, tenant: str) -> None:
        k = self._t(tenant)
        with self._lock:
            self._conns[k] = max(0, self._conns.get(k, 0) - 1)

    # --- requests ---------------------------------------------------------------
    def acquire(self, tenant: str, *, timeout: float = 5.0, revoke: bool = False) -> Callable[[], None]:
        """Take a slot of the tenant's own pool (or the revoke lane). Returns the release function.
        Refuses at once when the tenant's queue is full, and after `timeout` when no slot frees."""
        k = self._t(tenant)
        if revoke:
            with self._lock:
                lane = self._revoke_shared or self._revoke.setdefault(k, threading.Semaphore(self._revoke_n))
            if not lane.acquire(timeout=timeout):
                raise Refused("revoke_lane_busy")
            return lane.release
        with self._lock:
            sem = self._slots.setdefault(k, threading.Semaphore(self._slots_n))
            if sem.acquire(blocking=False):
                return sem.release
            if self._waiting.get(k, 0) >= self._queue_n:
                raise Refused("queue_full")
            self._waiting[k] = self._waiting.get(k, 0) + 1
        try:
            if not sem.acquire(timeout=timeout):
                raise Refused("slot_timeout")
        finally:
            with self._lock:
                self._waiting[k] -= 1
        return sem.release

    def handle(self, tenant: str, stream: IO[bytes], dispatch: Callable[[object], object]):
        """Bytes bounded first; then slot; then one parse-and-dispatch under the semaphore."""
        raw = read_line(stream, self.consts.max_line_bytes)
        release = self.acquire(tenant)
        try:
            with self.parse_semaphore:
                return dispatch(parse(raw, max_structures=1000))
        finally:
            release()
