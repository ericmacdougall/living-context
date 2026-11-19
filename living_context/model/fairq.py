"""Per-tenant fair queue in front of one global concurrency limit (DESIGN 15, 18.1; deficit round robin with
equal costs, i.e. round robin over tenants that have a waiting call). One tenant's flood waits behind its own queue."""
from __future__ import annotations

import collections
import itertools
import threading
from typing import Callable, Deque, Dict


class FairLimiter:
    def __init__(self, limit: int = 2):
        self._limit, self._running = limit, 0
        self._cv = threading.Condition()
        self._queues: Dict[str, Deque[int]] = {}
        self._ring: Deque[str] = collections.deque()
        self._ids = itertools.count()

    def run(self, tenant: str, fn: Callable[[], object]):
        me = next(self._ids)
        with self._cv:
            q = self._queues.setdefault(tenant, collections.deque())
            if tenant not in self._ring:
                self._ring.append(tenant)
            q.append(me)
            while True:
                if self._running < self._limit and self._granted(tenant, me):
                    q.popleft()
                    self._running += 1
                    break
                self._cv.wait()
        try:
            return fn()
        finally:
            with self._cv:
                self._running -= 1
                self._cv.notify_all()

    def _granted(self, tenant: str, me: int) -> bool:
        """Grant to the first waiting ticket of the tenant whose turn it is; rotate the turn after each grant."""
        for t in list(self._ring):
            if self._queues[t]:
                if t == tenant and self._queues[t][0] == me:
                    self._ring.remove(t)
                    self._ring.append(t)  # move to the back: the next grant goes to another tenant first
                    return True
                return False
        return False


class FifoLimiter:
    """Broken variant: one FIFO queue for all tenants."""

    def __init__(self, limit: int = 2):
        self._limit, self._running = limit, 0
        self._cv = threading.Condition()
        self._queue: Deque[int] = collections.deque()
        self._ids = itertools.count()

    def run(self, tenant: str, fn: Callable[[], object]):
        me = next(self._ids)
        with self._cv:
            self._queue.append(me)
            while not (self._running < self._limit and self._queue[0] == me):
                self._cv.wait()
            self._queue.popleft()
            self._running += 1
        try:
            return fn()
        finally:
            with self._cv:
                self._running -= 1
                self._cv.notify_all()
