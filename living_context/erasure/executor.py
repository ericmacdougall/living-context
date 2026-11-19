"""The erasure executor (DESIGN 14.2, 14.3, 14.5; BUILD-PLAN Step 14, first slice).

Order (review 2, F7: the tombstone is NOT published before the closure is complete):
  1. take the tenant's write gate exclusively (commits wait), refresh every index;
  2. compute the lineage closure (subject token, then leaf ids, then upward through `lin_nodes`) as a redact set;
  3. the BOUNDARY: append the tombstone (with the redact set) to the journal (hash chain, fsync, replica), record it in
     the tenant control index, refresh, and make it effective in memory together with the redact set under the send
     fence's exclusive side (no I/O there); the gate is then released;
  4. destroy the keys, delete the closure's documents, rewrite affected node versions to a code-built placeholder
     in every retained generation, purge (repeated expunge loop), recount, return the receipt.
The boundary time (request to step 3) and the physical-purge time (to a deleted count of 0) are reported separately."""
from __future__ import annotations

import datetime
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Callable, Iterable, Optional

from ..gateway import validator
from ..gateway.es import EsClient
from ..gateway.fence import SendFence
from ..gateway.gate import WriteGate
from ..gateway.indices import tenant_index
from ..gateway.journal import TombstoneJournal
from ..keystore.store import Keystore
from . import purge, scan

DELETE_KINDS = frozenset({"src", "leaf", "member", "edge", "log", "sq", "note", "cache", "quarantine", "dirty", "head",
                          "idem", "session", "grant", "judge"})
REWRITE_KINDS = frozenset({"node"})
KEEP_KINDS = frozenset({"manifest", "tomb", "counter", "idem_scope"})  # ids only, or no content


class State:
    """What the read and send paths consult in memory: the published tombstone sequence and the redact set."""

    def __init__(self):
        self.seq, self.redact, self.erased_leaves = 0, set(), set()

    def apply(self, seq: int, redact: Iterable[str], leaf_ids: Iterable[str]) -> None:
        self.redact |= set(redact)
        self.erased_leaves |= set(leaf_ids)
        self.seq = seq

    def is_redacted(self, doc_id: str) -> bool:
        return doc_id in self.redact


@dataclass
class Receipt:
    seq: int = 0
    boundary_s: float = 0.0
    closure: dict = field(default_factory=dict)
    keys_destroyed: int = 0
    deleted: int = 0
    rewritten: int = 0
    stripped: int = 0
    purge_s: dict = field(default_factory=dict)
    purge_left: dict = field(default_factory=dict)
    purge_ok: bool = True
    recount: int = -1


