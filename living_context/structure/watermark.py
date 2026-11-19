"""The write-path watermark P and the write-path jobs (DESIGN 5.6, 7.5, 7.7; BUILD-PLAN Steps 12 and 13).

A job is opened for every source sequence at allocation and finishes written or failed (a failure is counted). P is the highest
sequence s such that every job up to s has finished; it never decreases and moves only by compare-and-set on the tenant's `wp`
document. Members and edges are found by search, which sees a write only after a refresh, so P advances only after an explicit
refresh of every index the covered jobs touched has returned (the searchable-publication barrier): acknowledgement of a write is
never taken as visibility.

A job still open after `D_WP` is cancelled and JOINED: its lock is taken (an in-flight write finishes first), the job is marked
cancelled so it can write nothing more, the documents it wrote are deleted, the deletes are refreshed, and only then does P move
past it. The keywords `join_cancelled` and `refresh_before_advance` switch those steps off for the BROKEN variants of the tests.
"""
from __future__ import annotations

import datetime
import threading
import time
from typing import Callable, Optional

from ..config import Constants
from ..gateway import validator
from ..gateway.es import EsClient
from ..gateway.indices import TENANT_CLS, tenant_index

SCHEMA_V = 1
CAS_TRIES = 50


class WatermarkError(Exception):
    pass


class Job:
    def __init__(self, seq: int, started: float):
        self.seq, self.started = seq, started
        self.lock = threading.RLock()
        self.cancelled = False
        self.state = "open"          # open | done | failed | cancelled
        self.written: list[tuple[str, str]] = []   # (index, doc id) of every document this job wrote
        self.touched: set[str] = set()


class WritePath:
    def __init__(self, es: Callable[[], EsClient], tenant: str, *, consts: Optional[Constants] = None,
                 clock: Callable[[], float] = time.monotonic, pause: Optional[Callable[[str], None]] = None,
                 join_cancelled: bool = True, refresh_before_advance: bool = True):
        self._es_fn, self.tenant = es, tenant
        self._c = consts or Constants()
        self._clock, self._pause = clock, pause or (lambda step: None)
        self._join, self._refresh = join_cancelled, refresh_before_advance
        self._open: dict[int, Job] = {}
        self._max_seq = 0
        self._to_refresh: set[str] = set()
        self._lock = threading.Lock()
        self.failed = 0              # failed and cancelled jobs, counted (INV-11)
        self.cancelled_jobs = 0

    @property
    def _es(self) -> EsClient:
        return self._es_fn()

    # ---- jobs -------------------------------------------------------------------------------------------------
    def begin(self, seq: int) -> Job:
        with self._lock:
            job = self._open.get(seq)
            if job is None:
                job = self._open[seq] = Job(seq, self._clock())
            self._max_seq = max(self._max_seq, seq)
            return job

    def finish(self, job: Job, ok: bool = True) -> None:
        with self._lock:
            if job.state == "open":
                job.state = "done" if ok else "failed"
                if not ok:
                    self.failed += 1
            self._to_refresh |= job.touched
            self._open.pop(job.seq, None)

    def write(self, job: Job, index: str, doc_id: str, doc: dict, *, create: bool = True) -> bool:
        """One write of a job, under the job's lock. A cancelled job writes nothing."""
        with job.lock:
            if job.cancelled:
                return False
            validator.validate(doc, session_tenant=self.tenant, index=index, index_tenant=self.tenant, index_cls=doc["cls"])
            st, _ = self._es.request("PUT", f"/{index}/_doc/{doc_id}" + ("?op_type=create" if create else ""), doc)
            if st in (200, 201):
                job.written.append((index, doc_id))
                job.touched.add(index)
                return True
            return False

    def touch(self, job: Job, index: str) -> None:
        """A retirement update (a `gen_to` entry) the job made on an index must be searchable before P moves too."""
        job.touched.add(index)

    # ---- cancellation of a late job ---------------------------------------------------------------------------
    def _cancel(self, job: Job) -> None:
        if not self._join:     # BROKEN variant: the job is merely forgotten, it may still write after P has moved
            with self._lock:
                job.state = "cancelled"
                self._open.pop(job.seq, None)
                self.failed += 1
                self.cancelled_jobs += 1
            return
        with job.lock:         # joined: an in-flight write has finished, no later write can start
            job.cancelled = True
            for index, doc_id in job.written:
                self._es.request("DELETE", f"/{index}/_doc/{doc_id}")
                self._to_refresh.add(index)
            job.written.clear()
        with self._lock:
            job.state = "cancelled"
            self._open.pop(job.seq, None)
            self.failed += 1
            self.cancelled_jobs += 1

    def cancel_overdue(self) -> list[int]:
        now = self._clock()
        with self._lock:
            late = [j for j in self._open.values() if now - j.started > self._c.d_wp_s]
        for j in late:
            self._cancel(j)
        return [j.seq for j in late]

    # ---- P ---------------------------------------------------------------------------------------------------
    def _wp_index(self) -> str:
        return tenant_index(self.tenant, "ctl")

    def read_p(self) -> int:
        st, r = self._es.request("GET", f"/{self._wp_index()}/_doc/wp")
        if st == 404:
            return 0
        if st != 200:
            raise WatermarkError(f"read failed ({st})")
        return int(r["_source"]["P"])

    def target(self) -> int:
        with self._lock:
            return (min(self._open) - 1) if self._open else self._max_seq

    def advance(self) -> int:
        """Cancel overdue jobs, refresh what the covered jobs touched, then move P by compare-and-set. Returns P."""
        self.cancel_overdue()
        target = self.target()
        with self._lock:
            idxs, self._to_refresh = set(self._to_refresh), set()
        if self._refresh:
            for idx in sorted(idxs):
                self._es.request("POST", f"/{idx}/_refresh")
        self._pause("refreshed")
        for _ in range(CAS_TRIES):
            st, cur = self._es.request("GET", f"/{self._wp_index()}/_doc/wp")
            have = int(cur["_source"]["P"]) if st == 200 else 0
            if target <= have:
                return have
            doc = {"tenant": self.tenant, "cls": TENANT_CLS, "kind": "counter", "lin_leaves": [], "lin_leaf_ids": [],
                   "lin_nodes": [], "lin_subjects": [], "lin_queries": [], "derivation": "write_path", "erasure_rewrite": 0,
                   "created_at": datetime.datetime.now(datetime.timezone.utc).isoformat(), "schema_v": SCHEMA_V, "P": target}
            q = "?op_type=create" if st == 404 else f"?if_seq_no={cur['_seq_no']}&if_primary_term={cur['_primary_term']}"
            st2, _ = self._es.request("PUT", f"/{self._wp_index()}/_doc/wp{q}", doc)
            if st2 in (200, 201):
                self._es.request("POST", f"/{self._wp_index()}/_refresh")
                return target
        raise WatermarkError("P contended")
