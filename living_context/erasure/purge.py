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
from ..gateway.reads import IncompleteRead, acknowledged, broadcast, deleted_docs, setting_value

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
    """The deleted (not yet merged away) documents of `index` (`reads.deleted_docs`: every shard answered, exactly this index, an
    integer count): the purge is `ok` only when this reads 0, so a `(200, {})` or a foreign answer must never read as 0 (review round 6)."""
    try:
        return deleted_docs(*es.request("GET", f"/{index}/_stats/docs"), index, True)
    except IncompleteRead as e:
        raise PurgeError("stats not confirmed") from e


EXPUNGE = "index.merge.policy.expunge_deletes_allowed"


def ensure_expunge_threshold(es: EsClient, index: str) -> None:
    """The threshold must be 0, or a segment holding under the default 10 percent deleted documents is never merged and the
    count stays above zero for ever. The erase key cannot change settings (no `manage`); provisioning sets the value at
    creation. A change that is not acknowledged (`reads.acknowledged`) is therefore accepted only when the index is read back
    carrying 0 (`reads.setting_value`); anything else is an error, never a silent skip (the first build ignored the status and left
    one source index non-deterministically stuck)."""
    try:
        acknowledged(*es.request("PUT", f"/{index}/_settings", {EXPUNGE: "0"}))
        return
    except IncompleteRead:
        pass   # refused (the erase key has no `manage`) or not acknowledged: the readback decides
    try:
        got = setting_value(*es.request("GET", f"/{index}/_settings/{EXPUNGE}?flat_settings=true"), index, EXPUNGE, True)
    except IncompleteRead as e:
        raise PurgeError("the expunge threshold could not be read back") from e
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
        try:   # a force merge that every primary ran (`reads.broadcast`)
            broadcast(*es.request("POST", f"/{index}/_forcemerge?only_expunge_deletes=true"))
        except IncompleteRead as e:
            raise PurgeError("expunge failed") from e
        passes += 1
        left = deleted_count(es, index)
        if left == 0 or single_pass:
            return PurgeResult(left == 0, left, clock() - t0, passes)
        if clock() - t0 >= deadline_s:
            return PurgeResult(False, left, clock() - t0, passes)
        sleep(interval_s)
