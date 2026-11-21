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
from .reads import IncompleteRead, api_key_created, api_keys_active, api_keys_invalidated

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
    """The ids of the owner's active keys of the tenant (`reads.api_keys_active`): an answer without the key list, such as
    `(200, {})`, is not "no keys" (review round 6: offboarding would then invalidate nothing and report success)."""
    try:
        return api_keys_active(*owner.request("GET", f"/_security/api_key?owner=true&name=lc-{tenant}-*&active_only=true"),
                               f"lc-{tenant}-")
    except IncompleteRead as e:
        raise RotationError("could not list the owner's keys") from e


def invalidate(owner: EsClient, ids: Iterable[str]) -> list[str]:
    """Invalidate `ids` and confirm it: the invalidation answer must be well formed (`reads.api_keys_invalidated`), and since this
    cluster answers an id it did not invalidate with two empty lists and no error, the owner's active keys are read back and none of
    `ids` may remain among them. Returns `ids` (every one is now verified inactive); anything else raises RotationError."""
    ids = list(ids)
    if not ids:
        return []
    try:
        api_keys_invalidated(*owner.request("DELETE", "/_security/api_key", {"ids": ids, "owner": True}))
        active = set(api_keys_active(*owner.request("GET", "/_security/api_key?owner=true&active_only=true"), "lc-"))
    except IncompleteRead as e:
        raise RotationError("invalidation not confirmed") from e
    if active & set(ids):
        raise RotationError("a key is still active after its invalidation")
    return ids


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
            try:   # a created key of exactly this name, with its id and encoded value (`reads.api_key_created`)
                body = api_key_created(*owner.request("POST", "/_security/api_key", {
                    "name": f"lc-{t}-{role}", "expiration": expiry, "role_descriptors": {"r": rd}}), f"lc-{t}-{role}")
            except IncompleteRead as e:
                raise RotationError("key creation not confirmed") from e
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
