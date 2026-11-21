"""Revocation on the tenant's bounded epoch journal (DESIGN 4.4; BUILD-PLAN Step 15, core).

Order of a revoke: (1) the in-memory epoch flip under the send fence (no I/O inside it), which is the denial and is
returned first; (2) append and fsync to the tenant's journal, `durable_at` reported separately; (3) best-effort persistence
to the tenant's authz index. A blocked or failing Elasticsearch never delays or prevents (1) (probe P14).

Fail closed: a journal fault, an fsync over `D_FSYNC`, a torn or unterminated journal at start close the tenant (admission
refused) and, because a fault-closed tenant never gets a shutdown record, they stay closed across restarts until the
operator's break-glass recovery, which re-applies the named revokes BEFORE it reopens any administrator. Start writes a durable
start record (file and directory fsync) before admitting. Compaction writes the spare, fsyncs, exchanges the two names
atomically, fsyncs the directory. Clean stop: admission and the revoke lane close first, then the drain, then the shutdown
record. All file I/O goes through `epoch_journal.RealIO`, which tests replace (fault injection, power-loss simulation).

The keyword flags `broken_*` exist for the tests' broken variants only."""
from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional

from . import epoch_journal
from .epoch_journal import Journal, JournalFull, RealIO
from .sessions import EpochTable

OPEN, CLOSED, FAULT = "open", "closed", "fault_closed"


class Refused(Exception):
    pass


@dataclass
class Receipt:
    principal: str
    epoch: int
    denied_at: float
    durable_at: Optional[float] = None
    persisted: Optional[bool] = None


