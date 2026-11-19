"""Batch rebuild of one family of one class (DESIGN 7.6, 5.6, 13.2; BUILD-PLAN Step 13).

Under a lease (compare-and-set, with expiry) one builder at a time: read the head G and the write-path watermark P; the new
generation's source watermark W is that P. Every level-1 node with pending members (src_seq above W(G) and at most P) gets one
build call over the leaves it holds (the summary is refreshed when it has 4 or more new children or its size changed by 25%,
otherwise the node keeps its summary and is marked `stale`); a node over `F_MAX` children is split into balanced halves with a
forwarding record old id -> new ids; members of the old node get the new generation in their `gen_to`, members of new nodes are
written with `gen_from`; new versions are path-copied up to the top and the index-of-indexes rule is applied there. Nothing is
written before every build call has succeeded: a failed call aborts the whole generation (W is never published past a node it
did not summarise; INV-11 counts it) and the members stay pending. The commit then writes nodes, members and the manifest,
refreshes every index written (the searchable-publication barrier) and flips the head by compare-and-set; a lost flip deletes
this generation's unshared documents and the builder re-runs from the new head (rebase). Merge of an under-full node into its
sibling and collapse of single-child chains are not built (docs/decisions/design-readings.md).
"""
from __future__ import annotations

import datetime
import math
import time
from dataclasses import dataclass, field
from typing import Callable, Optional

from ..config import Constants
from ..embed import stub as embed_stub
from ..gateway.es import EsClient
from ..gateway.indices import class_index
from ..ids import new_node_id
from ..ingest import segment, title
from ..model.port import CallContext, ModelPort, Task
from . import cluster
from .build import BuildFailed, Node, Tree, build_tree, LeafRec
from .placement import NEW_PARENT
from .publish import Publisher
from .watermark import WritePath

SCHEMA_V = 1
LEASE_S = 600


class RebuildAborted(Exception):
    pass


class _Lost(Exception):
    """A document id this builder meant to create already exists: another builder owns that version."""


@dataclass
class RebuildResult:
    status: str                 # published | nothing | busy | aborted | lost
    gen: int = 0
    W: int = 0
    calls: int = 0
    rebased: int = 0
    reason: str = ""


def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def node_from_doc(ref: str, d: dict) -> Node:
    nid, nv = ref.split(":")
    return Node(nid, int(nv), int(d["level"]), list(d["children"]), d.get("summary", ""), list(d["summary_embedding"]),
                list(d.get("facts", [])), d.get("title", ""), int(d.get("total_tokens", 0)), list(d.get("lin_leaf_ids", [])),
                list(d.get("lin_nodes", [])), list(d.get("lin_subjects", [])), d.get("summary_status", "ok"))


def child_entry(n: Node) -> dict:
    return {"child_id": n.node_id, "child_version": n.nv, "is_leaf": False, "title": n.title, "gist": n.summary,
            "facts": n.facts[:3], "entities": [], "size_tokens": 0, "total_tokens": n.total_tokens}


