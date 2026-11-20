"""Pool delete: an erasure of scope `pool` (POOLS 9.4, DESIGN 14.1, 14.3; BUILD-PLAN Step 25, stage 0).

The rule: a pool delete erases what belongs to the pool. At stage 0 that is

* every physical index in the pool's manifest (the pool's own leaf index, dropped after the closure), found through the alias;
* the pool's documents in the tenant's source log (by their `pool` field) and the data keys of every leaf of the pool, so no ciphertext
  opens any more;
* every document in the tenant's other indices that carries the pool in `pool` or in `lin_pools`: leaf heads, the manifest, stage 0 rows
  and stored queries, join-class rows, multi-pool cache entries, held items, notes; and the pool's authz records (grants, the pool
  row), whose `pool` field names it.

What belongs to another pool is kept (POOLS 9.4): the scan never deletes by `lin_leaf_ids`, so a `pool_copy` leaf in pool B, whose
lineage names a leaf of this pool, stays (copies are not built yet; the rule is stated here and tested when they are).

Order: the whole tenant's write gate is taken exclusively (commits wait, so a write cannot land between the scan and the drop); the
closure and its complete key inventory are read (`_key_inventory`: a failed or partial read raises); the pool-scope tombstone is appended
to the tombstone journal (the boundary); the keys are destroyed (before the documents that name them, so a crash never loses a key id);
the closure's documents are deleted; the pool's indices are dropped; the pool's own records go last; the indices that held class-level
documents are purged. `drop_index` is a callable (index management
is a privilege no tenant key holds, as in provisioning). The `broken_*` flags are for the tests' broken variants only.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Callable, Iterable, Optional

from ..gateway.es import EsClient
from ..gateway.gate import WriteGate
from ..gateway.indices import check_id, check_pool_id, pool_index
from ..gateway.journal import TombstoneJournal
from ..gateway.reads import IncompleteRead, complete_hits, search_path
from ..keystore.store import Keystore
from . import purge

PAGE = 500
# Every document kind that carries ciphertext under its own data key, and the field that names the key's id. The pool delete destroys the
# key of every such document in its closure; tests/unit/test_registration.py fails if a producer writes a key-holding kind not listed here.
KEY_KINDS = {"src": "leaf_id", "quarantine": "qid"}


class PoolEraseError(Exception):
    pass


@dataclass
class PoolReceipt:
    seq: int = 0
    docs_deleted: int = 0
    indices_dropped: list = field(default_factory=list)
    keys_destroyed: int = 0
    purged: list = field(default_factory=list)
    boundary_s: float = 0.0


class PoolEraser:
    def __init__(self, es_erase: Callable[[], EsClient], drop_index: Callable[[str], None], tenant: str, keystore: Keystore,
                 journal: TombstoneJournal, gate: WriteGate, *, purge_deadline_s: float = purge.DEFAULT_DEADLINE_S,
                 purge_interval_s: float = 3.0, pause: Optional[Callable[[str], None]] = None,
                 broken_skip_class_level: bool = False, broken_skip_keys: bool = False, broken_keep_indices: bool = False):
        self._er, self._drop, self.tenant, self._ks, self._j, self._gate = es_erase, drop_index, check_id(tenant, "tenant"), keystore, journal, gate
        self._pd, self._pi, self._pause = purge_deadline_s, purge_interval_s, pause or (lambda s: None)
        self._b = dict(class_level=broken_skip_class_level, keys=broken_skip_keys, keep=broken_keep_indices)

    def _docs(self, pool: str) -> list[tuple[str, str, str, str]]:
        """(index, id, kind, key id) of every document of the tenant naming the pool, wherever it lives, except the pool's own leaf
        indices. The key id is the field `KEY_KINDS` names for a key-holding kind ("" otherwise). A failed or partial page raises."""
        er, out, after = self._er(), [], None
        pattern = f"lc-{self.tenant}-*"
        er.request("POST", f"/{pattern}/_refresh")
        fields = sorted({"kind", *KEY_KINDS.values()})
        while True:
            body = {"size": PAGE, "_source": fields, "sort": [{"_doc": "asc"}], "query": {"bool": {"should": [
                {"term": {"pool": pool}}, {"term": {"lin_pools": pool}}], "minimum_should_match": 1}}}
            if after is not None:
                body["search_after"] = after
            st, r = er.request("POST", search_path(pattern, "ignore_unavailable=true"), body)
            try:
                hits = complete_hits(st, r)
            except IncompleteRead as e:
                raise PoolEraseError("closure scan failed") from e
            if not hits:
                return out
            for h in hits:
                if f"-{pool}-" in h["_index"]:
                    continue
                src = h.get("_source") or {}
                k = src.get("kind", "")
                out.append((h["_index"], h["_id"], k, str(src.get(KEY_KINDS[k], "")) if k in KEY_KINDS else ""))
            after = hits[-1]["sort"]

    def _leaf_ids(self, cls: str, pool: str) -> set:
        er, ids, after = self._er(), set(), None
        idx = pool_index(self.tenant, cls, pool, "leaf")
        while True:
            body = {"size": PAGE, "_source": ["lin_leaf_ids"], "sort": [{"_doc": "asc"}], "query": {"match_all": {}}}
            if after is not None:
                body["search_after"] = after
            st, r = er.request("POST", search_path(idx, "ignore_unavailable=true"), body)
            if st == 404:
                return ids
            try:
                hits = complete_hits(st, r)
            except IncompleteRead as e:
                raise PoolEraseError("leaf scan failed") from e
            if not hits:
                return ids
            for h in hits:
                ids.update(h["_source"].get("lin_leaf_ids", []))
            after = hits[-1]["sort"]

    def _key_inventory(self, cls: str, pool: str) -> tuple[list, set]:
        """The closure's documents and the COMPLETE set of data-key ids of the pool: every leaf of the pool's own index, and the key id of
        every key-holding document (`KEY_KINDS`) in the closure: the source documents of the pool (a write that never reached its
        projection included) and its held saves. Read before anything is deleted; the keys are destroyed before the documents that name
        them, so a crash at any step leaves either the keys destroyed or the documents that name them in place (a resume finds them)."""
        docs = [] if self._b["class_level"] else self._docs(pool)
        keys = set(self._leaf_ids(cls, pool)) | {key for _, _, kind, key in docs if kind in KEY_KINDS and key}
        return docs, keys

    def _physical(self, cls: str, pool: str) -> list[str]:
        st, r = self._er().request("GET", f"/{pool_index(self.tenant, cls, pool, 'leaf')}/_alias")
        return sorted(r) if st == 200 and isinstance(r, dict) else []

    @staticmethod
    def _del(er, index: str, did: str, rc) -> None:
        st, _ = er.request("DELETE", f"/{index}/_doc/{did}")
        if st not in (200, 404):
            raise PoolEraseError("delete failed")
        rc.docs_deleted += st == 200

    def delete(self, cls: str, pool: str) -> PoolReceipt:
        check_pool_id(pool)
        t0, rc = time.monotonic(), PoolReceipt()
        with self._gate.exclusive():
            docs, keys = self._key_inventory(cls, pool)
            physical = self._physical(cls, pool)
            self._pause("closure")
            rc.seq = self._j.append({"scope": "pool", "pool": pool, "tenant": self.tenant})
            rc.boundary_s = time.monotonic() - t0
            self._pause("boundary")
            er = self._er()
            if not self._b["keys"] and keys:   # crypto-erasure first: no ciphertext of the pool opens from here on, backups included
                rc.keys_destroyed = len(self._ks.destroy(leaf_ids=sorted(keys)))
            self._pause("keys")
            # the pool's own record (`pp:` in authz, `pm:` manifest) goes LAST: until the indices are dropped it is what lets a restarted
            # gateway find the pool in state `deleting` and finish the delete (a crash at any step is resumable)
            last = [(i, d) for i, d, _, _ in docs if d.startswith(("pp:", "pm:"))]
            for index, did in [(i, d) for i, d, _, _ in docs if (i, d) not in last]:
                self._del(er, index, did, rc)
            self._pause("docs")
            if not self._b["keep"]:
                for phys in physical:
                    self._drop(phys)
                    rc.indices_dropped.append(phys)
            self._pause("indices")
            for index, did in last:
                self._del(er, index, did, rc)
        for index in sorted({d[0] for d in docs}):
            try:
                purge.purge_index(er, index, deadline_s=self._pd, interval_s=self._pi)
                rc.purged.append(index)
            except Exception:
                pass   # the purge is reported, not required for the boundary; the verifier's residue check decides
        return rc
