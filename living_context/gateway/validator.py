"""Document validator: fail closed (INV-1, INV-4; DESIGN 5, 19; BUILD-PLAN Step 5).

The gateway refuses to index any document that lacks the common fields of
DESIGN 5, whose tenant or class disagrees with the session or the target index
name, or whose lineage fields have the wrong shape. A refusal carries a code
for the gateway's log and never echoes the document.
"""
from __future__ import annotations

from typing import Any, Mapping

from .indices import CLASS_KINDS, TENANT_KINDS, ID

KINDS = {"leaf", "src", "node", "edge", "log", "sq", "note", "cache", "manifest", "head", "tomb", "member", "dirty", "idem", "idem_scope", "counter", "quarantine", "pool", "pgrant", "token", "grant", "session", "retraction", "event"}
LIST_FIELDS = ("lin_leaves", "lin_leaf_ids", "lin_nodes", "lin_subjects", "lin_queries")
REQUIRED = ("tenant", "cls", "kind", "derivation", "erasure_rewrite", "created_at", "schema_v") + LIST_FIELDS


class Refused(Exception):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def validate(doc: Mapping[str, Any], *, session_tenant: str, index: str, index_tenant: str,
             index_cls: str | None) -> None:
    """Raise Refused unless the document may be written to `index` by this session.
    `index_cls` is None for tenant-wide indices (src keeps its own `cls`)."""
    if not isinstance(doc, Mapping):
        raise Refused("not_an_object")
    for f in REQUIRED:
        if f not in doc:
            raise Refused(f"missing_{f}")
    if doc["tenant"] != session_tenant or index_tenant != session_tenant:
        raise Refused("tenant_mismatch")
    if not isinstance(doc["cls"], str) or not ID.match(doc["cls"]):
        raise Refused("bad_class")
    if index_cls is not None and doc["cls"] != index_cls:
        raise Refused("class_mismatch")
    if doc["kind"] not in KINDS:
        raise Refused("bad_kind")
    for f in LIST_FIELDS:
        v = doc[f]
        if not isinstance(v, list) or not all(isinstance(x, str) and x for x in v):
            raise Refused(f"bad_{f}")
    if not isinstance(doc["erasure_rewrite"], int) or isinstance(doc["erasure_rewrite"], bool) \
            or doc["erasure_rewrite"] < 0:
        raise Refused("bad_erasure_rewrite")
    if not isinstance(doc["schema_v"], int) or isinstance(doc["schema_v"], bool):
        raise Refused("bad_schema_v")
    # Lineage by kind (INV-4): a derived document names what it was derived from.
    if doc["kind"] in ("node", "edge", "member", "note", "cache", "sq", "log") and not (
            doc["lin_leaves"] or doc["lin_leaf_ids"] or doc["lin_nodes"] or doc["lin_queries"]
            or doc["lin_subjects"]):
        raise Refused("no_lineage")
