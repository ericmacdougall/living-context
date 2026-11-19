"""The per-tenant write gate (DESIGN 14.3; BUILD-PLAN Step 14): commits (writes, batch publications) take the shared
side, the erasure executor takes the exclusive side from the closure to the boundary. Writer-preferring, in-memory;
no I/O the gate does not itself wait for is done under the fence, which is the innermost lock (taken under the gate,
never the reverse)."""
from __future__ import annotations

import contextlib

from .fence import SendFence


class WriteGate(SendFence):
    pass


class NoGate:
    @contextlib.contextmanager
    def shared(self):
        yield
