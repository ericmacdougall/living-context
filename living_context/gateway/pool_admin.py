"""Pools as first-class objects: create, list, describe, detach, reattach, grant and remove a grant, revoke one token
(POOLS 1, 2.2, 2.5, 9.4; BUILD-PLAN Step 25, stage 0 "layout 1").

* Only a tenant administrator creates a pool, detaches it, or grants and removes pool operations (2.2). No self-grant. A `query` or
  `review` grant to a tenant administrator needs a second administrator in the design; it is REFUSED here (deviation: the
  two-administrator path for it is not built in this version, so no administrator reads content through a pool grant).
* A pool's own leaf index is created with the pool: one shard, an alias over a physical index with a random suffix. Its control
  documents (`pm:<pool>`: manifest and state) live in the class control index and are read only through the class reader.
* Pool-grant removal, detach, reattach and single-token revoke are journalled and fenced like a revoke (2.5): the in-memory change
  under the exclusive side of the send fence is the denial; the journal append and fsync follow; the authz copy is best effort.
  On start the directory is built from the authz copy and THEN the journal is applied on top, so a removed grant stays removed.
  Every write of a pool or grant record is a compare-and-set against the record it read (`_authz_put`), so a write built before a
  change can never land after the change's own write (review round 8); a removal writes a record with no operations, never a delete;
  the journal keeps a removal until a write of the grant made after it has landed (never on what a read shows), and pool counters
  are reserved in the journal per process (`reserve_counters`), so the counter order of a grant and its removal holds across restarts.
* Every pool-related refusal is the one `unavailable` answer (2.7). A caller who is not entitled to see a pool gets the same
  answer for a pool that does not exist.
* Seeing a pool (pool_list, pool_describe) is the one live visibility check `pool_authz.sees` at the call (a scoped token: some scope
  operation passes check 4 now; a tenant administrator's plain session: every pool; any other plain session: some operation passes
  check 4 now), and every pool an answer names (the pool_create receipt included, whose call is the administrator check) is carried
  with `VIEW`, so the send hand-off re-checks it under the fence and an ungrant, detach or revoke aborts a built answer (review F16).
"""
from __future__ import annotations

import datetime
import threading
from typing import Any, Callable, Mapping, Optional

from ..policy import ClassLattice, Principal, Role
from ..policy import pools as pl
from .class_reader import ClassReader, ReaderRefused
from .pool_authz import VIEW, carry, view
from .indices import POOL_KINDS, TENANT_CLS, physical_name, pool_index
from .provision import PURGE_SETTINGS, SLOWLOG_OFF, mapping_for
from .reads import IncompleteRead, put_doc, put_doc_landed, read_doc
from .revocation import RevocationService, Refused as TenantClosed
from .sessions import Record, Sessions

UNAVAILABLE = pl.UNAVAILABLE
REFUSED = {"ok": False, "error": "refused"}
POOL_OPS = frozenset({"pool_create", "pool_list", "pool_describe", "pool_detach", "pool_reattach", "pool_grant", "pool_ungrant",
                      "token_revoke", "pool_delete"})
COUNTER = "pool_counter"   # the epoch-journal record of a counter reservation (`PoolService.reserve_counters`)
COUNTER_BLOCK = 2 ** 32    # counters one process may take; the next start begins a block above the last ceiling
CAS_TRIES = 20             # lost compare-and-sets in a row before an authz write is refused (concurrent writes of one record)


def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _base(tenant: str, cls: str, kind: str, subjects: list, **extra) -> dict:
    d = {"tenant": tenant, "cls": cls, "kind": kind, "lin_leaves": [], "lin_leaf_ids": [], "lin_nodes": [],
         "lin_subjects": subjects, "lin_queries": [], "derivation": "pool", "erasure_rewrite": 0, "created_at": _now(),
         "schema_v": 1}
    d.update(extra)
    return d


