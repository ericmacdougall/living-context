"""Roles, operations and the permission table (DESIGN 4.3)."""
from __future__ import annotations

import enum


class Role(enum.Enum):
    WRITER = "writer"
    READER = "reader"
    REORGANISER = "reorganiser"
    ERASURE_REQUESTER = "erasure_requester"
    ERASURE_EXECUTOR = "erasure_executor"
    VERIFIER = "verifier"
    TENANT_ADMIN = "tenant_admin"
    REVOKER = "revoker"
    TENANT_KEY_OWNER = "tenant_key_owner"
    OPERATOR = "operator"


class Op(enum.Enum):
    REMEMBER = "remember"                    # write a leaf into a class
    RECALL = "recall"
    LOOKUP = "lookup"
    ASK = "ask"
    EXPLORE = "explore"
    REORG_READ = "reorg_read"                # plaintext of the class it rebuilds
    REORG_WRITE = "reorg_write"              # nodes, edges, manifests, notes of exactly that class
    ERASE_REQUEST = "erase_request"
    ERASE_RECEIPT_TOTALS = "erase_receipt_totals"  # totals over the requester's own read set
    ERASE_EXECUTE = "erase_execute"          # closure, delete, rewrite, purge
    VERIFY_READ = "verify_read"              # read-only search of every index of the tenant
    GRANT = "grant"                          # roles and classes to another principal
    REVOKE = "revoke"                        # a non-administrator, non-revoker principal
    REVOKE_PRIVILEGED = "revoke_privileged"  # an administrator or a revoker (two administrators)
    ROTATE_KEYS = "rotate_keys"
    PROVISION = "provision"
    BREAK_GLASS_REVOKE = "break_glass_revoke"
    READ_CONTENT_RAW = "read_content_raw"    # any content read outside recall/lookup/ask/explore


# Whether an operation is about a class, and which set of the principal governs it.
CLASS_READ = {Op.RECALL, Op.LOOKUP, Op.ASK, Op.EXPLORE, Op.ERASE_RECEIPT_TOTALS}
CLASS_WRITE = {Op.REMEMBER}
CLASS_OWN = {Op.REORG_READ, Op.REORG_WRITE}  # the reorganiser's own class only

# Operations each role may perform at all (before the class and tenant checks).
ROLE_OPS: dict[Role, frozenset[Op]] = {
    Role.WRITER: frozenset({Op.REMEMBER}),
    Role.READER: frozenset({Op.RECALL, Op.LOOKUP, Op.ASK, Op.EXPLORE}),
    Role.REORGANISER: frozenset({Op.REORG_READ, Op.REORG_WRITE}),
    Role.ERASURE_REQUESTER: frozenset({Op.ERASE_REQUEST, Op.ERASE_RECEIPT_TOTALS}),
    Role.ERASURE_EXECUTOR: frozenset({Op.ERASE_EXECUTE}),
    Role.VERIFIER: frozenset({Op.VERIFY_READ}),
    Role.TENANT_ADMIN: frozenset({Op.GRANT, Op.REVOKE, Op.REVOKE_PRIVILEGED}),
    Role.REVOKER: frozenset({Op.REVOKE}),
    Role.TENANT_KEY_OWNER: frozenset({Op.ROTATE_KEYS}),
    Role.OPERATOR: frozenset({Op.PROVISION, Op.BREAK_GLASS_REVOKE}),
}
