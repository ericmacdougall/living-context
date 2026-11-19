"""Graph edges: a minimal real producer on the stub (DESIGN 5.5, 7.7, 16; BUILD-PLAN Step 12).

An edge version is one document `edge_id:ev` in the edge index of the JOIN class of both endpoints and the evidence (INV-3), so
a reader who cannot read one endpoint cannot see the edge. `edge_id` is deterministic from the tenant, the endpoints and the
type; the version counter of an edge lives in a kept `ec:<edge_id>` document (ids and a number only), so a re-created edge,
including one re-created after an erasure deleted its versions, always starts above every version that ever existed. An edge
written on insert carries `src_seq` and never a generation number; a rebuild or reorganiser edge carries `gen_from`. Decay,
hub nodes and personalised ranking come after H1 behind the same interface; until then the degree cap only refuses edges past
it, counted.
"""
from __future__ import annotations

import datetime
import random
from collections import Counter
from dataclasses import dataclass, field
from typing import Callable, Iterable, Optional

from ..config import Constants
from ..embed import stub as embed_stub
from ..gateway import validator
from ..gateway.es import EsClient
from ..gateway.indices import TENANT_CLS, class_index, tenant_index
from ..ids import edge_id as make_edge_id
from ..model.port import STUB_ID, CallContext, ModelPort, Task
from ..policy import ClassLattice
from ..structure.watermark import Job, WritePath
from . import entities as entmod

SCHEMA_V = 1
CAS_TRIES = 50
DEGREE_CAP = 50              # ASSUMPTION (DESIGN 16.4): per type per leaf
RELATED_MIN_SIM = 0.25       # ASSUMPTION: the least cosine for an untyped `related` edge from similarity alone
HALF_LIFE_DAYS = 30.0        # ASSUMPTION (DESIGN 16.4)
TYPED_SLOTS = 3              # DESIGN 7.7: the top 3 are typed
PAIRS_PER_CALL = 20          # DESIGN 7.7

# type: (initial weight, directed, decays)   DESIGN 16.2
TYPES = {"same_entity": (0.6, False, True), "same_event": (0.8, False, True), "continues": (0.9, True, False),
         "supersedes": (1.0, True, False), "contradicts": (1.0, False, False), "causes": (0.7, True, True),
         "related": (0.3, False, True), "co_used": (0.5, False, True)}
MIN_TYPE_CONFIDENCE = 0.5    # a type assigned below this is stored as `related`


@dataclass(frozen=True)
class Leaf:
    leaf_id: str
    lv: int
    cls: str
    subjects: tuple = ()


@dataclass
class LinkStats:
    written: int = 0
    exists: int = 0
    cap_refused: int = 0
    self_loops: int = 0
    cycles: int = 0
    typed_calls: int = 0
    candidates: int = 0
    failed: int = 0


def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


