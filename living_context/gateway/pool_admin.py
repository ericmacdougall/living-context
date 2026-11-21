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
* Every pool-related refusal is the one `unavailable` answer (2.7). A caller who is not entitled to see a pool gets the same
  answer for a pool that does not exist.
* Seeing a pool (pool_list, pool_describe, the pool_create receipt) is the one live visibility check `pool_authz.sees` at the call (a
  scoped token: some scope operation passes check 4 now; a tenant administrator's plain session: every pool; any other plain session:
  some operation passes check 4 now), and every pool an answer names is carried with `VIEW`, so the send hand-off re-checks it under the
  fence and an ungrant, detach or revoke aborts a built answer (review F16).
"""
from __future__ import annotations

import datetime
from typing import Any, Callable, Mapping, Optional

from ..policy import ClassLattice, Principal, Role
from ..policy import pools as pl
from .class_reader import ClassReader, ReaderRefused
from .pool_authz import view
from .indices import POOL_KINDS, TENANT_CLS, physical_name, pool_index
from .provision import PURGE_SETTINGS, SLOWLOG_OFF, mapping_for
from .revocation import RevocationService, Refused as TenantClosed
from .sessions import Record, Sessions

UNAVAILABLE = pl.UNAVAILABLE
REFUSED = {"ok": False, "error": "refused"}
POOL_OPS = frozenset({"pool_create", "pool_list", "pool_describe", "pool_detach", "pool_reattach", "pool_grant", "pool_ungrant",
                      "token_revoke", "pool_delete"})


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
        revocation.extra_replay = self.apply_record
        revocation.extra_snapshot = self.snapshot_records

    # ---- journal records: replay and compaction ----------------------------------------------------------------------------
    def apply_record(self, r: Mapping) -> None:
        t = r.get("t")
        if t == "pool_grant_rm":
            self.dir.remove_grant(self.tenant, r["principal"], r["pool"], r.get("ops"), counter=int(r["counter"]))
            self._unconfirmed[(r["principal"], r["pool"], int(r["counter"]))] = dict(r)
        elif t == "pool_state":
            if self.dir.get(self.tenant, r["pool"]) is not None:
                self.dir.set_state(self.tenant, r["pool"], r["state"], counter=int(r["counter"]))
        elif t == "token_revoke":
            self.sessions.restore_token_revoke(r["hash"], float(r["exp"]))

    def snapshot_records(self) -> list:
        out = [dict(r) for r in self._unconfirmed.values()]
        out += [{"t": "pool_state", "pool": p.id, "state": p.state, "counter": p.state_counter}
                for p in self.dir.pools_of(self.tenant) if p.state != pl.ACTIVE]
        out += [{"t": "token_revoke", "hash": h, "exp": e} for h, e in self.sessions.revoked_snapshot()]
        return out

    def confirm_against_authz(self, persisted: Mapping[tuple, frozenset], *, complete: bool) -> None:
        """After start-up: a removal whose grant is absent from the authz copy has landed, so compaction may drop its record. Only from a
        COMPLETE read: an absent grant in a partial read proves nothing, so nothing is confirmed."""
        if not complete:
            return
        for k, r in list(self._unconfirmed.items()):
            have = persisted.get((r["principal"], r["pool"]), frozenset())
            if not (have & frozenset(r.get("ops") or have)):
                del self._unconfirmed[k]

    # ---- persistence (best effort; never blocks a call) -----------------------------------------------------------------------
    def _authz_put(self, doc_id: str, doc: dict) -> None:
        st, _ = self._authz().request("PUT", f"/lc-{self.tenant}-authz/_doc/{doc_id}?refresh=true", doc)
        if st not in (200, 201):
            raise RuntimeError("authz write refused")

    def _authz_delete(self, doc_id: str) -> None:
        st, _ = self._authz().request("DELETE", f"/lc-{self.tenant}-authz/_doc/{doc_id}?refresh=true")
        if st not in (200, 404):
            raise RuntimeError("authz delete refused")

    def _persist_pool(self, p: pl.Pool) -> None:
        self._authz_put(f"pp:{p.id}", _base(self.tenant, TENANT_CLS, "pool", [], pool=p.id, pool_cls=p.cls, pool_kind=p.kind,
                                            label=p.label, card=p.card, private=p.private, state=p.state, counter=p.state_counter))

    def _persist_grant(self, principal: str, pool: str) -> None:
        ops = self.dir.live_ops(self.tenant, principal, pool)
        gid = f"pg:{principal}:{pool}"
        if not ops:
            self._authz_delete(gid)
            return
        row = [r for r in self.dir.grant_rows(self.tenant) if r[0] == principal and r[1] == pool][0]
        self._authz_put(gid, _base(self.tenant, TENANT_CLS, "pgrant", [principal], principal=principal, pool=pool,
                                   ops=sorted(ops), counter=row[3]))

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
                return self.create(p, rec, req)
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

    def create(self, p: Principal, rec: Optional[Record], req: Mapping[str, Any]) -> dict:
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
                self._ctl(cls).request("PUT", f"/lc-{self.tenant}-{cls}-ctl/_doc/pm:{pool.id}?refresh=true", _base(
                    self.tenant, cls, "manifest", [], pool=pool.id, indices=[alias, phys], pool_kind=pool.kind, state=pl.ACTIVE))
            self._persist_pool(pool)
        except Exception:
            self.dir.drop(self.tenant, pool.id)
            return UNAVAILABLE
        if pool.id not in self._view(rec, [pool.id]):   # the receipt names the pool: carried with VIEW like every answer that does
            return UNAVAILABLE
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
            self._unconfirmed[(target, pool_id, c)] = r
            return r

        def persist(c):
            self._persist_grant(target, pool_id)
            self._unconfirmed.pop((target, pool_id, c), None)   # the authz copy no longer holds the grant: the record is not needed

        _, rc = self.rev.journalled(lambda: self.dir.remove_grant(self.tenant, target, pool_id, opset), record, persist=persist)
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
