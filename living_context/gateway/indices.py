"""Index naming and the shard budget (DESIGN 6.2, 6.4; BUILD-PLAN Step 5).

`lc-<tenant>-<class>-<kind>` for class-scoped kinds, `lc-<tenant>-<kind>` for
the tenant-wide ones. Tenant and class ids are lowercase `[a-z0-9]{4,16}`
with no hyphen, so no tenant's pattern `lc-<t>-*` can match another tenant's
name (the look-alike `tbxx` against `tbx` is the probe case). Each name is an
alias to a physical index whose suffix is random: a sequential suffix would
tell anyone who sees a physical name how often an index was swapped (probe
P2 found that an alias name can reveal its physical name through `_mget`).
"""
from __future__ import annotations

import math
import os
import re

ID = re.compile(r"^[a-z0-9]{4,16}$")
CLASS_KINDS = ("leaf", "node", "edge", "member", "log", "sq", "note", "cache", "ctl")
TENANT_KINDS = ("src", "ctl", "authz")
POOL_KINDS = ("leaf",)   # the indices a pool owns at stage 0 (POOLS 1.2, ADAPTIVE 2.2); the others come with the stages that need them
POOL_ID = re.compile(r"^[a-z0-9]{16}$")
REGISTRY = "lc-sys-registry"
TENANT_CLS = "tenantwide"  # the `cls` of tenant-wide documents; no tenant class may use it


class NameError_(ValueError):
    pass


def check_id(value: str, what: str = "id") -> str:
    if not isinstance(value, str) or not ID.match(value):
        raise NameError_(f"{what} must match [a-z0-9]{{4,16}}")
    return value


def class_index(tenant: str, cls: str, kind: str) -> str:
    if kind not in CLASS_KINDS:
        raise NameError_("unknown class-scoped kind")
    return f"lc-{check_id(tenant, 'tenant')}-{check_id(cls, 'class')}-{kind}"


def check_pool_id(value: str) -> str:
    if not isinstance(value, str) or not POOL_ID.match(value):
        raise NameError_("pool id must match [a-z0-9]{16}")
    return value


def pool_index(tenant: str, cls: str, pool: str, kind: str) -> str:
    """`lc-<t>-<c>-<p>-<kind>`: an index only one pool's content is scored in (POOLS 1.2). Pool ids are 16 characters and class ids
    are 4 to 16, and a pool name has one more segment than a class name, so it never equals a class-level name."""
    if kind not in POOL_KINDS:
        raise NameError_("unknown pool-scoped kind")
    return f"lc-{check_id(tenant, 'tenant')}-{check_id(cls, 'class')}-{check_pool_id(pool)}-{kind}"


def tenant_index(tenant: str, kind: str) -> str:
    if kind not in TENANT_KINDS:
        raise NameError_("unknown tenant-wide kind")
    return f"lc-{check_id(tenant, 'tenant')}-{kind}"


def physical_name(alias: str, rand: bytes | None = None) -> str:
    """Physical index behind an alias, with a random suffix."""
    return alias + "-p" + (rand if rand is not None else os.urandom(4)).hex()


def tenant_pattern(tenant: str) -> str:
    """The only pattern a tenant's role descriptor may name."""
    return f"lc-{check_id(tenant, 'tenant')}-*"


def class_patterns(tenant: str, cls: str) -> list[str]:
    """Patterns of one class's indices plus the tenant's own ctl (DESIGN 6.3)."""
    return [f"lc-{check_id(tenant, 'tenant')}-{check_id(cls, 'class')}-*", tenant_index(tenant, "ctl")]


def role_pattern_is_safe(pattern: str) -> bool:
    """A role descriptor may name one tenant's `lc-<t>-...` space only: never `lc-*`,
    a bare wildcard, or a wildcard that ends the tenant segment."""
    return re.match(r"^lc-[a-z0-9]{4,16}-[a-z0-9*-]*$", pattern) is not None


def shards_per_tenant(classes: int, replicas: int) -> int:
    """DESIGN 6.4: (9 x C + 3) x (1 + r)."""
    return (len(CLASS_KINDS) * classes + len(TENANT_KINDS)) * (1 + replicas)


def may_provision(existing_shards: int, classes: int, replicas: int, max_shards_per_node: int = 1000,
                  fraction: float = 0.8) -> bool:
    """Refuse a tenant that would take the node above `fraction` of the shard limit."""
    return existing_shards + shards_per_tenant(classes, replicas) <= math.floor(max_shards_per_node * fraction)
