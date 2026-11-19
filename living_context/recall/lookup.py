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

    def __init__(self, es, tenant: str):
        self._es, self._t = es, tenant

    def _search(self, pattern: str, ids: list, source: list):
        st, r = self._es.request("POST", f"/{pattern}/_search?ignore_unavailable=true", {
            "size": 1, "_source": source, "query": {"ids": {"values": ids}}})
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
