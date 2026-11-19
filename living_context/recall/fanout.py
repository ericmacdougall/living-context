"""The owner's fan-out read path on the stub (DESIGN 8.1 to 8.4; BUILD-PLAN Step 10).

Every top-level holder of the pinned generation is asked in parallel; the moment a holder answers, each named child
that is a node is dispatched as a new holder call (expansion is asynchronous, in confidence order, bounded by
`B_HOLDERS`); named leaves become pointers. A holder is one stateless model call over at most `F_MAX` child entries
(chunks in parallel above that), the entries shown under local labels c1..cN and never carrying `last_seen`. Output is
validated against the presented labels (unknown ids dropped and counted), reasons are bounded and 5-gram checked, a
failed call falls back to embedding routing and is flagged. The parent step merges pointers, sums sizes by code and
organises when two or more children answered. What leaves is the pointer field contract of `digest.py`, nothing else.
"""
from __future__ import annotations

import concurrent.futures as cf
import heapq
import itertools
import threading
from dataclasses import dataclass, field
from typing import Any, Mapping, Optional, Protocol, Sequence

from ..config import Constants
from ..embed import stub as embed_stub
from ..ids import KIND_LEAF, KIND_NODE, Pointer, encode_pointer
from ..model.port import CallContext, ModelPort, PortRefused, Task
from . import digest as dg
from .fusion import rrf_salted


class TreeView(Protocol):
    gen: int

    def top(self) -> list: ...
    def entries(self, ref: str) -> list:
        """Child entries of a node, each {child_id, child_version, is_leaf, title, gist, entities, facts, size_tokens,
        total_tokens, pending}; pending members carry only a code-built title and their size."""


@dataclass
class Counters:
    holder_calls: int = 0
    organise_calls: int = 0
    fallbacks: int = 0
    dropped_ids: int = 0
    reason_failures: int = 0
    label_failures: int = 0
    chunks: int = 0
    budget_reached: bool = False
    port_refusals: int = 0


@dataclass
class Cand:
    leaf_id: str
    lv: int
    title: str
    size: int
    path: tuple
    reason: str
    confidence: int
    depth: int
    fallback: bool = False


@dataclass
class Result:
    digest: dict
    counters: Counters
    leaves: list = field(default_factory=list)   # ranked Cand list of the tree
    log: list = field(default_factory=list)      # per-holder records (fallback flags, chunk counts)


def present(entries: Sequence[Mapping]) -> list[dict]:
    """What a holder sees: local labels and descriptors; never ids, never `last_seen`."""
    out = []
    for n, e in enumerate(entries, start=1):
        if e.get("pending"):
            out.append({"label": f"c{n}", "title": e["title"], "size_tokens": e["size_tokens"], "pending": True})
        else:
            out.append({"label": f"c{n}", "title": e["title"], "gist": e.get("gist", ""), "entities": e.get("entities", []),
                        "facts": e.get("facts", []), "size_tokens": e["size_tokens"], "total_tokens": e.get("total_tokens", 0)})
    return out


def input_texts(shown: Sequence[Mapping]) -> list[str]:
    return [t for e in shown for t in [e.get("gist", "")] + list(e.get("facts", []))]


