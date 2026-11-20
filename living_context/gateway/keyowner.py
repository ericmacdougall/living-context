"""The tenant key owner: rotation and invalidation of a tenant's API keys without the bootstrap credential
(DESIGN 4.3, 4.4; BUILD-PLAN Step 15; SEC-R6; probe P15).

The key owner is a native user per tenant held on the operator host (never by the gateway). Its role holds the cluster privilege
`manage_own_api_key` and index privileges on `lc-<t>-*` only, so every key it creates carries at most those privileges and no key it
does not own can be touched. `provision_owner` is the one operator-side step that needs the bootstrap credential; `rotate` and
`invalidate_all` take the OWNER's client and nothing else. The gateway's keys carry no cluster privilege at all, so they can neither
create nor invalidate a key.

Rotation order: list the owner's current keys for the tenant, create the new key of every role, write the new files (a temporary
name, then an atomic replace, mode 0600, never printed), and only then invalidate the old keys by id. A failure before the replace
invalidates the keys created so far and leaves the old files untouched. The caller then makes the gateway reload (`EsGate.reload`).
`broken_needs_superuser` exists for the test's broken variant: it asks for a privilege only a superuser holds."""
from __future__ import annotations

import os
import secrets
from pathlib import Path
from typing import Iterable

from .es import EsClient
from .indices import check_id
from .provision import roles_for, write_key_file

OWNER_INDEX_PRIVILEGES = ["read", "write", "delete", "create_index", "view_index_metadata", "maintenance", "monitor"]


class RotationError(Exception):
    pass


def owner_role(tenant: str) -> dict:
    t = check_id(tenant, "tenant")
    return {"cluster": ["manage_own_api_key"],
            "indices": [{"names": [f"lc-{t}-*"], "privileges": OWNER_INDEX_PRIVILEGES}]}


def provision_owner(admin: EsClient, tenant: str, secret_path: Path) -> str:
    """Operator side, with the bootstrap credential: the owner's role and native user. The password goes to a 0600 file and is
    never returned or printed. Returns the user name."""
    t = check_id(tenant, "tenant")
    user, role = f"lcko_{t}", f"lcko_{t}_role"
    st, _ = admin.request("PUT", f"/_security/role/{role}", owner_role(t))
    if st != 200:
        raise RotationError("owner role not created")
    password = secrets.token_urlsafe(32)
    st, _ = admin.request("PUT", f"/_security/user/{user}", {"password": password, "roles": [role]})
    if st != 200:
        raise RotationError("owner user not created")
    write_key_file(Path(secret_path), password)
    return user


def _owned_key_ids(owner: EsClient, tenant: str) -> list[str]:
    st, r = owner.request("GET", f"/_security/api_key?owner=true&name=lc-{tenant}-*&active_only=true")
    if st != 200 or not isinstance(r, dict):
        raise RotationError(f"could not list the owner's keys ({st})")
    return [k["id"] for k in r.get("api_keys", []) if not k.get("invalidated")]


def invalidate(owner: EsClient, ids: Iterable[str]) -> list[str]:
    ids = list(ids)
    if not ids:
        return []
    st, r = owner.request("DELETE", "/_security/api_key", {"ids": ids, "owner": True})
    if st != 200 or not isinstance(r, dict):
        raise RotationError(f"invalidate failed ({st})")
    return list(r.get("invalidated_api_keys", []))


def invalidate_all(owner: EsClient, tenant: str) -> list[str]:
    """Tenant-wide revocation (offboarding, key compromise): every active key of the tenant that the owner holds."""
    return invalidate(owner, _owned_key_ids(owner, check_id(tenant, "tenant")))


def rotate(owner: EsClient, tenant: str, classes: list[str], key_dir: Path, *, expiry: str = "7d",
           broken_needs_superuser: bool = False) -> dict:
    t = check_id(tenant, "tenant")
    if broken_needs_superuser:   # BROKEN variant: reads the users, a privilege only the bootstrap credential holds
        st, _ = owner.request("GET", "/_security/user")
        if st != 200:
            raise RotationError(f"rotation needs a privilege the key owner does not hold ({st})")
    old = _owned_key_ids(owner, t)
    created: list[str] = []
    staged: dict[str, tuple[Path, Path]] = {}
    try:
        for role, rd in roles_for(t, classes).items():
            st, body = owner.request("POST", "/_security/api_key", {
                "name": f"lc-{t}-{role}", "expiration": expiry, "role_descriptors": {"r": rd}})
            if st != 200 or not isinstance(body, dict):
                raise RotationError(f"key creation failed ({st})")
            created.append(body["id"])
            final = Path(key_dir) / t / f"{role}.key"
            tmp = final.with_suffix(".key.new")
            if tmp.exists():
                tmp.unlink()
            write_key_file(tmp, body["encoded"])
            staged[role] = (tmp, final)
    except Exception:
        invalidate(owner, created)
        for tmp, _ in staged.values():
            tmp.unlink(missing_ok=True)
        raise
    for tmp, final in staged.values():
        os.replace(tmp, final)
    done = invalidate(owner, old)
    return {"keys": {role: str(final) for role, (_, final) in staged.items()}, "created": created, "invalidated": done}
