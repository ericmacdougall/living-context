"""Retention of generations, rejection and rollback (DESIGN 5.6, 13.2, 13.3 step 5; BUILD-PLAN Step 13).

Retention expiry removes the CONTENT of an expired generation (its node documents that no retained generation reaches) and
leaves an `xp:<id>:<nv>` record (ids only) so a node pointer into it still resolves to `moved` (with the forwarding ids) or
`expired`. The member documents of a node id that no retained generation reaches any more, and that an expired generation retired
(`gen_to`) in the ancestry of the oldest retained generation, go with it: every view that can still be pinned has that generation in its ancestry, so those members are invisible
to all of them and only grow the index. It never removes or truncates ancestry: the ancestry list lives in the head and in every manifest, and the visibility
rule needs all of it, because an adopted edge or member can outlive its `gen_from` generation and a retired one must stay retired
after its `gen_to` generation has expired. `R_GEN` = the last `r_gen_count` adopted generations or `r_gen_days`, whichever keeps
more.

`discard_generation` removes everything a candidate or rolled-back generation owns (nodes, members, edges, forwarding records)
and its number from every `gen_to` set; write-path members and edges (bound by `src_seq`) are never touched. `rollback` flips
the head back by compare-and-set to a retained earlier generation, so the previous tree and edges are visible exactly as before
(the rolled-back numbers are not in the restored ancestry, so the candidate's edges stay invisible and are deleted).
"""
from __future__ import annotations

import datetime
from typing import Callable, Optional

from ..config import Constants
from ..gateway.es import EsClient
from ..gateway.indices import class_index
from .publish import Publisher


