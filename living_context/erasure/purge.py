"""Physical purge on a live index (DESIGN 14.5; BUILD-PLAN Step 14).

Delete, refresh, flush, then `_forcemerge?only_expunge_deletes=true` (with `index.merge.policy.expunge_deletes_allowed: 0`)
REPEATED every `interval` seconds until `_stats` reports `docs.deleted == 0` or the deadline passes. Probe P10 measured
that Elasticsearch drops soft-delete tombstones only after the retention lease advances (30.4 s on one node): one pass
right after the delete does nothing, so a single pass is the broken variant. The result carries the measured time to
zero, reported in the receipt; it is the physical-purge promise, separate from the in-memory boundary B_ERASE. A
deadline that passes with a non-zero count is reported (`ok` false) and the caller falls back to copy-and-swap."""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Callable

from ..gateway.es import EsClient

DEFAULT_DEADLINE_S = 120.0   # ASSUMPTION: four times the measured 30.4 s
DEFAULT_INTERVAL_S = 5.0


class PurgeError(Exception):
    pass


@dataclass(frozen=True)
class PurgeResult:
    ok: bool
    deleted_left: int
    seconds: float
    passes: int


def deleted_count(es: EsClient, index: str) -> int:
    st, r = es.request("GET", f"/{index}/_stats/docs")
    if st != 200:
        raise PurgeError(f"stats failed ({st})")
    return int(r["_all"]["primaries"]["docs"]["deleted"])


def ensure_expunge_threshold(es: EsClient, index: str) -> None:
    """The threshold must be 0, or a segment holding under the default 10 percent deleted documents is never merged and the
    count stays above zero for ever. The erase key cannot change settings (no `manage`); provisioning sets the value at
    creation. A refused change is therefore accepted only when the index already carries 0; anything else is an error,
    never a silent skip (the first build ignored the status and left one source index non-deterministically stuck)."""
    st, _ = es.request("PUT", f"/{index}/_settings", {"index.merge.policy.expunge_deletes_allowed": "0"})
    if st == 200:
        return
    st, r = es.request("GET", f"/{index}/_settings/index.merge.policy.expunge_deletes_allowed?flat_settings=true")
    got = next(iter(r.values()), {}).get("settings", {}).get("index.merge.policy.expunge_deletes_allowed") if st == 200 and isinstance(r, dict) else None
    if got not in ("0", 0):
        raise PurgeError("the index does not carry expunge_deletes_allowed 0 and the key may not set it")


def purge_index(es: EsClient, index: str, *, deadline_s: float = DEFAULT_DEADLINE_S, interval_s: float = DEFAULT_INTERVAL_S,
                single_pass: bool = False, clock: Callable[[], float] = time.monotonic,
                sleep: Callable[[float], None] = time.sleep) -> PurgeResult:
    """`single_pass` is the BROKEN variant used by tests: one expunge pass and no wait."""
    ensure_expunge_threshold(es, index)
    es.request("POST", f"/{index}/_refresh")
    es.request("POST", f"/{index}/_flush")
    t0, passes = clock(), 0
    while True:
        st, _ = es.request("POST", f"/{index}/_forcemerge?only_expunge_deletes=true")
        if st not in (200, 201):
            raise PurgeError(f"expunge failed ({st})")
        passes += 1
        left = deleted_count(es, index)
        if left == 0 or single_pass:
            return PurgeResult(left == 0, left, clock() - t0, passes)
        if clock() - t0 >= deadline_s:
            return PurgeResult(False, left, clock() - t0, passes)
        sleep(interval_s)
