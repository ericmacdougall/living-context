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
from .sessions import Sessions

UNAVAILABLE = pl.UNAVAILABLE
TOKEN_SHAPE = re.compile(r"[a-z0-9]{4,16}\.[0-9a-f]{64}")
ORIGIN_RANK = {"agent": 0, "import": 1, "human": 2}   # the least trusted wins (POOLS 3.8)


def err(code: str, **extra) -> dict:
    return {"ok": False, "error": code, **extra}


def least_trusted(*origins: str) -> str:
    return min(origins, key=lambda o: ORIGIN_RANK.get(o, 0))


class PoolTools:
    def __init__(self, tenant: str, directory: pl.PoolDirectory, sessions: Sessions, lattices: Mapping, writer: wmod.Writer,
                 reader: ClassReader, es_for: Callable[[str], Any], keys: pointers.PointerKeys, *,
                 consts: Optional[Constants] = None, clock: Callable[[], float] = time.time, model: Any = None,
                 broken_no_head_filter: bool = False, broken_pool_from_head: bool = False, broken_origin_from_editor: bool = False,
                 broken_key_scope_without_conversation: bool = False, broken_no_refill: bool = False,
                 broken_raw_title: bool = False, broken_best_match_pool: bool = False):
        self.tenant, self.dir, self.sessions, self.lattices, self.w, self.reader = tenant, directory, sessions, lattices, writer, reader
        self.es_for, self.keys, self.c, self._clock = es_for, keys, consts or Constants(), clock
        self._model = model   # never called: stage 0 costs no model call (a test passes a port that fails if it is touched)
        self._b = dict(head=broken_no_head_filter, pool_head=broken_pool_from_head, origin=broken_origin_from_editor,
                       key=broken_key_scope_without_conversation, refill=broken_no_refill, title=broken_raw_title,
                       best=broken_best_match_pool)
        self._lock = threading.Lock()
        self._saves: dict[tuple, list] = {}
        self._update_keys: dict[tuple, tuple] = {}
        self.plan: list[str] = []    # every index an operation named, for the request-plan assertions

    # ---- routing ---------------------------------------------------------------------------------------------------------
    def handle(self, op: str, p, rec, req: Mapping[str, Any], *, token: str) -> dict:
        try:
            return {"pool_save": self.save, "pool_update": self.update, "pool_query": self.query, "pool_fetch": self.fetch}[op](token, req)
        except (ReaderRefused, wmod.WriterError):
            return UNAVAILABLE

    # ---- helpers ---------------------------------------------------------------------------------------------------------
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
        scope_key = key if self._b["key"] else f"{rec.conversation}\x00{key}"   # (tenant, principal, conversation, key)
        res = self.w.remember(rec.principal_token, pool.cls, req["text"], key=scope_key, pool=pool.id,
                              prov={**self._prov(rec, "save"), "origin": "human" if rec.actor == "human" else "agent"})
        return self._answer(res, pool.id)

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
        old_origin = old.get("origin", "agent")
        if mode == "correct" and rec.actor == "agent" and old_origin != "agent":
            return err("invalid")   # an agent corrects only what an agent wrote; it may supersede instead
        writer_origin = "human" if rec.actor == "human" else "agent"
        origin = writer_origin if self._b["origin"] else least_trusted(writer_origin, old_origin)
        if mode == "correct":
            res = self.w.remember(rec.principal_token, pool.cls, req["text"], pool=pool.id, _version=(s.id, cur + 1),
                                  prov={**self._prov(rec, "correct"), "origin": origin})
            if res.status != wmod.STORED:
                return self._answer(res, pool.id)
            now_head = self._head(pool.cls, pool.id, s.id)
            if now_head is None or int(now_head.source["current_lv"]) != cur + 1:
                return err("version_conflict", current=self._seal(pool.id, s.id, int(now_head.source["current_lv"]) if now_head else cur))
            out = {"ok": True, "pointer": self._seal(pool.id, s.id, cur + 1), "pool": pool.id}
        else:
            res = self.w.remember(rec.principal_token, pool.cls, req["text"], pool=pool.id,
                                  prov={**self._prov(rec, "supersede", supersedes=s.id), "origin": origin})
            if res.status != wmod.STORED:
                return self._answer(res, pool.id)
            if not self.w.update_head(pool.cls, pool.id, s.id, cur, status="superseded", superseded_by=res.leaf_id):
                return err("version_conflict", current=self._seal(pool.id, s.id, cur))
            out = {"ok": True, "pointer": self._seal(pool.id, res.leaf_id, res.lv), "pool": pool.id,
                   "old": {"status": "superseded", "newer": self._seal(pool.id, res.leaf_id, res.lv)}}
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
        if s.version > cur or status != "live":
            return UNAVAILABLE
        leaf = self._leaf(pool.cls, pool.id, s.id, s.version)
        if leaf is None:
            return UNAVAILABLE
        ptr = req["pointer"]
        text = envelope.wrap(pool.id, pool.label, leaf.get("origin", "agent"), "fetch", ptr, leaf.get("title", ""), leaf.get("text", ""))
        handle = hashlib.sha256(f"{leaf.get('writer', '')}\x00{pool.id}".encode()).hexdigest()[:12]   # per-pool, cannot link a writer across pools
        return {"ok": True, "status": "ok", "text": text, "origin": leaf.get("origin", "agent"), "wrote_at": leaf.get("wrote_at"),
                "mode": leaf.get("mode"), "writer_handle": handle, "pool": pool.id}

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
            S, raised = sorted(pid for pid, pool in G.items() if not pool.private), []
        if not isinstance(text, str) or not text.strip() or len(text) > 4096:
            return err("invalid") if (G or hint) else UNAVAILABLE
        if not S:
            return UNAVAILABLE if not G else {"ok": True, "items": [], "pools": [], "taint_raised": raised}
        lists, info = {}, {}
        for pid in S:
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
            if d.get("origin", "agent") == "agent" and not self._b["title"]:
                title = envelope.wrap(pid, G[pid].label, "agent", "recall", ptr, title)   # built from agent-written text: inside the envelope
            items.append({"pointer": ptr, "title": title, "size": d.get("size_tokens", 0), "pool": pid, "label": G[pid].label,
                          "origin": d.get("origin", "agent"), "stage": 0})
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

    def handle(self, op: str, p, rec, req, *, token: str = ""):
        if op in ("pool_save", "pool_update", "pool_query", "pool_fetch"):
            return self.tools.handle(op, p, rec, req, token=token)
        return self.service.handle(op, p, rec, req)
