"""Bounded link expansion (DESIGN 8.6, 13.2; BUILD-PLAN Step 12).

From the leaves of a digest: at most `expand_hops` hops, `expand_neighbours` neighbours per node per hop and `expand_leaves`
expanded leaves per recall. Edge score = type weight x weight x decay x specificity (decay is 1.0 until the decay interface is
built after H1; specificity divides by log2(2 + degree of the target within the reader's visible edge indices). Per hop: one
terms query on `src_id` over the edge index of each class the reader may read, filtered by the visibility rule of the task's pin
(never "the latest edges"), then a lookup of the neighbour leaves in the readable leaf indices. A superseded leaf comes with the
leaf that supersedes it. Expanded leaves enter the digest in the `linked` section as pointers with path `edge`.

Visibility rule for a task pinned at generation G with write-path watermark P and ancestry anc(G):
  write-path versions (`src_seq`) are visible if `src_seq` <= P; rebuild and reorganiser versions (`gen_from`) if `gen_from` is in
  anc(G); in every case not if a number of the version's `gen_to` set is in anc(G). `ignore_pin` is the BROKEN variant.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Callable, Iterable, Optional, Sequence

from ..config import Constants
from ..gateway.es import EsClient
from ..gateway.indices import class_index
from ..ids import KIND_LEAF, Pointer, encode_pointer
from ..recall import digest as dg
from .edges import TYPES

SUPERSEDES = "supersedes"


@dataclass(frozen=True)
class Pin:
    P: int
    ancestry: tuple
    gen: int = 0


def visibility_filter(pin: Pin) -> dict:
    anc = list(pin.ancestry)
    should = [{"range": {"src_seq": {"lte": pin.P}}}]
    if anc:
        should.append({"terms": {"gen_from": anc}})
    must_not = [{"terms": {"gen_to": anc}}] if anc else []
    return {"bool": {"should": should, "minimum_should_match": 1, "must_not": must_not}}


def visible(doc: dict, pin: Pin) -> bool:
    """The same rule in code (used by tests as the reference reading of DESIGN 13.2)."""
    anc = set(pin.ancestry)
    if anc & set(doc.get("gen_to") or []):
        return False
    if doc.get("src_seq") is not None and int(doc["src_seq"]) <= pin.P:
        return True
    return doc.get("gen_from") is not None and int(doc["gen_from"]) in anc


@dataclass
class Linked:
    leaf_id: str
    lv: int
    cls: str
    title: str
    size: int
    score: float
    etype: str
    hop: int
    via: str
    partner: Optional[str] = None


class Expander:
    def __init__(self, client_for: Callable[[str], EsClient], tenant: str, *, consts: Optional[Constants] = None,
                 redacted: Callable[[str], bool] = lambda doc_id: False, ignore_pin: bool = False):
        self._client_for, self.tenant = client_for, tenant
        self._c = consts or Constants()
        self._redacted, self._ignore_pin = redacted, ignore_pin
        self.plan: list[str] = []

    def _edges(self, classes: Sequence[str], frontier: list, pin: Pin) -> list[dict]:
        flt = [{"terms": {"src_id": frontier}}]
        if not self._ignore_pin:
            flt.append(visibility_filter(pin))
        out = []
        for c in classes:
            idx = class_index(self.tenant, c, "edge")
            self.plan.append(idx)
            st, r = self._client_for(c).request("POST", f"/{idx}/_search", {"size": 1000, "query": {"bool": {"filter": flt}}})
            if st == 200:
                out += [h["_source"] for h in r["hits"]["hits"]]
        return out

    def _degrees(self, classes: Sequence[str], targets: list, pin: Pin) -> dict:
        flt = [{"terms": {"src_id": targets}}] + ([] if self._ignore_pin else [visibility_filter(pin)])
        deg: dict[str, int] = {}
        for c in classes:
            st, r = self._client_for(c).request("POST", f"/{class_index(self.tenant, c, 'edge')}/_search", {
                "size": 0, "query": {"bool": {"filter": flt}}, "aggs": {"d": {"terms": {"field": "src_id", "size": len(targets)}}}})
            if st == 200:
                for b in r["aggregations"]["d"]["buckets"]:
                    deg[b["key"]] = deg.get(b["key"], 0) + b["doc_count"]
        return deg

    def _leaves(self, classes: Sequence[str], wanted: dict) -> dict:
        """leaf id -> (cls, title, size, lv) for the wanted `id:lv` names found in a readable leaf index."""
        found = {}
        for c in classes:
            st, r = self._client_for(c).request("POST", f"/{class_index(self.tenant, c, 'leaf')}/_search", {
                "size": len(wanted), "_source": ["title", "size_tokens"], "query": {"ids": {"values": sorted(wanted.values())}}})
            for h in (r["hits"]["hits"] if st == 200 else []):
                lid, lv = h["_id"].rsplit(":", 1)
                found[lid] = (c, h["_source"].get("title", ""), int(h["_source"].get("size_tokens", 0)), int(lv))
        return found

    def expand(self, seeds: Iterable[str], classes: Sequence[str], pin: Pin) -> list[Linked]:
        c = self._c
        seen = set(seeds)
        frontier = sorted(seen)
        chosen: list[Linked] = []
        for hop in range(1, c.expand_hops + 1):
            if not frontier or len(chosen) >= c.expand_leaves:
                break
            edges = [e for e in self._edges(classes, frontier, pin) if not self._redacted(e["dst_id"]) and e["dst_id"] not in seen]
            if not edges:
                break
            deg = self._degrees(classes, sorted({e["dst_id"] for e in edges}), pin)
            scored: dict[str, list] = {}
            for e in edges:
                w0 = TYPES.get(e["type"], (0.0,))[0]
                score = w0 * float(e["weight"]) * 1.0 / math.log2(2 + deg.get(e["dst_id"], 0))
                scored.setdefault(e["src_id"], []).append((score, e))
            picks: dict[str, tuple] = {}
            for src, lst in scored.items():
                for score, e in sorted(lst, key=lambda t: (-t[0], t[1]["dst_id"]))[:c.expand_neighbours]:
                    if e["dst_id"] not in picks or score > picks[e["dst_id"]][0]:
                        picks[e["dst_id"]] = (score, e)
            room = c.expand_leaves - len(chosen)
            top = sorted(picks.items(), key=lambda kv: (-kv[1][0], kv[0]))[:room]
            info = self._leaves(classes, {lid: f"{lid}:{e['dst_lv']}" for lid, (_, e) in top})
            nxt = []
            for lid, (score, e) in top:
                if lid not in info or self._redacted(lid):
                    continue
                cls, name, size, lv = info[lid]
                chosen.append(Linked(lid, lv, cls, name, size, score, e["type"], hop, e["src_id"]))
                seen.add(lid)
                nxt.append(lid)
            frontier = nxt
        self._add_partners(chosen, classes, pin, seen)
        return chosen

    def _add_partners(self, chosen: list[Linked], classes: Sequence[str], pin: Pin, seen: set) -> None:
        """A leaf that something supersedes is returned with its superseding partner (DESIGN 8.6)."""
        ids = [l.leaf_id for l in chosen]
        if not ids:
            return
        flt = [{"terms": {"dst_id": ids}}, {"term": {"type": SUPERSEDES}}] + ([] if self._ignore_pin else [visibility_filter(pin)])
        partners = {}
        for cl in classes:
            st, r = self._client_for(cl).request("POST", f"/{class_index(self.tenant, cl, 'edge')}/_search",
                                                 {"size": 1000, "query": {"bool": {"filter": flt}}})
            for h in (r["hits"]["hits"] if st == 200 else []):
                e = h["_source"]
                partners[e["dst_id"]] = e
        wanted = {e["src_id"]: f"{e['src_id']}:{e['src_lv']}" for e in partners.values() if e["src_id"] not in seen}
        info = self._leaves(classes, wanted) if wanted else {}
        for l in list(chosen):
            e = partners.get(l.leaf_id)
            if e is None:
                continue
            l.partner = e["src_id"]
            if e["src_id"] in info and not self._redacted(e["src_id"]):
                cls, name, size, lv = info[e["src_id"]]
                chosen.append(Linked(e["src_id"], lv, cls, name, size, l.score, SUPERSEDES, l.hop, l.leaf_id, partner=l.leaf_id))
                seen.add(e["src_id"])


def as_pointers(linked: Iterable[Linked], gen: int = 0) -> list[dict]:
    """The `linked` section of a digest: contract pointers only, path `edge`, tagged by a short reason."""
    out = []
    for l in linked:
        out.append(dg.make_pointer(encode_pointer(Pointer(KIND_LEAF, gen, l.leaf_id, l.lv)), l.title, l.size, "edge",
                                   f"linked by {l.etype} edge", 2 if l.etype != "related" else 1))
    return out