class Rebuilder:
    def __init__(self, es: Callable[[], EsClient], leaf_es: Callable[[], EsClient], port: ModelPort, tenant: str, cls: str,
                 wp: WritePath, *, family: str = "topic", consts: Optional[Constants] = None, principal: str = "service",
                 owner: str = "builder", clock: Callable[[], float] = time.time, pause: Optional[Callable[[str], None]] = None,
                 rebase_tries: int = 5, abort_if: Optional[Callable[[], bool]] = None,
                 # the BROKEN variants of the tests:
                 refresh_before_flip: bool = True, use_cas: bool = True, publish_w_past_failed: bool = False,
                 reuse_node_ids: bool = False, ancestry_limit: Optional[int] = None):
        self._es_fn, self._leaf_fn, self._port = es, leaf_es, port
        self.tenant, self.cls, self.family, self._wp = tenant, cls, family, wp
        self._c = consts or Constants()
        self._principal, self._owner, self._clock = principal, owner, clock
        self._pause, self._tries, self._abort_if = pause or (lambda s: None), rebase_tries, abort_if
        self._refresh, self._cas, self._past_failed = refresh_before_flip, use_cas, publish_w_past_failed
        self._reuse_ids, self._anc_limit = reuse_node_ids, ancestry_limit
        self.pub = Publisher(es, tenant, cls, family=family, refresh_before_flip=refresh_before_flip, use_cas=use_cas)
        self.calls = 0
        self.failed_calls = 0   # counted (INV-11)

    @property
    def _es(self) -> EsClient:
        return self._es_fn()

    def _idx(self, kind: str) -> str:
        return class_index(self.tenant, self.cls, kind)

    # ---- the lease ----------------------------------------------------------------------------------------------
    def _lease(self) -> bool:
        doc = self.pub._base("counter", owner=self._owner, expires=self._clock() + LEASE_S)
        cur = self.pub._get("ctl", f"ls:{self.family}")
        if cur is None:
            st, _ = self.pub._put("ctl", f"ls:{self.family}", doc, create=True)
            return st in (200, 201)
        held = cur["_source"]
        if held.get("owner") != self._owner and float(held.get("expires", 0)) > self._clock():
            return False
        st, _ = self.pub._put("ctl", f"ls:{self.family}", doc, cas=(cur["_seq_no"], cur["_primary_term"]))
        return st in (200, 201)

    def _release(self) -> None:
        cur = self.pub._get("ctl", f"ls:{self.family}")
        if cur is not None and cur["_source"].get("owner") == self._owner:
            self._es.request("DELETE", f"/{self._idx('ctl')}/_doc/ls:{self.family}")

    # ---- bootstrap ---------------------------------------------------------------------------------------------------
    def leaf_recs(self, ids: Optional[list] = None) -> list[LeafRec]:
        """Leaves of the class (all current projections, or the named `id:lv`), from the leaf index."""
        out, after = [], None
        while True:
            body = {"size": 500, "sort": [{"src_seq": "asc"}],
                    "_source": ["text", "title", "size_tokens", "lin_subjects", "src_seq"],
                    "query": {"ids": {"values": ids}} if ids is not None else {"match_all": {}}}
            if after is not None:
                body["search_after"] = after
            st, r = self._leaf_fn().request("POST", f"/{self._idx('leaf')}/_search", body)
            hits = r["hits"]["hits"] if st == 200 else []
            for h in hits:
                s = h["_source"]
                lid, lv = h["_id"].rsplit(":", 1)
                out.append(LeafRec(lid, int(lv), s["text"], s["title"], embed_stub.embed(s["text"]), int(s["size_tokens"]),
                                   tuple(s.get("lin_subjects", [])), int(s["src_seq"])))
            if len(hits) < 500 or ids is not None:
                break
            after = hits[-1]["sort"]
        return out

    def bootstrap(self) -> RebuildResult:
        """The first generation: a bulk build over every leaf of the class, published with W = the highest src_seq covered."""
        self._es.request("POST", f"/{self._idx('leaf')}/_refresh")
        P = self._wp.read_p()
        leaves = [l for l in self.leaf_recs() if l.src_seq <= P]
        tree = build_tree(leaves, self._port, self.tenant, self.cls, consts=self._c, principal=self._principal)
        gen = self.pub.publish(tree, watermark=P)
        return RebuildResult("published" if gen else "lost", gen or 0, P, tree.calls)

    # ---- reading the live generation ----------------------------------------------------------------------------------
    def _load(self, top: list) -> dict:
        nodes, frontier = {}, list(top)
        while frontier:
            nxt = []
            for ref in frontier:
                d = self.pub._get("node", ref)
                if d is None:
                    raise RebuildAborted(f"node {ref} missing")
                n = node_from_doc(ref, d["_source"])
                nodes[ref] = n
                nxt += [f"{ch['child_id']}:{ch['child_version']}" for ch in n.children if not ch["is_leaf"]]
            frontier = nxt
        return nodes

    def _pending(self, W: int, P: int, anc: set) -> list:
        st, r = self._es.request("POST", f"/{self._idx('member')}/_search", {
            "size": 5000, "sort": [{"src_seq": "asc"}], "query": {"bool": {"filter": [{"range": {"src_seq": {"gt": W, "lte": P}}}]}}})
        out = []
        for h in (r["hits"]["hits"] if st == 200 else []):
            d = h["_source"]
            if anc & set(d.get("gen_to") or []):
                continue
            out.append(d)
        return out

    # ---- the pure part: plan the new nodes (model calls only) ---------------------------------------------------------
    def _call_l1(self, node_id: str, leaves: list[LeafRec]) -> dict:
        ctx = CallContext(self.tenant, self.cls, self._principal, (self.cls,), lin_leaf_ids=tuple(l.leaf_id for l in leaves),
                          lin_subjects=tuple(sorted({s for l in leaves for s in l.subjects})))
        self.calls += 1
        res = self._port.call(Task.BUILD_L1, {"leaves": [{"text": l.text, "title": l.title} for l in leaves]}, ctx=ctx)
        if not res.ok or len(res.output["entries"]) != len(leaves):
            self.failed_calls += 1
            raise BuildFailed(res.error or "build call failed")
        return res.output

    def _l1_node(self, node_id: str, nv: int, leaves: list[LeafRec], out: dict, status: str = "ok") -> Node:
        children = [{"child_id": m.leaf_id, "child_version": m.lv, "is_leaf": True, "title": m.title, "gist": e["gist"],
                     "facts": e["facts"], "entities": e["entities"], "size_tokens": m.size_tokens, "total_tokens": m.size_tokens}
                    for m, e in zip(leaves, out["entries"])]
        return Node(node_id, nv, 1, children, out["summary"], embed_stub.embed(out["summary"]),
                    [f for e in out["entries"] for f in e["facts"]][:12], title.build_title(out["summary"], n=self._c.title_terms),
                    sum(m.size_tokens for m in leaves), [m.leaf_id for m in leaves], [],
                    sorted({s for m in leaves for s in m.subjects}), status)

    def _upper_node(self, node_id: str, nv: int, level: int, kids: list[Node]) -> Node:
        ctx = CallContext(self.tenant, self.cls, self._principal, (self.cls,), lin_nodes=tuple(k.ref for k in kids),
                          lin_subjects=tuple(sorted({s for k in kids for s in k.lin_subjects})))
        self.calls += 1
        res = self._port.call(Task.BUILD_UPPER, {"children": [{"summary": k.summary, "facts": k.facts, "title": k.title} for k in kids]}, ctx=ctx)
        if not res.ok:
            self.failed_calls += 1
            raise BuildFailed(res.error or "build call failed")
        s = res.output["summary"]
        return Node(node_id, nv, level, [child_entry(k) for k in kids], s, embed_stub.embed(s), [f for k in kids for f in k.facts][:12],
                    title.build_title(s, n=self._c.title_terms), sum(k.total_tokens for k in kids), [], [k.ref for k in kids],
                    sorted({x for k in kids for x in k.lin_subjects}))

    def _plan(self, nodes: dict, top: list, pending: list) -> dict:
        c = self._c
        by_id = {n.node_id: n for n in nodes.values()}
        groups: dict[str, list] = {}
        for m in pending:
            groups.setdefault(m["parent_id"], []).append(m)
        wanted = [m["child_id"] for ms in groups.values() for m in ms]
        have = {f"{l.leaf_id}:{l.lv}": l for l in self.leaf_recs(wanted)} if wanted else {}
        changed: dict[str, list[Node]] = {}      # old ref -> replacement nodes
        created: list[Node] = []
        forward: dict[str, list[str]] = {}
        new_members: list[tuple] = []            # (node id, leaf id, lv)
        retire_parents: list[str] = []
        failed_nodes: list[str] = []
        new_top_nodes: list[Node] = []

        def do_leaf_group(old: Optional[Node], leaves: list[LeafRec], new_count: int):
            try:
                members, size_old = leaves, 0
                if old is not None:
                    known = {f"{ch['child_id']}:{ch['child_version']}": ch for ch in old.children}
                    allrecs = self.leaf_recs([k for k in known]) if known else []
                    members = allrecs + [l for l in leaves if f"{l.leaf_id}:{l.lv}" not in known]
                    size_old = old.total_tokens
                total = sum(l.size_tokens for l in members)
                over = len(members) > c.f_max
                if over:
                    groups_ix = cluster.balanced_groups([l.embedding for l in members], max(1, math.ceil(len(members) / 2)))
                    outs = []
                    for gi in groups_ix:
                        part = [members[i] for i in gi]
                        nid = old.node_id if (self._reuse_ids and not outs and old is not None) else new_node_id()
                        outs.append(self._l1_node(nid, 1, part, self._call_l1(nid, part)))
                    if old is not None:
                        changed[old.ref] = outs
                        forward[old.node_id] = [n.node_id for n in outs]
                        retire_parents.append(old.node_id)
                    created.extend(outs)
                    for n in outs:
                        new_members.extend((n.node_id, ch["child_id"], ch["child_version"]) for ch in n.children)
                    return
                refresh = old is None or new_count >= 4 or abs(total - size_old) >= 0.25 * max(size_old, 1)
                if refresh:
                    n = self._l1_node(old.node_id if old else new_node_id(), (old.nv + 1) if old else 1, members,
                                      self._call_l1("n", members))
                    if old is None:
                        new_members.extend((n.node_id, m.leaf_id, m.lv) for m in members)
                else:
                    # the summary is kept and marked stale; the new children are entered from one call over them only
                    news = [l for l in members if f"{l.leaf_id}:{l.lv}" not in {f"{ch['child_id']}:{ch['child_version']}" for ch in old.children}]
                    out = self._call_l1("n", news)
                    kept = [dict(ch) for ch in old.children]
                    add = self._l1_node(old.node_id, old.nv + 1, news, out).children
                    n = Node(old.node_id, old.nv + 1, 1, kept + add, old.summary, old.summary_embedding, old.facts, old.title,
                             old.total_tokens + sum(l.size_tokens for l in news), old.lin_leaf_ids + [l.leaf_id for l in news],
                             old.lin_nodes, sorted({*old.lin_subjects, *(s for l in news for s in l.subjects)}), "stale")
                created.append(n)
                if old is not None:
                    changed[old.ref] = [n]
                else:
                    new_top_nodes.append(n)
            except BuildFailed:
                failed_nodes.append(old.node_id if old else NEW_PARENT)
                if not self._past_failed:
                    raise

        for parent, ms in sorted(groups.items()):
            recs = [have[m["child_id"]] for m in ms if m["child_id"] in have]
            if parent == NEW_PARENT:
                # new level-1 nodes: balanced groups of the leaves that cleared no node
                ix = cluster.balanced_groups([l.embedding for l in recs], c.f_target) if recs else []
                for gi in ix:
                    do_leaf_group(None, [recs[i] for i in gi], len(gi))
                continue
            old = by_id.get(parent)
            if old is None:
                continue
            do_leaf_group(old, recs, len(recs))

        # path copy: replace changed children in their parents up to the top
        level_nodes: dict[int, list[Node]] = {}
        for n in nodes.values():
            level_nodes.setdefault(n.level, []).append(n)
        top_level = max((nodes[r].level for r in top), default=1)
        for level in range(2, top_level + 1):
            for u in sorted(level_nodes.get(level, []), key=lambda n: n.node_id):
                if not any(f"{ch['child_id']}:{ch['child_version']}" in changed for ch in u.children):
                    continue
                kids: list[Node] = []
                for ch in u.children:
                    ref = f"{ch['child_id']}:{ch['child_version']}"
                    kids += changed.get(ref, [nodes[ref]])
                if len(kids) > c.f_max:
                    ix = cluster.balanced_groups([k.summary_embedding for k in kids], max(1, math.ceil(len(kids) / 2)))
                    outs = [self._upper_node(new_node_id(), 1, level, [kids[i] for i in gi]) for gi in ix]
                    changed[u.ref] = outs
                    forward[u.node_id] = [n.node_id for n in outs]
                    created.extend(outs)
                else:
                    n = self._upper_node(u.node_id, u.nv + 1, level, kids)
                    changed[u.ref] = [n]
                    created.append(n)
        new_top: list[Node] = []
        for ref in top:
            new_top += changed.get(ref, [nodes[ref]])
        new_top += new_top_nodes   # nodes started from members that cleared no node join the top level (level 1 trees)
        if new_top_nodes and top_level > 1:
            raise RebuildAborted("a pending-new node in a tree taller than one level needs a parent (not built)")
        # the index-of-indexes rule at the top
        def too_big(ns):
            return len(ns) > c.f_max or sum(segment.tokens_of(n.summary, c.chars_per_token_bound) for n in ns) > c.h_ctx
        level = top_level
        while too_big(new_top) and len(new_top) > 1:
            ix = cluster.balanced_groups([n.summary_embedding for n in new_top], c.f_target)
            up = [self._upper_node(new_node_id(), 1, level + 1, [new_top[i] for i in gi]) for gi in ix]
            created.extend(up)
            new_top, level = up, level + 1
        return {"created": created, "top": new_top, "forward": forward, "new_members": new_members, "retire": retire_parents,
                "failed": failed_nodes, "levels": level}

    # ---- the commit ---------------------------------------------------------------------------------------------------
    def _attempt(self) -> RebuildResult:
        head = self.pub.head()
        if head is None:
            raise RebuildAborted("no head: bootstrap first")
        mf = self.pub._get("ctl", f"mf:{self.family}:{head['gen']}")["_source"]
        W, anc = int(mf["W"]), {int(g) for g in mf["ancestry"]}
        self._es.request("POST", f"/{self._idx('member')}/_refresh")
        P = self._wp.read_p()
        pending = self._pending(W, P, anc)
        if not pending:
            return RebuildResult("nothing", head["gen"], W)
        nodes = self._load(mf["top"])
        before = self.calls
        try:
            plan = self._plan(nodes, list(mf["top"]), pending)
        except BuildFailed as e:
            return RebuildResult("aborted", head["gen"], W, self.calls - before, reason=f"build call failed: {e}")
        if plan["failed"] and not self._past_failed:
            return RebuildResult("aborted", head["gen"], W, self.calls - before, reason="build call failed")
        gen = self.pub.next_gen()
        written: list[tuple[str, str]] = []
        touched = set()

        def put(kind, doc_id, doc, create=True):
            st, _ = self.pub._put(kind, doc_id, doc, create=create)
            if st == 409:   # not ours: never delete it on a discard, and never publish a node version another builder wrote
                raise _Lost()
            written.append((kind, doc_id))
            touched.add(kind)

        retired = []
        try:
            self._write(plan, gen, put, retired, touched)
        except _Lost:
            self._discard(gen, written, retired)
            return RebuildResult("lost", head["gen"], W, self.calls - before)
        return self._flip(head, plan, gen, W, P, anc, written, retired, touched, before)

    def _write(self, plan, gen, put, retired, touched) -> None:
        for n in plan["created"]:
            put("node", n.ref, self.pub._node_doc(n), create=not self._reuse_ids)
        for node_id, leaf_id, lv in plan["new_members"]:
            put("member", f"{node_id}>{leaf_id}:{lv}@{gen}", self.pub._base(
                "member", lin_leaf_ids=[leaf_id], lin_nodes=[node_id], parent_id=node_id, child_id=f"{leaf_id}:{lv}", score=1.0,
                shaped_by="build", gen_from=gen, gen_to=[]))
        for old, new in plan["forward"].items():
            put("ctl", f"fw:{self.family}:{gen}:{old}", self.pub._base("manifest", lin_nodes=[old], family=self.family, gen=gen,
                                                                         forward=True, old_id=old, new_ids=new))
        retired.extend(plan["retire"])   # members of the replaced nodes: this generation joins their gen_to set
        if retired:
            self._es.request("POST", f"/{self._idx('member')}/_update_by_query?refresh=false&conflicts=proceed", {
                "query": {"bool": {"filter": [{"terms": {"parent_id": retired}}]}},
                "script": {"lang": "painless", "source": "if (ctx._source.gen_to == null) { ctx._source.gen_to = []; } ctx._source.gen_to.add(params.g);",
                           "params": {"g": gen}}})
            touched.add("member")

    def _flip(self, head, plan, gen, W, P, anc, written, retired, touched, before) -> RebuildResult:
        def put(kind, doc_id, doc, create=True):
            self.pub._put(kind, doc_id, doc, create=create)
            written.append((kind, doc_id))
            touched.add(kind)

        ancestry = sorted(anc | {gen})
        if self._anc_limit is not None:   # BROKEN variant: ancestry truncated to retention
            ancestry = ancestry[-self._anc_limit:]
        top = [n.ref for n in plan["top"]]
        newW = P if not plan["failed"] else W
        if self._past_failed:
            newW = P   # BROKEN variant: the advanced W is published although a node kept its old version
        mfd = self.pub._base("manifest", lin_nodes=top or ["none"], family=self.family, gen=gen, top=top, W=newW,
                             parent_gen=head["gen"], ancestry=ancestry, status="candidate", levels=plan["levels"], tombstone_seq=0,
                             created_nodes=[n.ref for n in plan["created"]])
        put("ctl", f"mf:{self.family}:{gen}", mfd)
        if self._abort_if is not None and self._abort_if():
            self._discard(gen, written, retired)
            return RebuildResult("aborted", head["gen"], W, self.calls - before, reason="a tombstone intersects the inputs")
        if self._refresh:   # searchable publication: nothing flips before every index written has been refreshed
            for kind in sorted(touched):
                self._es.request("POST", f"/{self._idx(kind)}/_refresh")
        self._pause("refreshed")
        hd = self.pub._base("head", lin_nodes=top or ["none"], family=self.family, gen=gen, ancestry=ancestry)
        st, _ = self.pub._put("ctl", f"hd:{self.family}", hd, cas=(head["_seq_no"], head["_primary_term"]))
        if st == 409:
            self._discard(gen, written, retired)
            return RebuildResult("lost", head["gen"], W, self.calls - before)
        self.pub._put("ctl", f"mf:{self.family}:{gen}", dict(mfd, status="live"))
        old_mf = self.pub._get("ctl", f"mf:{self.family}:{head['gen']}")
        if old_mf is not None:
            self.pub._put("ctl", f"mf:{self.family}:{head['gen']}", dict(old_mf["_source"], status="retired", retired_by=gen))
        return RebuildResult("published", gen, newW, self.calls - before)

    def _discard(self, gen: int, written: list, retired: list) -> None:
        """A lost or abandoned candidate: its unshared nodes, members and forwarding records go, and its number leaves the
        `gen_to` set of the members it retired (they stay in the live tree)."""
        for kind, doc_id in written:
            self._es.request("DELETE", f"/{self._idx(kind)}/_doc/{doc_id}")
        if retired:
            self._es.request("POST", f"/{self._idx('member')}/_refresh")
            self._es.request("POST", f"/{self._idx('member')}/_update_by_query?refresh=true&conflicts=proceed", {
                "query": {"term": {"gen_to": gen}},
                "script": {"lang": "painless", "source": "ctx._source.gen_to.removeIf(x -> x == params.g);", "params": {"g": gen}}})
        for kind in ("node", "member", "ctl"):
            self._es.request("POST", f"/{self._idx(kind)}/_refresh")

    def run(self) -> RebuildResult:
        if not self._lease():
            return RebuildResult("busy")
        rebased = 0
        try:
            for _ in range(self._tries):
                r = self._attempt()
                if r.status != "lost":
                    r.rebased = rebased
                    return r
                rebased += 1
            return RebuildResult("lost", rebased=rebased, reason="rebase tries exhausted")
        finally:
            self._release()
