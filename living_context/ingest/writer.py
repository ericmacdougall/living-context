"""The Elasticsearch-backed write path (DESIGN 7.4; BUILD-PLAN Step 6).

`Writer.remember` runs the steps of 7.4 for one leaf of one tenant and class:

  0. refuse empty input, an over-cap leaf, a guard refusal or hold (the shared tombstone guard);
  1. claim the idempotency key (when given) and allocate `src_seq` from the tenant's counter by
     compare-and-set; the claim document stores leaf id, version and `src_seq`, so a retry after any
     failure resumes the same leaf with the same ids and never allocates a second one;
  2. create the data key (keystore) and append the ciphertext source document (`op_type=create`);
  3. embed outside Elasticsearch (the stub) and index the plaintext projection with its CODE-BUILT
     title (`title.build_title`, the only title path);
  4. create or advance the leaf head by compare-and-set (`if_seq_no`, `if_primary_term`);
  5. enqueue the leaf in the dirty queue (a document in the class's control index);
  6. refresh the leaf index so the leaf is visible to direct search.

Every step is idempotent for the same ids, so a crash between any two steps leaves something
`repair()` can finish (a source document without a projection is re-projected from the source) or
undo (a projection without a source document is deleted: the projection is never the truth).
Every document passes the gateway validator before it is sent. `pause(step)` is a test seam: it is
called after each step and may raise to simulate a crash or block to force an interleaving.
"""
from __future__ import annotations

import base64
import hmac
import datetime
import json
import threading
import time
from dataclasses import dataclass
from typing import Callable, Iterable, Optional

from ..config import Constants
from ..embed import stub as embed_stub
from ..gateway import reads, validator
from ..gateway.gate import NoGate
from ..gateway.es import EsClient
from ..gateway.indices import TENANT_CLS, class_index, pool_index, tenant_index
from ..gateway.reads import IncompleteRead, complete_hits
from ..graph import entities
from ..ids import new_leaf_id
from ..keystore.store import Keystore, KeystoreError
from . import segment, subjects, title

STORED, HELD, REFUSED, EMPTY, TOO_LARGE, KEY_REUSE = "stored", "held", "refused", "empty", "too_large", "key_reuse"
EMBED_MODEL = "stub-hashed-bow-v1"
SCHEMA_V = 1
CAS_TRIES = 50


_SRC_LOCKS: dict = {}
_SRC_LOCKS_GUARD = threading.Lock()


def _src_lock(tenant: str, leaf_id: str, lv: int) -> threading.Lock:
    """One lock per leaf version: the source check, the key creation and the source append are one step."""
    with _SRC_LOCKS_GUARD:
        return _SRC_LOCKS.setdefault((tenant, leaf_id, lv), threading.Lock())


_LEAF_LOCKS: dict = {}
_LEAF_INFLIGHT: dict = {}


def _leaf_lock(tenant: str, pool: str, leaf_id: str) -> threading.Lock:
    """One lock per pool leaf, for the whole process (not per request handler): the claim, the recovery of a stale claim and a retraction
    of one leaf are serialised here; the head compare-and-set is what makes them correct across processes."""
    with _SRC_LOCKS_GUARD:
        return _LEAF_LOCKS.setdefault((tenant, pool, leaf_id), threading.Lock())


PENDING = ("pending_op", "pending_kind", "pending_lv", "pending_new")
# Every Writer entry point that writes a pool's documents (class C4): each takes the shared side of the write gate and checks that the pool
# still accepts writes before its first write. tests/unit/test_registration.py checks the list against the code.
POOL_WRITES = ("remember", "head_transition", "record_retraction", "hold_tainted", "record_event", "release_held",
               "_ensure_projection", "_create_pool_head")


class WriterError(Exception):
    """An Elasticsearch step failed (typed; no body or index name is carried)."""


@dataclass(frozen=True)
class WriteResult:
    status: str
    leaf_id: str = ""
    lv: int = 0
    src_seq: int = 0

    @property
    def ok(self) -> bool:
        return self.status == STORED


ORIGINS = ("agent", "import", "human")


def origin_of(doc) -> str:
    """The origin of a stored version (POOLS 3.8), read in ONE place: a missing or unknown value is `unknown`, which is the least
    trusted for display and never grants an agent the right to correct or retract (class C6: missing provenance is the strict case)."""
    o = (doc or {}).get("origin")
    return o if o in ORIGINS else "unknown"


def source_prov(src: dict) -> dict:
    """The provenance a pool source document carries (`prov_json`, written with the version), for a repair or a recovery: the rebuilt
    projection keeps the version's own writer, mode and origin. A source without it gets origin `unknown` (strict)."""
    try:
        prov = json.loads(src["prov_json"]) if isinstance(src.get("prov_json"), str) else None
    except ValueError:
        prov = None
    if not isinstance(prov, dict):
        return {"origin": "unknown", "mode": "repair"}
    return dict(prov, origin=origin_of(prov))


def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


