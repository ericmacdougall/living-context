"""Pointer resolution with typed answers (DESIGN 8.4; BUILD-PLAN Step 10).

`ok`; `superseded` (a newer version exists and the reader may read it; its pointer is included); `moved` (a node pointer
of a retired generation, with its forwarding node ids); `expired` (a retired generation with no forwarding); and
`unavailable`, ONE answer, byte-identical for erased, forbidden and nonexistent. `opened` is logged here, by the
gateway side, never from an agent's claim."""
from __future__ import annotations

from typing import Callable, Iterable, Optional, Protocol

from ..ids import KIND_LEAF, KIND_NODE, IdError, Pointer, decode_pointer, encode_pointer

UNAVAILABLE = {"status": "unavailable"}


class Store(Protocol):
    def leaf(self, leaf_id: str, lv: int) -> Optional[dict]: ...      # {"cls", "text"} or None
    def current_lv(self, leaf_id: str) -> Optional[int]: ...
    def gen_status(self, gen: int) -> Optional[str]: ...               # live, retired, expired or None
    def forwarding(self, gen: int, node_id: str) -> Optional[list]: ...


class Resolver:
    def __init__(self, store: Store, read_classes: Iterable[str], erased: Callable[[str], bool],
                 opened: Callable[[dict], None], *, check_class: bool = True):
        self._s, self._classes, self._erased, self._opened = store, set(read_classes), erased, opened
        self._check_class = check_class

    def resolve(self, pointer: str, *, actor: str = "agent") -> dict:
        try:
            p = decode_pointer(pointer)
        except IdError:
            return dict(UNAVAILABLE)
        return self._leaf(p, actor) if p.kind == KIND_LEAF else self._node(p)

    def _readable(self, leaf_id: str, lv: int) -> Optional[dict]:
        if self._erased(leaf_id):
            return None
        doc = self._s.leaf(leaf_id, lv)
        if doc is None or (self._check_class and doc["cls"] not in self._classes):
            return None
        return doc

    def _leaf(self, p: Pointer, actor: str) -> dict:
        doc = self._readable(p.id, p.version)
        if doc is None:
            return dict(UNAVAILABLE)  # erased, forbidden (including a relabel out of the read set), nonexistent
        cur = self._s.current_lv(p.id)
        if cur is not None and cur > p.version:
            if self._readable(p.id, cur) is None:
                return dict(UNAVAILABLE)
            return {"status": "superseded", "newer": encode_pointer(Pointer(KIND_LEAF, p.gen, p.id, cur))}
        self._opened({"leaf_id": p.id, "lv": p.version, "gen": p.gen, "actor": actor})
        return {"status": "ok", "text": doc["text"]}

    def _node(self, p: Pointer) -> dict:
        answer = getattr(self._s, "node_answer", None)
        if answer is not None:   # a store that knows the node's class (the Elasticsearch store): one answer, class-checked
            got = answer(p)
            if got is None or (self._check_class and got[0] not in self._classes):
                return dict(UNAVAILABLE)
            return got[1]
        st = self._s.gen_status(p.gen)
        if st == "live":
            return {"status": "ok"}
        if st == "retired":
            fw = self._s.forwarding(p.gen, p.id)
            return {"status": "moved", "forward": list(fw)} if fw else {"status": "expired"}
        if st == "expired":
            return {"status": "expired"}
        return dict(UNAVAILABLE)


class EsLeafStore:
    """The gateway-side store behind `Resolver`: one key that reads the leaf indices of every class of the tenant (the
    `ingest` key); the class check is the Resolver's, never this store's. Node generations are not served from here
    (`gen_status` answers None, so a node pointer is `unavailable` until Step 13 adds the retention records)."""

    def __init__(self, es, tenant: str, node_es=None):
        self._es, self._t = es, tenant
        self._nes = node_es or es   # node and control indices are read with the structure key, leaves with the ingest key

    def _search(self, pattern: str, ids: list, source: list):
        st, r = self._es.request("POST", f"/{pattern}/_search?ignore_unavailable=true", {
            "size": 1, "_source": source, "query": {"bool": {"filter": [{"ids": {"values": ids}}],
                                                         "must_not": [{"exists": {"field": "pool"}}]}}})   # a pool's documents are read only through the pool tools
        return r["hits"]["hits"][0]["_source"] if st == 200 and r["hits"]["hits"] else None

    def leaf(self, leaf_id: str, lv: int) -> Optional[dict]:
        src = self._search(f"lc-{self._t}-*-leaf", [f"{leaf_id}:{lv}"], ["cls", "text"])
        return {"cls": src["cls"], "text": src.get("text", "")} if src else None

    def current_lv(self, leaf_id: str) -> Optional[int]:
        src = self._search(f"lc-{self._t}-*-ctl", [f"lh:{leaf_id}"], ["current_lv"])
        return int(src["current_lv"]) if src else None

    def gen_status(self, gen: int) -> Optional[str]:
        return None

    def forwarding(self, gen: int, node_id: str) -> Optional[list]:
        return None

    def node_answer(self, p, family: str = "topic"):
        """(class, answer) for a node pointer, or None (unavailable). A node document that still exists answers `ok` while
        its generation is retained or still shared with a retained one (documents are immutable, so the content is never another
        node's); once retention expiry removed it, the `xp:` record names its class and the answer is `moved` with the forwarding
        ids of the first later generation that replaced the node, else `expired`."""
        ref = f"{p.id}:{p.version}"
        es, self._es = self._es, self._nes
        try:
            return self._node_answer(p, ref, family)
        finally:
            self._es = es

    def _node_answer(self, p, ref: str, family: str):
        src = self._search(f"lc-{self._t}-*-node", [ref], ["cls"])
        if src is not None:
            mf = self._search(f"lc-{self._t}-{src['cls']}-ctl", [f"mf:{family}:{p.gen}"], ["status"])
            if mf is None or mf.get("status") == "rejected":
                return None
            return src["cls"], {"status": "ok"}
        xp = self._search(f"lc-{self._t}-*-ctl", [f"xp:{ref}"], ["cls"])
        if xp is None:
            return None
        st, r = self._es.request("POST", f"/lc-{self._t}-{xp['cls']}-ctl/_search", {
            "size": 1, "_source": ["new_ids"], "sort": [{"gen": "asc"}], "query": {"bool": {"filter": [
                {"term": {"old_id": p.id}}, {"term": {"forward": True}}, {"range": {"gen": {"gt": p.gen}}}]}}})
        hits = r["hits"]["hits"] if st == 200 else []
        if hits:
            return xp["cls"], {"status": "moved", "forward": list(hits[0]["_source"]["new_ids"])}
        return xp["cls"], {"status": "expired"}
