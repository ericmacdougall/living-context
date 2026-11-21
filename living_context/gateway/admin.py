"""The people operations of the gateway: grant, revoke, class-grant removal and the operator's break-glass revoke
(DESIGN 4.3, 4.4; BUILD-PLAN Step 15; SEC-R4, R5).

Every operation goes through `policy.decide`; the request body never names a tenant or a principal token (the tenant and the
actor come from the session; a target is named by its identifier, `who`, and mapped to its per-tenant token).

* grant: tenant administrators only, never to oneself (no self-grant), never the operator role, never a class set that is not
  down-closed, never to a revoked principal; after a break-glass recovery only the administrators the operator named may call it.
* revoke: a non-privileged principal by an administrator or a revoker. An administrator or a revoker is revoked only when two
  distinct administrators ask (the first call is recorded as pending for `PENDING_S`, the second executes).
* remove_class: takes one class (and every class that dominates it, so the read set stays down-closed) from a principal and bumps its
  epoch under the fence (an epoch bump, not a revoke): sessions of the principal end at their next check, a new one carries the reduced
  grants. A privileged target needs two administrators, like a revoke.
* break_glass_revoke: the operator's call, made from the operator's own session (the platform tenant), reaches any principal of any
  tenant and depends on nothing a tenant role holds. Tenant roles cannot remove it.

The keyword flags `broken_*` exist for the tests' broken variants only."""
from __future__ import annotations

import threading
import time
from typing import Any, Callable, Mapping, Optional

from ..policy import ClassLattice, Op, Principal, Role, decide
from ..policy.decide import validate_principal
from .revocation import Refused, RevocationService

OK_ANSWER = {"ok": True}
REFUSED = {"ok": False, "error": "refused"}
UNAVAILABLE = {"ok": False, "error": "unavailable"}
PENDING = {"ok": False, "error": "pending"}
ADMIN_OPS = frozenset({"grant", "revoke", "remove_class", "break_glass_revoke", "offboard_tenant"})
REVOKE_LANE_OPS = frozenset({"revoke", "remove_class", "break_glass_revoke", "offboard_tenant"})   # their own admission lane (DESIGN 4.4)
LANE_ROLES = {"revoke": frozenset({Role.TENANT_ADMIN, Role.REVOKER}), "remove_class": frozenset({Role.TENANT_ADMIN}),
              "break_glass_revoke": frozenset({Role.OPERATOR}), "offboard_tenant": frozenset({Role.OPERATOR})}   # who may use the lane for which call (the policy decides the rest)


def lane_allowed(op: Any, p: Principal) -> bool:
    """The revoke lane is for authenticated principals whose role admits the operation; a request merely shaped like a revoke
    (a reader's, an unknown role's) goes to the tenant's general lane, where the handler refuses it."""
    return isinstance(op, str) and op in LANE_ROLES and bool(LANE_ROLES[op] & p.roles)


PENDING_S = 900.0   # ASSUMPTION: a first administrator's call waits this long for the second


REDUCE = "grant_reduce"   # the epoch-journal record of a class-grant reduction


def narrower(a: Principal, b: Principal) -> bool:
    """Whether `a` holds nothing `b` does not (roles, read and write classes, own class)."""
    return a.roles <= b.roles and a.read_set <= b.read_set and a.write_set <= b.write_set and a.own_class in (None, b.own_class)


def meet(a: Principal, b: Principal) -> Principal:
    """What both grants hold. Never wider than either; down-closed read sets stay down-closed."""
    return Principal(a.tenant, a.token, a.roles & b.roles, a.read_set & b.read_set, a.write_set & b.write_set,
                     a.own_class if a.own_class == b.own_class else None)


def _of_record(tenant: str, r: Mapping) -> Principal:
    return Principal(tenant, r["principal"], frozenset(Role(x) for x in r["roles"]), frozenset(r["read"]), frozenset(r["write"]),
                     r.get("own_class") or None)