class Writer:
    def __init__(self, es: Callable[[], EsClient], keystore: Keystore, tenant: str, guard: subjects.Guard, *,
                 consts: Optional[Constants] = None, embed: Callable[[str], list] = embed_stub.embed,
                 pause: Optional[Callable[[str], None]] = None, use_cas: bool = True, gate=None, write_path=None, reader=None,
                 broken_quarantine_key_unlinked: bool = False):
        self._reader = reader  # gateway.class_reader.ClassReader: the only way a pool's class-level documents are read
        self._wp = write_path  # a structure.watermark.WritePath: a job opens at allocation and stays open until the link job finishes
        self._es_fn, self._ks, self.tenant, self._guard = es, keystore, tenant, guard
        self._c = consts or Constants()
        self._embed, self._pause, self._use_cas = embed, pause or (lambda step: None), use_cas
        self._gate = gate or NoGate()
        self._unlinked = broken_quarantine_key_unlinked   # BROKEN variant: the held blob is sealed under a key no subject token names

    # ---- plumbing -------------------------------------------------------------------------------
    @property
    def _es(self) -> EsClient:
        return self._es_fn()

    def _base(self, kind: str, cls: str, **extra) -> dict:
        d = {"tenant": self.tenant, "cls": cls, "kind": kind, "lin_leaves": [], "lin_leaf_ids": [], "lin_nodes": [],
             "lin_subjects": [], "lin_queries": [], "derivation": "write", "erasure_rewrite": 0,
             "created_at": _now(), "schema_v": SCHEMA_V}
        d.update(extra)
        return d

    def _put(self, index: str, doc_id: str, doc: dict, *, cls: Optional[str], create: bool = False,
             cas: Optional[tuple[int, int]] = None) -> tuple[int, dict]:
        validator.validate(doc, session_tenant=self.tenant, index=index, index_tenant=self.tenant, index_cls=cls)
        q = []
        if create:
            q.append("op_type=create")
        if cas is not None and self._use_cas:
            q.append(f"if_seq_no={cas[0]}&if_primary_term={cas[1]}")
        path = f"/{index}/_doc/{doc_id}" + ("?" + "&".join(q) if q else "")
        st, body = self._es.request("PUT", path, doc)
        if st not in (200, 201, 409):
            raise WriterError(f"index step failed ({st})")
        return st, body if isinstance(body, dict) else {}

    def _cget(self, cls: str, kind: str, doc_id: str, pool: Optional[str]) -> Optional[dict]:
        """A class-level document: through the class reader for a pool's document (it carries the pool term), directly otherwise."""
        if pool is None:
            return self._get(class_index(self.tenant, cls, kind), doc_id)
        d = self._reader.get(cls, kind, doc_id, pool)
        return None if d is None else {"_source": dict(d.source), "_seq_no": d.seq_no, "_primary_term": d.primary_term}

    def _get(self, index: str, doc_id: str) -> Optional[dict]:
        st, body = self._es.request("GET", f"/{index}/_doc/{doc_id}")
        if st == 404:
            return None
        if st != 200 or not isinstance(body, dict):
            raise WriterError(f"read step failed ({st})")
        return body

    # ---- counters and heads (compare-and-set) ------------------------------------------------------
    def next_seq(self) -> int:
        """Allocate the next `src_seq` of the tenant: one monotone counter, compare-and-set."""
        idx = tenant_index(self.tenant, "ctl")
        for _ in range(CAS_TRIES):
            cur = self._get(idx, "seq")
            self._pause("seq_read")
            if cur is None:
                st, _ = self._put(idx, "seq", self._base("counter", TENANT_CLS, value=1), cls=None, create=True)
                if st in (200, 201):
                    return 1
                continue
            v = int(cur["_source"]["value"]) + 1
            st, _ = self._put(idx, "seq", self._base("counter", TENANT_CLS, value=v), cls=None,
                              cas=(cur["_seq_no"], cur["_primary_term"]))
            if st in (200, 201):
                return v
            time.sleep(0.005)
        raise WriterError("sequence counter contended")

    def bind_pools(self, active: Callable[[str], bool]) -> None:
        """`active(pool)`: whether the pool still accepts writes. Checked inside every pool commit, under the shared side of the write
        gate, so a pool delete (which takes the gate exclusively after flipping the state) cannot be raced by a write that was authorized
        earlier (POOLS 9.4)."""
        self._pool_active = active

    def _pool_ok(self, pool: Optional[str]) -> bool:
        return pool is None or getattr(self, "_pool_active", None) is None or bool(self._pool_active(pool))

    def advance_head(self, cls: str, leaf_id: str, lv: int, pool: Optional[str] = None, *, status: str = "live",
                     extra: Optional[dict] = None) -> int:
        """Create or advance the leaf head `lh:<leaf_id>` to at least `lv` by compare-and-set; return the stored version. A POOL head
        (`lh:<pool>:<leaf_id>`) is only CREATED here (with `status`, `live` or `pending` for a supersede's new leaf): an existing pool head
        is never rewritten, because every later change of a pool head (a new version, a supersede, a retraction) is an
        expected-version-and-status transition (`head_transition`), never "at least this version, live"."""
        idx = class_index(self.tenant, cls, "ctl")
        hid = f"lh:{pool}:{leaf_id}" if pool else f"lh:{leaf_id}"
        for _ in range(CAS_TRIES):
            cur = self._cget(cls, "ctl", hid, pool)
            doc = self._base("head", cls, current_lv=lv, status=status if pool else "live", lin_leaf_ids=[leaf_id],
                             **({"pool": pool} if pool else {}), **(extra or {}))
            if cur is None:
                st, _ = self._put(idx, hid, doc, cls=cls, create=True)
                if st in (200, 201):
                    return lv
                continue
            self._pause("head_read")
            have = int(cur["_source"]["current_lv"])
            if have >= lv or pool:
                return have
            st, _ = self._put(idx, hid, doc, cls=cls, cas=(cur["_seq_no"], cur["_primary_term"]))
            if st in (200, 201):
                return lv
        raise WriterError("head contended")

    def update_head(self, cls: str, pool: str, leaf_id: str, expect_lv: int, **fields) -> bool:
        """Set fields of a LIVE pool leaf head with no pending claim, only while its `current_lv` is still `expect_lv`."""
        return self.head_transition(cls, pool, leaf_id, {"current_lv": expect_lv, "status": "live", "pending_op": None}, fields) is not None

    def head_transition(self, cls: str, pool: str, leaf_id: str, expect: dict, changes: dict, drop: Iterable[str] = ()) -> Optional[dict]:
        """THE compare-and-set of a pool leaf head (class C3): the head must hold every `expect` field (None: absent), then `changes` are
        set and `drop` fields removed, with `if_seq_no`. Returns the new head, or None when the head is gone, differs from `expect`, or the
        pool no longer accepts writes. Under the shared side of the write gate (POOLS 9.4)."""
        with self._gate.shared():
            if not self._pool_ok(pool):
                return None
            return self._update_head(cls, pool, leaf_id, expect, changes, drop)

    def _update_head(self, cls: str, pool: str, leaf_id: str, expect: dict, changes: dict, drop: Iterable[str] = ()) -> Optional[dict]:
        idx, hid = class_index(self.tenant, cls, "ctl"), f"lh:{pool}:{leaf_id}"
        for _ in range(CAS_TRIES):
            cur = self._cget(cls, "ctl", hid, pool)
            if cur is None:
                return None
            src = cur["_source"]
            for k, v in expect.items():
                have = src.get(k)
                if (int(have) if k == "current_lv" and have is not None else have) != v:
                    return None
            doc = {k: v for k, v in src.items() if k not in set(drop)}
            doc.update(changes)
            st, _ = self._put(idx, hid, doc, cls=cls, cas=(cur["_seq_no"], cur["_primary_term"]))
            if st in (200, 201):
                return doc
        raise WriterError("head contended")

    # ---- leaf mutations: one claim on the head, shared by correct, supersede and retract (POOLS 3.3, 3.5; class C3) ----------------
    def _inflight(self, pool: str, leaf_id: str) -> set:
        with _SRC_LOCKS_GUARD:
            return _LEAF_INFLIGHT.setdefault((self.tenant, pool, leaf_id), set())

    def leaf_head(self, cls: str, pool: str, leaf_id: str) -> Optional[dict]:
        cur = self._cget(cls, "ctl", f"lh:{pool}:{leaf_id}", pool)
        return None if cur is None else dict(cur["_source"])

    def _settle(self, cls: str, pool: str, leaf_id: str) -> Optional[dict]:
        """Under the leaf lock: a pending claim that no operation of THIS process holds is the trace of a crashed process (one gateway
        process per tenant is enforced by the journal lock), so it is finished or undone before anything else touches the leaf."""
        head = self.leaf_head(cls, pool, leaf_id)
        if head is not None and head.get("pending_op") and head["pending_op"] not in self._inflight(pool, leaf_id):
            self.recover_claim(cls, pool, leaf_id, head)
            head = self.leaf_head(cls, pool, leaf_id)
        return head

    def leaf_claim(self, cls: str, pool: str, leaf_id: str, cur: int, kind: str, *, lv: Optional[int] = None,
                   new: Optional[str] = None) -> Optional[str]:
        """Claim the head of a live leaf at version `cur` for one change (`correct` to version `lv`, or `supersede` by the new leaf
        `new`). Returns the claim id, or None when the head is not live at `cur` with no claim (a version conflict)."""
        opid = new_leaf_id()
        with _leaf_lock(self.tenant, pool, leaf_id):
            self._settle(cls, pool, leaf_id)
            got = self.head_transition(cls, pool, leaf_id, {"current_lv": cur, "status": "live", "pending_op": None},
                                       {"pending_op": opid, "pending_kind": kind, "pending_lv": lv or cur, "pending_new": new or ""})
            if got is None:
                return None
            self._inflight(pool, leaf_id).add(opid)
        return opid

    def leaf_commit(self, cls: str, pool: str, leaf_id: str, opid: str, changes: dict) -> bool:
        """Apply the claimed change: only while the head is still live and still carries this claim."""
        return self.head_transition(cls, pool, leaf_id, {"status": "live", "pending_op": opid}, changes, drop=PENDING) is not None

    def leaf_release(self, cls: str, pool: str, leaf_id: str, opid: str, *, abandon: bool) -> None:
        """End of the claim in this process; `abandon` also clears it on the head (the change did not happen)."""
        try:
            if abandon:
                self.head_transition(cls, pool, leaf_id, {"pending_op": opid}, {}, drop=PENDING)
        finally:
            self._inflight(pool, leaf_id).discard(opid)

    def leaf_retract(self, cls: str, pool: str, leaf_id: str, cur: int, reason: str) -> bool:
        """Retract a live leaf at version `cur` with no pending claim (one head compare-and-set). False: the head changed or a change
        of the leaf is in flight (the caller answers a version conflict for that pointer)."""
        with _leaf_lock(self.tenant, pool, leaf_id):
            self._settle(cls, pool, leaf_id)
            return self.update_head(cls, pool, leaf_id, cur, status="retracted", retract_reason=reason)

    def _source_doc(self, leaf_id: str, lv: int) -> Optional[dict]:
        got = self._get(tenant_index(self.tenant, "src"), f"{leaf_id}:{lv}")
        return None if got is None else got["_source"]

    def _ensure_projection(self, cls: str, pool: str, leaf_id: str, lv: int, src: dict) -> bool:
        """The projection of a durable pool source, re-built from the source (text and provenance) when it is missing. Under the write
        gate, refused for a pool that no longer accepts writes."""
        with self._gate.shared():
            if not self._pool_ok(pool):
                return False
            if self._get(pool_index(self.tenant, cls, pool, "leaf"), f"{leaf_id}:{lv}") is not None:
                return True
            try:
                text = self._ks.decrypt(leaf_id, lv, base64.b64decode(src["ct"]), base64.b64decode(src["nonce"])).decode()
            except KeystoreError:
                return False
            self._project(cls, leaf_id, lv, text, src, pool)
            return True

    def _create_pool_head(self, cls: str, pool: str, leaf_id: str, lv: int, status: str, extra: Optional[dict] = None) -> bool:
        """Create a missing pool head (a repair or a recovery); never rewrites one. Under the write gate."""
        with self._gate.shared():
            if not self._pool_ok(pool):
                return False
            self.advance_head(cls, leaf_id, lv, pool, status=status, extra=extra)
            return True

    def recover_claim(self, cls: str, pool: str, leaf_id: str, head: dict) -> None:
        """Finish or undo a claim left by a crashed process. Rolled FORWARD when the change's source is durable (its projection is rebuilt
        from it, with its own provenance); rolled BACK otherwise (the claim is cleared; a supersede's new head is aborted)."""
        opid, kind = head.get("pending_op"), head.get("pending_kind")
        if kind == "correct":
            lv = int(head.get("pending_lv") or 0)
            src = self._source_doc(leaf_id, lv)
            if src is not None and self._ensure_projection(cls, pool, leaf_id, lv, src):
                self.head_transition(cls, pool, leaf_id, {"pending_op": opid}, {"current_lv": lv}, drop=PENDING)
                return
            self.head_transition(cls, pool, leaf_id, {"pending_op": opid}, {}, drop=PENDING)
            return
        if kind == "supersede":
            new = head.get("pending_new") or ""
            src = self._source_doc(new, 1) if new else None
            if src is not None and self._ensure_projection(cls, pool, new, 1, src):
                self._create_pool_head(cls, pool, new, 1, "pending", {"supersedes": leaf_id})
                if self.head_transition(cls, pool, leaf_id, {"pending_op": opid},
                                        {"status": "superseded", "superseded_by": new}, drop=PENDING) is not None:
                    self.head_transition(cls, pool, new, {"status": "pending"}, {"status": "live"})
                return
            self.head_transition(cls, pool, leaf_id, {"pending_op": opid}, {}, drop=PENDING)
            if new:
                self.head_transition(cls, pool, new, {"status": "pending"}, {"status": "aborted"})
            return
        self.head_transition(cls, pool, leaf_id, {"pending_op": opid}, {}, drop=PENDING)   # an unknown claim: undone

    def settle_new_head(self, cls: str, pool: str, leaf_id: str, head: dict) -> None:
        """A supersede's new head still `pending`: live once the old head points at it, aborted when the old head no longer can."""
        old = head.get("supersedes") or ""
        with _leaf_lock(self.tenant, pool, old):
            oh = self._settle(cls, pool, old) if old else None
            if oh is not None and oh.get("status") == "superseded" and oh.get("superseded_by") == leaf_id:
                self.head_transition(cls, pool, leaf_id, {"status": "pending"}, {"status": "live"})
            elif oh is None or not (oh.get("pending_op") and oh.get("pending_new") == leaf_id):
                self.head_transition(cls, pool, leaf_id, {"status": "pending"}, {"status": "aborted"})

    # ---- the write ---------------------------------------------------------------------------------
    def remember(self, principal: str, cls: str, text: str, *, subjects_declared: Iterable[str] = (),
                 key: Optional[str] = None, source_ref: Optional[dict] = None, refresh: bool = True,
                 _admitted: bool = False, _version: Optional[tuple] = None, pool: Optional[str] = None,
                 prov: Optional[dict] = None, _head: Optional[str] = "live", head_extra: Optional[dict] = None) -> WriteResult:
        """A commit: takes the shared side of the tenant's write gate (an erasure's closure-to-boundary holds it
        exclusively), and the guard check runs inside it so a tombstone published meanwhile is seen. For a pool, `_head` is the status a
        NEW leaf's head is created with (`pending` for a supersede), or None for a new version of an existing leaf (its head moves only
        through the caller's claim, `leaf_commit`)."""
        with self._gate.shared():
            return self._remember(principal, cls, text, subjects_declared=subjects_declared, key=key,
                                  source_ref=source_ref, refresh=refresh, _admitted=_admitted, _version=_version, pool=pool, prov=prov,
                                  _head=_head, head_extra=head_extra)

    def relabel_write(self, principal: str, leaf_id: str, lv: int, new_cls: str, text: str, **kw) -> WriteResult:
        """The new-class copy of a relabelled leaf (DESIGN 14.1): the same leaf id with a new `lv`, written after the old-class
        copy was erased at its boundary (`Executor.relabel_erase`). `lv` must be above every version that existed."""
        return self.remember(principal, new_cls, text, _version=(leaf_id, int(lv)), **kw)

    def _remember(self, principal: str, cls: str, text: str, *, subjects_declared: Iterable[str] = (),
                  key: Optional[str] = None, source_ref: Optional[dict] = None, refresh: bool = True,
                  _admitted: bool = False, _version: Optional[tuple] = None, pool: Optional[str] = None,
                  prov: Optional[dict] = None, _head: Optional[str] = "live", head_extra: Optional[dict] = None) -> WriteResult:
        if cls == TENANT_CLS or (pool is not None and self._reader is None) or not self._pool_ok(pool):
            return WriteResult(REFUSED)
        if not isinstance(text, str) or not text.strip():
            return WriteResult(EMPTY)
        if segment.tokens_of(text, self._c.chars_per_token_bound) > self._c.leaf_cap:
            return WriteResult(TOO_LARGE)  # the cap is never exceeded; callers segment first
        declared = sorted({s for s in subjects_declared if isinstance(s, str) and s.strip()})
        verdict = self._guard.check("\n".join(declared + [text]))
        if verdict == subjects.REFUSED:
            return WriteResult(REFUSED)
        if verdict == subjects.HELD and not _admitted:
            self._quarantine(principal, cls, text, declared, source_ref)
            return WriteResult(HELD)
        body = json.dumps({"cls": cls, "text": text, "subjects": declared, "ref": source_ref,
                           **({"pool": pool} if pool else {})}, sort_keys=True).encode()
        claim = None
        if key is not None:
            claim = self._claim(principal, key, body, cls, pool) if pool else self._claim(principal, key, body, cls)
            if isinstance(claim, WriteResult):
                return claim
            leaf_id, lv, seq = claim
        else:
            leaf_id, lv, seq = (_version[0], _version[1], self.next_seq()) if _version else (new_leaf_id(), 1, self.next_seq())
        job = self._wp.begin(seq) if self._wp is not None else None
        try:
            self._pause("allocated")
            tokens = sorted({self._ks.subject_token(s) for s in declared + subjects.identifiers(text)} | ({principal} if pool else set()))   # the writer is in a pool leaf's lineage (POOLS 3.8)
            src = self._append_source(cls, leaf_id, lv, seq, text, tokens, source_ref, pool, prov)
            self._pause("source")
            self._project(cls, leaf_id, lv, text, src, pool)   # from the STORED source: a resumed save keeps the first writer's provenance
            self._pause("projection")
            if pool is None or _head is not None:
                self.advance_head(cls, leaf_id, lv, pool, status=_head or "live", extra=head_extra)
            self._pause("head")
            if pool is None:   # a pool at stage 0 has no tree: nothing is placed (ADAPTIVE 3.1)
                self._enqueue(cls, leaf_id, lv, seq)
            self._pause("queued")
        except BaseException:
            if job is not None:
                self._wp.finish(job, ok=False)   # a failed write-path job is counted and no longer holds P back
            raise
        if refresh:
            self._es.request("POST", f"/{pool_index(self.tenant, cls, pool, 'leaf') if pool else class_index(self.tenant, cls, 'leaf')}/_refresh")
        if claim is not None:
            self._finish_claim(principal, key, cls, pool)
        return WriteResult(STORED, leaf_id, lv, seq)

    # ---- quarantine (DESIGN 7.3: a bare-name match of an erased identity is held for review, not refused) --------
    def _quarantine(self, principal: str, cls: str, text: str, declared: list, source_ref) -> None:
        """Keep the held write for review: ciphertext under its own data key whose subject tokens are those found in
        the text, in the class's control index, with the same lineage fields, so erasure reaches both."""
        qid = new_leaf_id()
        tokens = sorted(set(self._guard.tokens_in("\n".join(declared + [text]))))
        blob = json.dumps({"text": text, "subjects": declared, "ref": source_ref}, sort_keys=True).encode()
        ct, nonce, key_ref = self._ks.encrypt(qid, 1, blob, [] if self._unlinked else tokens)
        doc = self._base("quarantine", cls, lin_subjects=tokens or [self._ks.digest("quarantine", qid.encode())[:16]],
                         lin_leaf_ids=[qid], qid=qid, principal=principal, state="held", ct=base64.b64encode(ct).decode(),
                         nonce=base64.b64encode(nonce).decode(), key_ref=key_ref)
        self._put(class_index(self.tenant, cls, "ctl"), f"qh:{qid}", doc, cls=cls, create=True)

    def quarantine_list(self, cls: str) -> list[dict]:
        """Held items of a class: id, time, state (never the content). Authorisation is the gateway's (class held)."""
        idx = class_index(self.tenant, cls, "ctl")
        self._es.request("POST", f"/{idx}/_refresh")
        st, r = self._es.request("POST", f"/{idx}/_search", {"size": 1000, "_source": ["qid", "created_at", "state"],
                                                              "query": {"bool": {"filter": [{"term": {"kind": "quarantine"}}],
                                                                                 "must_not": [{"exists": {"field": "pool"}}]}}})   # not the holds of a pool
        try:
            hits = complete_hits(st, r)   # a partial answer would hide a held item from its reviewer
        except IncompleteRead as e:
            raise WriterError("quarantine scan failed") from e
        return sorted((h["_source"] for h in hits), key=lambda d: d["created_at"])

    def quarantine_review(self, cls: str, qid: str, decision: str) -> WriteResult:
        """`admit` writes the held text as an ordinary leaf (the reviewer decided it is not the erased subject);
        `reject` drops it. Either way the held ciphertext is stripped and its key destroyed."""
        idx = class_index(self.tenant, cls, "ctl")
        cur = self._get(idx, f"qh:{qid}")
        if cur is None or cur["_source"].get("state") != "held" or decision not in ("admit", "reject"):
            return WriteResult(REFUSED)
        s = cur["_source"]
        res = WriteResult(REFUSED)
        if decision == "admit":
            held = json.loads(self._ks.decrypt(qid, 1, base64.b64decode(s["ct"]), base64.b64decode(s["nonce"])))
            res = self.remember(s["principal"], cls, held["text"], subjects_declared=held["subjects"],
                                source_ref=held["ref"], _admitted=True)
            if not res.ok:
                return res
        doc = {k: v for k, v in s.items() if k not in ("ct", "nonce", "key_ref")}
        doc["state"] = "admitted" if decision == "admit" else "rejected"
        self._put(idx, f"qh:{qid}", doc, cls=cls, cas=(cur["_seq_no"], cur["_primary_term"]))
        self._ks.destroy(leaf_id=qid)
        return res if decision == "admit" else WriteResult("rejected")

    def record_retraction(self, cls: str, pool: str, leaf_id: str, lv: int, principal: str, actor: str, reason: str, state: str) -> None:
        with self._gate.shared():   # under the write gate and refused for a pool that is going away (POOLS 9.4)
            if not self._pool_ok(pool):
                raise WriterError("pool not active")
            self._record_retraction(cls, pool, leaf_id, lv, principal, actor, reason, state)

    def _record_retraction(self, cls: str, pool: str, leaf_id: str, lv: int, principal: str, actor: str, reason: str, state: str) -> None:
        """The append-only record of a retraction or of a request for one (POOLS 3.5), in the class control index, inside the erasure
        closure by lineage (`lin_leaf_ids`, the actor in `lin_subjects`)."""
        prefix = "rt" if state == "done" else "rq"
        doc = self._base("retraction", cls, lin_subjects=[principal], lin_leaf_ids=[leaf_id], pool=pool, leaf_id=leaf_id, lv=lv,
                         actor=actor, reason=reason, state=state)
        self._put(class_index(self.tenant, cls, "ctl"), f"{prefix}:{pool}:{leaf_id}:{lv}", doc, cls=cls)

    # ---- held saves of a tainted conversation (POOLS 2.8) -------------------------------------------------------
    def hold_tainted(self, principal: str, taint_cls: str, pool: str, lin_pools: Iterable[str], text: str, prov: dict) -> str:
        with self._gate.shared():   # under the write gate and refused for a pool that is going away (POOLS 9.4)
            if not self._pool_ok(pool):
                raise WriterError("pool not active")
            return self._hold_tainted(principal, taint_cls, pool, lin_pools, text, prov)

    def _hold_tainted(self, principal: str, taint_cls: str, pool: str, lin_pools: Iterable[str], text: str, prov: dict) -> str:
        """Keep a save that the write floor held: ciphertext under its own data key (named by the subject tokens found in the text and
        the writer), stored at the TAINT class in the class control index with `pool = target` and `lin_pools` = the private set, so
        every erasure and the pool delete reach it. It is never written to the target pool's indices."""
        hid = new_leaf_id()
        tokens = sorted(set(self._guard.tokens_in(text)) | {principal})
        ct, nonce, key_ref = self._ks.encrypt(hid, 1, json.dumps({"text": text}).encode(), tokens)
        doc = self._base("quarantine", taint_cls, lin_subjects=tokens, lin_leaf_ids=[hid], qid=hid, principal=principal, state="held",
                         reason="taint", pool=pool, lin_pools=sorted(set(lin_pools) | {pool}), prov=prov,
                         ct=base64.b64encode(ct).decode(), nonce=base64.b64encode(nonce).decode(), key_ref=key_ref)
        self._put(class_index(self.tenant, taint_cls, "ctl"), f"th:{pool}:{hid}", doc, cls=taint_cls, create=True)
        self._es.request("POST", f"/{class_index(self.tenant, taint_cls, 'ctl')}/_refresh")
        return hid

    def held_text(self, taint_cls: str, pool: str, hid: str):
        """(text, content hash) of a held save that is still held or being released, for the human who is about to approve it; None when
        there is no such hold. The hash is what a release binds to (POOLS 3.5: show, then hash)."""
        cur = self._cget(taint_cls, "ctl", f"th:{pool}:{hid}", pool)
        if cur is None or cur["_source"].get("state") not in ("held", "releasing"):
            return None
        s = cur["_source"]
        text = json.loads(self._ks.decrypt(hid, 1, base64.b64decode(s["ct"]), base64.b64decode(s["nonce"])))["text"]
        return text, self._ks.digest("content", text.encode())

    def record_event(self, cls: str, pool: str, name: str, principal: str, subjects: Iterable[str], *, event_id: Optional[str] = None,
                     **fields) -> None:
        """A gateway event row (POOLS 2.8, 3.1): an append-only `event` document in the class control index (read only through the class
        reader), lineage by pool and subjects, no text (hashes and ids only), so the pool delete and every erasure reach it and no signal
        counts it as a recall. Under the write gate and refused for a pool going away (POOLS 9.4). `event_id` gives a stable identity (a
        retry writes the same row once)."""
        with self._gate.shared():
            if not self._pool_ok(pool):
                raise WriterError("pool not active")
            doc = self._base("event", cls, lin_subjects=sorted(set(subjects) | {principal}), lin_pools=[pool], pool=pool, event=name,
                             actor_token=principal, **fields)
            self._put(class_index(self.tenant, cls, "ctl"), f"ev:{pool}:{name}:{event_id or new_leaf_id()}", doc, cls=cls, create=True)

    def release_held(self, taint_cls: str, pool: str, pool_cls: str, hid: str, decision: str, release_prov: Optional[dict] = None,
                     expect_hash: Optional[str] = None) -> WriteResult:
        """`admit` writes the held text into the target pool as an ordinary pool leaf (origin and writer kept from the hold: a release does
        not change origin) and strips the held ciphertext; `discard` strips it. Either way the key is destroyed. An admit binds to the
        content hash the human was shown, claims the hold (`held` to `releasing` by compare-and-set) before writing, and writes the leaf
        under a key derived from the hold id, so two concurrent admits, a double click and a retry after a crash write one leaf."""
        with self._gate.shared():   # every write of a release under the write gate, refused for a pool going away (POOLS 9.4)
            if not self._pool_ok(pool):
                return WriteResult(REFUSED)
            return self._release_held(taint_cls, pool, pool_cls, hid, decision, release_prov, expect_hash)

    def _release_held(self, taint_cls, pool, pool_cls, hid, decision, release_prov, expect_hash) -> WriteResult:
        idx = class_index(self.tenant, taint_cls, "ctl")
        cur = self._cget(taint_cls, "ctl", f"th:{pool}:{hid}", pool)
        state = None if cur is None else cur["_source"].get("state")
        if state not in ("held", "releasing") or decision not in ("admit", "discard") or (decision == "discard" and state != "held"):
            return WriteResult(REFUSED)
        s = cur["_source"]
        res = WriteResult(REFUSED)
        if decision == "admit":
            held = json.loads(self._ks.decrypt(hid, 1, base64.b64decode(s["ct"]), base64.b64decode(s["nonce"])))
            if s.get("prov", {}).get("mode") not in (None, "save"):
                return WriteResult(REFUSED)   # only a held save can be released here; a held update is not releasable yet
            if expect_hash is None or not hmac.compare_digest(str(expect_hash), self._ks.digest("content", held["text"].encode())):
                return WriteResult(REFUSED)   # the human approved other content than the one held
            if state == "held":
                st, _ = self._put(idx, f"th:{pool}:{hid}", dict(s, state="releasing"), cls=taint_cls, cas=(cur["_seq_no"], cur["_primary_term"]))
                if st not in (200, 201):
                    return WriteResult(REFUSED)   # another admit claimed it first
            res = self._remember(s["principal"], pool_cls, held["text"], pool=pool, key=f"release:{hid}",
                                 prov=dict(s["prov"], **(release_prov or {})))
            if not res.ok:
                return res
            cur = self._cget(taint_cls, "ctl", f"th:{pool}:{hid}", pool)
            if cur is None or cur["_source"].get("state") != "releasing":
                return res   # a concurrent admit already finished: one leaf, the same answer
            s = cur["_source"]
        doc = {k: v for k, v in s.items() if k not in ("ct", "nonce", "key_ref")}
        doc["state"] = "released" if decision == "admit" else "discarded"
        st, _ = self._put(idx, f"th:{pool}:{hid}", doc, cls=taint_cls, cas=(cur["_seq_no"], cur["_primary_term"]))
        if st in (200, 201):
            self._es.request("POST", f"/{idx}/_refresh")
            self._ks.destroy(leaf_id=hid)
        return res if decision == "admit" else WriteResult("discarded")

    # ---- idempotency ------------------------------------------------------------------------------
    def _claim_id(self, principal: str, cls: str, key: str) -> str:
        """Scope (tenant, principal, key), as DESIGN 7.1 names it: the class is NOT part of the scope. The full claim
        (leaf id, version, sequence) lives in the class's control index so that a lower-class reader cannot see it;
        a scope marker holding only the keyed digest of the body (which includes the class) lives in the tenant
        control index, so the same key with a different body in another class is refused."""
        return "idem:" + self._ks.digest("idem", f"{self.tenant}\x00{principal}\x00{key}".encode())[:40]

    def _scope_marker(self, principal: str, key: str, digest: str, cls: str) -> Optional[WriteResult]:
        tidx, cid = tenant_index(self.tenant, "ctl"), self._claim_id(principal, cls, key)
        for _ in range(CAS_TRIES):
            cur = self._get(tidx, cid)
            if cur is None:
                st, _ = self._put(tidx, cid, self._base("idem_scope", TENANT_CLS, body_digest=digest), cls=None, create=True)
                if st in (200, 201):
                    return None
                continue
            return None if cur["_source"].get("body_digest") == digest else WriteResult(KEY_REUSE)
        raise WriterError("claim contended")

    def _claim(self, principal: str, key: str, body: bytes, cls: str, pool: Optional[str] = None):
        """Return (leaf_id, lv, src_seq) of the claim, or a WriteResult when the key was used with another body
        or the leaf is already complete (the first result is returned)."""
        idx = class_index(self.tenant, cls, "ctl")
        cid, digest = self._claim_id(principal, cls, key), self._ks.digest("idem", body)
        if pool:
            cid = f"idem:{pool}:{cid[5:]}"   # the id embeds the pool so the class reader can read it
        refused = self._scope_marker(principal, key, digest, cls)
        if refused is not None:
            return refused
        for _ in range(CAS_TRIES):
            cur = self._cget(cls, "ctl", cid, pool)
            if cur is None:
                leaf_id, seq = new_leaf_id(), self.next_seq()
                doc = self._base("idem", cls, lin_leaf_ids=[leaf_id], body_digest=digest, leaf_id=leaf_id, lv=1,
                                 src_seq=seq, state="open", **({"pool": pool} if pool else {}))
                st, _ = self._put(idx, cid, doc, cls=cls, create=True)
                if st in (200, 201):
                    return leaf_id, 1, seq
                continue  # lost the race: read the winner's claim
            src = cur["_source"]
            if src.get("body_digest") != digest:
                return WriteResult(KEY_REUSE)
            if src.get("state") == "done":
                return WriteResult(STORED, src["leaf_id"], int(src["lv"]), int(src["src_seq"]))
            return src["leaf_id"], int(src["lv"]), int(src["src_seq"])  # resume an open claim
        raise WriterError("claim contended")

    def _finish_claim(self, principal: str, key: str, cls: str, pool: Optional[str] = None) -> None:
        idx = class_index(self.tenant, cls, "ctl")
        cid = self._claim_id(principal, cls, key)
        if pool:
            cid = f"idem:{pool}:{cid[5:]}"
        cur = self._cget(cls, "ctl", cid, pool)
        if cur is None or cur["_source"].get("state") == "done":
            return
        doc = dict(cur["_source"], state="done")
        self._put(idx, cid, doc, cls=cls, cas=(cur["_seq_no"], cur["_primary_term"]))

    # ---- steps --------------------------------------------------------------------------------------
    def _append_source(self, cls, leaf_id, lv, seq, text, tokens, source_ref, pool=None, prov=None) -> dict:
        """Append the source version (or find it, on a resume) and return the STORED source document: the projection is built from
        it, so a retry by another caller cannot change the version's provenance or lineage (review F3)."""
        with _src_lock(self.tenant, leaf_id, lv):  # check, key and append are one step (retries race otherwise)
            return self._append_source_locked(cls, leaf_id, lv, seq, text, tokens, source_ref, pool, prov)

    def _append_source_locked(self, cls, leaf_id, lv, seq, text, tokens, source_ref, pool=None, prov=None) -> dict:
        idx, did = tenant_index(self.tenant, "src"), f"{leaf_id}:{lv}"
        have = self._get(idx, did)
        self._pause("src_checked")
        if have is not None:
            if pool is not None:   # a resume must be the SAME text: a version is never shared by two texts (class C3)
                s = have["_source"]
                try:
                    same = self._ks.decrypt(leaf_id, lv, base64.b64decode(s["ct"]), base64.b64decode(s["nonce"])) == text.encode()
                except KeystoreError:
                    same = False
                if not same:
                    raise WriterError("version already written with another text")
            return have["_source"]  # resumed after the append: the stored version, not the retrying caller's
        if self._ks.has_key(leaf_id, lv):  # a key made before a crash that never reached the append
            self._ks.destroy_exact_version(leaf_id, lv)   # this version only: the live earlier versions of the leaf keep their keys
        ct, nonce, key_ref = self._ks.encrypt(leaf_id, lv, text.encode(), tokens)
        self._pause("key_made")
        doc = self._base("src", cls, lin_subjects=tokens, lin_leaf_ids=[leaf_id], leaf_id=leaf_id, lv=lv, src_seq=seq,
                         ct=base64.b64encode(ct).decode(), nonce=base64.b64encode(nonce).decode(), key_ref=key_ref,
                         valid_from=_now(), ingested_at=_now(), source_ref=source_ref or {},
                         **({"pool": pool, "prov_json": json.dumps(prov or {}, sort_keys=True)} if pool else {}))
        self._put(idx, did, doc, cls=cls, create=True)
        stored = self._get(idx, did)  # whoever won the create, the stored ciphertext must open under a live key
        try:
            self._ks.decrypt(leaf_id, lv, base64.b64decode(stored["_source"]["ct"]), base64.b64decode(stored["_source"]["nonce"]))
        except KeystoreError as e:
            raise WriterError("stored source does not open under its key") from e
        return stored["_source"]

    def _project(self, cls, leaf_id, lv, text, src, pool=None) -> None:
        """Write the projection of the source version `src` (the STORED source document): its lineage (`lin_subjects`), its sequence
        and, for a pool, its provenance (`source_prov`) come from the source, never from a caller, so every path that creates or resumes a
        projection (a save, a resume, a repair, a release) carries the version's own writer and origin (review F3, class C6;
        tests/unit/test_writer_resume_prov.py enumerates the paths)."""
        extra = dict(source_prov(src)) if pool else {}
        if pool:
            extra.update(pool=pool, lv=lv)
        tokens, seq = list(src.get("lin_subjects", [])), int(src["src_seq"])
        doc = self._base("leaf", cls, lin_subjects=tokens, **extra, lin_leaf_ids=[leaf_id], src_seq=seq, text=text,
                         title=title.build_title(text, n=self._c.title_terms), entities=entities.extract(text),
                         size_tokens=segment.tokens_of(text, self._c.chars_per_token_bound),
                         embedding=self._embed(text), embed_model=EMBED_MODEL, valid_from=_now(),
                         content_hash=self._ks.digest("content", text.encode()))
        self._put(pool_index(self.tenant, cls, pool, "leaf") if pool else class_index(self.tenant, cls, "leaf"), f"{leaf_id}:{lv}", doc, cls=cls)

    def _enqueue(self, cls, leaf_id, lv, seq) -> None:
        doc = self._base("dirty", cls, lin_leaf_ids=[leaf_id], leaf_id=leaf_id, lv=lv, src_seq=seq, state="pending")
        self._put(class_index(self.tenant, cls, "ctl"), f"dq:{leaf_id}:{lv}", doc, cls=cls, create=True)  # 409 = already queued

    # ---- orphan repair ------------------------------------------------------------------------------
    def _repair_pool_source(self, s: dict, doc_id: str, out: dict) -> None:
        """Finish a pool write: the leaf is re-projected into the pool's own index from its source, WITH the provenance the source holds
        (`source_prov`: writer, mode, origin; none held is origin `unknown`, the strict case), a missing head is created (a supersede's new
        leaf as `pending`), and a claim or a pending head left by a crash is finished or undone (`recover_claim`, `settle_new_head`). A
        pool has no placement before stage 1, so no queue record is written. A pool that no longer accepts writes is left alone."""
        cls, pool, leaf_id, lv = s["cls"], s["pool"], s["leaf_id"], int(s["lv"])
        try:
            head = self.leaf_head(cls, pool, leaf_id)
            if head is None and lv == 1:
                prov = source_prov(s)
                sup = prov.get("supersedes") if prov.get("mode") == "supersede" else None
                if not self._ensure_projection(cls, pool, leaf_id, lv, s):
                    out["erased_skipped"] += 1
                    return
                self._create_pool_head(cls, pool, leaf_id, 1, "pending" if sup else "live", {"supersedes": sup} if sup else None)
                out["reprojected"] += 1
            elif head is not None and lv <= int(head.get("current_lv", 0)):
                if self._get(pool_index(self.tenant, cls, pool, "leaf"), doc_id) is None:
                    if self._ensure_projection(cls, pool, leaf_id, lv, s):
                        out["reprojected"] += 1
                    else:
                        out["erased_skipped"] += 1
            head = self.leaf_head(cls, pool, leaf_id)
            if head is not None and head.get("pending_op"):
                with _leaf_lock(self.tenant, pool, leaf_id):
                    self._settle(cls, pool, leaf_id)
            head = self.leaf_head(cls, pool, leaf_id)
            if head is not None and head.get("status") == "pending":
                self.settle_new_head(cls, pool, leaf_id, head)
        except WriterError:
            return

    def _refresh_checked(self, index: str) -> None:
        try:
            reads.refresh(self._es, index)
        except IncompleteRead as e:
            raise WriterError("refresh failed") from e

    @staticmethod
    def _page(st, r) -> list:
        try:
            return complete_hits(st, r)   # a partial page (HTTP 200 with a failed shard) is not the end of the scan
        except IncompleteRead as e:
            raise WriterError("scan failed") from e

    def repair(self, classes: Iterable[str]) -> dict:
        """Finish or undo partial writes. Source documents without a projection are re-projected from the
        source (decrypted with the leaf's key); a source whose key is gone was erased and is left alone;
        projections without a source document are deleted. Heads and queue entries missing for a stored
        leaf are recreated."""
        classes = list(classes)
        src_idx = tenant_index(self.tenant, "src")
        self._refresh_checked(src_idx)
        out = {"reprojected": 0, "erased_skipped": 0, "projections_deleted": 0, "heads_created": 0, "queued": 0}
        after = None
        while True:
            body = {"size": 200, "sort": [{"src_seq": "asc"}], "query": {"match_all": {}}}
            if after is not None:
                body["search_after"] = [after]
            st, r = self._es.request("POST", f"/{src_idx}/_search", body)
            hits = self._page(st, r)
            if not hits:
                break
            for h in hits:
                s = h["_source"]
                cls, leaf_id, lv, seq = s["cls"], s["leaf_id"], int(s["lv"]), int(s["src_seq"])
                if cls not in classes:
                    continue
                if s.get("pool"):
                    self._repair_pool_source(s, h["_id"], out)   # a pool source goes back into its own pool's index, never a class-level one
                    continue
                leaf = class_index(self.tenant, cls, "leaf")
                if self._get(leaf, h["_id"]) is None:
                    try:
                        text = self._ks.decrypt(leaf_id, lv, base64.b64decode(s["ct"]), base64.b64decode(s["nonce"])).decode()
                    except KeystoreError:
                        out["erased_skipped"] += 1
                        continue
                    self._project(cls, leaf_id, lv, text, s)
                    out["reprojected"] += 1
                heads = class_index(self.tenant, cls, "ctl")
                if self._get(heads, f"lh:{leaf_id}") is None:
                    self.advance_head(cls, leaf_id, lv)
                    out["heads_created"] += 1
                if self._get(heads, f"dq:{leaf_id}:{lv}") is None:
                    self._enqueue(cls, leaf_id, lv, seq)
                    out["queued"] += 1
            after = hits[-1]["sort"][0]
        for cls in classes:  # projections whose source document is missing
            leaf = class_index(self.tenant, cls, "leaf")
            self._refresh_checked(leaf)
            after = None
            while True:
                body = {"size": 200, "sort": [{"src_seq": "asc"}], "_source": False, "query": {"match_all": {}}}
                if after is not None:
                    body["search_after"] = [after]
                st, r = self._es.request("POST", f"/{leaf}/_search", body)
                hits = self._page(st, r)
                if not hits:
                    break
                ids = [h["_id"] for h in hits]
                st, m = self._es.request("POST", "/_mget", {"docs": [{"_index": src_idx, "_id": i} for i in ids]})
                docs = m.get("docs") if st == 200 and isinstance(m, dict) else None
                if not isinstance(docs, list) or len(docs) != len(ids):
                    raise WriterError("scan failed")
                for d in docs:
                    # only a VERIFIED absence deletes: a per-document error (an unavailable shard) is not "the source is missing"
                    if not isinstance(d, dict) or "error" in d or d.get("found") not in (True, False):
                        raise WriterError("source lookup incomplete")
                    if d["found"] is False:
                        st, _ = self._es.request("DELETE", f"/{leaf}/_doc/{d['_id']}")
                        if st not in (200, 404):
                            raise WriterError("projection delete failed")
                        out["projections_deleted"] += 1
                after = hits[-1]["sort"][0]
        for cls in classes:
            self._es.request("POST", f"/{class_index(self.tenant, cls, 'leaf')}/_refresh")
        return out
