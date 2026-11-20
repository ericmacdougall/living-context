"""Request handler: the only place that turns a session and a request into Elasticsearch calls.

Rules (DESIGN 3, 7.1, 8.7; probe P2 mitigations, main's decision):
* tenant, principal, roles and sets come from the session record; a request body that
  names a tenant, principal or class set it is not entitled to is refused;
* the class is checked by the pure policy before any index is chosen (INV-2);
* index names come only from `indices` builders; the tenant's API keys are read from
  files by path inside this process and never appear in a response;
* no Elasticsearch error body, sub-response or index name is ever forwarded: every
  failure is one of two typed answers, `refused` or `unavailable`, identical for
  forbidden, erased, missing and failed.
"""
from __future__ import annotations

import json
import os
from typing import Any, Callable, Mapping, Optional

from ..ids import IdError, KIND_LEAF, Pointer, decode_pointer, encode_pointer
from ..policy import ClassLattice, Op, decide
from .admin import ADMIN_OPS
from .pool_admin import POOL_OPS
POOL_TOOLS = frozenset({"pool_save", "pool_update", "pool_query", "pool_fetch", "pool_holds", "pool_hold_show", "pool_release", "pool_retract"})
from .es import EsClient, EsError
from ..recall.direct import DirectSearch
from .indices import class_index
from .sessions import Sessions

FORBIDDEN_BODY_FIELDS = {"tenant", "principal", "principal_token", "session", "roles", "read_set", "write_set"}
REFUSED = {"ok": False, "error": "refused"}
UNAVAILABLE = {"ok": False, "error": "unavailable"}
HELD = {"ok": False, "error": "held"}
# A scoped or delegated token is attenuated to its pool scope (POOLS 2.6): it may call only these (default-deny, an explicit allow-list).
# `token_revoke` is allowed without a `token_id` only (its own revocation).
SCOPED_OPS = POOL_TOOLS | frozenset({"pool_list", "pool_describe", "token_revoke", "token_renew", "token_delegate", "ping"})


class EsGate:
    """Per-tenant Elasticsearch access with the role-scoped keys of the tenant."""

    def __init__(self, url: str, ca_path: str, keys: Mapping[str, Mapping[str, str]]):
        self._url, self._ca, self._keys = url, ca_path, keys
        self._clients: dict[tuple[str, str], EsClient] = {}
        self._dropped: set[str] = set()

    def client(self, tenant: str, role: str) -> EsClient:
        if tenant in self._dropped:
            raise EsError("tenant keys dropped")
        k = (tenant, role)
        if k not in self._clients:
            self._clients[k] = EsClient(self._url, self._ca, self._keys[tenant][role], scheme="apikey")
        return self._clients[k]

    def drop_tenant(self, tenant: str) -> None:
        """Tenant-wide revocation (offboarding, key compromise): forget every client of the tenant at once and refuse to build a new
        one until `reload` names new keys (DESIGN 4.4). The gateway is the only client, so this alone stops every gateway path."""
        self._dropped.add(tenant)
        for k in [k for k in self._clients if k[0] == tenant]:
            del self._clients[k]

    def reload(self, tenant: str, keys: Mapping[str, str]) -> None:
        """After a rotation (`keyowner.rotate`): new key files for the tenant, old clients forgotten, the tenant usable again."""
        self._keys = {**self._keys, tenant: dict(keys)}
        for k in [k for k in self._clients if k[0] == tenant]:
            del self._clients[k]
        self._dropped.discard(tenant)