class Executor:
    def __init__(self, es_erase: Callable[[], EsClient], es_struct: Callable[[], EsClient], tenant: str, keystore: Keystore,
                 journal: TombstoneJournal, gate: WriteGate, fence: SendFence, state: State, *,
                 pause: Optional[Callable[[str], None]] = None, transitive: bool = True, destroy_only_keys: bool = False,
                 purge_deadline_s: float = purge.DEFAULT_DEADLINE_S, purge_interval_s: float = 3.0,
                 broken_delete_foreign_log_rows: bool = False, es_log: Optional[Callable[[], EsClient]] = None):
        self._er, self._st, self.tenant, self._ks = es_erase, es_struct, tenant, keystore
        self._j, self._gate, self._fence, self.state = journal, gate, fence, state
        self._pause = pause or (lambda s: None)
        self._transitive, self._keys_only = transitive, destroy_only_keys
        self._pd, self._pi = purge_deadline_s, purge_interval_s
        self._lg = es_log or es_struct   # the rewrite of a log row needs write on the log indices (the erase key has delete only)
        self._del_foreign = broken_delete_foreign_log_rows  # BROKEN variant: another actor's history is deleted too

    # ---- closure ---------------------------------------------------------------------------------------------
    def closure(self, subject: str) -> tuple[dict, set]:
        """Return ({(index, id): Hit}, erased leaf ids). Subject, then leaf ids, then upward through lin_nodes."""
        er = self._er()
        hits = {(h.index, h.id): h for h in scan.find(er, self.tenant, subjects=[subject])}
        leaves = {lid for h in hits.values() if h.kind in ("src", "leaf") for lid in h.lin_leaf_ids}
        if leaves:
            for h in scan.find(er, self.tenant, leaf_ids=leaves):
                hits[(h.index, h.id)] = h
        if self._transitive:
            frontier = {h.id for h in hits.values() if h.kind == "node"}
            while frontier:
                nxt = set()
                for h in scan.find(er, self.tenant, nodes=frontier):
                    if (h.index, h.id) not in hits:
                        hits[(h.index, h.id)] = h
                        if h.kind == "node":
                            nxt.add(h.id)
                frontier = nxt
        return hits, leaves

    # ---- the request ------------------------------------------------------------------------------------------
    def erase_subject(self, subject: str, *, identifiers: Iterable[str] = (), apply_guard: Callable[[], None] = lambda: None) -> Receipt:
        t0 = time.monotonic()
        er, rc = self._er(), Receipt()
        with self._gate.exclusive():
            er.request("POST", f"/lc-{self.tenant}-*/_refresh")
            hits, leaves = self.closure(subject)
            self._pause("closure")
            redact = sorted({h.id for h in hits.values()} | leaves)
            seq = self._j.append({"subject": subject, "leaf_ids": sorted(leaves), "redact": redact})
            tomb = {"tenant": self.tenant, "cls": "tenantwide", "kind": "tomb", "lin_leaves": [], "lin_leaf_ids": [],
                    "lin_nodes": [], "lin_subjects": [], "lin_queries": [], "derivation": "erase", "erasure_rewrite": 0,
                    "created_at": datetime.datetime.now(datetime.timezone.utc).isoformat(), "schema_v": 1, "seq": seq}
            st, _ = self._st().request("PUT", f"/{tenant_index(self.tenant, 'ctl')}/_doc/tb:{seq}", tomb)
            er.request("POST", f"/{tenant_index(self.tenant, 'ctl')}/_refresh")
            self._pause("recorded")
            with self._fence.exclusive():  # one in-memory step: the tombstone and its redact set become effective together
                self.state.apply(seq, redact, leaves)
                apply_guard()
            rc.seq, rc.boundary_s = seq, time.monotonic() - t0
        self._pause("boundary")
        # ---- after the boundary: keys, deletes, rewrites, purge ------------------------------------------------
        rc.closure = {}
        for h in hits.values():
            rc.closure[h.kind] = rc.closure.get(h.kind, 0) + 1
        rc.keys_destroyed = len(self._ks.destroy(subject_token=subject))
        rc.keys_destroyed += len(self._ks.destroy(leaf_ids=leaves))
        if self._keys_only:  # BROKEN variant: the keys are gone, the plaintext projections stay
            rc.recount = len(scan.find(er, self.tenant, subjects=[subject]))
            return rc
        touched: set[str] = set()
        for h in list(hits.values()):
            if h.kind == "log" and not self._del_foreign:
                if self._strip_or_delete_log_row(h, leaves, subject, seq, rc):
                    touched.add(h.index)
            elif h.kind in DELETE_KINDS:
                rc.deleted += scan.delete(er, [h])
                touched.add(h.index)
            elif h.kind in REWRITE_KINDS:
                rc.rewritten += self._rewrite_node(h, leaves, subject)
                touched.add(h.index)
        def one(ix: str):
            er.request("POST", f"/{ix}/_refresh")
            return ix, purge.purge_index(er, ix, deadline_s=self._pd, interval_s=self._pi)

        with ThreadPoolExecutor(max(1, min(8, len(touched)))) as ex:  # the tombstone clocks run side by side
            for ix, res in ex.map(one, sorted(touched)):
                rc.purge_s[ix] = round(res.seconds, 1)
                rc.purge_left[ix] = res.deleted_left
                rc.purge_ok &= res.ok
        er.request("POST", f"/lc-{self.tenant}-*/_refresh")
        remaining = [h for h in scan.find(er, self.tenant, subjects=[subject], leaf_ids=leaves) if h.kind not in KEEP_KINDS | REWRITE_KINDS]
        rc.recount = len(remaining)
        return rc

    def _rewrite_node(self, h: scan.Hit, leaves: set, subject: str) -> int:
        """A node version that names an erased leaf or derives from one: the child entries of erased leaves and every
        gist, fact and summary go (the code-built placeholder, no model call); lineage is pruned of what was erased."""
        st = self._st()
        code, doc = st.request("GET", f"/{h.index}/_doc/{h.id}")
        if code != 200:
            return 0
        s = doc["_source"]
        s["children"] = [{**{k: v for k, v in c.items() if k not in ("gist", "facts", "entities")}, "gist": "", "facts": [],
                          "entities": []} for c in s.get("children", []) if c.get("child_id") not in leaves]
        s.update(summary="", facts=[], summary_embedding=[], title="", summary_status="erased_placeholder",
                 child_count=len(s["children"]), erasure_rewrite=int(s.get("erasure_rewrite", 0)) + 1)
        s["lin_leaf_ids"] = [x for x in s.get("lin_leaf_ids", []) if x not in leaves]
        s["lin_subjects"] = [x for x in s.get("lin_subjects", []) if x != subject]
        if not (s["lin_leaf_ids"] or s["lin_nodes"] or s["lin_subjects"]):
            s["lin_nodes"] = ["erased"]
        code, _ = st.request("PUT", f"/{h.index}/_doc/{h.id}?if_seq_no={doc['_seq_no']}&if_primary_term={doc['_primary_term']}", s)
        return 1 if code in (200, 201) else 0

    def _strip_or_delete_log_row(self, h: scan.Hit, leaves: set, subject: str, seq: int, rc: Receipt) -> bool:
        """DESIGN 14.3 step 5: a row whose actor is the subject, or whose query text mentions the subject, is the subject's
        own and is deleted. A row of another actor whose only link to the scope is an offered erased item is that actor's
        history: its pointer and the item's subject tokens are stripped by an in-place rewrite (`erasure_rewrite = seq`,
        DEVIATION 6). The document id still holds the offered leaf id (an id, no content; a stated limit)."""
        st = self._lg()
        code, doc = st.request("GET", f"/{h.index}/_doc/{h.id}")
        if code != 200:
            raise scan.ScanError(f"log row read failed ({code})")
        s = doc["_source"]
        item_subjects = set(s.get("item_subjects", []))
        own = (set(s.get("lin_subjects", [])) - item_subjects) | {s.get("actor", "")}
        if subject in own:
            rc.deleted += scan.delete(self._er(), [h])
            return True
        s.update(pointer={}, lin_leaf_ids=[], item_subjects=[], lin_subjects=sorted(own - {""}), erasure_rewrite=seq)
        code, _ = st.request("PUT", f"/{h.index}/_doc/{h.id}?if_seq_no={doc['_seq_no']}&if_primary_term={doc['_primary_term']}", s)
        if code not in (200, 201):
            raise scan.ScanError(f"log row rewrite failed ({code})")
        rc.stripped += 1
        return True
