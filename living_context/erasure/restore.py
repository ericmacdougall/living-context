"""Restore replay (DESIGN 14.6; BUILD-PLAN Step 14): after any restore of Elasticsearch or of the keystore, with the tenant
disabled, every journal tombstone is re-recorded and made effective, the keys it names are destroyed, its closure is replayed over
every restored index (authz and control included) and the independent verifier's residue checks run; the tenant is enabled only
when they report zero. The journal is the newest copy and is never the snapshot's. No model call."""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Callable

from ..verifier.check import Report
from .executor import Executor


@dataclass
class RestoreResult:
    enabled: bool = False
    residue: int = -1
    replayed: int = 0
    seconds: float = 0.0
    steps: list = field(default_factory=list)   # the verifier's step list, as the receipt states it
    reason: str = ""


def restore_replay(executor: Executor, journal, verify: Callable[[], Report], *, replay: bool = True) -> RestoreResult:
    """`verify` runs the verifier's residue checks against the restored indices; `replay=False` is the BROKEN variant of a restore
    that serves without re-applying the journal."""
    t0, out = time.monotonic(), RestoreResult()
    if replay:
        for rec in journal.records():
            scope = rec["scope"]
            if scope.get("cls"):   # a relabel: the old-class copy only (DESIGN 14.1)
                executor.replay_relabel(scope.get("leaf_ids", []), scope["cls"], int(rec["seq"]), scope.get("lv_max", {}))
            else:
                executor.replay(scope["subject"], scope.get("leaf_ids", []), int(rec["seq"]))
            out.replayed += 1
    rep = verify()
    out.residue, out.steps, out.seconds = rep.total, list(rep.steps), time.monotonic() - t0
    out.enabled = rep.total == 0 and rep.probes > 0
    if not out.enabled:
        out.reason = f"residue {rep.total} after the replay of {out.replayed} tombstone(s)"
    return out
