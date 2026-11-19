"""The admission decision function (pure; DESIGN 4.3, 4.4, INV-1, INV-2, INV-3).

`decide` never raises for a refusal and never reveals why to the caller: the
`Decision.code` is for the gateway's own log (no content). Anything it does
not recognise is refused (fail closed).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from .classes import ClassLattice, NoJoin
from .roles import CLASS_OWN, CLASS_READ, CLASS_WRITE, Op, ROLE_OPS, Role


@dataclass(frozen=True)
class Principal:
    tenant: str
    token: str                      # per-tenant HMAC token, never a raw identifier
    roles: frozenset[Role]
    read_set: frozenset[str] = frozenset()
    write_set: frozenset[str] = frozenset()
    own_class: Optional[str] = None  # a reorganiser's class
    revoked: bool = False


@dataclass(frozen=True)
class Decision:
    allowed: bool
    code: str = ""

    def __bool__(self):
        return self.allowed


def _deny(code: str) -> Decision:
    return Decision(False, code)


OK = Decision(True, "ok")
PLATFORM_OPS = {Op.PROVISION, Op.BREAK_GLASS_REVOKE}


def validate_principal(p: Principal, lattice: ClassLattice) -> bool:
    """A read set must be down-closed; sets and own class must be declared."""
    return (lattice.is_down_closed(p.read_set) and set(p.write_set) <= set(lattice.classes)
            and (p.own_class is None or lattice.declared(p.own_class)))


def decide(p: Principal, op: Op, *, tenant: str, lattice: ClassLattice,
           cls: Optional[str] = None, target: Optional[Principal] = None,
           co_signer: Optional[Principal] = None,
           input_classes: tuple[str, ...] = ()) -> Decision:
    if not isinstance(op, Op) or p.revoked:
        return _deny("revoked_or_unknown")
    if not validate_principal(p, lattice):
        return _deny("malformed_principal")
    # Tenant boundary (INV-1): only the operator's platform operations cross it.
    if op in PLATFORM_OPS:
        if Role.OPERATOR not in p.roles:
            return _deny("role")
        return OK
    if p.tenant != tenant:
        return _deny("tenant")
    if not any(op in ROLE_OPS.get(r, ()) for r in p.roles):
        return _deny("role")

    if op in CLASS_READ:
        # INV-2: trim before retrieval; a class outside the read set is never touched.
        if cls is None or cls not in p.read_set:
            return _deny("class")
        return OK
    if op in CLASS_WRITE:
        if cls is None or cls not in p.write_set:
            return _deny("class")
        return OK
    if op in CLASS_OWN:
        if Role.REORGANISER not in p.roles or p.own_class is None or cls != p.own_class:
            return _deny("class")
        if op is Op.REORG_WRITE:
            # INV-3: nothing derived from a class it does not dominate is written into its tree.
            if not input_classes or any(not lattice.dominates(p.own_class, c) for c in input_classes):
                return _deny("derived_from_higher")
        return OK
    if op in (Op.ERASE_REQUEST, Op.ERASE_EXECUTE, Op.VERIFY_READ, Op.ROTATE_KEYS):
        return OK
    if op is Op.GRANT:
        if target is None or target.tenant != tenant or target.token == p.token:
            return _deny("self_grant_or_target")  # no self-grant (review F14d)
        return OK
    if op is Op.REVOKE:
        if target is None or target.tenant != tenant:
            return _deny("target")
        if target.roles & {Role.TENANT_ADMIN, Role.REVOKER}:
            return _deny("privileged_target")  # needs REVOKE_PRIVILEGED with two administrators
        if Role.REVOKER in p.roles or Role.TENANT_ADMIN in p.roles:
            return OK
        return _deny("role")
    if op is Op.REVOKE_PRIVILEGED:
        if Role.TENANT_ADMIN not in p.roles:
            return _deny("role")
        if (target is None or target.tenant != tenant or co_signer is None or co_signer.revoked
                or co_signer.tenant != tenant or Role.TENANT_ADMIN not in co_signer.roles
                or co_signer.token == p.token):
            return _deny("two_administrators")
        if target.token in (p.token, co_signer.token):
            return _deny("two_administrators")
        return OK
    return _deny("unknown_operation")  # includes READ_CONTENT_RAW: no role holds it


def derived_class(lattice: ClassLattice, *input_classes: str) -> Optional[str]:
    """The class of a derived thing, or None if it must not be built (INV-3)."""
    try:
        return lattice.join(*input_classes)
    except NoJoin:
        return None
