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
from .revocation import RevocationService

OK_ANSWER = {"ok": True}
REFUSED = {"ok": False, "error": "refused"}
UNAVAILABLE = {"ok": False, "error": "unavailable"}
PENDING = {"ok": False, "error": "pending"}
ADMIN_OPS = frozenset({"grant", "revoke", "remove_class", "break_glass_revoke"})
REVOKE_LANE_OPS = frozenset({"revoke", "remove_class", "break_glass_revoke"})   # their own admission lane (DESIGN 4.4)
PENDING_S = 900.0   # ASSUMPTION: a first administrator's call waits this long for the second


class GrantStore:
    """The gateway's own grants: (tenant, principal token) to roles and sets. Memory first; `persist` is the best-effort write to
    `lc-<t>-authz` and never blocks or fails a call."""

    def __init__(self, persist: Optional[Callable[[Principal], None]] = None):
        self._g: dict[tuple[str, str], Principal] = {}
        self._lock = threading.Lock()
        self._persist = persist
        self.persist_failures = 0

    def get(self, tenant: str, token: str) -> Optional[Principal]:
        with self._lock:
            return self._g.get((tenant, token))

    def put(self, p: Principal) -> None:
        with self._lock:
            self._g[(p.tenant, p.token)] = p
        if self._persist:
            try:
                self._persist(p)
            except Exception:
                self.persist_failures += 1


class Admin:
    def __init__(self, store: GrantStore, epochs, lattices: Mapping[str, ClassLattice],
                 revocations: Callable[[str], RevocationService], principal_token: Callable[[str, str], str], *,
                 clock: Callable[[], float] = time.time, pending_s: float = PENDING_S,
                 broken_self_grant: bool = False, broken_single_admin_revokes_privileged: bool = False,
                 broken_removal_without_bump: bool = False, broken_operator_authority_in_tenant: bool = False,
                 broken_ignore_recovery_admins: bool = False):
        self._st, self._ep, self._lat, self._rev, self._tok = store, epochs, lattices, revocations, principal_token
        self._clock, self._pending_s = clock, pending_s
        self._pending: dict[tuple[str, str], tuple[str, float]] = {}
        self._lock = threading.Lock()
        self._b = dict(self_grant=broken_self_grant, single=broken_single_admin_revokes_privileged,
                       nobump=broken_removal_without_bump, op_in_tenant=broken_operator_authority_in_tenant,
                       any_admin=broken_ignore_recovery_admins)

    # ---- dispatch -----------------------------------------------------------------------------------------------
    def handle(self, op: str, tenant: str, p: Principal, req: Mapping[str, Any]) -> dict:
        if op == "break_glass_revoke":
            return self.break_glass(p, req)
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
        self._st.put(tgt)
        return OK_ANSWER

    # ---- revoke and class removal -------------------------------------------------------------------------------
    def _authorise(self, p: Principal, tenant: str, lattice: ClassLattice, tgt: Principal) -> Optional[dict]:
        """None when the actor may act on the target now; otherwise the typed answer (`pending` after a first administrator's call)."""
        if tgt.roles & {Role.TENANT_ADMIN, Role.REVOKER}:
            if self._b["single"] and Role.TENANT_ADMIN in p.roles:    # BROKEN: one administrator revokes a revoker or an administrator
                return None
            key, now = (tenant, tgt.token), self._clock()
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
        verdict = self._authorise(p, tenant, lattice, tgt)
        if verdict is not None:
            return verdict
        rc = self._rev(tenant).revoke(tgt.token)    # Refused (a closed tenant) is a typed, retryable `unavailable` upstream
        return {"ok": True, "denied": True, "epoch": rc.epoch, "durable": rc.durable_at is not None}

    def remove_class(self, p: Principal, tenant: str, lattice: ClassLattice, who: str, cls: Any) -> dict:
        tgt, _ = self._target(p, tenant, who)
        if tgt is None or not isinstance(cls, str) or not lattice.declared(cls):
            return REFUSED
        verdict = self._authorise(p, tenant, lattice, tgt)
        if verdict is not None:
            return verdict
        read = frozenset(c for c in tgt.read_set if not lattice.dominates(c, cls))   # keeps the read set down-closed
        write = frozenset(c for c in tgt.write_set if c != cls)
        self._st.put(Principal(tenant, tgt.token, tgt.roles, read, write, tgt.own_class))
        epoch = self._ep.epoch(tenant, tgt.token)
        if not self._b["nobump"]:    # BROKEN variant: the grants shrink, the old sessions keep the class until they end
            try:
                epoch = self._rev(tenant).bump(tgt.token).epoch
            except Exception:        # a closed tenant: nothing changed
                self._st.put(tgt)
                raise
        return {"ok": True, "epoch": epoch}

    # ---- the operator --------------------------------------------------------------------------------------------
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
