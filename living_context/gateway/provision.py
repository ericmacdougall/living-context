"""Tenant provisioning (DESIGN 4.4, 5.10, 6.2 to 6.5; BUILD-PLAN Step 5). Operator-side.

Creates, for one tenant: the indices (each name an alias to a physical index with a
random suffix, slow logs disabled), its registry row, API keys with no cluster privilege
(one per role, read keys per class), and the epoch journal with its spare, refusing the
tenant if the journal filesystem cannot exchange two names atomically or if the shard
budget would be exceeded. Keys are written to 0600 files by path and never printed.

The cluster this talks to is an argument (`admin` client): pointing it at a dedicated
cluster per tenant is a configuration choice, not a code change.
"""
from __future__ import annotations

import datetime
import os
from pathlib import Path
from typing import Any

from ..config import Constants
from . import epoch_journal, fsx
from .es import EsClient
from .indices import (CLASS_KINDS, REGISTRY, TENANT_CLS, TENANT_KINDS, class_index, class_patterns, check_id,
                      may_provision, physical_name, role_pattern_is_safe, shards_per_tenant, tenant_index)

SLOWLOG_OFF = {f"index.{k}.slowlog.threshold.{q}.{lvl}": "-1"
               for k, qs in (("search", ("query", "fetch")), ("indexing", ("index",)))
               for q in qs for lvl in ("warn", "info", "debug", "trace")}
COMMON = {"tenant": {"type": "keyword"}, "cls": {"type": "keyword"}, "kind": {"type": "keyword"},
          "lin_leaves": {"type": "keyword"}, "lin_leaf_ids": {"type": "keyword"}, "lin_nodes": {"type": "keyword"},
          "lin_subjects": {"type": "keyword"}, "lin_queries": {"type": "keyword"}, "derivation": {"type": "keyword"},
          "erasure_rewrite": {"type": "long"}, "created_at": {"type": "date"}, "schema_v": {"type": "short"},
          "src_seq": {"type": "long"}, "gen_from": {"type": "long"}, "gen_to": {"type": "long"}}
EXTRA = {
    "cache": {"value": {"type": "object", "enabled": False}},
    "leaf": {"text": {"type": "text", "analyzer": "english"}, "title": {"type": "text"}, "entities": {"type": "keyword"},
             "size_tokens": {"type": "integer"}, "content_hash": {"type": "keyword"},
             "embedding": {"type": "dense_vector", "dims": 384, "similarity": "cosine",
                           "index_options": {"type": "int8_hnsw"}}},
    "sq": {"q": {"type": "percolator"}, "text": {"type": "text"},
           "embedding": {"type": "dense_vector", "dims": 384, "similarity": "cosine",
                         "index_options": {"type": "int8_hnsw"}}},
}


def mapping_for(kind: str) -> dict:
    return {"dynamic": "true", "properties": {**COMMON, **EXTRA.get(kind, {})}}


def kind_patterns(tenant: str, kind: str) -> list[str]:
    """Alias and physical names of one class-scoped kind, all classes."""
    t = check_id(tenant, "tenant")
    return [f"lc-{t}-*-{kind}", f"lc-{t}-*-{kind}-p*"]