class EdgeStore:
    def __init__(self, es: Callable[[], EsClient], tenant: str, lattice: ClassLattice, *, wp: Optional[WritePath] = None,
                 degree_cap: int = DEGREE_CAP, store_in_source_class: bool = False, random_edges: Optional[random.Random] = None,
                 leaf_pool: Optional[Callable[[], list]] = None):
        self._es_fn, self.tenant, self._lat, self._wp = es, tenant, lattice, wp
        self._cap = degree_cap
        self._src_class = store_in_source_class   # BROKEN variant: the edge sits in the source leaf's class (SEC-A2 red)
        self._rand, self._pool = random_edges, leaf_pool  # BROKEN variant RANDOM-EDGES: the destination is a random leaf
        self.stats = LinkStats()
        self._own_deg: Counter = Counter()      # (src, type) -> edges this writer wrote
        self._own_sup: dict = {}                # src -> destinations of the supersedes edges this writer wrote

    @property
    def _es(self) -> EsClient:
        return self._es_fn()

    def join_class(self, a: Leaf, b: Leaf) -> str:
        return a.cls if self._src_class else self._lat.join(a.cls, b.cls)

    # ---- the version counter (kept through erasure) ------------------------------------------------------------
    def _next_version(self, eid: str) -> int:
        idx = tenant_index(self.tenant, "ctl")
        for _ in range(CAS_TRIES):
            st, cur = self._es.request("GET", f"/{idx}/_doc/ec:{eid}")
            base = {"tenant": self.tenant, "cls": TENANT_CLS, "kind": "counter", "lin_leaves": [], "lin_leaf_ids": [],
                    "lin_nodes": [], "lin_subjects": [], "lin_queries": [], "derivation": "edge", "erasure_rewrite": 0,
                    "created_at": _now(), "schema_v": SCHEMA_V}
            if st == 404:
                st2, _ = self._es.request("PUT", f"/{idx}/_doc/ec:{eid}?op_type=create", dict(base, value=1))
                if st2 in (200, 201):
                    return 1
                continue
            v = int(cur["_source"]["value"]) + 1
            st2, _ = self._es.request("PUT", f"/{idx}/_doc/ec:{eid}?if_seq_no={cur['_seq_no']}&if_primary_term={cur['_primary_term']}",
                                      dict(base, value=v))
            if st2 in (200, 201):
                return v
        raise RuntimeError("edge counter contended")

    # ---- reads over every class's edge index (structure key) ---------------------------------------------------
    def _all_edges(self) -> str:
        return f"lc-{self.tenant}-*-edge"

    def live_exists(self, eid: str) -> bool:
        """Realtime: the latest version of the edge (from the kept counter) is read by id in each class's edge index; a
        search would miss a version that has not been refreshed yet."""
        st, cur = self._es.request("GET", f"/{tenant_index(self.tenant, 'ctl')}/_doc/ec:{eid}")
        if st != 200:
            return False
        ev = int(cur["_source"]["value"])
        for c in self._lat.classes:
            st, r = self._es.request("GET", f"/{class_index(self.tenant, c, 'edge')}/_doc/{eid}:{ev}")
            if st == 200 and r.get("found") and not r["_source"].get("gen_to"):
                return True
        return False

    def degree(self, src_id: str, etype: str) -> int:
        """Live edges of this type from the leaf: the search count (older documents) or this writer's own count of what it
        has written (not yet searchable), whichever is larger."""
        st, r = self._es.request("POST", f"/{self._all_edges()}/_count", {"query": {"bool": {
            "filter": [{"term": {"src_id": src_id}}, {"term": {"type": etype}}],
            "must_not": [{"exists": {"field": "gen_to"}}]}}})
        return max(int(r.get("count", 0)) if st == 200 else 0, self._own_deg[(src_id, etype)])

    def _supersedes_reaches(self, start: str, goal: str, limit: int = 64) -> bool:
        """Is `goal` reachable from `start` along live supersedes edges? Searched edges plus this writer's own (not yet
        searchable) ones."""
        seen, frontier = {start}, [start]
        for _ in range(limit):
            if not frontier:
                return False
            dsts = [d for f in frontier for d in self._own_sup.get(f, ())]
            st, r = self._es.request("POST", f"/{self._all_edges()}/_search", {"size": 1000, "_source": ["dst_id"], "query": {"bool": {
                "filter": [{"terms": {"src_id": frontier}}, {"term": {"type": "supersedes"}}],
                "must_not": [{"exists": {"field": "gen_to"}}]}}})
            dsts += [h["_source"]["dst_id"] for h in (r["hits"]["hits"] if st == 200 else [])]
            nxt = []
            for d in dsts:
                if d == goal:
                    return True
                if d not in seen:
                    seen.add(d)
                    nxt.append(d)
            frontier = nxt
        return False

    # ---- writing ----------------------------------------------------------------------------------------------
    def link(self, src: Leaf, dst: Leaf, etype: str, *, weight: Optional[float] = None, confidence: float = 0.8,
             evidence: Iterable[str] = (), src_seq: Optional[int] = None, gen_from: Optional[int] = None,
             job: Optional[Job] = None, similarity: float = 0.0, model: str = STUB_ID) -> list[str]:
        """Write the edge (both directed documents for an undirected type). Returns the ids written (`edge_id:ev`)."""
        if etype not in TYPES:
            raise ValueError("unknown edge type")
        if (src_seq is None) == (gen_from is None):
            raise ValueError("an edge is bound by exactly one of src_seq (insert) or gen_from (rebuild)")
        if src.leaf_id == dst.leaf_id:
            self.stats.self_loops += 1
            return []
        w0, directed, decays = TYPES[etype]
        if etype == "supersedes" and self._supersedes_reaches(dst.leaf_id, src.leaf_id):
            etype, (w0, directed, decays), flagged = "contradicts", TYPES["contradicts"], True   # DESIGN 16.3: a cycle
            self.stats.cycles += 1
        else:
            flagged = False
        if confidence < MIN_TYPE_CONFIDENCE and etype not in ("related", "supersedes", "contradicts"):
            etype = "related"
            w0, directed, decays = TYPES["related"]
        if weight is None:
            weight = w0 * (similarity if etype == "related" else 1.0)
        pairs = [(src, dst)] if directed else [(src, dst), (dst, src)]
        out: list[str] = []
        for a, b in pairs:
            if self._rand is not None and self._pool is not None:   # RANDOM-EDGES: same writer, random destination
                pool = [x for x in self._pool() if x.leaf_id != a.leaf_id]
                if pool:
                    b = self._rand.choice(pool)
            eid = make_edge_id(self.tenant, a.leaf_id, b.leaf_id, etype)
            if self.live_exists(eid):
                self.stats.exists += 1
                continue
            if self.degree(a.leaf_id, etype) >= self._cap:
                self.stats.cap_refused += 1
                continue
            ev = self._next_version(eid)
            cls = self.join_class(a, b)
            subjects = sorted({*a.subjects, *b.subjects})
            doc = {"tenant": self.tenant, "cls": cls, "kind": "edge", "lin_leaves": [], "lin_leaf_ids": sorted({a.leaf_id, b.leaf_id}),
                   "lin_nodes": [], "lin_subjects": subjects, "lin_queries": [], "derivation": "link",
                   "erasure_rewrite": 0, "created_at": _now(), "schema_v": SCHEMA_V, "edge_id": eid, "ev": ev,
                   "src_id": a.leaf_id, "dst_id": b.leaf_id, "src_lv": a.lv, "dst_lv": b.lv, "type": etype,
                   "weight": float(weight), "confidence": float(confidence), "model": model, "hit_confirmations": 0,
                   "half_life_days": HALF_LIFE_DAYS if decays else 0.0, "evidence": list(evidence), "valid_from": _now(),
                   "cycle": flagged}
            if src_seq is not None:
                doc["src_seq"] = int(src_seq)
            else:
                doc["gen_from"] = int(gen_from)
            idx = class_index(self.tenant, cls, "edge")
            did = f"{eid}:{ev}"
            if job is not None and self._wp is not None:
                ok = self._wp.write(job, idx, did, doc)
            else:
                validator.validate(doc, session_tenant=self.tenant, index=idx, index_tenant=self.tenant, index_cls=cls)
                st, _ = self._es.request("PUT", f"/{idx}/_doc/{did}?op_type=create", doc)
                ok = st in (200, 201)
            if ok:
                self.stats.written += 1
                self._own_deg[(a.leaf_id, etype)] += 1
                if etype == "supersedes":
                    self._own_sup.setdefault(a.leaf_id, set()).add(b.leaf_id)
                out.append(did)
            else:
                self.stats.failed += 1
        return out

    def retire(self, index: str, doc_id: str, gen: int) -> bool:
        """Retirement of an edge version: the retiring generation is added to its `gen_to` set (never a delete)."""
        st, _ = self._es.request("POST", f"/{index}/_update/{doc_id}", {"script": {
            "lang": "painless", "source": "if (ctx._source.gen_to == null) { ctx._source.gen_to = []; } "
                                          "if (!ctx._source.gen_to.contains(params.g)) { ctx._source.gen_to.add(params.g); }",
            "params": {"g": int(gen)}}})
        return st == 200