class Retention:
    def __init__(self, es: Callable[[], EsClient], tenant: str, cls: str, *, family: str = "topic",
                 consts: Optional[Constants] = None, ancestry_truncate: bool = False, members_by_gen_from: bool = False,
                 keep_dead_members: bool = False):
        self._es_fn, self.tenant, self.cls, self.family = es, tenant, cls, family
        self._c = consts or Constants()
        self.pub = Publisher(es, tenant, cls, family=family)
        self._truncate = ancestry_truncate   # BROKEN variant: expiry also truncates the ancestry (V2, V4 red)
        self._by_gen_from = members_by_gen_from   # BROKEN variant: members are deleted by the generation that wrote them
        self._keep_dead = keep_dead_members       # BROKEN variant: no member cleanup at all

    @property
    def _es(self) -> EsClient:
        return self._es_fn()

    def _idx(self, kind: str) -> str:
        return class_index(self.tenant, self.cls, kind)

    def manifest(self, gen: int) -> Optional[dict]:
        d = self.pub._get("ctl", f"mf:{self.family}:{gen}")
        return None if d is None else d["_source"]

    def _reachable(self, top: list) -> set:
        seen, frontier = set(), list(top)
        while frontier:
            ref = frontier.pop()
            if ref in seen:
                continue
            seen.add(ref)
            d = self.pub._get("node", ref)
            if d is not None:
                frontier += [f"{ch['child_id']}:{ch['child_version']}" for ch in d["_source"]["children"] if not ch["is_leaf"]]
        return seen

    def expire(self, *, now: Optional[datetime.datetime] = None) -> list[int]:
        """Expire adopted generations outside R_GEN; returns the generation numbers expired."""
        head = self.pub.head()
        if head is None:
            return []
        now = now or datetime.datetime.now(datetime.timezone.utc)
        adopted = [int(g) for g in head["ancestry"]]
        retained = set(adopted[-self._c.r_gen_count:])
        cutoff = now - datetime.timedelta(days=self._c.r_gen_days)
        for g in adopted:
            mf = self.manifest(g)
            if mf is not None and datetime.datetime.fromisoformat(mf["created_at"]) > cutoff:
                retained.add(g)
        retained.add(int(head["gen"]))
        keep_nodes: set = set()
        for g in retained:
            mf = self.manifest(g)
            if mf is not None and mf.get("status") != "expired":
                keep_nodes |= self._reachable(mf["top"])
        expired, deleted_ids = [], set()
        for g in adopted:
            mf = self.manifest(g)
            if g in retained or mf is None or mf.get("status") == "expired":
                continue
            for ref in mf.get("created_nodes", []):
                if ref in keep_nodes:
                    continue
                deleted_ids.add(ref.split(":")[0])
                self._es.request("DELETE", f"/{self._idx('node')}/_doc/{ref}")
                self.pub._put("ctl", f"xp:{ref}", self.pub._base("manifest", lin_nodes=[ref.split(":")[0]], family=self.family,
                                                                   gen=g, expired_node=ref))
            self.pub._put("ctl", f"mf:{self.family}:{g}", dict(mf, status="expired", top=[], created_nodes=[]))
            expired.append(g)
        if expired and not self._keep_dead:
            self._drop_dead_members(deleted_ids - {r.split(":")[0] for r in keep_nodes}, expired, min(retained))
        if self._truncate and expired:   # BROKEN variant
            keep = sorted(retained)
            self.pub._put("ctl", f"hd:{self.family}", dict(self.pub._base("head", lin_nodes=["none"], family=self.family,
                                                                             gen=head["gen"], ancestry=keep)),
                          cas=(head["_seq_no"], head["_primary_term"]))
        for kind in ("node", "ctl"):
            self._es.request("POST", f"/{self._idx(kind)}/_refresh")
        return expired

    def _drop_dead_members(self, gone: set, expired: list, oldest_retained: int) -> int:
        """Delete the members of node ids no retained generation reaches that a generation of the oldest retained generation's
        ancestry retired: that generation is in the ancestry of every view that can still be pinned."""
        self._es.request("POST", f"/{self._idx('member')}/_refresh")
        if self._by_gen_from:   # BROKEN variant
            q = {"terms": {"gen_from": expired}}
        else:
            if not gone:
                return 0
            anc0 = [int(g) for g in (self.manifest(oldest_retained) or {}).get("ancestry", [])]
            q = {"bool": {"filter": [{"terms": {"parent_id": sorted(gone)}}, {"terms": {"gen_to": anc0}}]}}
        st, r = self._es.request("POST", f"/{self._idx('member')}/_delete_by_query?refresh=true&conflicts=proceed", {"query": q})
        return int(r.get("deleted", 0)) if st == 200 else 0

    # ---- rejection and rollback ------------------------------------------------------------------------------------------
    def discard_generation(self, gen: int) -> None:
        mf = self.manifest(gen)
        for ref in (mf or {}).get("created_nodes", []):
            self._es.request("DELETE", f"/{self._idx('node')}/_doc/{ref}")
        for pattern in (self._idx("member"), self._idx("ctl"), f"lc-{self.tenant}-*-edge"):   # a delete-by-query sees refreshed documents only
            self._es.request("POST", f"/{pattern}/_refresh")
        self._es.request("POST", f"/{self._idx('member')}/_delete_by_query?refresh=true&conflicts=proceed", {"query": {"term": {"gen_from": gen}}})
        self._es.request("POST", f"/lc-{self.tenant}-*-edge/_delete_by_query?refresh=true&conflicts=proceed", {"query": {"term": {"gen_from": gen}}})
        self._es.request("POST", f"/{self._idx('ctl')}/_delete_by_query?refresh=true&conflicts=proceed", {"query": {"bool": {"filter": [
            {"term": {"family": self.family}}, {"term": {"gen": gen}}, {"term": {"forward": True}}]}}})
        script = {"lang": "painless", "source": "if (ctx._source.gen_to != null) { ctx._source.gen_to.removeIf(x -> x == params.g); }",
                  "params": {"g": gen}}
        for idx in (self._idx("member"), f"lc-{self.tenant}-*-edge"):
            self._es.request("POST", f"/{idx}/_update_by_query?refresh=true&conflicts=proceed", {"query": {"term": {"gen_to": gen}}, "script": script})
        if mf is not None:
            self.pub._put("ctl", f"mf:{self.family}:{gen}", dict(mf, status="rejected"))
        for kind in ("node", "ctl"):
            self._es.request("POST", f"/{self._idx(kind)}/_refresh")

    def rollback(self, to_gen: int, *, keep_ancestry: bool = False) -> bool:
        """Flip the head back to the retained generation `to_gen` (compare-and-set) and discard what the later ones own."""
        head = self.pub.head()
        target = self.manifest(to_gen)
        if head is None or target is None or target.get("status") in ("expired", "rejected"):
            return False
        later = [int(g) for g in head["ancestry"] if int(g) > to_gen]
        hd = self.pub._base("head", lin_nodes=list(target["top"]) or ["none"], family=self.family, gen=to_gen,
                            ancestry=list(head["ancestry"]) if keep_ancestry else list(target["ancestry"]))   # keep_ancestry: BROKEN
        st, _ = self.pub._put("ctl", f"hd:{self.family}", hd, cas=(head["_seq_no"], head["_primary_term"]))
        if st == 409:
            return False
        self.pub._put("ctl", f"mf:{self.family}:{to_gen}", dict(target, status="live"))
        for g in later:
            if not keep_ancestry:    # the BROKEN variant also leaves the candidate's documents in place
                self.discard_generation(g)
        return True
