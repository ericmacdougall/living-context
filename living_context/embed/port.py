"""The embedding port: every embedding call passes the per-tenant fair queue in front of one global concurrency
limit (DESIGN 15, 18.1; BUILD-PLAN Step 8), so one tenant's flood of writes waits behind its own queue.
The backend is the hashed stub until a real encoder is approved (EXPERIMENT 10)."""
from __future__ import annotations

import threading
from typing import Callable, Optional

from ..model.fairq import FairLimiter
from . import stub


class EmbedPort:
    def __init__(self, limiter: Optional[FairLimiter] = None, backend: Callable[[str], list] = stub.embed):
        self._lim, self._backend = limiter or FairLimiter(2), backend
        self.calls, self._lock = 0, threading.Lock()

    def embed(self, tenant: str, text: str) -> list:
        with self._lock:
            self.calls += 1
        return self._lim.run(tenant, lambda: self._backend(text))

    def for_tenant(self, tenant: str) -> Callable[[str], list]:
        """The one-argument function the writer takes."""
        return lambda text: self.embed(tenant, text)