class RevocationService:
    def __init__(self, tenant: str, epochs: EpochTable, directory: Path, *, io=None, d_fsync_s: float = 2.0,
                 persist: Optional[Callable[[str, int], None]] = None, clock: Callable[[], float] = time.time,
                 broken_wait_for_journal: bool = False, broken_require_persist: bool = False,
                 broken_start_without_fsync: bool = False, broken_accept_unterminated: bool = False,
                 broken_admins_first: bool = False, broken_shutdown_when_faulted: bool = False,
                 broken_exchange_before_fsync: bool = False, broken_skip_dir_fsync: bool = False,
                 broken_compaction_drops_start: bool = False, broken_lane_open_during_drain: bool = False,
                 broken_shutdown_before_drain: bool = False, broken_journal_error_keeps_tenant_open: bool = False,
                 broken_compaction_in_place: bool = False, broken_admit_before_start_fsync: bool = False,
                 broken_compaction_copies_shutdown: bool = False):
        self.tenant, self._ep, self._dir = tenant, epochs, Path(directory)
        self._io, self._d_fsync, self._persist, self._clock = io or RealIO(), d_fsync_s, persist, clock
        self._b = dict(wait=broken_wait_for_journal, persist=broken_require_persist, nostart=broken_start_without_fsync,
                       unterminated=broken_accept_unterminated, admins_first=broken_admins_first,
                       shutdown_faulted=broken_shutdown_when_faulted, xchg_first=broken_exchange_before_fsync,
                       skip_dir=broken_skip_dir_fsync, drop_start=broken_compaction_drops_start,
                       lane_open_drain=broken_lane_open_during_drain, shutdown_first=broken_shutdown_before_drain,
                       keep_open=broken_journal_error_keeps_tenant_open, in_place=broken_compaction_in_place,
                       admit_first=broken_admit_before_start_fsync, copy_shutdown=broken_compaction_copies_shutdown)
        self._lock = threading.RLock()
        self.state, self.lane_open, self.fault = CLOSED, False, ""
        self.grant_admins: Optional[set] = None
        self.persist_failures = 0
        self.events: list[str] = []
        self._j: Optional[Journal] = None
        # pool records (POOLS 2.5): the pool directory and the token table hand the journal their own record kinds. `extra_replay`
        # is called for every record that is not an epoch record, in order; `extra_snapshot` gives the records compaction keeps.
        self.extra_replay: Optional[Callable[[dict], None]] = None
        self.extra_snapshot: Optional[Callable[[], list]] = None
        # other owners of record kinds (the class grants' reductions, `admin.GrantStore`): a record whose kind starts with one of the
        # prefixes is replayed to that owner, and compaction keeps what its snapshot returns
        self._kinds: list[tuple[tuple[str, ...], Callable[[dict], None], Callable[[], list]]] = []

    def register_records(self, prefixes: tuple[str, ...], replay: Callable[[dict], None], snapshot: Callable[[], list]) -> None:
        """Register the owner of the record kinds starting with `prefixes`, BEFORE `start` (the replay runs there)."""
        if any(p.startswith(("pool_", "token_", "revoke", "bump", "start", "shutdown", "recovery")) for p in prefixes):
            raise ValueError("a record kind the journal or the pool records already own")
        self._kinds.append((tuple(prefixes), replay, snapshot))

    def _extras(self) -> list:
        """The non-epoch records compaction keeps: the pool records and every registered owner's."""
        out = list(self.extra_snapshot() if self.extra_snapshot else [])
        for _, _, snapshot in self._kinds:
            out += snapshot()
        return out

    @property
    def journal_path(self) -> Path:
        return self._dir / "epochs.journal"

    @property
    def spare_path(self) -> Path:
        return self._dir / "epochs.spare"

    # ---- start and stop ---------------------------------------------------------------------------------------
    def _replay(self, j: Journal) -> None:
        for r in j.records:
            t = str(r.get("t", ""))
            if t in ("revoke", "bump"):
                self._ep.restore(self.tenant, r["principal"], int(r["epoch"]), t == "revoke")
            elif self.extra_replay is not None and t.startswith(("pool_", "token_")):
                self.extra_replay(r)
            else:
                for prefixes, replay, _ in self._kinds:
                    if t.startswith(prefixes):
                        replay(r)

    def _fault(self, why: str) -> None:
        self.state, self.lane_open, self.fault = FAULT, False, why   # no shutdown record is ever written for it
        self.events.append(f"fault:{why}")

    def _write_start(self, j: Journal) -> None:
        j.append({"t": "start", "at": self._clock()}, sync=not self._b["nostart"])
        if not self._b["nostart"]:
            self._io.fsync_dir(self._dir)

    def _take_lock(self) -> bool:
        """One process serves a tenant's journal (DESIGN 20): an exclusive non-blocking lock (`lockf`) on a lock file beside it. A second process
        finds the lock held and refuses to start (the tenant stays closed there) instead of serving stale grants and revocations."""
        import fcntl
        import os
        fd = os.open(self._dir / "epochs.lock", os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.lockf(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)   # a POSIX lock: per process, so a restart inside one process is not refused
        except OSError:
            os.close(fd)
            return False
        self._lock_fd = fd
        return True

    def _drop_lock(self) -> None:
        fd, self._lock_fd = getattr(self, "_lock_fd", None), None
        if fd is not None:
            import os
            os.close(fd)   # closing the descriptor releases the flock

    def start(self) -> str:
        with self._lock:
            if getattr(self, "_lock_fd", None) is None and not self._take_lock():
                self._j = None
                self._fault("journal in use by another process")
                return self.state
            j = Journal(self.journal_path, self._io)
            if j.torn:
                self._j = j
                self._fault("torn journal")
                return self.state
            if j.records and j.records[-1].get("t") != "shutdown" and not self._b["unterminated"]:
                self._j = j
                self._fault("incomplete journal: no shutdown record")
                return self.state
            self._replay(j)
            self._j = j
            if self._b["admit_first"]:   # BROKEN variant: admission opens before the start record is durable
                self.state, self.lane_open = OPEN, True
            self._write_start(j)  # admission only after this returned
            self.state, self.lane_open = OPEN, True
            self.events.append("open")
            return self.state

    def admit(self) -> bool:
        return self.state == OPEN

    def clean_stop(self) -> None:
        if not self._b["lane_open_drain"]:   # BROKEN variant: the lane stays open while the drain runs
            self.lane_open = False           # admission and the revoke lane close first
        if self._b["shutdown_first"]:        # BROKEN variant: the shutdown record is written without waiting for pending appends
            self._write_shutdown()
        with self._lock:                     # then the drain: an append in flight finishes
            if not self._b["shutdown_first"]:
                self._write_shutdown()
            self.lane_open = False
            if self.state == OPEN:
                self.state = CLOSED
            if self._j:
                self._j.close()
                self._j = None
            self._drop_lock()

    def _write_shutdown(self) -> None:
        if self._j is not None and (self.state == OPEN or (self._b["shutdown_faulted"] and self.state == FAULT)):
            self._j.append({"t": "shutdown", "at": self._clock()})
            self._io.fsync_dir(self._dir)

    # ---- revoke -----------------------------------------------------------------------------------------------
    def _append(self, rec: dict) -> bool:
        """Append and fsync; True when durable within D_FSYNC. A fault closes the tenant."""
        try:
            t0 = time.monotonic()
            try:
                self._j.append(rec)
            except JournalFull:
                self.compact()
                self._j.append(rec)
            if time.monotonic() - t0 > self._d_fsync:
                self._append_fault("fsync over D_FSYNC")
                return False
            return True
        except (OSError, JournalFull):  # a full journal that compaction cannot shrink (live rows exceed J_SIZE) is a fault too
            self._append_fault("journal I/O error or full")
            return False

    def _append_fault(self, why: str) -> None:
        if not self._b["keep_open"]:   # BROKEN variant: a journal error leaves the tenant admitted
            self._fault(why)

    def revoke(self, principal: str) -> Receipt:
        return self._flip(principal, True)

    def bump(self, principal: str) -> Receipt:
        """An epoch bump without a revoke (class-grant removal, DESIGN 4.4): every session of the principal ends at its next check,
        the principal can mint a new one that carries the reduced grants. Same order as a revoke: memory, then the journal."""
        return self._flip(principal, False)

    def journalled(self, apply: Callable[[], Any], record: Callable[[Any], dict], *, persist: Optional[Callable[[Any], None]] = None):
        """A pool-grant removal, a detach, a single-token revoke (POOLS 2.5) or a class-grant reduction (`admin.GrantStore.reduce`), in
        the order of a revoke: (1) `apply` runs under the
        EXCLUSIVE side of the tenant's send fence, in memory only, which is the denial; (2) after the fence is released the record
        `record(result)` is appended and fsynced, and (3) `persist(result)` is the best-effort write to the authz copy. Returns
        (result, Receipt-like dict with `denied_at` and `durable_at`)."""
        if self.state != OPEN or not self.lane_open:
            raise Refused("tenant closed")
        result = self._ep.under_fence(self.tenant, apply)
        rc = {"denied_at": self._clock(), "durable_at": None, "persisted": None}
        with self._lock:
            if self._append(record(result)):
                rc["durable_at"] = self._clock()
        if persist is not None:
            try:
                persist(result)
                rc["persisted"] = True
            except Exception:
                self.persist_failures += 1
                rc["persisted"] = False
        return result, rc

    def _flip(self, principal: str, revoke: bool) -> Receipt:
        if self.state != OPEN or not self.lane_open:
            raise Refused("tenant closed")
        kind = "revoke" if revoke else "bump"
        if self._b["persist"] and self._persist:     # BROKEN: the revoke needs the Elasticsearch write first
            self._persist(principal, self._ep.epoch(self.tenant, principal) + 1)
        if self._b["wait"]:                           # BROKEN: denial after the journal fsync
            with self._lock:
                self._append({"t": kind, "principal": principal, "epoch": self._ep.epoch(self.tenant, principal) + 1})
        den = self._ep.bump(self.tenant, principal, revoke=revoke)
        epoch = self._ep.epoch(self.tenant, principal)
        rc = Receipt(principal, epoch, den.denied_at)
        if not self._b["wait"]:
            with self._lock:
                if self._append({"t": kind, "principal": principal, "epoch": epoch}):
                    rc.durable_at = self._clock()
        if self._persist and not self._b["persist"]:
            try:
                self._persist(principal, epoch)
                rc.persisted = True
            except Exception:
                self.persist_failures += 1
                rc.persisted = False
        return rc

    # ---- compaction -------------------------------------------------------------------------------------------
    def _compact_in_place(self) -> None:
        """BROKEN variant: the live journal is zeroed and rewritten where it is. Between the zeroing and the last record a power loss
        leaves a journal that holds no revoke at all."""
        size = self.journal_path.stat().st_size
        fd = os.open(self.journal_path, os.O_RDWR)
        try:
            self._io.pwrite(fd, b"\0" * size, 0)
            self._io.fsync(fd)
        finally:
            os.close(fd)
        self._j.close()
        sj = Journal(self.journal_path, self._io)
        sj.append({"t": "start", "at": self._clock(), "compacted": True})
        for principal, epoch, revoked in self._ep.snapshot(self.tenant):
            sj.append({"t": "revoke" if revoked else "bump", "principal": principal, "epoch": epoch})
        for extra in self._extras():
            sj.append(extra)
        self._j = sj
        self.events.append("compacted")

    def compact(self) -> None:
        with self._lock:
            if self._b["in_place"]:
                return self._compact_in_place()
            spare = self.spare_path
            size = spare.stat().st_size
            fd = os.open(spare, os.O_RDWR)
            try:                                       # the spare is reused: zero it, then write the live state into it
                self._io.pwrite(fd, b"\0" * size, 0)
                self._io.fsync(fd)
            finally:
                os.close(fd)
            sj = Journal(spare, self._io)
            sync = not self._b["xchg_first"]           # BROKEN variant: the spare's bytes are fsynced only after the exchange
            try:
                if not self._b["drop_start"]:          # BROKEN variant: the new file carries no start record
                    sj.append({"t": "start", "at": self._clock(), "compacted": True}, sync=sync)
                for principal, epoch, revoked in self._ep.snapshot(self.tenant):
                    sj.append({"t": "revoke" if revoked else "bump", "principal": principal, "epoch": epoch}, sync=sync)
                for extra in self._extras():
                    sj.append(extra, sync=sync)
                if self._b["copy_shutdown"] and any(r.get("t") == "shutdown" for r in self._j.records):
                    # BROKEN variant: the previous run's shutdown state is carried into the compacted file, so a crash after it
                    # reads as a clean stop
                    sj.append({"t": "shutdown", "at": self._clock()}, sync=sync)
            finally:
                sj.close()                             # every append fsynced the file
            self._io.exchange(str(self.journal_path), str(spare))   # an error here leaves the old journal in place
            if not self._b["skip_dir"]:                # BROKEN variant: the exchange is never made durable
                self._io.fsync_dir(self._dir)
            if not sync:
                fd = os.open(self.journal_path, os.O_RDWR)
                try:
                    self._io.fsync(fd)
                finally:
                    os.close(fd)
            self._j.close()
            self._j = Journal(self.journal_path, self._io)
            self.events.append("compacted")

    # ---- break-glass ------------------------------------------------------------------------------------------
    def break_glass(self, revokes: list[str], admins: list[str]) -> None:
        """Operator call for a fault-closed or incomplete tenant. `revokes`: the principals of every revoke receipt without
        `durable_at`; `admins`: the administrators to reopen. Order: the revokes are journalled and fsynced first, then only
        the named administrators may call grant, then a recovery record and a new durable start record."""
        with self._lock:
            if self.state == OPEN:
                raise Refused("tenant is open")
            if self._j:
                self._j.close()
            j = Journal(self.journal_path, self._io)   # a torn tail is overwritten from its start
            self._j = j
            self._replay(j)

            def reopen_admins():
                self.grant_admins = set(admins)
                self.events.append("admins:" + ",".join(sorted(admins)))

            if self._b["admins_first"]:
                reopen_admins()
            for p in revokes:
                self._ep.bump(self.tenant, p, revoke=True)
                j.append({"t": "revoke", "principal": p, "epoch": self._ep.epoch(self.tenant, p)})
                self.events.append(f"revoke:{p}")
            if not self._b["admins_first"]:
                reopen_admins()
            j.append({"t": "recovery", "admins": sorted(admins), "at": self._clock()})
            self._write_start(j)
            self.state, self.lane_open, self.fault = OPEN, True, ""
            self.events.append("open")

    def may_grant(self, admin: str) -> bool:
        return self.state == OPEN and (self.grant_admins is None or admin in self.grant_admins)
