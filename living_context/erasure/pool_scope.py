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
pool-scope tombstone is appended to the tombstone journal (the boundary); the closure's documents are deleted; keys are destroyed;
the pool's indices are dropped; the indices that held class-level documents are purged. `drop_index` is a callable (index management
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
from ..keystore.store import Keystore
from . import purge

PAGE = 500


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

    def _docs(self, pool: str) -> list[tuple[str, str]]:
        """(index, id) of every document of the tenant naming the pool, wherever it lives, except the pool's own leaf indices."""
        er, out, after = self._er(), [], None
        pattern = f"lc-{self.tenant}-*"
        er.request("POST", f"/{pattern}/_refresh")
        while True:
            body = {"size": PAGE, "_source": False, "sort": [{"_doc": "asc"}], "query": {"bool": {"should": [
                {"term": {"pool": pool}}, {"term": {"lin_pools": pool}}], "minimum_should_match": 1}}}
            if after is not None:
                body["search_after"] = after
            st, r = er.request("POST", f"/{pattern}/_search?ignore_unavailable=true", body)
            if st != 200:
                raise PoolEraseError("closure scan failed")
            hits = r["hits"]["hits"]
            if not hits:
                return out
            out += [(h["_index"], h["_id"]) for h in hits if f"-{pool}-" not in h["_index"]]
            after = hits[-1]["sort"]

    def _leaf_ids(self, cls: str, pool: str) -> set:
        er, ids, after = self._er(), set(), None
        idx = pool_index(self.tenant, cls, pool, "leaf")
        while True:
            body = {"size": PAGE, "_source": ["lin_leaf_ids"], "sort": [{"_doc": "asc"}], "query": {"match_all": {}}}
            if after is not None:
                body["search_after"] = after
            st, r = er.request("POST", f"/{idx}/_search?ignore_unavailable=true", body)
            if st == 404:
                return ids
            if st != 200:
                raise PoolEraseError("leaf scan failed")
            hits = r["hits"]["hits"]
            if not hits:
                return ids
            for h in hits:
                ids.update(h["_source"].get("lin_leaf_ids", []))
            after = hits[-1]["sort"]

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
            leaves = self._leaf_ids(cls, pool)
            docs = [] if self._b["class_level"] else self._docs(pool)
            physical = self._physical(cls, pool)
            self._pause("closure")
            rc.seq = self._j.append({"scope": "pool", "pool": pool, "tenant": self.tenant})
            rc.boundary_s = time.monotonic() - t0
            self._pause("boundary")
            er = self._er()
            # the pool's own record (`pp:` in authz, `pm:` manifest) goes LAST: until the keys are destroyed and the indices dropped it is
            # what lets a restarted gateway find the pool in state `deleting` and finish the delete (a crash at any step is resumable)
            last = [(i, d) for i, d in docs if d.startswith(("pp:", "pm:"))]
            for index, did in [x for x in docs if x not in last]:
                self._del(er, index, did, rc)
            self._pause("docs")
            if not self._b["keys"]:
                for leaf in sorted(leaves):
                    self._ks.destroy(leaf_id=leaf)
                    rc.keys_destroyed += 1
            self._pause("keys")
            if not self._b["keep"]:
                for phys in physical:
                    self._drop(phys)
                    rc.indices_dropped.append(phys)
            self._pause("indices")
            for index, did in last:
                self._del(er, index, did, rc)
        for index in sorted({i for i, _ in docs}):
            try:
                purge.purge_index(er, index, deadline_s=self._pd, interval_s=self._pi)
                rc.purged.append(index)
            except Exception:
                pass   # the purge is reported, not required for the boundary; the verifier's residue check decides
        return rc