class Gateway:
    def __init__(self, es: EsGate, sessions: Sessions, lattices: Mapping[str, ClassLattice],
                 writers: Optional[Callable[[str], Any]] = None, admin: Any = None,
                 pools: Optional[Callable[[str], Any]] = None):
        self._es, self._sessions, self._lattices = es, sessions, lattices
        self._writers = writers
        self._pools = pools   # tenant -> gateway.pool_admin.PoolService (pools, grants, token revoke) and the pool tools
        self._admin = admin   # gateway.admin.Admin: grant, revoke, remove_class, break_glass_revoke

    # the single shaping point for every successful answer: nothing from Elasticsearch passes through unfiltered
    def _shape_hit(self, tenant: str, cls: str, hit: Mapping[str, Any]) -> Optional[dict]:
        src = hit.get("_source") or {}
        raw_id = hit.get("_id")
        if not isinstance(raw_id, str) or ":" not in raw_id:
            return None
        leaf_id, _, lv = raw_id.partition(":")
        try:
            ptr = encode_pointer(Pointer(KIND_LEAF, 0, leaf_id, int(lv)))
        except (IdError, ValueError):
            return None
        return {"pointer": ptr, "title": src.get("title", ""), "size": src.get("size_tokens", 0)}

    def mint(self, tenant: str, proof: bytes, request: Any) -> dict:
        """The one way to obtain a scoped token over the socket: an authentication proof (verified by the tenant's identity adapter) and a
        request naming the conversation, the scope (pool id to operations), an optional default save pool and a lifetime. The `human` flag
        is never read from the request: a token is `agent` unless the harness's own adapter says a human is present, and no request field
        can say so. Every failure is the one `unavailable` answer."""
        from .sessions import TokenError
        try:
            if not isinstance(request, dict) or not isinstance(request.get("scope"), dict) or not isinstance(proof, bytes):
                return UNAVAILABLE
            scope = {k: frozenset(v) for k, v in request["scope"].items() if isinstance(k, str) and isinstance(v, list)}
            if len(scope) != len(request["scope"]):
                return UNAVAILABLE
            ttl = request.get("ttl_s")
            tok = self._sessions.mint_scoped(tenant, proof, conversation=request.get("conversation"), scope=scope,
                                             default_save_pool=request.get("default_save_pool"),
                                             ttl_s=float(ttl) if isinstance(ttl, (int, float)) and not isinstance(ttl, bool) else None)
            return {"ok": True, "token": tok}
        except (TokenError, ValueError, TypeError):
            return UNAVAILABLE

    def _token_op(self, op: str, token: str, rec, request: dict) -> dict:
        from .sessions import TokenError
        try:
            if op == "token_renew":
                return {"ok": True, "exp": self._sessions.renew(token)}
            scope = request.get("scope")
            ttl = request.get("ttl_s")
            if not isinstance(scope, dict) or not isinstance(ttl, (int, float)) or isinstance(ttl, bool):
                return UNAVAILABLE
            return {"ok": True, "token": self._sessions.delegate(token, {k: frozenset(v) for k, v in scope.items()}, float(ttl))}
        except (TokenError, ValueError, TypeError):
            return UNAVAILABLE

    def handle(self, token: str, request: Any) -> dict:
        rec = self._sessions.resolve(token)
        if rec is None:
            return UNAVAILABLE
        if not isinstance(request, dict) or FORBIDDEN_BODY_FIELDS & set(request):
            return REFUSED
        p = self._sessions.principal(rec)
        op = request.get("op")
        if rec.scope is not None and (op not in SCOPED_OPS or (op == "token_revoke" and request.get("token_id") is not None)):
            return UNAVAILABLE
        if op in ADMIN_OPS:   # the operator's break-glass comes from the platform tenant, which has no lattice
            if self._admin is None:
                return REFUSED
            try:
                return self._admin.handle(op, rec.tenant, p, {k: v for k, v in request.items() if k != "op"})
            except Exception:  # a closed tenant (journal fault) is the one retryable answer
                return UNAVAILABLE
        lattice = self._lattices.get(rec.tenant)
        if lattice is None:
            return UNAVAILABLE
        if op in ("token_renew", "token_delegate"):
            return self._token_op(op, token, rec, request)
        if isinstance(op, str) and (op in POOL_OPS or op in POOL_TOOLS):
            svc = self._pools(rec.tenant) if self._pools else None
            if svc is None:
                return UNAVAILABLE
            try:   # every failure of a pool request is the one typed answer, with no body and no trace
                return svc.handle(op, p, rec, {k: v for k, v in request.items() if k != "op"}, token=token) if op in POOL_TOOLS \
                    else svc.handle(op, p, rec, {k: v for k, v in request.items() if k != "op"})
            except Exception:
                return UNAVAILABLE
        try:
            if op == "ping":
                return {"ok": True}
            if op == "search":
                return self._search(rec.tenant, p, lattice, request)
            if op == "lookup":
                return self._lookup(rec.tenant, p, lattice, request)
            if op == "write":
                return self._write(rec.tenant, p, lattice, request)
        except Exception:  # fail closed: every internal failure is the one typed answer, never a body or a trace
            return UNAVAILABLE
        return REFUSED

    def _search(self, tenant, p, lattice, req):
        cls, text = req.get("cls"), req.get("text")
        if not isinstance(cls, str) or not isinstance(text, str):
            return REFUSED
        if not decide(p, Op.RECALL, tenant=tenant, lattice=lattice, cls=cls):
            return UNAVAILABLE  # no distinction between forbidden and missing
        ds = DirectSearch(lambda c: self._es.client(tenant, f"read-{c}"), tenant)
        hits = [h for h in (self._shape_hit(tenant, x.cls, {"_id": x.doc_id, "_source": {"title": x.title, "size_tokens": x.size}})
                            for x in ds.search(p, lattice, text, classes=[cls])) if h]
        return {"ok": True, "hits": hits}

    def _lookup(self, tenant, p, lattice, req):
        try:
            ptr = decode_pointer(req.get("pointer"))
        except IdError:
            return UNAVAILABLE
        # a pointer names no class: try the classes of the read set only
        for cls in sorted(p.read_set):
            if not decide(p, Op.LOOKUP, tenant=tenant, lattice=lattice, cls=cls):
                continue
            st, body = self._es.client(tenant, f"read-{cls}").request(
                "POST", "/_mget", {"docs": [{"_index": class_index(tenant, cls, "leaf"), "_id": f"{ptr.id}:{ptr.version}"}]})
            docs = body.get("docs") if st == 200 and isinstance(body, dict) else None
            if docs and len(docs) == 1 and docs[0].get("found") is True and "error" not in docs[0]:
                src = docs[0].get("_source") or {}
                return {"ok": True, "text": src.get("text", ""), "title": src.get("title", "")}
        return UNAVAILABLE

    def _write(self, tenant, p, lattice, req):
        """Delegate to the tenant's writer: the client names text, optional subjects and an idempotency key;
        every stored field (title, lineage, vector, ids) is built by code, never taken from the request."""
        cls, doc = req.get("cls"), req.get("doc")
        if not isinstance(cls, str) or not isinstance(doc, dict) or self._writers is None:
            return REFUSED
        if not decide(p, Op.REMEMBER, tenant=tenant, lattice=lattice, cls=cls):
            return REFUSED
        text, subs, key = doc.get("text"), doc.get("subjects", []), doc.get("key")
        if not isinstance(text, str) or not isinstance(subs, list) or not (key is None or isinstance(key, str)):
            return REFUSED
        res = self._writers(tenant).remember(p.token, cls, text, subjects_declared=subs, key=key)
        if res.status == "stored":
            return {"ok": True, "pointer": encode_pointer(Pointer(KIND_LEAF, 0, res.leaf_id, res.lv))}
        if res.status == "held":
            return HELD
        return REFUSED  # empty, over the cap, erased identifier and key reuse look the same to a writer