class GrantStore:
    """The gateway's own grants: (tenant, principal token) to roles and sets, memory first, with a copy in `lc-<t>-authz` (`persist`)
    that the next start reads back. Each change takes the next revision of the principal's grant, and the copy carries it. A revision
    is never reused, across restarts too: it is past both the last one and the current time in microseconds, so a write of a stopped
    process that lands late never carries the revision of a later change.

    * An addition (`put`) persists best effort: a write that does not land only loses access at the next start (fail safe), and it
      never fails the call. `put` refuses a change that takes anything away.
    * A REDUCTION (`reduce`: a class removal, a grant that takes roles or classes away) must hold across a restart even when its authz
      write is refused or fails (review round 7, the admin-lane form of F18). Before the call answers, it is journalled in the tenant's
      epoch journal (a `grant_reduce` record with the reduced grant and its revision, `RevocationService.journalled`). At start-up the
      record is replayed over the grant read back from the copy, which is narrowed to it (`meet`, never widened), and compaction keeps
      it until a validated authz write of that revision or a later one lands, or a COMPLETE start-up read shows the copy past that
      revision, at it with exactly the reduced grant, or holding no grant. This is the pool lane's design (pool_admin.py: unconfirmed
      removals) for the class grants.
    * A rollback (`rollback`: the undo of a change whose epoch bump failed, so the call raises) puts the earlier grant back, best effort.
    The store must be `attach`ed to the tenant's journal before the journal starts, so the replay reaches it."""

    def __init__(self, persist: Optional[Callable[..., None]] = None, *, clock: Callable[[], float] = time.time):
        self._g: dict[tuple[str, str], Principal] = {}
        self._rev: dict[tuple[str, str], int] = {}        # the revision of the grant in memory
        self._copy_rev: dict[tuple[str, str], int] = {}   # the revision the authz copy is known to hold (read back, or a landed write)
        self._copy: dict[tuple[str, str], Principal] = {}  # the grant it holds at that revision
        self._unconfirmed: dict[tuple[str, str], dict] = {}   # the latest reduction whose authz write has not been confirmed
        self._complete: set[str] = set()                  # tenants whose start-up read of the copy was complete
        self._journals: dict[str, Any] = {}
        self._lock = threading.Lock()
        self._write_locks: dict[tuple[str, str], threading.Lock] = {}
        self._persist, self._clock = persist, clock
        self.persist_failures = 0

    def _next_rev(self, k: tuple[str, str]) -> int:
        """Under the lock: the revision of a change, past the last one and past the current time in microseconds (never reused)."""
        rev = self._rev[k] = max(self._rev.get(k, 0) + 1, int(self._clock() * 1_000_000))
        return rev

    def get(self, tenant: str, token: str) -> Optional[Principal]:
        """The grant, or None. A grant that holds no role (a replayed reduction narrowed it to nothing) is no grant: such a principal
        cannot mint a session (fail closed), and a new grant to it is an addition."""
        with self._lock:
            g = self._g.get((tenant, token))
            return g if g is not None and g.roles else None

    # ---- start-up -------------------------------------------------------------------------------------------------------------
    def attach(self, tenant: str, journal: Any) -> None:
        """Make the store the owner of the tenant's `grant_reduce` records (replay and compaction). Before `journal.start()`."""
        self._journals[tenant] = journal
        journal.register_records((REDUCE,), lambda r: self.apply_record(tenant, r), lambda: self.snapshot_records(tenant))

    def restore(self, p: Principal, rev: int = 0) -> None:
        """A grant read back at start-up (revision `rev` of the copy): memory only, no persist; narrowed to an unconfirmed reduction."""
        k = (p.tenant, p.token)
        with self._lock:
            self._g[k] = p
            if rev >= self._copy_rev.get(k, 0):
                self._copy_rev[k], self._copy[k] = rev, p
            self._rev[k] = max(self._rev.get(k, 0), rev)
            self._settle(k)

    def loaded(self, tenant: str) -> None:
        """The start-up read of the tenant's copy was COMPLETE (every grant it holds was restored): a reduction of a principal the copy
        holds no grant for is confirmed. Never after a failed or partial read, which proves nothing."""
        with self._lock:
            self._complete.add(tenant)
            for k in [k for k in self._unconfirmed if k[0] == tenant]:
                self._settle(k)

    def apply_record(self, tenant: str, r: Mapping) -> None:
        """Journal replay of a reduction: the latest one per principal is kept (it was made from a memory that held every earlier one)."""
        if r.get("t") != REDUCE:
            return
        k, rev = (tenant, r["principal"]), int(r["rev"])
        with self._lock:
            cur = self._unconfirmed.get(k)
            if cur is None or int(cur["rev"]) < rev:
                self._unconfirmed[k] = dict(r)
            self._rev[k] = max(self._rev.get(k, 0), rev)
            self._settle(k)

    def snapshot_records(self, tenant: str) -> list:
        with self._lock:
            return [dict(r) for k, r in self._unconfirmed.items() if k[0] == tenant]

    def _settle(self, k: tuple[str, str]) -> None:
        """Under the lock: drop a reduction the copy is known to hold (a later revision, or this one with exactly the reduced grant; or,
        after a complete read, a principal the copy holds no grant for), else narrow the grant in memory to it."""
        r = self._unconfirmed.get(k)
        if r is None:
            return
        cr, rr = self._copy_rev.get(k, 0), int(r["rev"])
        held = cr > rr or (cr == rr and self._copy.get(k) == _of_record(k[0], r))
        if held or (k not in self._g and k[0] in self._complete):
            del self._unconfirmed[k]
            return
        if k in self._g:
            self._g[k] = meet(self._g[k], _of_record(k[0], r))

    # ---- changes --------------------------------------------------------------------------------------------------------------
    def put(self, p: Principal) -> None:
        """A new grant, or one that only adds: memory, then the best-effort authz write (never fails the call)."""
        k = (p.tenant, p.token)
        with self._lock:
            old = self._g.get(k)
            if old is not None and old.roles and not narrower(old, p):
                raise ValueError("a grant that takes anything away is a reduction (GrantStore.reduce)")
            self._g[k] = p
            rev = self._next_rev(k)
        if self._persist:
            try:
                self._persist_at(p, rev)
            except Exception:
                pass   # an addition is best effort (counted in persist_failures): a lost write loses access at the next start (fail safe)

    def rollback(self, p: Principal) -> None:
        """The undo of a change whose epoch bump failed (the caller raises, so it is never told ok): memory back to `p`, which may be
        wider than the change, then the best-effort write. A journalled reduction it undoes is confirmed only if that write lands (the
        copy then holds `p` at a later revision); a lost write leaves the reduction to apply at the next start (fail safe)."""
        k = (p.tenant, p.token)
        with self._lock:
            self._g[k] = p
            rev = self._next_rev(k)
        if self._persist:
            try:
                self._persist_at(p, rev)
            except Exception:
                pass   # the caller raises either way; a lost write keeps the journalled reduction (fail safe)

    def reduce(self, p: Principal) -> dict:
        """A grant that takes something away: in memory under the tenant's send fence, then the journal record (appended and fsynced
        before this returns), then the authz write. Returns the journal's receipt (`durable_at`, `persisted`). Raises
        `revocation.Refused` when the tenant's journal is closed (nothing changed) or when the record did not become durable (the
        append faulted and closed the tenant: the reduction holds in memory, but the caller is never told ok for it), and refuses
        when the store is not attached to the journal."""
        journal = self._journals.get(p.tenant)
        if journal is None:
            raise RuntimeError("the grant store is not attached to the tenant's journal: a reduction would not survive a restart")
        k = (p.tenant, p.token)

        def apply():
            with self._lock:
                self._g[k] = p
                rev = self._next_rev(k)
                r = {"t": REDUCE, "principal": p.token, "roles": sorted(x.value for x in p.roles), "read": sorted(p.read_set),
                     "write": sorted(p.write_set), "own_class": p.own_class or "", "rev": rev}
                self._unconfirmed[k] = r
                return r

        write = (lambda r: self._persist_at(p, int(r["rev"]))) if self._persist else None   # no copy configured: the journal alone
        _, rc = journal.journalled(apply, lambda r: dict(r), persist=write)
        if rc["durable_at"] is None:
            raise Refused("the reduction's journal record is not durable")
        return rc

    def _persist_at(self, p: Principal, rev: int) -> None:
        """The authz write of revision `rev`. Writes of one principal go one at a time, and a write that a later revision has
        superseded is not sent (so an earlier, wider state never lands after a later one). A write that lands confirms every
        reduction of the principal up to `rev`; one that does not (refused, malformed, failed: the persister raises) is counted and
        raises, and the reduction stays unconfirmed."""
        k = (p.tenant, p.token)
        with self._lock:
            wl = self._write_locks.setdefault(k, threading.Lock())
        with wl:
            with self._lock:
                superseded = self._rev.get(k, 0) > rev
            if superseded:
                self._count_failure()
                raise RuntimeError("a later revision superseded this write")
            try:
                self._persist(p, rev)
            except Exception:
                self._count_failure()
                raise
            with self._lock:
                if rev >= self._copy_rev.get(k, 0):
                    self._copy_rev[k], self._copy[k] = rev, p
                r = self._unconfirmed.get(k)
                if r is not None and int(r["rev"]) <= rev:
                    del self._unconfirmed[k]

    def _count_failure(self) -> None:
        with self._lock:
            self.persist_failures += 1