class PoolService:
    def __init__(self, tenant: str, directory: pl.PoolDirectory, sessions: Sessions, revocation: RevocationService,
                 lattice: ClassLattice, principal_token: Callable[[str, str], str], *,
                 create_index: Optional[Callable[[str, dict], None]] = None, ctl_client: Optional[Callable[[str], Any]] = None,
                 authz_client: Optional[Callable[[], Any]] = None, reader: Optional[ClassReader] = None,
                 grants: Optional[Callable[[str, str], Optional[Principal]]] = None,
                 broken_removal_outside_journal: bool = False, broken_state_not_journalled: bool = False,
                 broken_self_grant: bool = False, broken_member_creates: bool = False,
                 eraser: Optional[Callable[[str, str], Any]] = None, clock: Callable[[], float] = None,
                 pending_s: float = 900.0, broken_single_admin_delete: bool = False):
        self.tenant, self.dir, self.sessions, self.rev, self.lattice = tenant, directory, sessions, revocation, lattice
        self._grants = grants or (lambda t, k: None)
        self._tok, self._create_index, self._ctl, self._authz, self.reader = principal_token, create_index, ctl_client, authz_client, reader
        self._b = dict(outside=broken_removal_outside_journal, state=broken_state_not_journalled, self_grant=broken_self_grant,
                       member=broken_member_creates)
        import time as _t
        self._eraser, self._clock, self._pending_s = eraser, clock or _t.time, pending_s
        self._del_pending: dict[str, tuple[str, float]] = {}
        self._b['single'] = broken_single_admin_delete
        self._unconfirmed: dict[tuple, dict] = {}   # pool-grant removals whose authz write has not landed (kept by compaction)
        self._ulock = threading.Lock()              # `_unconfirmed` and `_ceiling`: the call threads, start-up and compaction
        self._ceiling = 0                           # the highest counter ceiling the journal holds (`reserve_counters`)
        revocation.extra_replay = self.apply_record
        revocation.extra_snapshot = self.snapshot_records

    # ---- journal records: replay and compaction ----------------------------------------------------------------------------
    def apply_record(self, r: Mapping) -> None:
        t = r.get("t")
        if t == "pool_grant_rm":
            self.dir.remove_grant(self.tenant, r["principal"], r["pool"], r.get("ops"), counter=int(r["counter"]))
            with self._ulock:
                self._unconfirmed[(r["principal"], r["pool"], int(r["counter"]))] = dict(r)
        elif t == "pool_state":
            if self.dir.get(self.tenant, r["pool"]) is not None:
                self.dir.set_state(self.tenant, r["pool"], r["state"], counter=int(r["counter"]))
        elif t == "token_revoke":
            self.sessions.restore_token_revoke(r["hash"], float(r["exp"]))
        elif t == COUNTER:
            with self._ulock:
                self._ceiling = max(self._ceiling, int(r["ceiling"]))
            self.dir.observe_counter(self.tenant, int(r["ceiling"]))   # every counter an earlier process took is below its ceiling

    def snapshot_records(self) -> list:
        with self._ulock:
            out = [dict(r) for r in self._unconfirmed.values()]
            out += [{"t": COUNTER, "ceiling": self._ceiling}] if self._ceiling else []
        out += [{"t": "pool_state", "pool": p.id, "state": p.state, "counter": p.state_counter}
                for p in self.dir.pools_of(self.tenant) if p.state != pl.ACTIVE]
        out += [{"t": "token_revoke", "hash": h, "exp": e} for h, e in self.sessions.revoked_snapshot()]
        return out

    def reserve_counters(self) -> None:
        """At start-up, after the journal opened and replayed, before any change: reserve this process's block of the tenant's counters
        in the journal (a `pool_counter` record with its ceiling, fsynced; compaction keeps the latest) and start above every counter an
        earlier process could have taken. The ordering of a pool grant's removal against a grant (`PoolDirectory.remove_grant`) then
        holds across restarts without a wall clock (review round 7, C4): a grant whose write was still in flight when its process
        stopped carries a counter below the next process's removals. A start with no ceiling in the journal (the first, or one written
        before this record) skips a whole block above what it observed. Refused (`revocation.Refused`) when the record is not durable."""
        with self._ulock:
            floor = max(self.dir.counter(self.tenant), self._ceiling) + COUNTER_BLOCK
        ceiling = floor + COUNTER_BLOCK

        def apply():
            self.dir.observe_counter(self.tenant, floor)
            self.dir.limit_counter(self.tenant, ceiling)
            with self._ulock:
                self._ceiling = ceiling
        _, rc = self.rev.journalled(apply, lambda _: {"t": COUNTER, "ceiling": ceiling})
        if rc["durable_at"] is None:
            raise TenantClosed("the counter reservation is not durable")

    def confirm_against_authz(self, *, complete: bool) -> None:
        """After start-up (`reserve_counters` first): each pool-grant removal still unconfirmed is written again, the grant as memory now
        holds it (`_persist_grant`), and is confirmed only when that write lands. A read never confirms a removal (review round 8): a
        grant write of an earlier process may still be in flight, and only a landed write is past it (a compare-and-set it cannot pass
        any more); a grant absent from the copy, or held without the removed operations, proves nothing. Nothing is written or confirmed
        after an INCOMPLETE read (a partial or refused record), which is no account of what memory was rebuilt from."""
        if not complete:
            return
        with self._ulock:
            rows = sorted({(k[0], k[1]) for k in self._unconfirmed})
        for principal, pool in rows:
            self._rewrite(principal, pool)

    def _rewrite(self, principal: str, pool: str) -> None:
        """The start-up write of one grant. A failure is not an answer to anyone and confirms nothing: only `_persist_grant`, after its
        write landed, drops a removal's record, so a failed write leaves the removal journalled for the next start; the other grants
        are still written (tests/unit/test_write_failures.py, ALLOWED)."""
        try:
            self._persist_grant(principal, pool)
        except Exception:
            pass

    # ---- persistence (best effort; never blocks a call) -----------------------------------------------------------------------
    def _authz_put(self, doc_id: str, build: Callable[[], tuple[dict, Any]]) -> Any:
        """Write the record `doc_id` as memory holds it, ordered against every other write of it by a compare-and-set (review round 8):
        read the stored record (`reads.read_doc`), THEN take the record from memory (`build()`: the body and a mark), then write it
        conditioned on what was read (`if_seq_no` and `if_primary_term`, or `op_type=create` for an absent record). A write that was
        built before a change and lands after it is refused by the index: the later change's own write moved the record on (and a removal
        writes a record too, `ops: []`, never a delete, so the record is never absent again). The typed version conflict of a lost race is
        retried (`CAS_TRIES`); only a well-formed acknowledgement about exactly this record counts as written (`reads.doc_written`), and
        any other answer raises (review round 6, F18). Returns the mark of the body that landed."""
        idx = f"lc-{self.tenant}-authz"
        try:
            for _ in range(CAS_TRIES):
                cur = read_doc(self._authz(), idx, doc_id, alias=True)
                body, mark = build()
                cas = ("op_type=create",) if cur is None else (f"if_seq_no={cur['_seq_no']}", f"if_primary_term={cur['_primary_term']}")
                if put_doc(self._authz(), idx, doc_id, body, "refresh=true", *cas, alias=True):
                    return mark
        except IncompleteRead as e:
            raise RuntimeError("authz write refused") from e
        raise RuntimeError("authz write not landed (lost compare-and-set)")

    def _persist_pool(self, p: pl.Pool) -> None:
        """The pool record as memory holds it when written (a pool no longer in memory is not written: its delete removes the record)."""
        def build():
            q = self.dir.get(self.tenant, p.id)
            if q is None:
                raise pl.PoolError("pool not in memory")
            return _base(self.tenant, TENANT_CLS, "pool", [], pool=q.id, pool_cls=q.cls, pool_kind=q.kind, label=q.label, card=q.card,
                         private=q.private, state=q.state, counter=q.state_counter), None
        self._authz_put(f"pp:{p.id}", build)

    def _persist_grant(self, principal: str, pool: str) -> None:
        """The grant record as memory holds it when written; no operations left is a record with `ops: []` (a removal is a write, never a
        delete). A landed write confirms every removal of this grant made up to its mark (the counter when its body was taken): its body
        holds them, and every write built before it can no longer land."""
        def build():
            ops, counter, mark = self.dir.grant_state(self.tenant, principal, pool)
            return _base(self.tenant, TENANT_CLS, "pgrant", [principal], principal=principal, pool=pool, ops=sorted(ops),
                         counter=counter), mark
        mark = self._authz_put(f"pg:{principal}:{pool}", build)
        with self._ulock:
            for k in [k for k in self._unconfirmed if k[0] == principal and k[1] == pool and k[2] <= mark]:
                del self._unconfirmed[k]

    # ---- authorization of the people operations ------------------------------------------------------------------------------
    def _is_admin(self, p: Principal) -> bool:
        return Role.TENANT_ADMIN in p.roles and p.tenant == self.tenant

    def _view(self, rec: Optional[Record], pool_ids) -> dict:
        """The pools of `pool_ids` that `rec` sees now, through the one live visibility check (`pool_authz.view`: live grant, class and
        state, or the tenant administrator's role), each carried with VIEW so the send hand-off re-checks it (review F16)."""
        return view(self.sessions, self.dir, {self.tenant: self.lattice}, rec, pool_ids) if rec is not None else {}

    # ---- the operations ------------------------------------------------------------------------------------------------------
    def handle(self, op: str, p: Principal, rec: Record, req: Mapping[str, Any]) -> dict:
        try:
            if op == "pool_list":
                return self.list(p, rec)
            if op == "pool_describe":
                return self.describe(p, rec, req.get("pool"))
            if op == "token_revoke":
                return self.token_revoke(p, rec, req.get("token_id"))
            if not self._is_admin(p) and not self._b["member"]:
                return UNAVAILABLE   # the same answer a missing pool gets
            if op == "pool_create":
                return self.create(p, req)
            if op == "pool_detach":
                return self.set_state(req.get("pool"), pl.DETACHED)
            if op == "pool_reattach":
                return self.set_state(req.get("pool"), pl.ACTIVE)
            if op == "pool_grant":
                return self.grant(p, req)
            if op == "pool_ungrant":
                return self.ungrant(p, req)
            if op == "pool_delete":
                return self.delete(p, req.get("pool"))
        except (TenantClosed, ReaderRefused, pl.PoolError, KeyError, TypeError, ValueError):
            return UNAVAILABLE
        return REFUSED

    def create(self, p: Principal, req: Mapping[str, Any]) -> dict:
        cls, kind, label, card = req.get("cls"), req.get("kind"), req.get("label"), req.get("card", "")
        if not isinstance(cls, str) or not self.lattice.declared(cls):
            return UNAVAILABLE
        pool = self.dir.create(self.tenant, cls, kind, label, card)   # PoolError -> unavailable in handle
        alias = pool_index(self.tenant, cls, pool.id, "leaf")
        try:
            if self._create_index is not None:
                phys = physical_name(alias)
                self._create_index(phys, {"settings": {"number_of_shards": 1, "number_of_replicas": 0, **SLOWLOG_OFF, **PURGE_SETTINGS},
                                          "mappings": mapping_for("leaf"), "aliases": {alias: {}}})
                # the manifest names the pool's indices for erasure discovery (erasure/pool_scope.py): a pool whose manifest did not
                # land is not created (an IncompleteRead is caught below)
                put_doc_landed(self._ctl(cls), f"lc-{self.tenant}-{cls}-ctl", f"pm:{pool.id}", _base(
                    self.tenant, cls, "manifest", [], pool=pool.id, indices=[alias, phys], pool_kind=pool.kind, state=pl.ACTIVE),
                    "refresh=true", alias=True)
            self._persist_pool(pool)
        except Exception:
            self.dir.drop(self.tenant, pool.id)
            return UNAVAILABLE
        # the receipt names the pool: carried with VIEW, so the hand-off re-checks that the caller sees it (review F16). At the call the
        # authority is the administrator check in `handle`, which is `pool_authz.sees`'s administrator lane for a pool that exists.
        carry({pool.id: {VIEW}})
        return {"ok": True, "pool": pool.id}

    def list(self, p: Principal, rec: Optional[Record]) -> dict:
        seen = self._view(rec, [pool.id for pool in self.dir.pools_of(self.tenant)])
        return {"ok": True, "pools": [{"pool": pool.id, "label": pool.label, "cls": pool.cls, "kind": pool.kind, "state": pool.state}
                                      for pool in seen.values()]}

    def describe(self, p: Principal, rec: Optional[Record], pool_id: Any) -> dict:
        pool = self._view(rec, [pool_id]).get(pool_id) if isinstance(pool_id, str) else None
        if pool is None:
            return UNAVAILABLE
        return {"ok": True, "pool": pool.id, "label": pool.label, "card": pool.card, "cls": pool.cls, "kind": pool.kind,
                "private": pool.private, "state": pool.state}

    def set_state(self, pool_id: Any, state: str) -> dict:
        pool = self.dir.get(self.tenant, pool_id) if isinstance(pool_id, str) else None
        if pool is None or pool.state == pl.DELETING:
            return UNAVAILABLE
        if self._b["state"]:     # BROKEN variant: the state change is memory only, no journal record
            self.dir.set_state(self.tenant, pool_id, state)
            return {"ok": True, "denied": True, "durable": False}
        res, rc = self.rev.journalled(lambda: self.dir.set_state(self.tenant, pool_id, state),
                                      lambda np: {"t": "pool_state", "pool": np.id, "state": np.state, "counter": np.state_counter},
                                      persist=self._persist_pool)
        return {"ok": True, "denied": True, "durable": rc["durable_at"] is not None}

    def grant(self, p: Principal, req: Mapping[str, Any]) -> dict:
        who, pool_id = req.get("who"), req.get("pool")
        ops = frozenset(req.get("ops") or pl.DEFAULT_GRANT)
        pool = self.dir.get(self.tenant, pool_id) if isinstance(pool_id, str) else None
        if pool is None or not isinstance(who, str) or not who or pool.state == pl.DELETING:
            return UNAVAILABLE
        target = self._tok(self.tenant, who)
        if target == p.token and not self._b["self_grant"]:
            return REFUSED   # no self-grant (2.2)
        if not ops <= pl.OPERATIONS:
            return REFUSED
        tgt = self._grants_of_principal(target)
        if tgt is not None and Role.TENANT_ADMIN in tgt.roles and ops & {pl.QUERY, pl.REVIEW}:
            return REFUSED   # a second administrator is needed in the design; not built: no administrator reads through a grant
        self.dir.grant(self.tenant, target, pool_id, ops)
        try:
            self._persist_grant(target, pool_id)
        except Exception:
            pass   # best effort
        return {"ok": True}

    def _grants_of_principal(self, token: str) -> Optional[Principal]:
        return self._grants(self.tenant, token)

    def ungrant(self, p: Principal, req: Mapping[str, Any]) -> dict:
        who, pool_id = req.get("who"), req.get("pool")
        ops = req.get("ops")
        if not isinstance(who, str) or not isinstance(pool_id, str) or self.dir.get(self.tenant, pool_id) is None:
            return UNAVAILABLE
        target = self._tok(self.tenant, who)
        tgt = self._grants_of_principal(target)
        if tgt is not None and Role.TENANT_ADMIN in tgt.roles:
            return REFUSED   # removing an administrator's grant needs two administrators (unchanged): not built here
        if ops is not None and not frozenset(ops) <= pl.OPERATIONS:
            return REFUSED
        opset = None if ops is None else sorted(set(ops))
        if self._b["outside"]:    # BROKEN variant: the table is changed outside the fence and no record is appended
            self.dir.remove_grant(self.tenant, target, pool_id, opset)
            return {"ok": True, "denied": True, "durable": False}

        def record(c):
            r = {"t": "pool_grant_rm", "principal": target, "pool": pool_id, "ops": opset, "counter": c}
            with self._ulock:
                self._unconfirmed[(target, pool_id, c)] = r
            return r

        # the persist confirms the removal when its write lands (`_persist_grant`); a failed one leaves it to the next start
        _, rc = self.rev.journalled(lambda: self.dir.remove_grant(self.tenant, target, pool_id, opset), record,
                                    persist=lambda c: self._persist_grant(target, pool_id))
        return {"ok": True, "denied": True, "durable": rc["durable_at"] is not None}

    def token_revoke(self, p: Principal, rec: Record, token_id: Any) -> dict:
        """The harness revokes the token it holds (the bearer); a tenant administrator revokes any token of the tenant by id."""
        if token_id is None:
            h = rec.hash if rec is not None and rec.tenant == self.tenant else None
        elif self._is_admin(p) and isinstance(token_id, str):
            h = self.sessions.find_by_id(self.tenant, token_id)   # tenant-bound: a token of another tenant is never found
        else:
            return UNAVAILABLE
        if h is None:
            return UNAVAILABLE
        def apply():
            r = self.sessions.revoke_hash(h, self.tenant)
            if r is None:
                raise KeyError("no such token in this tenant")   # nothing is journalled; `handle` answers unavailable
            return r
        res, rc = self.rev.journalled(apply, lambda r: {"t": "token_revoke", "hash": r[0], "exp": r[1]})
        return {"ok": True, "denied": True, "durable": rc["durable_at"] is not None}

    def delete(self, p: Principal, pool_id: Any) -> dict:
        """An erasure of scope `pool` (POOLS 9.4): requested by a tenant administrator and confirmed by a second, distinct one. The
        first call is `pending`; the second, within `pending_s`, runs it. The state goes to `deleting` first (journalled), so a write
        into the pool is `unavailable` from then on; then the erasure runs, and the pool leaves the directory."""
        pool = self.dir.get(self.tenant, pool_id) if isinstance(pool_id, str) else None
        if pool is None or self._eraser is None:
            return UNAVAILABLE
        now = self._clock()
        if not self._b["single"]:
            first = self._del_pending.get(pool_id)
            if first is None or first[1] < now or first[0] == p.token:
                self._del_pending[pool_id] = (p.token, now + self._pending_s)
                return {"ok": False, "error": "pending"}
            del self._del_pending[pool_id]
        if pool.state != pl.DELETING:
            self.set_state(pool_id, pl.DELETING)
        rc = self._eraser(pool.cls, pool.id)
        self.dir.drop(self.tenant, pool_id)
        return {"ok": True, "deleted": True, "documents": rc.docs_deleted, "indices": len(rc.indices_dropped)}
