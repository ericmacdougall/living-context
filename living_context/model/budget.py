"""Persistent model-call counter (DESIGN 18.1; BUILD-PLAN Step 8).

`charge()` increments the counter BEFORE the call is made and durably (temp file, fsync, atomic rename), so a
failed or crashed call still counts, and refuses once the cap is reached (a refused attempt is not counted).
"""
from __future__ import annotations

import os
import threading
from pathlib import Path


class BudgetExceeded(Exception):
    pass


class Budget:
    def __init__(self, path: str | os.PathLike, cap: int):
        self.path, self.cap, self._lock = Path(path), cap, threading.Lock()
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def count(self) -> int:
        try:
            return int(self.path.read_text().strip() or 0)
        except FileNotFoundError:
            return 0

    def _store(self, n: int) -> None:
        tmp = self.path.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(str(n))
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, self.path)

    def charge(self) -> int:
        with self._lock:
            n = self.count()
            if n >= self.cap:
                raise BudgetExceeded("model-call budget reached")
            self._store(n + 1)
            return n + 1