class LinkJob:
    """DESIGN 7.7: for a new leaf, its top-10 kNN neighbours (any class: the edge lands in the join class) plus the leaves
    that share an entity; the top 3 are typed by one stub call per 20 pairs, the rest may get an untyped `related` edge from
    similarity alone. Entity-sharing candidates take the typing slots first (ASSUMPTION, docs/decisions/design-readings.md)."""

    def __init__(self, store: EdgeStore, leaf_es: Callable[[], EsClient], port: ModelPort, tenant: str, classes: Iterable[str], *,
                 consts: Optional[Constants] = None, principal: str = "linker", embed: Callable = embed_stub.embed):
        self._store, self._leaf_es, self._port, self.tenant = store, leaf_es, port, tenant
        self._classes = list(classes)
        self._c = consts or Constants()
        self._principal, self._embed = principal, embed

    def _search(self, body: dict) -> list:
        idx = ",".join(class_index(self.tenant, c, "leaf") for c in self._classes)
        st, r = self._leaf_es().request("POST", f"/{idx}/_search", body)
        return r["hits"]["hits"] if st == 200 else []

    def run(self, job: Optional[Job], cls: str, leaf_id: str, lv: int, *, src_seq: int) -> LinkStats:
        stats = self._store.stats
        st, doc = self._leaf_es().request("GET", f"/{class_index(self.tenant, cls, 'leaf')}/_doc/{leaf_id}:{lv}")
        if st != 200:
            stats.failed += 1
            return stats
        d = doc["_source"]
        me = Leaf(leaf_id, lv, cls, tuple(d.get("lin_subjects", [])))
        text, ents = d["text"], list(d.get("entities", []))
        not_me = {"bool": {"must_not": [{"term": {"lin_leaf_ids": leaf_id}}]}}
        src = ["text", "entities", "lin_subjects", "cls", "title"]
        cands: dict[str, dict] = {}
        for h in self._search({"size": 11, "_source": src, "knn": {"field": "embedding", "query_vector": self._embed(text), "k": 11,
                                                                      "num_candidates": 100, "filter": not_me}}):
            cands.setdefault(h["_id"].rsplit(":", 1)[0], dict(h["_source"], lv=int(h["_id"].rsplit(":", 1)[1]), shared=False))
        if ents:
            for h in self._search({"size": 20, "_source": src, "query": {"bool": {"filter": [{"terms": {"entities": ents}}],
                                                                                     "must_not": [{"term": {"lin_leaf_ids": leaf_id}}]}}}):
                lid = h["_id"].rsplit(":", 1)[0]
                cands.setdefault(lid, dict(h["_source"], lv=int(h["_id"].rsplit(":", 1)[1]), shared=False))["shared"] = True
        me_vec = self._embed(text)
        ranked = sorted(((embed_stub.cosine(me_vec, self._embed(c["text"])), lid, c) for lid, c in cands.items()),
                        key=lambda t: (not t[2]["shared"], -t[0], t[1]))
        stats.candidates += len(ranked)
        typed = ranked[:TYPED_SLOTS]
        types: dict[str, str] = {}
        for i in range(0, len(typed), PAIRS_PER_CALL):
            chunk = typed[i:i + PAIRS_PER_CALL]
            items = [{"label": "c0", "text": text, "entities": ents}] + [
                {"label": f"c{n}", "text": c["text"], "entities": list(c.get("entities", []))} for n, (_, _, c) in enumerate(chunk, 1)]
            classes_in = tuple(sorted({cls, *(c["cls"] for _, _, c in chunk)}))
            ctx = CallContext(self.tenant, self._store._lat.join(*classes_in), self._principal, classes_in,
                              lin_leaf_ids=tuple([leaf_id] + [lid for _, lid, _ in chunk]),
                              lin_subjects=tuple(sorted({*me.subjects, *(s for _, _, c in chunk for s in c.get("lin_subjects", []))})))
            res = self._port.call(Task.TYPE_EDGES, {"items": items}, ctx=ctx)
            stats.typed_calls += 1
            if not res.ok:
                stats.failed += 1
                continue
            for e in res.output["edges"]:
                if e["src"] == "c0" and e["dst"].startswith("c"):
                    types.setdefault(chunk[int(e["dst"][1:]) - 1][1], e["type"])
                elif e["dst"] == "c0" and e["src"].startswith("c"):
                    types.setdefault(chunk[int(e["src"][1:]) - 1][1], e["type"])
        for n, (sim, lid, c) in enumerate(ranked[:10]):
            other = Leaf(lid, c["lv"], c["cls"], tuple(c.get("lin_subjects", [])))
            etype = types.get(lid) or ("related" if sim >= RELATED_MIN_SIM else None)
            if etype is None:
                continue
            self._store.link(me, other, etype, similarity=max(sim, 0.0), src_seq=src_seq, job=job)
        return stats
