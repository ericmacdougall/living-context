"""Publication of a built tree (DESIGN 5.4, 5.6, 7.6; BUILD-PLAN Step 9): node documents, member documents, the
generation manifest and the head, flipped only by compare-and-set and only after an explicit refresh of every index
written has returned (the searchable-publication barrier). A lost compare-and-set deletes this generation's unshared
documents and reports the loss (the rebase is Step 13). Class-scoped: every write goes to the class's own indices."""
from __future__ import annotations

import datetime
from typing import Callable, Optional

from ..gateway import validator
from ..gateway.es import EsClient
from ..gateway.indices import TENANT_CLS, class_index
from .build import Node, Tree

SCHEMA_V = 1
CAS_TRIES = 50


class PublishError(Exception):
    pass


def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


class Publisher:
    def __init__(self, es: Callable[[], EsClient], tenant: str, cls: str, *, family: str = "topic",
                 pause: Optional[Callable[[str], None]] = None, refresh_before_flip: bool = True, use_cas: bool = True):
        self._es_fn, self.tenant, self.cls, self.family = es, tenant, cls, family
        self._pause, self._refresh, self._cas = pause or (lambda s: None), refresh_before_flip, use_cas

    @property
    def _es(self) -> EsClient:
        return self._es_fn()

    def _idx(self, kind: str) -> str:
        return class_index(self.tenant, self.cls, kind)

    def _base(self, kind: str, **extra) -> dict:
        d = {"tenant": self.tenant, "cls": self.cls, "kind": kind, "lin_leaves": [], "lin_leaf_ids": [], "lin_nodes": [],
             "lin_subjects": [], "lin_queries": [], "derivation": "build", "erasure_rewrite": 0, "created_at": _now(),
             "schema_v": SCHEMA_V}
        d.update(extra)
        return d

    def _put(self, kind: str, doc_id: str, doc: dict, *, create: bool = False, cas: Optional[tuple] = None):
        idx = self._idx(kind)
        validator.validate(doc, session_tenant=self.tenant, index=idx, index_tenant=self.tenant, index_cls=self.cls)
        q = []
        if create:
            q.append("op_type=create")
        if cas is not None and self._cas:
            q.append(f"if_seq_no={cas[0]}&if_primary_term={cas[1]}")
        st, body = self._es.request("PUT", f"/{idx}/_doc/{doc_id}" + ("?" + "&".join(q) if q else ""), doc)
        if st not in (200, 201, 409):
            raise PublishError(f"write failed ({st})")
        return st, body

    def _get(self, kind: str, doc_id: str) -> Optional[dict]:
        st, body = self._es.request("GET", f"/{self._idx(kind)}/_doc/{doc_id}")
        if st == 404:
            return None
        if st != 200:
            raise PublishError(f"read failed ({st})")
        return body

    def head(self) -> Optional[dict]:
        h = self._get("ctl", f"hd:{self.family}")
        return None if h is None else dict(h["_source"], _seq_no=h["_seq_no"], _primary_term=h["_primary_term"])

    def next_gen(self) -> int:
        """Generation numbers: one per-class counter, compare-and-set, never reused."""
        for _ in range(CAS_TRIES):
            cur = self._get("ctl", f"gc:{self.family}")
            if cur is None:
                st, _ = self._put("ctl", f"gc:{self.family}", self._base("counter", value=1), create=True)
                if st in (200, 201):
                    return 1
                continue
            v = int(cur["_source"]["value"]) + 1
            st, _ = self._put("ctl", f"gc:{self.family}", self._base("counter", value=v),
                              cas=(cur["_seq_no"], cur["_primary_term"]))
            if st in (200, 201):
                return v
        raise PublishError("generation counter contended")

    def publish(self, tree: Tree, *, watermark: int = 0, expect_head: Optional[dict] = None) -> Optional[int]:
        """Write the tree as a new generation and flip the head from `expect_head` (None: no head yet). Returns the
        generation number, or None when the compare-and-set was lost (the generation's documents are deleted)."""
        gen = self.next_gen()
        written: list[tuple[str, str]] = []
        for n in tree.nodes.values():
            self._put("node", n.ref, self._node_doc(n), create=True)
            written.append(("node", n.ref))
        for parent, leaf_id, lv, score in tree.members:
            node_id = parent.split(":")[0]   # a member names its parent by the stable node id, never by a version
            mid = f"{node_id}>{leaf_id}:{lv}"
            doc = self._base("member", lin_leaf_ids=[leaf_id], lin_nodes=[node_id], parent_id=node_id, child_id=f"{leaf_id}:{lv}",
                             score=score, shaped_by="build", gen_from=gen, gen_to=[])
            self._put("member", mid, doc, create=True)
            written.append(("member", mid))
        created_nodes = list(tree.nodes)
        self._pause("written")
        ancestry = list(expect_head["ancestry"]) + [gen] if expect_head else [gen]
        mf = self._base("manifest", lin_nodes=list(tree.top) or ["none"], family=self.family, gen=gen, top=list(tree.top),
                        W=watermark, parent_gen=(expect_head or {}).get("gen"), ancestry=ancestry, status="candidate",
                        levels=tree.levels, tombstone_seq=0, created_nodes=created_nodes)
        self._put("ctl", f"mf:{self.family}:{gen}", mf, create=True)
        written.append(("ctl", f"mf:{self.family}:{gen}"))
        if self._refresh:  # searchable publication: nothing flips before every index written has been refreshed
            for kind in ("node", "member", "ctl"):
                self._es.request("POST", f"/{self._idx(kind)}/_refresh")
        self._pause("refreshed")
        head_doc = self._base("head", lin_nodes=list(tree.top) or ["none"], family=self.family, gen=gen, ancestry=ancestry)
        if expect_head is None:
            st, _ = self._put("ctl", f"hd:{self.family}", head_doc, create=True)
        else:
            st, _ = self._put("ctl", f"hd:{self.family}", head_doc,
                              cas=(expect_head["_seq_no"], expect_head["_primary_term"]))
        if st == 409:  # another builder won: discard this generation's unshared documents
            for kind, did in written:
                self._es.request("DELETE", f"/{self._idx(kind)}/_doc/{did}")
            return None
        self._put("ctl", f"mf:{self.family}:{gen}", dict(mf, status="live"))
        return gen

    def _node_doc(self, n: Node) -> dict:
        return self._base("node", lin_leaf_ids=n.lin_leaf_ids, lin_leaves=[], lin_nodes=n.lin_nodes,
                          lin_subjects=n.lin_subjects, family=self.family, level=n.level, children=n.children,
                          summary=n.summary, summary_embedding=list(n.summary_embedding), facts=n.facts, title=n.title,
                          child_count=n.child_count, total_tokens=n.total_tokens, summary_status=n.summary_status)
