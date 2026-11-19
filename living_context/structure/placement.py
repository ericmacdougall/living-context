"""Incremental placement (DESIGN 7.5; BUILD-PLAN Step 13).

A new leaf descends the topic family of its class by cosine similarity between its embedding and the node summary embeddings,
with a threshold that rises with depth (ASSUMPTION; SOURCE design: MemTree). It joins up to `M_MAX` level-1 nodes whose
similarity clears the threshold; if none does it joins the pending-new group, which the next batch rebuild turns into a node.
Placement writes MEMBER documents bound by the leaf's `src_seq` and changes no node; a member is pending for a reader pinned
at G when its `src_seq` is above W(G) and visible once P covers it. Members name their parent by the stable node id.
"""
from __future__ import annotations

import datetime
from typing import Callable, Optional, Sequence

from ..config import Constants
from ..embed import stub as embed_stub
from ..gateway.es import EsClient
from ..gateway.indices import class_index
from .watermark import Job, WritePath

SCHEMA_V = 1
THETA = (0.0, 0.05, 0.10, 0.20)   # ASSUMPTION: threshold by depth from the top (index 0 = top level)
NEW_PARENT = "pendingnew"         # the parent id of a member that cleared no level-1 node


def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


class Placer:
    def __init__(self, es: Callable[[], EsClient], tenant: str, cls: str, wp: WritePath, *, family: str = "topic",
                 consts: Optional[Constants] = None, theta: Sequence[float] = THETA):
        self._es_fn, self.tenant, self.cls, self._wp, self.family = es, tenant, cls, wp, family
        self._c = consts or Constants()
        self._theta = theta

    @property
    def _es(self) -> EsClient:
        return self._es_fn()

    def _get(self, kind: str, doc_id: str) -> Optional[dict]:
        st, r = self._es.request("GET", f"/{class_index(self.tenant, self.cls, kind)}/_doc/{doc_id}")
        return r["_source"] if st == 200 else None

    def _node(self, ref: str) -> Optional[dict]:
        return self._get("node", ref)

    def targets(self, text: str) -> list[str]:
        """Node ids of the level-1 nodes the leaf joins (empty: it starts a pending new node)."""
        head = self._get("ctl", f"hd:{self.family}")
        if head is None:
            return []
        mf = self._get("ctl", f"mf:{self.family}:{head['gen']}")
        vec = embed_stub.embed(text)
        level_refs = list(mf["top"])
        depth = 0
        while level_refs:
            docs = [(r, self._node(r)) for r in level_refs]
            docs = [(r, d) for r, d in docs if d is not None]
            if not docs:
                return []
            theta = self._theta[min(depth, len(self._theta) - 1)]
            scored = sorted(((embed_stub.cosine(vec, d["summary_embedding"]), r, d) for r, d in docs), key=lambda t: (-t[0], t[1]))
            clearing = [t for t in scored if t[0] >= theta and t[0] > 0]
            if not clearing:
                return []
            if docs[0][1]["level"] == 1:
                return [r.split(":")[0] for _, r, _ in clearing[: self._c.m_max]]
            best = clearing[0][2]
            level_refs = [f"{ch['child_id']}:{ch['child_version']}" for ch in best["children"] if not ch["is_leaf"]]
            depth += 1
        return []

    def place(self, job: Job, leaf_id: str, lv: int, src_seq: int, text: str, title: str, size_tokens: int) -> list[str]:
        parents = self.targets(text) or [NEW_PARENT]
        out = []
        for n, node_id in enumerate(parents):
            doc = {"tenant": self.tenant, "cls": self.cls, "kind": "member", "lin_leaves": [], "lin_leaf_ids": [leaf_id],
                   "lin_nodes": [] if node_id == NEW_PARENT else [node_id], "lin_subjects": [], "lin_queries": [],
                   "derivation": "placement", "erasure_rewrite": 0, "created_at": _now(), "schema_v": SCHEMA_V,
                   "parent_id": node_id, "child_id": f"{leaf_id}:{lv}", "score": 1.0 / (1 + n), "shaped_by": "placement",
                   "src_seq": int(src_seq), "title": title, "size_tokens": int(size_tokens)}
            mid = f"{node_id}>{leaf_id}:{lv}"
            if self._wp.write(job, class_index(self.tenant, self.cls, "member"), mid, doc):
                out.append(mid)
        return out
