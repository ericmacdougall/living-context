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
import datetime
import json
import threading
import time
from dataclasses import dataclass
from typing import Callable, Iterable, Optional

from ..config import Constants
from ..embed import stub as embed_stub
from ..gateway import validator
from ..gateway.gate import NoGate
from ..gateway.es import EsClient
from ..gateway.indices import TENANT_CLS, class_index, tenant_index
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


def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


class Writer:
    def __init__(self, es: Callable[[], EsClient], keystore: Keystore, tenant: str, guard: subjects.Guard, *,
                 consts: Optional[Constants] = None, embed: Callable[[str], list] = embed_stub.embed,
                 pause: Optional[Callable[[str], None]] = None, use_cas: bool = True, gate=None):
        self._es_fn, self._ks, self.tenant, self._guard = es, keystore, tenant, guard
        self._c = consts or Constants()
        self._embed, self._pause, self._use_cas = embed, pause or (lambda step: None), use_cas
        self._gate = gate or NoGate()

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

    def advance_head(self, cls: str, leaf_id: str, lv: int) -> int:
        """Create or advance the leaf head `lh:<leaf_id>` to at least `lv` by compare-and-set; return the stored version."""
        idx = class_index(self.tenant, cls, "ctl")
        hid = f"lh:{leaf_id}"
        for _ in range(CAS_TRIES):
            cur = self._get(idx, hid)
            doc = self._base("head", cls, current_lv=lv, status="live", lin_leaf_ids=[leaf_id])
            if cur is None:
                st, _ = self._put(idx, hid, doc, cls=cls, create=True)
                if st in (200, 201):
                    return lv
                continue
            self._pause("head_read")
            have = int(cur["_source"]["current_lv"])
            if have >= lv:
                return have
            st, _ = self._put(idx, hid, doc, cls=cls, cas=(cur["_seq_no"], cur["_primary_term"]))
            if st in (200, 201):
                return lv
        raise WriterError("head contended")

    # ---- the write ---------------------------------------------------------------------------------
    def remember(self, principal: str, cls: str, text: str, *, subjects_declared: Iterable[str] = (),
                 key: Optional[str] = None, source_ref: Optional[dict] = None, refresh: bool = True,
                 _admitted: bool = False) -> WriteResult:
        """A commit: takes the shared side of the tenant's write gate (an erasure's closure-to-boundary holds it
        exclusively), and the guard check runs inside it so a tombstone published meanwhile is seen."""
        with self._gate.shared():
            return self._remember(principal, cls, text, subjects_declared=subjects_declared, key=key,
                                  source_ref=source_ref, refresh=refresh, _admitted=_admitted)

    def _remember(self, principal: str, cls: str, text: str, *, subjects_declared: Iterable[str] = (),
                  key: Optional[str] = None, source_ref: Optional[dict] = None, refresh: bool = True,
                  _admitted: bool = False) -> WriteResult:
        if cls == TENANT_CLS:
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
        body = json.dumps({"cls": cls, "text": text, "subjects": declared, "ref": source_ref}, sort_keys=True).encode()
        claim = None
        if key is not None:
            claim = self._claim(principal, key, body, cls)
            if isinstance(claim, WriteResult):
                return claim
            leaf_id, lv, seq = claim
        else:
            leaf_id, lv, seq = new_leaf_id(), 1, self.next_seq()
        self._pause("allocated")
        tokens = sorted({self._ks.subject_token(s) for s in declared + subjects.identifiers(text)})
        self._append_source(cls, leaf_id, lv, seq, text, tokens, source_ref)
        self._pause("source")
        self._project(cls, leaf_id, lv, seq, text, tokens)
        self._pause("projection")
        self.advance_head(cls, leaf_id, lv)
        self._pause("head")
        self._enqueue(cls, leaf_id, lv, seq)
        self._pause("queued")
        if refresh:
            self._es.request("POST", f"/{class_index(self.tenant, cls, 'leaf')}/_refresh")
        if claim is not None:
            self._finish_claim(principal, key, cls)
        return WriteResult(STORED, leaf_id, lv, seq)

    # ---- quarantine (DESIGN 7.3: a bare-name match of an erased identity is held for review, not refused) --------
    def _quarantine(self, principal: str, cls: str, text: str, declared: list, source_ref) -> None:
        """Keep the held write for review: ciphertext under its own data key whose subject tokens are those found in
        the text, in the class's control index, with the same lineage fields, so erasure reaches both."""
        qid = new_leaf_id()
        tokens = sorted(set(self._guard.tokens_in("\n".join(declared + [text]))))
        blob = json.dumps({"text": text, "subjects": declared, "ref": source_ref}, sort_keys=True).encode()
        ct, nonce, key_ref = self._ks.encrypt(qid, 1, blob, tokens)
        doc = self._base("quarantine", cls, lin_subjects=tokens or [self._ks.digest("quarantine", qid.encode())[:16]],
                         lin_leaf_ids=[qid], qid=qid, principal=principal, state="held", ct=base64.b64encode(ct).decode(),
                         nonce=base64.b64encode(nonce).decode(), key_ref=key_ref)
        self._put(class_index(self.tenant, cls, "ctl"), f"qh:{qid}", doc, cls=cls, create=True)

    def quarantine_list(self, cls: str) -> list[dict]:
        """Held items of a class: id, time, state (never the content). Authorisation is the gateway's (class held)."""
        idx = class_index(self.tenant, cls, "ctl")
        self._es.request("POST", f"/{idx}/_refresh")
        st, r = self._es.request("POST", f"/{idx}/_search", {"size": 1000, "_source": ["qid", "created_at", "state"],
                                                              "query": {"term": {"kind": "quarantine"}}})
        if st != 200:
            raise WriterError("quarantine scan failed")
        return sorted((h["_source"] for h in r["hits"]["hits"]), key=lambda d: d["created_at"])

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

    def _claim(self, principal: str, key: str, body: bytes, cls: str):
        """Return (leaf_id, lv, src_seq) of the claim, or a WriteResult when the key was used with another body
        or the leaf is already complete (the first result is returned)."""
        idx = class_index(self.tenant, cls, "ctl")
        cid, digest = self._claim_id(principal, cls, key), self._ks.digest("idem", body)
        refused = self._scope_marker(principal, key, digest, cls)
        if refused is not None:
            return refused
        for _ in range(CAS_TRIES):
            cur = self._get(idx, cid)
            if cur is None:
                leaf_id, seq = new_leaf_id(), self.next_seq()
                doc = self._base("idem", cls, lin_leaf_ids=[leaf_id], body_digest=digest, leaf_id=leaf_id, lv=1,
                                 src_seq=seq, state="open")
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

    def _finish_claim(self, principal: str, key: str, cls: str) -> None:
        idx = class_index(self.tenant, cls, "ctl")
        cid = self._claim_id(principal, cls, key)
        cur = self._get(idx, cid)
        if cur is None or cur["_source"].get("state") == "done":
            return
        doc = dict(cur["_source"], state="done")
        self._put(idx, cid, doc, cls=cls, cas=(cur["_seq_no"], cur["_primary_term"]))

    # ---- steps --------------------------------------------------------------------------------------
    def _append_source(self, cls, leaf_id, lv, seq, text, tokens, source_ref) -> None:
        with _src_lock(self.tenant, leaf_id, lv):  # check, key and append are one step (retries race otherwise)
            self._append_source_locked(cls, leaf_id, lv, seq, text, tokens, source_ref)

    def _append_source_locked(self, cls, leaf_id, lv, seq, text, tokens, source_ref) -> None:
        idx, did = tenant_index(self.tenant, "src"), f"{leaf_id}:{lv}"
        have = self._get(idx, did)
        self._pause("src_checked")
        if have is not None:
            return  # resumed after the append
        if self._ks.has_key(leaf_id, lv):  # a key made before a crash that never reached the append
            self._ks.destroy(leaf_id=leaf_id)
        ct, nonce, key_ref = self._ks.encrypt(leaf_id, lv, text.encode(), tokens)
        self._pause("key_made")
        doc = self._base("src", cls, lin_subjects=tokens, lin_leaf_ids=[leaf_id], leaf_id=leaf_id, lv=lv, src_seq=seq,
                         ct=base64.b64encode(ct).decode(), nonce=base64.b64encode(nonce).decode(), key_ref=key_ref,
                         valid_from=_now(), ingested_at=_now(), source_ref=source_ref or {})
        self._put(idx, did, doc, cls=cls, create=True)
        stored = self._get(idx, did)  # whoever won the create, the stored ciphertext must open under a live key
        try:
            self._ks.decrypt(leaf_id, lv, base64.b64decode(stored["_source"]["ct"]), base64.b64decode(stored["_source"]["nonce"]))
        except KeystoreError as e:
            raise WriterError("stored source does not open under its key") from e

    def _project(self, cls, leaf_id, lv, seq, text, tokens) -> None:
        doc = self._base("leaf", cls, lin_subjects=tokens, lin_leaf_ids=[leaf_id], src_seq=seq, text=text,
                         title=title.build_title(text, n=self._c.title_terms), entities=[],
                         size_tokens=segment.tokens_of(text, self._c.chars_per_token_bound),
                         embedding=self._embed(text), embed_model=EMBED_MODEL, valid_from=_now(),
                         content_hash=self._ks.digest("content", text.encode()))
        self._put(class_index(self.tenant, cls, "leaf"), f"{leaf_id}:{lv}", doc, cls=cls)

    def _enqueue(self, cls, leaf_id, lv, seq) -> None:
        doc = self._base("dirty", cls, lin_leaf_ids=[leaf_id], leaf_id=leaf_id, lv=lv, src_seq=seq, state="pending")
        self._put(class_index(self.tenant, cls, "ctl"), f"dq:{leaf_id}:{lv}", doc, cls=cls, create=True)  # 409 = already queued

    # ---- orphan repair ------------------------------------------------------------------------------
    def repair(self, classes: Iterable[str]) -> dict:
        """Finish or undo partial writes. Source documents without a projection are re-projected from the
        source (decrypted with the leaf's key); a source whose key is gone was erased and is left alone;
        projections without a source document are deleted. Heads and queue entries missing for a stored
        leaf are recreated."""
        classes = list(classes)
        src_idx = tenant_index(self.tenant, "src")
        self._es.request("POST", f"/{src_idx}/_refresh")
        out = {"reprojected": 0, "erased_skipped": 0, "projections_deleted": 0, "heads_created": 0, "queued": 0}
        after = None
        while True:
            body = {"size": 200, "sort": [{"src_seq": "asc"}], "query": {"match_all": {}}}
            if after is not None:
                body["search_after"] = [after]
            st, r = self._es.request("POST", f"/{src_idx}/_search", body)
            if st != 200:
                raise WriterError("scan failed")
            hits = r["hits"]["hits"]
            if not hits:
                break
            for h in hits:
                s = h["_source"]
                cls, leaf_id, lv, seq = s["cls"], s["leaf_id"], int(s["lv"]), int(s["src_seq"])
                if cls not in classes:
                    continue
                leaf = class_index(self.tenant, cls, "leaf")
                if self._get(leaf, h["_id"]) is None:
                    try:
                        text = self._ks.decrypt(leaf_id, lv, base64.b64decode(s["ct"]), base64.b64decode(s["nonce"])).decode()
                    except KeystoreError:
                        out["erased_skipped"] += 1
                        continue
                    self._project(cls, leaf_id, lv, seq, text, list(s.get("lin_subjects", [])))
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
            self._es.request("POST", f"/{leaf}/_refresh")
            after = None
            while True:
                body = {"size": 200, "sort": [{"src_seq": "asc"}], "_source": False, "query": {"match_all": {}}}
                if after is not None:
                    body["search_after"] = [after]
                st, r = self._es.request("POST", f"/{leaf}/_search", body)
                if st != 200:
                    raise WriterError("scan failed")
                hits = r["hits"]["hits"]
                if not hits:
                    break
                ids = [h["_id"] for h in hits]
                st, m = self._es.request("POST", "/_mget", {"docs": [{"_index": src_idx, "_id": i} for i in ids]})
                if st != 200:
                    raise WriterError("scan failed")
                for d in m["docs"]:
                    if d.get("found") is not True:
                        self._es.request("DELETE", f"/{leaf}/_doc/{d['_id']}")
                        out["projections_deleted"] += 1
                after = hits[-1]["sort"][0]
        for cls in classes:
            self._es.request("POST", f"/{class_index(self.tenant, cls, 'leaf')}/_refresh")
        return out