class Admin:
    def __init__(self, store: GrantStore, epochs, lattices: Mapping[str, ClassLattice],
                 revocations: Callable[[str], RevocationService], principal_token: Callable[[str, str], str], *,
                 clock: Callable[[], float] = time.time, pending_s: float = PENDING_S,
                 broken_self_grant: bool = False, broken_single_admin_revokes_privileged: bool = False,
                 broken_removal_without_bump: bool = False, broken_operator_authority_in_tenant: bool = False,
                 broken_ignore_recovery_admins: bool = False,
                 broken_signature_not_bound_to_action: bool = False,
                 offboard: Optional[Callable[[str], Any]] = None):
        self._st, self._ep, self._lat, self._rev, self._tok = store, epochs, lattices, revocations, principal_token
        self._clock, self._pending_s, self._offboard = clock, pending_s, offboard
        self._pending: dict[tuple[str, str], tuple[str, float]] = {}
        self._lock = threading.Lock()
        self._b = dict(self_grant=broken_self_grant, single=broken_single_admin_revokes_privileged,
                       nobump=broken_removal_without_bump, op_in_tenant=broken_operator_authority_in_tenant,
                       any_admin=broken_ignore_recovery_admins, anyact=broken_signature_not_bound_to_action)

    # ---- dispatch -----------------------------------------------------------------------------------------------
    def handle(self, op: str, tenant: str, p: Principal, req: Mapping[str, Any]) -> dict:
        if op == "break_glass_revoke":
            return self.break_glass(p, req)
        if op == "offboard_tenant":
            return self.offboard_tenant(p, req)
        lattice = self._lat.get(tenant)
        who = req.get("who")
        if lattice is None or not isinstance(who, str) or not who:
            return REFUSED
        if op == "grant":
            return self.grant(p, tenant, lattice, who, req)
        if op == "revoke":
            return self.revoke(p, tenant, lattice, who)
        if op == "remove_class":
            return self.remove_class(p, tenant, lattice, who, req.get("cls"))
        return REFUSED

    def _target(self, p: Principal, tenant: str, who: str) -> tuple[Optional[Principal], Optional[Principal]]:
        tok = self._tok(tenant, who)
        t = self._st.get(tenant, tok)
        return t, t   # (stored grants, the principal the policy judges)

    # ---- grant --------------------------------------------------------------------------------------------------
    def grant(self, p: Principal, tenant: str, lattice: ClassLattice, who: str, req: Mapping[str, Any]) -> dict:
        try:
            roles = frozenset(Role(r) for r in req.get("grant_roles", []))
            read, write = frozenset(req.get("read", [])), frozenset(req.get("write", []))
            own = req.get("own_class")
        except (ValueError, TypeError):
            return REFUSED
        if not roles or Role.OPERATOR in roles or not (own is None or isinstance(own, str)):
            return REFUSED   # the operator is a platform role: no tenant role can create or hold it
        if not all(isinstance(c, str) for c in read | write):
            return REFUSED
        tgt = Principal(tenant, self._tok(tenant, who), roles, read, write, own)
        if self._b["self_grant"]:     # BROKEN: an administrator may grant to itself
            d = Role.TENANT_ADMIN in p.roles and p.tenant == tenant
        else:
            d = bool(decide(p, Op.GRANT, tenant=tenant, lattice=lattice, target=tgt)) and Role.TENANT_ADMIN in p.roles
        if not d or not validate_principal(tgt, lattice) or self._ep.is_revoked(tenant, tgt.token):
            return REFUSED
        svc = self._rev(tenant)
        if not (svc.state == "open" if self._b["any_admin"] else svc.may_grant(p.token)):   # a recovering tenant admits grant calls only from the administrators the operator named
            return UNAVAILABLE
        old = self._st.get(tenant, tgt.token)
        reduces = old is not None and not narrower(old, tgt)
        if reduces and old.roles & {Role.TENANT_ADMIN, Role.REVOKER}:
            # a grant that takes roles or classes from an administrator or a revoker is a revocation of part of its authority:
            # two distinct administrators, like the revoke itself
            act = ("grant", tuple(sorted(r.value for r in tgt.roles)), tuple(sorted(tgt.read_set)), tuple(sorted(tgt.write_set)), tgt.own_class)
            verdict = self._authorise(p, tenant, lattice, old, act)
            if verdict is not None:
                return verdict
        if not reduces:
            self._st.put(tgt)    # an addition: best effort
            return OK_ANSWER
        # a grant that takes anything away is a reduction: journalled before the answer, so it holds across a restart whatever the
        # authz write answered (GrantStore.reduce), and the sessions minted under the larger grant end at their next check
        self._st.reduce(tgt)
        if not self._b["nobump"]:     # BROKEN variant: they keep the removed roles and classes
            try:
                self._rev(tenant).bump(tgt.token)
            except Exception:
                self._st.rollback(old)
                raise
        return OK_ANSWER

    # ---- revoke and class removal -------------------------------------------------------------------------------
    def _authorise(self, p: Principal, tenant: str, lattice: ClassLattice, tgt: Principal,
                   action: tuple = ("revoke",)) -> Optional[dict]:
        """None when the actor may act on the target now; otherwise the typed answer (`pending` after a first administrator's call).
        A first signature counts for the same action on the same target only: a signature for removing a class does not complete
        a revoke, and a revoke does not complete the removal of a role."""
        if tgt.roles & {Role.TENANT_ADMIN, Role.REVOKER}:
            if self._b["single"] and Role.TENANT_ADMIN in p.roles:    # BROKEN: one administrator revokes a revoker or an administrator
                return None
            key, now = (tenant, tgt.token) if self._b["anyact"] else (tenant, tgt.token, action), self._clock()
            with self._lock:
                first = self._pending.get(key)
                if first is None or first[1] < now or first[0] == p.token:
                    if Role.TENANT_ADMIN not in p.roles or p.tenant != tenant or p.token == tgt.token:
                        return REFUSED
                    self._pending[key] = (p.token, now + self._pending_s)
                    return PENDING
                co = self._st.get(tenant, first[0])
                del self._pending[key]
            if co is None:
                return REFUSED
            co = Principal(co.tenant, co.token, co.roles, co.read_set, co.write_set, co.own_class, self._ep.is_revoked(tenant, co.token))
            return None if decide(p, Op.REVOKE_PRIVILEGED, tenant=tenant, lattice=lattice, target=tgt, co_signer=co) else REFUSED
        return None if decide(p, Op.REVOKE, tenant=tenant, lattice=lattice, target=tgt) else REFUSED

    def revoke(self, p: Principal, tenant: str, lattice: ClassLattice, who: str) -> dict:
        tgt, _ = self._target(p, tenant, who)
        if tgt is None:
            return REFUSED
        verdict = self._authorise(p, tenant, lattice, tgt, ("revoke",))
        if verdict is not None:
            return verdict
        rc = self._rev(tenant).revoke(tgt.token)    # Refused (a closed tenant) is a typed, retryable `unavailable` upstream
        return {"ok": True, "denied": True, "epoch": rc.epoch, "durable": rc.durable_at is not None}

    def remove_class(self, p: Principal, tenant: str, lattice: ClassLattice, who: str, cls: Any) -> dict:
        tgt, _ = self._target(p, tenant, who)
        if tgt is None or not isinstance(cls, str) or not lattice.declared(cls) or Role.TENANT_ADMIN not in p.roles:
            return REFUSED   # a revoker has the single revoke call and no other (4.3)
        verdict = self._authorise(p, tenant, lattice, tgt, ("remove_class", cls))
        if verdict is not None:
            return verdict
        read = frozenset(c for c in tgt.read_set if not lattice.dominates(c, cls))   # keeps the read set down-closed
        write = frozenset(c for c in tgt.write_set if c != cls)
        # a reduction: journalled before the answer, so it holds across a restart whatever the authz write answered (GrantStore.reduce)
        self._st.reduce(Principal(tenant, tgt.token, tgt.roles, read, write, tgt.own_class))
        epoch = self._ep.epoch(tenant, tgt.token)
        if not self._b["nobump"]:    # BROKEN variant: the grants shrink, the old sessions keep the class until they end
            try:
                epoch = self._rev(tenant).bump(tgt.token).epoch
            except Exception:        # a closed tenant: the change is undone, and the call raises
                self._st.rollback(tgt)
                raise
        return {"ok": True, "epoch": epoch}

    # ---- the operator --------------------------------------------------------------------------------------------
    def offboard_tenant(self, p: Principal, req: Mapping[str, Any]) -> dict:
        """The operator's tenant-wide revocation (offboarding, key compromise): the gateway drops every client of the tenant at once
        and, when the deployment wired a key owner, the cluster keys are invalidated. The operator's own authority only."""
        tenant = req.get("target_tenant")
        if not isinstance(tenant, str) or not tenant or self._offboard is None:
            return REFUSED
        if not decide(p, Op.BREAK_GLASS_REVOKE, tenant=tenant, lattice=ClassLattice(["none"], [])):
            return REFUSED
        self._offboard(tenant)
        return OK_ANSWER

    def break_glass(self, p: Principal, req: Mapping[str, Any]) -> dict:
        tenant, who = req.get("target_tenant"), req.get("who")
        if not isinstance(tenant, str) or not isinstance(who, str) or not who:
            return REFUSED
        if self._b["op_in_tenant"]:    # BROKEN: the operator's authority is a grant inside the tenant, which a tenant role can revoke
            held = self._st.get(tenant, p.token)
            if held is None or Role.OPERATOR not in held.roles or self._ep.is_revoked(tenant, p.token):
                return REFUSED
        elif not decide(p, Op.BREAK_GLASS_REVOKE, tenant=tenant, lattice=ClassLattice(["none"], [])):
            return REFUSED
        svc = self._rev(tenant)
        rc = svc.revoke(self._tok(tenant, who))
        return {"ok": True, "denied": True, "epoch": rc.epoch, "durable": rc.durable_at is not None}