class Recall:
    def __init__(self, port: ModelPort, tenant: str, cls: str, principal: str, *, consts: Optional[Constants] = None,
                 workers: int = 8, salt: str = "lc", check_reasons: bool = True, reasons_pass_through: bool = False):
        self._port, self.tenant, self.cls, self.principal = port, tenant, cls, principal
        self._c = consts or Constants()
        self._workers, self._salt = workers, salt
        self._check = check_reasons

    # ---- one holder -----------------------------------------------------------------------------------------
    def _ctx(self, ref: str) -> CallContext:
        return CallContext(self.tenant, self.cls, self.principal, (self.cls,), lin_nodes=(ref,))

    def _fallback(self, prompt: str, shown: list, entries: list) -> list[tuple]:
        """Embedding routing over the children (cosine of prompt and gist/title), top 3; marked, never the method."""
        qv = embed_stub.embed(prompt)
        sc = sorted(((embed_stub.cosine(qv, embed_stub.embed(f"{s.get('title', '')} {s.get('gist', '')}")), i)
                     for i, s in enumerate(shown)), key=lambda t: (-t[0], t[1]))
        return [(i, "embedding fallback", 1) for _, i in sc[:3]]

    def ask_holder(self, ref: str, prompt: str, entries: list, cnt: Counters, lock: threading.Lock) -> tuple[list, dict]:
        """Return ([(entry index, reason, confidence)], record). Chunks of at most F_MAX entries run in parallel."""
        f_max = self._c.f_max
        chunks = [list(range(i, min(i + f_max, len(entries)))) for i in range(0, len(entries), f_max)] or [[]]
        with lock:
            cnt.holder_calls += 1
            cnt.chunks += len(chunks)
        picked: list[tuple] = []
        rec = {"node": ref, "chunks": len(chunks), "fallback": False}

        def one(idx: list) -> list:
            sub = [entries[i] for i in idx]
            shown = present(sub)
            try:
                res = self._port.call(Task.ROUTE, {"query": prompt, "children": shown, "b_fanout": self._c.b_fanout},
                                      ctx=self._ctx(ref))
            except PortRefused:
                with lock:
                    cnt.port_refusals += 1
                res = None
            if res is None or not res.ok:
                with lock:
                    cnt.fallbacks += 1
                rec["fallback"] = True
                return [(idx[i], r, c) for i, r, c in self._fallback(prompt, shown, sub)]
            labels = {s["label"]: n for n, s in enumerate(shown)}
            texts = input_texts(shown)
            out = []
            for lab in res.output["selected"]:
                if lab not in labels:  # an id that was not presented: dropped and counted
                    with lock:
                        cnt.dropped_ids += 1
                    continue
                reason = res.output["reasons"].get(lab, "")
                if self._check and not dg.reason_ok(reason, texts, self._c.reason_max_words, self._c.reason_ngram):
                    with lock:
                        cnt.reason_failures += 1
                    reason = ""
                conf = res.output.get("confidence", {}).get(lab, 1)
                out.append((idx[labels[lab]], reason, max(0, min(3, int(conf)))))
            return out

        if len(chunks) == 1:
            picked = one(chunks[0])
        else:
            with cf.ThreadPoolExecutor(len(chunks)) as ex:
                for part in ex.map(one, chunks):
                    picked += part
        return picked[: self._c.b_fanout * len(chunks)], rec

    # ---- the whole recall -----------------------------------------------------------------------------------
    def recall(self, prompt: str, view: TreeView, *, direct: Sequence[Mapping] = (), linked: Sequence[Mapping] = ()) -> Result:
        c, cnt, lock = self._c, Counters(), threading.Lock()
        tops = view.top()
        budget = len(tops) + c.b_holders_extra
        log, cands, unopened = [], [], []
        order = itertools.count()
        frontier: list = []   # (-confidence, depth, seq, ref, path)
        for ref in tops:
            heapq.heappush(frontier, (0, 1, next(order), ref, (ref,)))
        used, inflight = 0, {}
        results: dict[str, list] = {}      # holder ref(path) -> named children records for the parent step
        with cf.ThreadPoolExecutor(self._workers) as ex:
            while frontier or inflight:
                while frontier and len(inflight) < self._workers:
                    negc, depth, _, ref, path = frontier[0]
                    if used >= budget and depth > 1:
                        break
                    heapq.heappop(frontier)
                    used += 1
                    entries = view.entries(ref)
                    inflight[ex.submit(self.ask_holder, ref, prompt, entries, cnt, lock)] = (ref, path, depth, entries)
                if not inflight:
                    break
                done, _ = cf.wait(list(inflight), return_when=cf.FIRST_COMPLETED)
                for fut in done:
                    ref, path, depth, entries = inflight.pop(fut)
                    picked, rec = fut.result()
                    log.append(rec)
                    named = []
                    for idx, reason, conf in picked:
                        e = entries[idx]
                        if e["is_leaf"]:
                            cand = Cand(e["child_id"], e["child_version"], e["title"], e["size_tokens"], path, reason, conf,
                                        depth, rec["fallback"])
                            cands.append(cand)
                            named.append(("leaf", cand))
                        else:
                            child_ref = f"{e['child_id']}:{e['child_version']}"
                            heapq.heappush(frontier, (-conf, depth + 1, next(order), child_ref, path + (child_ref,)))
                            named.append(("node", child_ref))
                    results[ref] = named
            for negc, depth, _, ref, path in frontier:  # budget reached: branch not opened
                cnt.budget_reached = True
                unopened.append((ref, path))
        ranked = sorted(cands, key=lambda k: (-k.confidence, k.depth, k.path, k.leaf_id))
        seen, tree_list = set(), []
        for k in ranked:  # dedupe by leaf id, keep the best path
            if k.leaf_id not in seen:
                seen.add(k.leaf_id)
                tree_list.append(k)
        digest = self._digest(prompt, view, tree_list, unopened, direct, linked, cnt, results)
        return Result(digest, cnt, tree_list, log)

    # ---- parent step and digest ---------------------------------------------------------------------------------
    def _pointer(self, view, k: Cand) -> dict:
        return dg.make_pointer(encode_pointer(Pointer(KIND_LEAF, view.gen, k.leaf_id, k.lv)), k.title, k.size,
                               ">".join(k.path), k.reason, k.confidence)

    def _digest(self, prompt, view, tree_list, unopened, direct, linked, cnt, results) -> dict:
        c = self._c
        tree_ptrs = [self._pointer(view, k) for k in tree_list]
        by_ptr = {p["pointer"]: p for p in tree_ptrs}
        key = lambda p: p["pointer"]
        d_ptrs = [dict(p) for p in direct]
        l_ptrs = [dict(p) for p in linked]
        fused = rrf_salted([[key(p) for p in tree_ptrs], [key(p) for p in d_ptrs], [key(p) for p in l_ptrs]], self._salt)
        allp = {key(p): p for p in tree_ptrs + d_ptrs + l_ptrs}
        pointers = [allp[i] for i, _ in fused]
        outline = []
        groups: dict[str, list] = {}
        for p in tree_ptrs:
            groups.setdefault(p["path"], []).append(p)
        if len(groups) >= 2 and cnt.organise_calls < c.b_organise:
            ctx = CallContext(self.tenant, self.cls, self.principal, (self.cls,), lin_nodes=tuple(sorted({n for g in groups for n in g.split(">")})))
            res = self._port.call(Task.ORGANISE, {"pointers": [{"pointer": p["pointer"], "path": p["path"], "score": p["confidence"]}
                                                                for p in tree_ptrs]}, ctx=ctx)
            cnt.organise_calls += 1
            if res.ok:
                for o in res.output["outline"]:
                    label = o["label"]
                    if not dg.reason_ok(label, [], c.reason_max_words, c.reason_ngram):
                        cnt.label_failures += 1
                        label = "branch"
                    outline.append({"label": label, "pointers": [x for x in o["pointers"] if x in by_ptr]})
        elif groups:
            outline = [{"label": path, "pointers": [p["pointer"] for p in ps]} for path, ps in sorted(groups.items())]
        node_ptrs = []
        for ref, path in unopened:
            nid, nv = ref.split(":")
            entry = next((e for e in view.entries(path[-2]) if f"{e['child_id']}:{e['child_version']}" == ref), None) if len(path) > 1 else None
            node_ptrs.append({"pointer": encode_pointer(Pointer(KIND_NODE, view.gen, nid, int(nv))),
                              "title": entry["title"] if entry else "", "size": entry["size_tokens"] if entry else 0,
                              "total": entry.get("total_tokens", 0) if entry else 0, "path": ">".join(path)})
        d = {"outline": outline, "pointers": pointers, "sizes": {"total": sum(p["size"] for p in pointers), "count": len(pointers)},
             "node_pointers": node_ptrs, "pinned": {"gen": view.gen},
             "sources": {"tree": tree_ptrs, "direct": d_ptrs, "linked": l_ptrs},
             "counts": {"holder_calls": cnt.holder_calls, "fallbacks": cnt.fallbacks, "dropped_ids": cnt.dropped_ids}}
        dg.check_digest(d)
        return d
