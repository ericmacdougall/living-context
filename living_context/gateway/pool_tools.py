"""The agent tools at stage 0, through the gateway (POOLS 3; BUILD-PLAN Step 25): `pool_save`, `pool_update`, `pool_query`,
`pool_fetch`. Zero model calls on write and read: this class holds no model port, and the stub embedding is a function.

Each tool resolves the token, authorizes EVERY pool it names or reaches (`pool_authz.authorize`: memory only), and only then touches an
index. A pointer is opened (a failed open is `unavailable`), its sealed pool is authorized from the token alone, and only then is the
head read, by id, through the class reader, and the leaf read from the pool's own index; `head.pool` must equal the sealed pool.
Every refusal of a pool request is the one `{"ok": false, "error": "unavailable"}`. Provenance comes from the token, never from the
arguments, and an edit never raises a version's origin (the least trusted of the writer, the edited version and a copy's source).

Stage 0 limits (listed in docs/decisions/design-readings.md): a text over the leaf cap is `too_large` (no segmentation yet); update
idempotency is memory only; `meta` is validated and not stored; selection is the hint or all non-private pools of the grant set; no
taint, review mode, retraction or copy yet.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import re
import threading
import time
from typing import Any, Callable, Mapping, Optional

from ..config import Constants
from ..embed import stub as embed_stub
from ..ids import KIND_LEAF
from ..ingest import writer as wmod
from ..policy import pools as pl
from ..recall import envelope
from ..recall.fusion import K_RRF, rrf, rrf_salted
from . import pointers
from .class_reader import ClassReader, ReaderRefused
from .indices import pool_index
from .pool_authz import authorize
from .session_state import SessionStates, StateError
from .sessions import Sessions
from ..policy import Op, decide

UNAVAILABLE = pl.UNAVAILABLE
TOKEN_SHAPE = re.compile(r"[a-z0-9]{4,16}\.[0-9a-f]{64}")
ORIGIN_RANK = {"unknown": -1, "agent": 0, "import": 1, "human": 2}   # the least trusted wins (POOLS 3.8); unknown is below agent
origin_of = wmod.origin_of   # the ONE reader of a version's origin: missing or unknown is `unknown` (strict for edits, untrusted for display)


def err(code: str, **extra) -> dict:
    return {"ok": False, "error": code, **extra}


def pl_taint(cls: str, private_ids: list, directory, tenant: str):
    """The taint of having read a hold: its class and the private pools in its set."""
    from .session_state import Taint
    return Taint(cls, frozenset(x for x in private_ids if (directory.get(tenant, x) is not None and directory.get(tenant, x).private)))


def least_trusted(*origins: str) -> str:
    return min(origins, key=lambda o: ORIGIN_RANK.get(o, -1))


class PoolTools:
    def __init__(self, tenant: str, directory: pl.PoolDirectory, sessions: Sessions, lattices: Mapping, writer: wmod.Writer,
                 reader: ClassReader, es_for: Callable[[str], Any], keys: pointers.PointerKeys, *,
                 consts: Optional[Constants] = None, clock: Callable[[], float] = time.time, model: Any = None,
                 broken_no_head_filter: bool = False, broken_pool_from_head: bool = False, broken_origin_from_editor: bool = False,
                 broken_key_scope_without_conversation: bool = False, broken_no_refill: bool = False,
                 broken_raw_title: bool = False, broken_best_match_pool: bool = False, states: Optional[SessionStates] = None,
                 broken_taint_ignored: bool = False, broken_hold_in_target: bool = False, broken_release_by_review_only: bool = False,
                 broken_default_selection_over_g: bool = False, broken_agent_retracts_human: bool = False,
                 broken_no_retract_rate: bool = False, stage_of: Optional[Callable[[str], int]] = None):
        self.tenant, self.dir, self.sessions, self.lattices, self.w, self.reader = tenant, directory, sessions, lattices, writer, reader
        self.es_for, self.keys, self.c, self._clock = es_for, keys, consts or Constants(), clock
        self.states = states
        self.w.bind_pools(lambda pid: (lambda p: p is not None and p.state == pl.ACTIVE)(self.dir.get(self.tenant, pid)))
        self._model = model   # reached only through `_stage_hook`, and only for a pool above stage 0
        self._stage_of = stage_of or (lambda pool_id: 0)
        self._b = dict(head=broken_no_head_filter, pool_head=broken_pool_from_head, origin=broken_origin_from_editor,
                       key=broken_key_scope_without_conversation, refill=broken_no_refill, raw_title=broken_raw_title,
                       best=broken_best_match_pool, taint=broken_taint_ignored, hold_target=broken_hold_in_target,
                       review_only=broken_release_by_review_only, sel_g=broken_default_selection_over_g, ret_human=broken_agent_retracts_human, ret_rate=broken_no_retract_rate)
        self._lock = threading.Lock()
        self._saves: dict[tuple, list] = {}
        self._retracts: dict[tuple, list] = {}
        self._update_keys: dict[tuple, tuple] = {}
        self.plan: list[str] = []    # every index an operation named, for the request-plan assertions

    # ---- routing ---------------------------------------------------------------------------------------------------------
    def handle(self, op: str, p, rec, req: Mapping[str, Any], *, token: str) -> dict:
        try:
            return {"pool_save": self.save, "pool_update": self.update, "pool_query": self.query, "pool_fetch": self.fetch,
                    "pool_holds": self.holds, "pool_hold_show": self.hold_show, "pool_release": self.release, "pool_retract": self.retract}[op](token, req)
        except (ReaderRefused, wmod.WriterError, StateError):
            return UNAVAILABLE

    # ---- helpers ---------------------------------------------------------------------------------------------------------
    def _stage_hook(self, pool, what: str) -> None:
        """The one place a pool operation may reach the model port: only a pool above stage 0 consults it (the tree path). A stage 0 pool
        never does; the tests hand this class a port that raises, so a stage 0 operation that reached it would fail at once, and a stage 1
        pool shows the port IS reachable from here (the assertion is not vacuous)."""
        if self._stage_of(pool.id) >= 1:
            self._model.call(what, {"pool": pool.id}, ctx=None)

    def _seal(self, pool: str, leaf_id: str, lv: int) -> str:
        return pointers.seal(self.keys, self.tenant, pointers.Sealed(pool, KIND_LEAF, 0, leaf_id, lv))

    def _prov(self, rec, mode: str, **extra) -> dict:
        return {"writer": rec.principal_token, "writer_actor": rec.actor,
                "writer_session": self.w._ks.digest("principal", f"{rec.principal_token}\x00{rec.conversation}".encode())[:32],
                "wrote_at": time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(self._clock())), "mode": mode, **extra}

    def _rate_ok(self, rec) -> bool:
        now = self._clock()
        k = (rec.tenant, rec.principal_token, rec.conversation)
        with self._lock:
            w = [t for t in self._saves.get(k, []) if t > now - 3600]
            if len(w) >= self.c.save_rate_per_h:
                self._saves[k] = w
                return False
            w.append(now)
            self._saves[k] = w
        return True

    def _text_ok(self, text: Any) -> Optional[dict]:
        if not isinstance(text, str) or not text.strip():
            return err("invalid")
        if len(text) > self.c.save_max_chars:
            return err("too_large")
        if TOKEN_SHAPE.search(text):
            return err("invalid")   # a bearer token never goes into a memory (POOLS 2.3)
        return None

    def _head(self, cls: str, pool: str, leaf_id: str):
        return self.reader.get(cls, "ctl", f"lh:{pool}:{leaf_id}", pool)

    def _leaf(self, cls: str, pool: str, leaf_id: str, lv: int) -> Optional[dict]:
        idx = pool_index(self.tenant, cls, pool, "leaf")
        self.plan.append(idx)
        st, r = self.es_for(cls).request("GET", f"/{idx}/_doc/{leaf_id}:{lv}")
        if st != 200 or not isinstance(r, dict) or not r.get("found"):
            return None
        src = r.get("_source") or {}
        return src if src.get("pool") == pool else None   # the leaf must belong to the pool the pointer sealed

    # ---- pool_save -------------------------------------------------------------------------------------------------------
    def save(self, token: str, req: Mapping[str, Any]) -> dict:
        rec = self.sessions.resolve(token)
        if rec is None or rec.scope is None:
            return UNAVAILABLE
        named = req.get("pool")
        pool_id = named if named is not None else rec.default_save_pool
        if pool_id is None and self._b["best"] and rec.scope:
            pool_id = sorted(rec.scope)[0]   # BROKEN variant: a "best matching" pool is guessed
        if pool_id is None:
            return err("pool_required")
        auth = authorize(self.sessions, self.dir, self.lattices, token, {pool_id: {pl.SAVE}})
        if auth is None:
            return UNAVAILABLE
        pool = auth.pools[pool_id]
        key = req.get("key")
        bad = self._text_ok(req.get("text"))
        meta = req.get("meta") or {}
        tags = meta.get("tags", []) if isinstance(meta, dict) else None
        if bad or not isinstance(key, str) or not key or len(key) > 128 or tags is None or not isinstance(tags, list) \
                or len(tags) > 16 or any(not isinstance(t, str) or len(t) > 32 for t in tags):
            return bad or err("invalid")
        if not self._rate_ok(rec):
            return err("rate_limited")
        self._stage_hook(pool, "write")
        if self._tainted_write(rec, pool):
            return self._hold(rec, pool, req["text"], "save")
        scope_key = key if self._b["key"] else f"{rec.conversation}\x00{key}"   # (tenant, principal, conversation, key)
        res = self.w.remember(rec.principal_token, pool.cls, req["text"], key=scope_key, pool=pool.id,
                              prov={**self._prov(rec, "save"), "origin": "human" if rec.actor == "human" else "agent"})
        return self._answer(res, pool.id)

    def _tainted_write(self, rec, pool) -> bool:
        return self.states is not None and not self._b["taint"] and self.states.blocks_write(rec, pool)

    def _hold(self, rec, pool, text: str, mode: str) -> dict:
        """The write floor (POOLS 2.8): the item is kept at the TAINT class (never in the target pool's indices) for a human to approve."""
        t = self.states.get(rec)
        cls = pool.cls if self._b["hold_target"] or t.cls is None else t.cls
        self.w.hold_tainted(rec.principal_token, cls, pool.id, t.private, text,
                            {**self._prov(rec, mode), "origin": "human" if rec.actor == "human" else "agent"})
        return err("held", reason="taint")

    def _answer(self, res, pool_id: str) -> dict:
        if res.status == wmod.STORED:
            return {"ok": True, "pointer": self._seal(pool_id, res.leaf_id, res.lv), "pool": pool_id}
        if res.status == wmod.TOO_LARGE:
            return err("too_large")
        if res.status == wmod.HELD:
            return err("held", reason="review")
        return err("invalid")   # empty, refused, erased identifier and key reuse look the same

    # ---- pool_update -----------------------------------------------------------------------------------------------------
    def update(self, token: str, req: Mapping[str, Any]) -> dict:
        s = pointers.open_(self.keys, self.tenant, req.get("pointer"))
        if s is None:
            return UNAVAILABLE
        auth = authorize(self.sessions, self.dir, self.lattices, token, {s.pool: {pl.SAVE, pl.UPDATE}})
        if auth is None:
            return UNAVAILABLE
        rec, pool = auth.rec, auth.pools[s.pool]
        mode, key, if_lv = req.get("mode"), req.get("key"), req.get("if_lv")
        bad = self._text_ok(req.get("text"))
        if bad or mode not in ("correct", "supersede") or not isinstance(key, str) or not key or len(key) > 128 \
                or (if_lv is not None and not isinstance(if_lv, int)):
            return bad or err("invalid")
        body = hashlib.sha256(json.dumps([mode, pool.id, s.id, req["text"], if_lv]).encode()).hexdigest()
        ik = (rec.tenant, rec.principal_token, rec.conversation, key)
        return self._update_claimed(rec, pool, s, mode, req, if_lv, body, ik)

    def _conflict(self, pool, leaf_id: str, cur: int) -> dict:
        head = self._head(pool.cls, pool.id, leaf_id)
        return err("version_conflict", current=self._seal(pool.id, leaf_id, int(head.source["current_lv"]) if head else cur))

    def _update_claimed(self, rec, pool, s, mode, req, if_lv, body, ik) -> dict:
        """POOLS 3.3 by one claim on the head (class C3): the head is claimed at the version read (live, no claim in flight) BEFORE any
        write, the new version or leaf is written, and the claim is committed by compare-and-set. A retract, another update or a crash in
        between can never be overwritten: a retract sees the claim and conflicts, a second update loses the claim, and a claim left by a
        crashed process is finished or undone by the next operation on the leaf (`Writer.recover_claim`)."""
        with self._lock:
            first = self._update_keys.get(ik)
        if first is not None:
            return first[1] if first[0] == body else err("invalid")
        head = self._head(pool.cls, pool.id, s.id)
        if head is None or head.source.get("pool") != pool.id:
            return UNAVAILABLE
        cur = int(head.source["current_lv"])
        old = self._leaf(pool.cls, pool.id, s.id, cur)
        if old is None:
            return UNAVAILABLE
        if head.source.get("status") != "live" or s.version != cur or (if_lv is not None and if_lv != cur):
            return err("version_conflict", current=self._seal(pool.id, s.id, cur))
        if not self._rate_ok(rec):
            return err("rate_limited")
        old_origin = origin_of(old)
        if mode == "correct" and rec.actor == "agent" and old_origin != "agent":
            return err("invalid")   # an agent corrects only what an agent wrote; it may supersede instead
        if self._tainted_write(rec, pool):
            return self._hold(rec, pool, req["text"], "update")
        writer_origin = "human" if rec.actor == "human" else "agent"
        origin = writer_origin if self._b["origin"] else least_trusted(writer_origin, old_origin)
        new_id = wmod.new_leaf_id() if mode == "supersede" else None
        opid = self.w.leaf_claim(pool.cls, pool.id, s.id, cur, mode, lv=cur + 1, new=new_id)
        if opid is None:
            return self._conflict(pool, s.id, cur)
        done = False
        try:
            if mode == "correct":
                res = self.w.remember(rec.principal_token, pool.cls, req["text"], pool=pool.id, _version=(s.id, cur + 1), _head=None,
                                      prov={**self._prov(rec, "correct"), "origin": origin})
                if res.status != wmod.STORED:
                    return self._answer(res, pool.id)
                if not self.w.leaf_commit(pool.cls, pool.id, s.id, opid, {"current_lv": cur + 1}):
                    return self._conflict(pool, s.id, cur)
                done = True
                out = {"ok": True, "pointer": self._seal(pool.id, s.id, cur + 1), "pool": pool.id}
            else:
                # the new leaf's head is created `pending` (out of retrieval) and goes live only after the old head says superseded
                res = self.w.remember(rec.principal_token, pool.cls, req["text"], pool=pool.id, _version=(new_id, 1), _head="pending",
                                      head_extra={"supersedes": s.id}, prov={**self._prov(rec, "supersede", supersedes=s.id), "origin": origin})
                if res.status != wmod.STORED:
                    return self._answer(res, pool.id)
                if not self.w.leaf_commit(pool.cls, pool.id, s.id, opid, {"status": "superseded", "superseded_by": new_id}):
                    self.w.head_transition(pool.cls, pool.id, new_id, {"status": "pending"}, {"status": "aborted"})
                    return self._conflict(pool, s.id, cur)
                done = True
                self.w.head_transition(pool.cls, pool.id, new_id, {"status": "pending"}, {"status": "live"})   # a crash here: repair
                out = {"ok": True, "pointer": self._seal(pool.id, new_id, 1), "pool": pool.id,
                       "old": {"status": "superseded", "newer": self._seal(pool.id, new_id, 1)}}
        finally:
            self.w.leaf_release(pool.cls, pool.id, s.id, opid, abandon=not done)
        with self._lock:
            self._update_keys[ik] = (body, out)
        return out

    # ---- pool_fetch ------------------------------------------------------------------------------------------------------
    def fetch(self, token: str, req: Mapping[str, Any]) -> dict:
        s = pointers.open_(self.keys, self.tenant, req.get("pointer"))
        if s is None:
            return UNAVAILABLE
        auth = authorize(self.sessions, self.dir, self.lattices, token, {s.pool: {pl.QUERY}})   # before any index read
        if auth is None:
            return UNAVAILABLE
        pool = auth.pools[s.pool]
        head = self._head(pool.cls, pool.id, s.id)
        if head is None or (head.source.get("pool") != pool.id and not self._b["pool_head"]):
            return UNAVAILABLE
        cur, status = int(head.source["current_lv"]), head.source.get("status", "live")
        if status == "superseded" and head.source.get("superseded_by"):
            nxt = self._head(pool.cls, pool.id, head.source["superseded_by"])
            if nxt is None:
                return UNAVAILABLE
            return {"ok": True, "status": "superseded", "newer": self._seal(pool.id, head.source["superseded_by"], int(nxt.source["current_lv"]))}
        if s.version < cur:
            return {"ok": True, "status": "superseded", "newer": self._seal(pool.id, s.id, cur)}
        if status == "retracted" and s.version == cur:
            return {"ok": True, "status": "retracted", "reason": head.source.get("retract_reason", "other")}   # for a token that could read it
        if s.version > cur or status != "live":
            return UNAVAILABLE
        leaf = self._leaf(pool.cls, pool.id, s.id, s.version)
        if leaf is None:
            return UNAVAILABLE
        ptr = req["pointer"]
        raised = self._raise(auth.rec, [pool])
        text = envelope.wrap(pool.id, pool.label, origin_of(leaf), "fetch", ptr, leaf.get("title", ""), leaf.get("text", ""))
        handle = hashlib.sha256(f"{leaf.get('writer', '')}\x00{pool.id}".encode()).hexdigest()[:12]   # per-pool, cannot link a writer across pools
        return {"ok": True, "status": "ok", "text": text, "origin": origin_of(leaf), "wrote_at": leaf.get("wrote_at"),
                "mode": leaf.get("mode"), "writer_handle": handle, "pool": pool.id,
                **({"taint_raised": raised} if raised else {})}

    def _raise(self, rec, pools) -> list:
        """Raise the conversation's taint for what is about to be handed off, durably, BEFORE the response goes (a failed write aborts it).
        Returns the pool ids that changed it."""
        if self.states is None:
            return []
        new = self.states.raised_by(rec, pools)
        old = self.states.get(rec)
        if new == old:
            return []
        lat = self.lattices[rec.tenant]
        self.states.raise_to(rec, new)
        return sorted(p.id for p in pools if not (old.cls is not None and lat.dominates(old.cls, p.cls) and (not p.private or p.id in old.private)))

    # ---- pool_retract (POOLS 3.5): hides an item, reversible, never erases ---------------------------------------------------------
    REASONS = ("wrong", "outdated", "harmful", "private", "other")

    def _retract_rate_ok(self, rec, pool_ids: list) -> bool:
        if self._b["ret_rate"]:
            return True
        now = self._clock()
        with self._lock:
            for key, limit in (((rec.tenant, rec.principal_token), self.c.retract_rate_per_h),
                               *(((rec.tenant, "pool", pid), self.c.retract_pool_rate_per_h) for pid in set(pool_ids))):
                w = [t for t in self._retracts.get(key, []) if t > now - 3600]
                need = pool_ids.count(key[2]) if len(key) == 3 else len(pool_ids)
                if len(w) + need > limit:
                    return False
            for key in [(rec.tenant, rec.principal_token)] + [(rec.tenant, "pool", pid) for pid in set(pool_ids)]:
                w = [t for t in self._retracts.get(key, []) if t > now - 3600]
                w += [now] * (len(pool_ids) if len(key) == 2 else pool_ids.count(key[2]))
                self._retracts[key] = w
        return True

    def retract(self, token: str, req: Mapping[str, Any]) -> dict:
        ptrs, reason, key = req.get("pointers"), req.get("reason"), req.get("key")
        if not isinstance(ptrs, list) or not ptrs or len(ptrs) > self.c.retract_max or not all(isinstance(x, str) for x in ptrs):
            return UNAVAILABLE if not isinstance(ptrs, list) else err("invalid")
        seals = [pointers.open_(self.keys, self.tenant, x) for x in ptrs]
        if any(x is None for x in seals):
            return UNAVAILABLE
        need = {x.pool: {pl.RETRACT, pl.QUERY} for x in seals}
        auth = authorize(self.sessions, self.dir, self.lattices, token, need)   # the whole call is unavailable if any pool fails
        if auth is None:
            return UNAVAILABLE
        if reason not in self.REASONS or not isinstance(key, str) or not key:
            return err("invalid")
        if not self._retract_rate_ok(auth.rec, [x.pool for x in seals]):
            return err("rate_limited")
        out = []
        for x in seals:
            pool = auth.pools[x.pool]
            head = self._head(pool.cls, pool.id, x.id)
            result = "retracted"
            if head is not None and head.source.get("status") == "live":
                cur = int(head.source["current_lv"])
                leaf = self._leaf(pool.cls, pool.id, x.id, cur)
                if leaf is None:
                    return UNAVAILABLE   # fail closed: an unread leaf is never assumed to be agent-written (a 429, a 503, a missing leaf)
                if auth.rec.actor == "agent" and origin_of(leaf) != "agent" and not self._b["ret_human"]:
                    self.w.record_retraction(pool.cls, pool.id, x.id, cur, auth.rec.principal_token, "agent", reason, "requested")
                    result = "held"       # the item stays live until a human with `review` confirms
                elif self.w.leaf_retract(pool.cls, pool.id, x.id, cur, reason):   # the head claim shared with pool_update (class C3)
                    self.w.record_retraction(pool.cls, pool.id, x.id, cur, auth.rec.principal_token, auth.rec.actor, reason, "done")
                else:
                    result = "version_conflict"   # a change of this leaf is in flight or landed first: nothing was retracted
            out.append(result)     # a leaf already retracted or gone reads the same: no existence signal
        return {"ok": True, "accepted": True, "results": out}

    # ---- held saves: list and release (POOLS 2.8) ----------------------------------------------------------------------------
    def _holds_of(self, rec, p, pool) -> list:
        out = []
        for cls in sorted(p.read_set):
            for d in self.reader.search(cls, "ctl", pool.id, [{"term": {"kind": "quarantine"}}, {"term": {"reason": "taint"}}], size=200):
                out.append((cls, d))
        return out

    def holds(self, token: str, req: Mapping[str, Any]) -> dict:
        """The held saves of a pool: id, time, state, never the text. For a principal with `review` and `query` on the pool."""
        pid = req.get("pool")
        auth = authorize(self.sessions, self.dir, self.lattices, token, {pid: {pl.REVIEW, pl.QUERY}}) if isinstance(pid, str) else None
        if auth is None:
            return UNAVAILABLE
        return {"ok": True, "holds": [{"hold": d.id.split(":")[2], "state": d.source.get("state"), "at": d.source.get("created_at")}
                                      for _, d in self._holds_of(auth.rec, auth.principal, auth.pools[pid])]}

    def _hold_auth(self, token: str, req: Mapping[str, Any]):
        """The checks of a human reviewing a hold (show and release): a token of actor `human` holding `review` and `query` on the target
        pool and `query` on every pool of the hold's private set, whose read set covers the taint class. Returns (auth, pool, cls, doc, hid)."""
        pid, hid = req.get("pool"), req.get("hold")
        if not (isinstance(pid, str) and isinstance(hid, str)):
            return None
        need = {pl.REVIEW} if self._b["review_only"] else {pl.REVIEW, pl.QUERY}
        auth = authorize(self.sessions, self.dir, self.lattices, token, {pid: need})
        if auth is None or (auth.rec.actor != "human" and not self._b["review_only"]):
            return None
        pool = auth.pools[pid]
        found = [(c, d) for c, d in self._holds_of(auth.rec, auth.principal, pool) if d.id == f"th:{pid}:{hid}"]
        if not found:
            return None
        cls, doc = found[0]
        priv = [x for x in doc.source.get("lin_pools", []) if x != pid]
        if priv and not self._b["review_only"] and authorize(self.sessions, self.dir, self.lattices, token, {x: {pl.QUERY} for x in priv}) is None:
            return None
        if not self._b["review_only"] and not decide(auth.principal, Op.RECALL, tenant=self.tenant, lattice=self.lattices[self.tenant], cls=cls):
            return None
        return auth, pool, cls, doc, hid

    def hold_show(self, token: str, req: Mapping[str, Any]) -> dict:
        """What the human is about to approve: the held text (inside the data envelope) and its content hash, which `release` must carry.
        Reading it raises this conversation's taint to the hold's class and private set, durably, before the response goes."""
        got = self._hold_auth(token, req)
        if got is None:
            return UNAVAILABLE
        auth, pool, cls, doc, hid = got
        shown = self.w.held_text(cls, pool.id, hid)
        if shown is None:
            return UNAVAILABLE
        text, digest = shown
        if self.states is not None:
            self.states.raise_to(auth.rec, pl_taint(cls, [x for x in doc.source.get("lin_pools", []) if x != pool.id], self.dir, self.tenant))
        self.w.record_event(cls, pool.id, "hold_show", auth.rec.principal_token, doc.source.get("lin_subjects", []), hold=hid, content_hash=digest)
        return {"ok": True, "hold": hid, "text": envelope.wrap(pool.id, pool.label, "agent", "fetch", "held", "held save", text), "content_hash": digest}

    def release(self, token: str, req: Mapping[str, Any]) -> dict:
        """Release (`admit`) or discard a held save. Same checks as `hold_show`. An admit must carry the content hash that `hold_show`
        returned (the human approves the content they saw), is claimed before it is written, and is logged as a gateway event (hashes and
        ids only). A discard needs no hash."""
        decision = req.get("decision")
        if decision not in ("admit", "discard"):
            return UNAVAILABLE
        got = self._hold_auth(token, req)
        if got is None:
            return UNAVAILABLE
        auth, pool, cls, doc, hid = got
        pid = pool.id
        if decision == "admit":
            shown = self.w.held_text(cls, pid, hid)
            if shown is None or not isinstance(req.get("content_hash"), str) or not hmac.compare_digest(shown[1], req["content_hash"]):
                return UNAVAILABLE   # the human approved other content than the one held: nothing is logged, nothing released
        # the approval is logged FIRST, with a stable identity, under the write gate and the active-pool check (class C4): a crash after
        # it leaves a hold that a retry finishes and the same event row; a pool deleted meanwhile refuses the event and the release
        self.w.record_event(cls, pid, "hold_" + decision, auth.rec.principal_token, doc.source.get("lin_subjects", []), event_id=hid,
                            hold=hid, content_hash=req.get("content_hash", "") if decision == "admit" else "", state="approved")
        res = self.w.release_held(cls, pid, pool.cls, hid, decision, {"released_by": auth.rec.principal_token},
                                  expect_hash=req.get("content_hash") if decision == "admit" else None)
        ok = res.status == "discarded" if decision == "discard" else res.status == wmod.STORED
        if decision == "discard":
            return {"ok": True, "status": "discarded"} if ok else UNAVAILABLE
        return self._answer(res, pid) if ok else UNAVAILABLE

    # ---- pool_query ------------------------------------------------------------------------------------------------------
    def query(self, token: str, req: Mapping[str, Any]) -> dict:
        rec = self.sessions.resolve(token)
        text, hint = req.get("query"), req.get("pools")
        if rec is None or rec.scope is None:
            return UNAVAILABLE
        if hint is not None and (not isinstance(hint, list) or not all(isinstance(h, str) for h in hint)):
            return UNAVAILABLE
        # G: the pools of the token scope with `query` that pass check 4 now; a hint outside G fails the whole call
        G = {}
        for pid, ops in rec.scope.items():
            a = authorize(self.sessions, self.dir, self.lattices, token, {pid: {pl.QUERY}}) if pl.QUERY in ops else None
            if a is not None:
                G[pid] = a.pools[pid]
        if hint:
            if any(h not in G for h in hint):
                return UNAVAILABLE
            S = sorted(set(hint))
            raised = sorted(h for h in S if G[h].private)
        else:
            S = sorted(pid for pid, pool in G.items()
                       if self._b["sel_g"] or (self.states.neutral(rec, pool) if self.states is not None else not pool.private))
            raised = []
        if not isinstance(text, str) or not text.strip() or len(text) > 4096:
            return err("invalid") if (G or hint) else UNAVAILABLE
        if not S:
            return UNAVAILABLE if not G else {"ok": True, "items": [], "pools": [], "taint_raised": raised}
        if hint and self.states is not None:
            raised = self._raise(rec, [G[h] for h in S])
        lists, info = {}, {}
        for pid in S:
            self._stage_hook(G[pid], "route")
            lists[pid] = self._pool_candidates(G[pid], text, info)
        salt = hashlib.sha256(token.encode() + text.encode()).hexdigest()[:16]
        keyed = {pid: [i for i, _ in rows] for pid, rows in lists.items()}
        merged = rrf_salted([[f"{pid}|{i}" for i in ids] for pid, ids in keyed.items()], salt, K_RRF, self.c.k_pool)
        items = []
        for ident, _ in merged[: self.c.k_total]:
            pid, leaf_ref = ident.split("|", 1)
            d = info[(pid, leaf_ref)]
            leaf_id, lv = leaf_ref.split(":")
            ptr = self._seal(pid, leaf_id, int(lv))
            title = d.get("title", "")
            if origin_of(d) not in ("human", "import") and not self._b["raw_title"]:
                title = envelope.wrap(pid, G[pid].label, "agent", "recall", ptr, title)   # built from agent-written text: inside the envelope
            items.append({"pointer": ptr, "title": title, "size": d.get("size_tokens", 0), "pool": pid, "label": G[pid].label,
                          "origin": origin_of(d), "stage": 0})
        return {"ok": True, "items": items, "pools": S, "taint_raised": raised}

    def _pool_candidates(self, pool: pl.Pool, text: str, info: dict) -> list:
        """BM25 and kNN on the pool's own leaf index, fused by rank, then the version-currency filter (POOLS 3.9): one multi-get of
        the heads of the candidates, keeping a leaf only if its `lv` is the head's `current_lv` and its status is live. Each list is cut
        at `pool_window`; if the filter leaves fewer than `k_pool`, one more page of the same size is searched."""
        idx, win = pool_index(self.tenant, pool.cls, pool.id, "leaf"), self.c.pool_window
        kept: list = []
        seen: set = set()
        pages = 1 if self._b["refill"] else 2
        for page in range(pages):
            off = page * win
            lines = [{"index": idx}, {"size": win, "from": off, "_source": ["title", "size_tokens", "origin", "pool"],
                                      "query": {"multi_match": {"query": text, "fields": ["text", "title", "entities"]}}},
                     {"index": idx}, {"size": off + win, "_source": ["title", "size_tokens", "origin", "pool"],
                                      "knn": {"field": "embedding", "query_vector": embed_stub.embed(text), "k": off + win,
                                              "num_candidates": max(2 * (off + win), 100)}}]
            self.plan.append(idx)
            st, body = self.es_for(pool.cls).request("POST", "/_msearch", lines, ndjson=True)
            if st != 200 or not isinstance(body, dict):
                break
            ranked, srcs = [], {}
            for j, resp in enumerate(body.get("responses", [])):
                ids = []
                if isinstance(resp, dict) and "error" not in resp:
                    hits = resp.get("hits", {}).get("hits", [])
                    hits = hits[off:] if j == 1 else hits   # the kNN section has no offset: its page is the tail
                    for h in hits:
                        if (h.get("_source") or {}).get("pool") != pool.id:
                            continue   # a leaf of another pool is never a candidate
                        ids.append(h["_id"])
                        srcs[h["_id"]] = h.get("_source") or {}
                ranked.append(ids)
            fused = [i for i, _ in rrf(ranked, K_RRF, win * 2)]
            fresh = [i for i in fused if i not in seen]
            seen.update(fresh)
            if self._b["head"]:
                live = fresh
            else:
                heads = self.reader.mget(pool.cls, "ctl", [f"lh:{pool.id}:{i.split(':')[0]}" for i in fresh], pool.id) if fresh else {}
                live = []
                for i in fresh:
                    h = heads.get(f"lh:{pool.id}:{i.split(':')[0]}")
                    if h is not None and int(h.source.get("current_lv", -1)) == int(i.split(":")[1]) and h.source.get("status") == "live":
                        live.append(i)
            for i in live:
                info[(pool.id, i)] = srcs[i]
                kept.append((i, 0))
            if len(kept) >= self.c.k_pool or len(fused) < win:
                break
        return kept[: max(self.c.k_pool, 1)]


class PoolRouter:
    """What `Gateway` calls for a pool request: the people operations (`PoolService`) and the tools (`PoolTools`)."""

    def __init__(self, service, tools):
        self.service, self.tools = service, tools

    def admit(self) -> bool:
        """Whether the tenant's epoch journal admits (open, lane open). A fault closes the tenant; nothing pool-related runs then."""
        return self.service.rev.admit()

    def handle(self, op: str, p, rec, req, *, token: str = ""):
        if op in ("pool_save", "pool_update", "pool_query", "pool_fetch", "pool_holds", "pool_hold_show", "pool_release", "pool_retract"):
            return self.tools.handle(op, p, rec, req, token=token)
        return self.service.handle(op, p, rec, req)
