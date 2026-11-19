"""A pinned in-memory view of one built tree (BUILD-PLAN Step 10 tests and the stub gate). The product reads the same
shape from the node index at the pinned generation; this view keeps `last_seen` in the entries to show that the holder
call never receives it."""
from __future__ import annotations

from typing import Mapping, Optional

from ..structure.build import Tree


class MemView:
    def __init__(self, tree: Tree, gen: int = 1, pending: Optional[Mapping[str, list]] = None, mangle=None):
        self.tree, self.gen, self._pending, self._mangle = tree, gen, dict(pending or {}), mangle

    def top(self) -> list:
        return list(self.tree.top)

    def entries(self, ref: str) -> list:
        node = self.tree.nodes[ref]
        out = [dict(ch, pending=False, last_seen="2026-01-01T00:00:00Z") for ch in node.children]
        out += [dict(p, pending=True, is_leaf=True, last_seen="2026-01-01T00:00:00Z") for p in self._pending.get(ref, [])]
        return self._mangle(ref, out) if self._mangle else out


class ViewError(Exception):
    pass


class EsView:
    """The same view read from the tenant's indices at a PINNED generation (DESIGN 5.6, 8; SEC-V1). The head is read once, when
    the task pins; every later read names that generation's manifest and node versions, so a head flip, a rebuild or a
    rollback during the task changes nothing the task sees. `repin_each_call` is the BROKEN variant used by tests."""

    def __init__(self, es, tenant: str, cls: str, family: str, *, repin_each_call: bool = False,
                 broken_ignore_p: bool = False):
        from ..gateway.indices import class_index, tenant_index
        self._es, self._family, self._repin = es, family, repin_each_call
        self._ignore_p = broken_ignore_p  # BROKEN variant: pending members above the pinned P are shown
        self._member = class_index(tenant, cls, "member")
        self._wp_index = tenant_index(tenant, "ctl")
        self.P, self.ancestry = 0, []
        self._ctl, self._node = class_index(tenant, cls, "ctl"), class_index(tenant, cls, "node")
        self.gen = self.W = 0
        self._top: list = []
        self._pin()

    def _get(self, index: str, doc_id: str):
        st, r = self._es.request("GET", f"/{index}/_doc/{doc_id}")
        if st == 404:
            return None
        if st != 200:
            raise ViewError(f"read failed ({st})")
        return r["_source"]

    def _pin(self) -> None:
        head = self._get(self._ctl, f"hd:{self._family}")
        if head is None:
            self.gen, self.W, self._top, self.ancestry = 0, 0, [], []
            self.P = self._read_p()
            return
        mf = self._get(self._ctl, f"mf:{self._family}:{head['gen']}")
        if mf is None:
            raise ViewError("head names a generation without a manifest")
        self.gen, self.W, self._top = int(head["gen"]), int(mf.get("W", 0)), list(mf["top"])
        self.ancestry = [int(g) for g in mf.get("ancestry", [self.gen])]
        self.P = self._read_p()

    def _read_p(self) -> int:
        """The write-path watermark, read once with the head (DESIGN 5.6); 0 until a write-path job has finished."""
        doc = self._get(self._wp_index, "wp")
        return int(doc["P"]) if doc else 0

    def _feeders(self, ref: str) -> set:
        """The parent ids whose pending members show under this node: its own, and those of nodes a generation of the pinned
        ancestry replaced by it (a member written after the batch's P stays under its old parent and is reached through the
        forwarding record: the first successor takes it, so a split or merge never strands a late member, SEC-V7)."""
        nid = ref.split(":")[0]
        out, frontier = {ref, nid}, {nid}
        for _ in range(8):
            if not frontier:
                break
            st, r = self._es.request("POST", f"/{self._ctl}/_search", {"size": 200, "query": {"bool": {"filter": [
                {"term": {"family": self._family}}, {"term": {"forward": True}}, {"terms": {"new_ids": sorted(frontier)}},
                {"terms": {"gen": [int(g) for g in self.ancestry]}}]}}})
            nxt = {h["_source"]["old_id"] for h in (r["hits"]["hits"] if st == 200 else []) if h["_source"]["new_ids"][0] in frontier}
            frontier = nxt - out
            out |= nxt
        return out

    def pending(self, ref: str) -> list:
        """Write-path members of this node above the pinned W and at most the pinned P (DESIGN 8.1, 13.2): code-built title
        and size only, no gist. A member whose `gen_to` names a generation of the pinned ancestry is retired."""
        rng = {"gt": self.W} if self._ignore_p else {"gt": self.W, "lte": self.P}
        body = {"size": 1000, "query": {"bool": {"filter": [{"terms": {"parent_id": sorted(self._feeders(ref))}}, {"range": {"src_seq": rng}}]}},
                "sort": [{"src_seq": "asc"}, {"child_id": "asc"}]}
        st, r = self._es.request("POST", f"/{self._member}/_search", body)
        if st != 200:
            raise ViewError(f"member read failed ({st})")
        anc = set(self.ancestry)
        out = []
        for h in r["hits"]["hits"]:
            d = h["_source"]
            if anc & set(d.get("gen_to", []) or []):
                continue
            leaf_id, lv = d["child_id"].rsplit(":", 1)
            out.append({"child_id": leaf_id, "child_version": int(lv), "is_leaf": True, "title": d.get("title", ""),
                        "size_tokens": int(d.get("size_tokens", 0)), "total_tokens": int(d.get("size_tokens", 0)), "pending": True,
                        "gist": "", "facts": [], "entities": []})
        return out

    def top(self) -> list:
        if self._repin:
            self._pin()
        return list(self._top)

    def entries(self, ref: str) -> list:
        doc = self._get(self._node, ref)
        if doc is None:
            raise ViewError("node version not found")
        return [dict(ch, pending=False) for ch in doc.get("children", [])] + self.pending(ref)