def roles_for(tenant: str, classes: list[str]) -> dict[str, dict]:
    """Role descriptors per key. No cluster privilege anywhere; every pattern names this tenant only."""
    t = check_id(tenant, "tenant")
    rd: dict[str, dict] = {}

    def desc(patterns: list[str], privs: list[str]) -> dict:
        assert all(role_pattern_is_safe(p) for p in patterns), patterns
        return {"cluster": [], "indices": [{"names": patterns, "privileges": privs}]}

    for c in classes:
        rd[f"read-{c}"] = desc(class_patterns(t, c) + [f"lc-{t}-ctl", f"lc-{t}-ctl-p*"],
                               ["read", "view_index_metadata"])
    ingest = [f"lc-{t}-src", f"lc-{t}-src-p*", f"lc-{t}-ctl", f"lc-{t}-ctl-p*"] + kind_patterns(t, "leaf") + kind_patterns(t, "ctl")
    rd["ingest"] = desc(ingest, ["read", "write", "view_index_metadata", "maintenance"])
    structure = [f"lc-{t}-ctl", f"lc-{t}-ctl-p*"] + sum((kind_patterns(t, k) for k in ("node", "edge", "member", "ctl")), [])
    rd["structure"] = desc(structure, ["read", "write", "view_index_metadata", "maintenance"])
    log = sum((kind_patterns(t, k) for k in ("log", "sq", "note", "cache")), [])
    rd["log"] = desc(log, ["read", "write", "view_index_metadata"])
    rd["erase"] = desc([f"lc-{t}-*"], ["read", "delete", "view_index_metadata", "maintenance"])
    rd["authz"] = desc([f"lc-{t}-authz", f"lc-{t}-authz-p*"], ["read", "write", "view_index_metadata"])
    return rd


def write_key_file(path: Path, encoded: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(encoded)


class ProvisionRefused(Exception):
    pass


def provision_tenant(admin: EsClient, tenant: str, classes: list[str], *, key_dir: Path, data_root: Path,
                     consts: Constants | None = None, replicas: int = 0, max_shards_per_node: int = 1000,
                     journal_size: int | None = None, key_expiry: str = "7d") -> dict[str, Any]:
    c = consts or Constants()
    check_id(tenant, "tenant")
    if not classes or len(classes) > c.max_classes:
        raise ProvisionRefused("class count")
    for cl in classes:
        check_id(cl, "class")
        if cl == TENANT_CLS:
            raise ProvisionRefused("reserved class id")
    jdir = data_root / "epochs" / tenant
    data_root.mkdir(parents=True, exist_ok=True)
    (data_root / "epochs").mkdir(exist_ok=True)
    if not fsx.supported(str(data_root / "epochs")):
        raise ProvisionRefused("journal filesystem cannot exchange two names atomically")
    st, shards = admin.request("GET", "/_cat/shards?format=json&h=index")
    existing = len(shards) if st == 200 and isinstance(shards, list) else max_shards_per_node
    if not may_provision(existing, len(classes), replicas, max_shards_per_node, c.shard_budget_fraction):
        raise ProvisionRefused("shard budget")
    st, got = admin.request("GET", f"/{REGISTRY}/_doc/{tenant}")
    if st == 200 and got.get("found"):
        raise ProvisionRefused("tenant exists")
    names = [(tenant_index(tenant, k), k) for k in TENANT_KINDS]
    names += [(class_index(tenant, cl, k), k) for cl in classes for k in CLASS_KINDS]
    for alias, kind in names:
        phys = physical_name(alias)
        st, body = admin.request("PUT", f"/{phys}", {
            "settings": {"number_of_shards": 1, "number_of_replicas": replicas, **SLOWLOG_OFF},
            "mappings": mapping_for(kind), "aliases": {alias: {}}})
        if st != 200:
            raise ProvisionRefused(f"create index failed ({st})")
    j, _ = epoch_journal.provision(jdir, journal_size or c.j_size)
    keys: dict[str, str] = {}
    for role, rd in roles_for(tenant, classes).items():
        st, body = admin.request("POST", "/_security/api_key", {
            "name": f"lc-{tenant}-{role}", "expiration": key_expiry, "role_descriptors": {"r": rd}})
        if st != 200:
            raise ProvisionRefused(f"key creation failed ({st})")
        p = key_dir / tenant / f"{role}.key"
        write_key_file(p, body["encoded"])
        keys[role] = str(p)
    admin.request("PUT", f"/{REGISTRY}/_doc/{tenant}?refresh=true", {
        "tenant": tenant, "status": "active", "tier": "dedicated",
        "created_at": datetime.datetime.now(datetime.timezone.utc).isoformat()})
    return {"tenant": tenant, "classes": classes, "keys": keys, "journal": str(j),
            "shards": shards_per_tenant(len(classes), replicas)}
