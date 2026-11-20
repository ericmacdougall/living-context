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


class Held(Exception):
    """A legal hold covers the closure (DESIGN 14.1): nothing was published, deleted or rewritten; the caller queues the request."""


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
    by_class: dict = field(default_factory=dict)    # (kind, class) -> documents in the closure
    keys_destroyed: int = 0
    deleted: int = 0
    rewritten: int = 0
    stripped: int = 0
    purge_s: dict = field(default_factory=dict)
    purge_left: dict = field(default_factory=dict)
    purge_ok: bool = True
    recount: int = -1
    hold_delay_s: float = 0.0   # a legal hold queued the request for this long (DESIGN 14.1)


class Executor:
    def __init__(self, es_erase: Callable[[], EsClient], es_struct: Callable[[], EsClient], tenant: str, keystore: Keystore,
                 journal: TombstoneJournal, gate: WriteGate, fence: SendFence, state: State, *,
                 pause: Optional[Callable[[str], None]] = None, transitive: bool = True, destroy_only_keys: bool = False,
                 purge_deadline_s: float = purge.DEFAULT_DEADLINE_S, purge_interval_s: float = 3.0,
                 broken_delete_foreign_log_rows: bool = False, es_log: Optional[Callable[[], EsClient]] = None,
                 broken_skip_authz: bool = False, broken_relabel_keeps_old_copy: bool = False, broken_relabel_all_classes: bool = False,
                 broken_no_identifier_hmacs: bool = False, broken_publish_before_closure: bool = False, broken_replay_keeps_keys: bool = False,
                 broken_no_query_net: bool = False, broken_current_generation_only: bool = False, broken_string_closure: str = ""):
        self._er, self._st, self.tenant, self._ks = es_erase, es_struct, tenant, keystore
        self._j, self._gate, self._fence, self.state = journal, gate, fence, state
        self._pause = pause or (lambda s: None)
        self._transitive, self._keys_only = transitive, destroy_only_keys
        self._pd, self._pi = purge_deadline_s, purge_interval_s
        self._lg = es_log or es_struct   # the rewrite of a log row needs write on the log indices (the erase key has delete only)
        self._del_foreign = broken_delete_foreign_log_rows  # BROKEN variant: another actor's history is deleted too
        self._skip_authz = broken_skip_authz   # BROKEN variant: the closure replay does not cover the authz index
        self._relabel_keeps = broken_relabel_keeps_old_copy   # BROKEN variant: the relabel publishes the tombstone and erases nothing
        self._no_hmacs = broken_no_identifier_hmacs
        self._early = broken_publish_before_closure
        self._no_query_net, self._cur_only, self._string = broken_no_query_net, broken_current_generation_only, broken_string_closure
        self._keep_keys = broken_replay_keeps_keys   # BROKEN variant: a restore's replay leaves the keys named by newer tombstones
        self._relabel_all = broken_relabel_all_classes       # BROKEN variant: the relabel closure ignores the class

    # ---- closure ---------------------------------------------------------------------------------------------
    def closure(self, subject: str, item_leaves: Iterable[str] = ()) -> tuple[dict, set]:
        """Return ({(index, id): Hit}, erased leaf ids). Subject (or the named items), then leaf ids, then upward through
        lin_nodes. An item erasure (DESIGN 14.1) names leaf ids and no subject."""
        er = self._er()
        hits = {(h.index, h.id): h for h in scan.find(er, self.tenant, subjects=[subject] if subject else [])}
        leaves = {lid for h in hits.values() if h.kind in ("src", "leaf") for lid in h.lin_leaf_ids} | set(item_leaves)
        if leaves:
            for h in scan.find(er, self.tenant, leaf_ids=leaves):
                hits[(h.index, h.id)] = h
        sids = self._own_search_ids(hits, subject) if subject and not self._no_query_net else set()
        if sids:   # the second net (DESIGN 14.2, E9): everything an erased search shaped
            for h in scan.find(er, self.tenant, queries=sids):
                hits.setdefault((h.index, h.id), h)
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
        if self._cur_only:   # BROKEN variant: only the newest version of each node is erased, the retained generations keep theirs
            top = {}
            for h in hits.values():
                if h.kind == "node":
                    stem, _, v = h.id.rpartition(":")
                    top[stem] = max(top.get(stem, 0), int(v or 0))
            hits = {k: h for k, h in hits.items() if h.kind != "node" or int(h.id.rpartition(":")[2] or 0) == top[h.id.rpartition(":")[0]]}
        if self._string:     # BROKEN variant: the closure is a string search over the bodies, not the lineage
            hits = {(h.index, h.id): h for h in scan.find_text(er, self.tenant, self._string)}
        if self._skip_authz:
            hits = {k: h for k, h in hits.items() if not h.index.startswith(f"lc-{self.tenant}-authz")}
        return hits, leaves

    def _scope(self, subject: str, leaves: list, redact: list, identifiers: Iterable[str], bare_tokens: Iterable[str]) -> dict:
        scope = {"subject": subject, "leaf_ids": leaves, "redact": redact}
        if not self._no_hmacs:   # BROKEN variant: the journal records no erased-identifier HMACs
            scope["erased_ids"] = sorted(({subject} if subject else set()) | set(identifiers))
            scope["erased_bare"] = sorted(set(bare_tokens))
        return scope

    @staticmethod
    def _own_search_ids(hits: dict, subject: str) -> set:
        """Search ids of the rows and stored queries that are the subject's own (the actor, or the query mentions the subject):
        a row that only offered an item of the subject is another actor's history and shapes nothing that must go."""
        out = set()
        for h in hits.values():
            if h.kind in ("log", "sq") and h.search_id:
                own = (set(h.lin_subjects) - set(h.item_subjects)) | {h.actor}
                if subject in own:
                    out.add(h.search_id)
        return out

    # ---- the request ------------------------------------------------------------------------------------------
    def erase_item(self, leaf_ids: Iterable[str], *, holds=None, apply_guard: Callable[[], None] = lambda: None) -> Receipt:
        """Erase one or more items (DESIGN 14.1): every version of the named leaves and everything derived from them."""
        return self.erase_subject("", item_leaves=leaf_ids, holds=holds, apply_guard=apply_guard)

    def erase_subject(self, subject: str, *, identifiers: Iterable[str] = (), bare_tokens: Iterable[str] = (),
                      item_leaves: Iterable[str] = (), holds=None, apply_guard: Callable[[], None] = lambda: None) -> Receipt:
        """`holds` (a `HoldRegistry`) is checked against the closure under the write gate, before anything is published: a hold on a
        leaf the closure reaches, or on a subject of a leaf it reaches, raises `Held`. `identifiers` and `bare_tokens` are the erased identifier and bare-name HMAC tokens: they go into the journal
        record so a restart or a restore rebuilds the guard that refuses re-ingest (E3; the journal holds HMACs, no content)."""
        t0 = time.monotonic()
        er, rc = self._er(), Receipt()
        with self._gate.exclusive():
            er.request("POST", f"/lc-{self.tenant}-*/_refresh")
            hits, leaves = self.closure(subject, item_leaves)
            self._pause("closure")
            if holds is not None:
                toks = {t for h in hits.values() if h.kind in ("src", "leaf") for t in h.lin_subjects}
                if holds.covered(subject, leaves, toks):
                    raise Held()
            pub = hits
            if self._early:   # BROKEN variant: the boundary publishes a redact set from a closure that is not complete yet
                keep, self._transitive = self._transitive, False
                try:
                    pub, _ = self.closure(subject, item_leaves)
                finally:
                    self._transitive = keep
            redact = sorted({h.id for h in pub.values()} | leaves)
            seq = self._j.append(self._scope(subject, sorted(leaves), redact, identifiers, bare_tokens))
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
        return self._physical(hits, leaves, subject, seq, rc)

    def replay(self, subject: str, leaf_ids: Iterable[str], seq: int, *, apply_guard: Callable[[], None] = lambda: None,
               run_purge: bool = True) -> Receipt:
        """A restore (DESIGN 14.6 steps 1 and 2): the journal tombstone `seq` is re-recorded in `ctl`, made effective in memory, and
        its closure is replayed over EVERY index of the tenant (authz and control included) with the same deletes, strips and
        rewrites as the original erasure. Runs with the tenant disabled; takes no journal append."""
        t0 = time.monotonic()
        er, rc = self._er(), Receipt()
        leaves = set(leaf_ids)
        with self._gate.exclusive():
            er.request("POST", f"/lc-{self.tenant}-*/_refresh")
            hits, found = self.closure(subject, leaves)
            leaves |= found
            if leaves:
                for h in scan.find(er, self.tenant, leaf_ids=leaves):
                    hits[(h.index, h.id)] = h
            redact = sorted({h.id for h in hits.values()} | leaves)
            tomb = {"tenant": self.tenant, "cls": "tenantwide", "kind": "tomb", "lin_leaves": [], "lin_leaf_ids": [],
                    "lin_nodes": [], "lin_subjects": [], "lin_queries": [], "derivation": "erase", "erasure_rewrite": 0,
                    "created_at": datetime.datetime.now(datetime.timezone.utc).isoformat(), "schema_v": 1, "seq": seq}
            self._st().request("PUT", f"/{tenant_index(self.tenant, 'ctl')}/_doc/tb:{seq}", tomb)
            er.request("POST", f"/{tenant_index(self.tenant, 'ctl')}/_refresh")
            with self._fence.exclusive():
                self.state.apply(seq, redact, leaves)
                apply_guard()
            rc.seq, rc.boundary_s = seq, time.monotonic() - t0
        return self._physical(hits, leaves, subject, seq, rc, run_purge=run_purge, replaying=True)

    # ---- class relabel (DESIGN 14.1, review F19c) -----------------------------------------------------------------------
    def class_closure(self, leaf_ids: Iterable[str], cls: str) -> tuple[dict, dict]:
        """The old-class copy of the leaves: documents that name a leaf id and live in an index of class `cls` (or are the
        tenant-wide source records of its versions), then upward through `lin_nodes` inside the class. Returns the hits and
        {leaf id: highest version that existed}. Documents of other classes are never in it."""
        er, leaf_ids = self._er(), set(leaf_ids)
        hits, lv_max = {}, {}
        for h in scan.find(er, self.tenant, leaf_ids=leaf_ids):
            icls = scan.index_class(self.tenant, h.index)
            if icls == cls or (icls == "tenantwide" and h.kind == "src") or self._relabel_all:
                hits[(h.index, h.id)] = h
                if h.kind in ("src", "leaf") and ":" in h.id:
                    lid, _, lv = h.id.rpartition(":")
                    if lid in leaf_ids and lv.isdigit():
                        lv_max[lid] = max(lv_max.get(lid, 0), int(lv))
        frontier = {h.id for h in hits.values() if h.kind == "node"}
        while frontier:
            nxt = set()
            for h in scan.find(er, self.tenant, nodes=frontier):
                if (scan.index_class(self.tenant, h.index) == cls or self._relabel_all) and (h.index, h.id) not in hits:
                    hits[(h.index, h.id)] = h
                    if h.kind == "node":
                        nxt.add(h.id)
            frontier = nxt
        return hits, lv_max

    @staticmethod
    def _within(h, lv_max: dict) -> bool:
        """Source and leaf documents of a version above the relabelled one (the new-class copy) are not part of the old copy."""
        if h.kind in ("src", "leaf") and ":" in h.id:
            lid, _, lv = h.id.rpartition(":")
            return not (lid in lv_max and lv.isdigit() and int(lv) > int(lv_max[lid]))
        return True

    def relabel_erase(self, leaf_ids: Iterable[str], old_cls: str, *, apply_guard: Callable[[], None] = lambda: None) -> Receipt:
        """An upward relabel is an erasure of the old-class copy: a tombstone with scope (leaf ids, old class) and its redact set is
        published at the boundary, then the usual keys, deletes, rewrites and purge of every old-class artefact derived from it.
        The caller writes the new version (a higher `lv`) in the new class afterwards."""
        t0, er, rc = time.monotonic(), self._er(), Receipt()
        leaf_ids = sorted(set(leaf_ids))
        with self._gate.exclusive():
            er.request("POST", f"/lc-{self.tenant}-*/_refresh")
            hits, lv_max = self.class_closure(leaf_ids, old_cls)
            redact = sorted({h.id for h in hits.values()})
            seq = self._j.append({"subject": "", "leaf_ids": leaf_ids, "cls": old_cls, "lv_max": lv_max, "redact": redact})
            self._record_and_publish(seq, redact, set(), rc, t0, apply_guard)
        self._pause("boundary")
        if self._relabel_keeps:
            return rc
        return self._physical(hits, set(leaf_ids), "", seq, rc, cls=old_cls, lv_max=lv_max)

    def replay_relabel(self, leaf_ids: Iterable[str], old_cls: str, seq: int, lv_max: dict, *, run_purge: bool = True) -> Receipt:
        """The restore's replay of a relabel tombstone: the old-class closure only; the new-class version (a higher `lv`) stays."""
        t0, er, rc = time.monotonic(), self._er(), Receipt()
        with self._gate.exclusive():
            er.request("POST", f"/lc-{self.tenant}-*/_refresh")
            hits, _ = self.class_closure(leaf_ids, old_cls)
            hits = {k: h for k, h in hits.items() if self._relabel_all or self._within(h, lv_max)}
            redact = sorted({h.id for h in hits.values()})
            self._record_and_publish(seq, redact, set(), rc, t0, lambda: None)
        return self._physical(hits, set(leaf_ids), "", seq, rc, run_purge=run_purge, cls=old_cls, lv_max=lv_max, replaying=True)

    def _record_and_publish(self, seq: int, redact: list, leaves: set, rc: Receipt, t0: float, apply_guard) -> None:
        er = self._er()
        tomb = {"tenant": self.tenant, "cls": "tenantwide", "kind": "tomb", "lin_leaves": [], "lin_leaf_ids": [],
                "lin_nodes": [], "lin_subjects": [], "lin_queries": [], "derivation": "erase", "erasure_rewrite": 0,
                "created_at": datetime.datetime.now(datetime.timezone.utc).isoformat(), "schema_v": 1, "seq": seq}
        self._st().request("PUT", f"/{tenant_index(self.tenant, 'ctl')}/_doc/tb:{seq}", tomb)
        er.request("POST", f"/{tenant_index(self.tenant, 'ctl')}/_refresh")
        with self._fence.exclusive():
            self.state.apply(seq, redact, leaves)
            apply_guard()
        rc.seq, rc.boundary_s = seq, time.monotonic() - t0

    def _physical(self, hits: dict, leaves: set, subject: str, seq: int, rc: Receipt, *, run_purge: bool = True,
                  cls: Optional[str] = None, lv_max: Optional[dict] = None, replaying: bool = False) -> Receipt:
        er = self._er()
        rc.closure = {}
        for h in hits.values():
            rc.closure[h.kind] = rc.closure.get(h.kind, 0) + 1
            key = (h.kind, scan.index_class(self.tenant, h.index))
            rc.by_class[key] = rc.by_class.get(key, 0) + 1
        if replaying and self._keep_keys:
            pass
        elif cls is not None:   # a relabel destroys the keys of the old versions only
            rc.keys_destroyed = len(self._ks.destroy_versions((lid, (lv_max or {}).get(lid, 0)) for lid in leaves))
        else:
            rc.keys_destroyed = len(self._ks.destroy(subject_token=subject)) if subject else 0
            rc.keys_destroyed += len(self._ks.destroy(leaf_ids=leaves))
        if self._keys_only:  # BROKEN variant: the keys are gone, the plaintext projections stay
            rc.recount = len(scan.find(er, self.tenant, subjects=[subject] if subject else [], leaf_ids=leaves if cls else ()))
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
            for ix, res in ex.map(one, sorted(touched) if run_purge else []):
                rc.purge_s[ix] = round(res.seconds, 1)
                rc.purge_left[ix] = res.deleted_left
                rc.purge_ok &= res.ok
        er.request("POST", f"/lc-{self.tenant}-*/_refresh")
        remaining = [h for h in scan.find(er, self.tenant, subjects=[subject] if subject else [], leaf_ids=leaves)
                     if h.kind not in KEEP_KINDS | REWRITE_KINDS]
        if cls is not None:   # the new-class copy names the same leaf ids: only the old class's documents are residue
            remaining = [h for h in remaining if self._within(h, lv_max or {}) and
                         (scan.index_class(self.tenant, h.index) == cls or h.kind == "src")]
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
        if subject and subject in own:
            rc.deleted += scan.delete(self._er(), [h])
            return True
        s.update(pointer={}, lin_leaf_ids=[], item_subjects=[], lin_subjects=sorted(own - {""}), erasure_rewrite=seq)
        code, _ = st.request("PUT", f"/{h.index}/_doc/{h.id}?if_seq_no={doc['_seq_no']}&if_primary_term={doc['_primary_term']}", s)
        if code not in (200, 201):
            raise scan.ScanError(f"log row rewrite failed ({code})")
        rc.stripped += 1
        return True
