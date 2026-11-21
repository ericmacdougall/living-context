"""Request handler: the only place that turns a session and a request into Elasticsearch calls.

Rules (DESIGN 3, 7.1, 8.7; probe P2 mitigations, main's decision):
* tenant, principal, roles and sets come from the session record; a request body that
  names a tenant, principal or class set it is not entitled to is refused;
* the class is checked by the pure policy before any index is chosen (INV-2);
* index names come only from `indices` builders; the tenant's API keys are read from
  files by path inside this process and never appear in a response;
* no Elasticsearch error body, sub-response or index name is ever forwarded: every
  failure is one of two typed answers, `refused` or `unavailable`, identical for
  forbidden, erased, missing and failed;
* the answer goes out with what its hand-off must re-check (`serve` returns a `Reply`): the token whose authority it carries and every
  pool (with the operations) whose content it carries; the server runs `send_ok` under the shared side of the tenant's send fence at
  every hand-off (POOLS 2.4, 2.5, AC-2) and aborts the whole answer when it fails.
"""
from __future__ import annotations

import base64
import binascii
import json
import os
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Optional

from ..ids import IdError, KIND_LEAF, Pointer, decode_pointer, encode_pointer
from ..policy import ClassLattice, Op, decide
from .admin import ADMIN_OPS
from .pool_admin import POOL_OPS
POOL_TOOLS = frozenset({"pool_save", "pool_update", "pool_query", "pool_fetch", "pool_holds", "pool_hold_show", "pool_release", "pool_retract"})
from .es import EsClient, EsError
from ..recall.direct import DirectSearch
from .indices import class_index
from .pool_authz import carried
from .reads import IncompleteRead, mget_items
from .sessions import Sessions

FORBIDDEN_BODY_FIELDS = {"tenant", "principal", "principal_token", "session", "roles", "read_set", "write_set"}
REFUSED = {"ok": False, "error": "refused"}
UNAVAILABLE = {"ok": False, "error": "unavailable"}
HELD = {"ok": False, "error": "held"}
# A scoped or delegated token is attenuated to its pool scope (POOLS 2.6): it may call only these (default-deny, an explicit allow-list).
# `token_revoke` is allowed without a `token_id` only (its own revocation).
SCOPED_OPS = POOL_TOOLS | frozenset({"pool_list", "pool_describe", "token_revoke", "token_renew", "token_delegate", "ping"})


@dataclass(frozen=True)
class Reply:
    """An answer and what its send hand-off re-checks (POOLS 2.4): `token`, the token whose authority the answer carries (checks 1 to 3;
    None only for the receipt of a bearer's revocation of its own token, which carries nothing that token could still read), and
    `needs`, pool id to the operations, for every pool whose content the answer carries (check 4)."""
    body: Any
    token: Optional[str]
    needs: Mapping[str, frozenset] = field(default_factory=dict)


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
                 pools: Optional[Callable[[str], Any]] = None, admits: Optional[Callable[[str], bool]] = None):
        self._es, self._sessions, self._lattices = es, sessions, lattices
        self._admits = admits   # tenant -> does its journal admit; None: the pool runtime's `admit` (no runtime, no admission)
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

    def _admit(self, tenant: str) -> bool:
        """The ONE journal admission check (POOLS 2.5; DESIGN 4.4: a journal fault closes the tenant): every operation except the
        administrator's own lane (ADMIN_OPS, which `Admin.handle` admits by its own rules) and both mint forms pass it before anything
        is dispatched; a closed tenant is the one `unavailable`. There is no fail-open default: with neither an explicit `admits` nor a
        pool runtime, nothing is admitted. tests/unit/test_admission_guard.py enumerates the operations and mint forms."""
        try:
            if self._admits is not None:
                return self._admits(tenant) is True
            rt = self._pools(tenant) if self._pools else None
            return rt is not None and rt.admit() is True
        except Exception:
            return False

    def _runtime(self, tenant: str):
        """The tenant's pool runtime, only while its epoch journal admits."""
        rt = self._pools(tenant) if self._pools else None
        if rt is None or not self._admit(tenant):
            return None
        return rt

    def mint(self, tenant: str, proof: bytes, request: Any) -> dict:
        """A scoped token from an authentication proof (verified by the tenant's identity adapter) and a request naming the conversation,
        the scope (pool id to operations), an optional default save pool and a lifetime. The actor is `human` only when the tenant's
        identity adapter says the proof carries a fresh user-presence assertion (POOLS 2.3); no request field can say so. Every failure is
        the one `unavailable` answer."""
        from .sessions import TokenError
        try:
            if self._runtime(tenant) is None:
                return UNAVAILABLE
            if not isinstance(request, dict) or not isinstance(request.get("scope"), dict) or not isinstance(proof, bytes):
                return UNAVAILABLE
            scope = {k: frozenset(v) for k, v in request["scope"].items() if isinstance(k, str) and isinstance(v, list)}
            if len(scope) != len(request["scope"]):
                return UNAVAILABLE
            tok = self._sessions.mint_scoped(tenant, proof, conversation=request.get("conversation"), scope=scope,
                                             default_save_pool=request.get("default_save_pool"), ttl_s=request.get("ttl_s"), human=None)
            return {"ok": True, "token": tok}
        except (TokenError, ValueError, TypeError):
            return UNAVAILABLE

    def mint_line(self, m: Any):
        """The socket's `mint` line (DESIGN 3, POOLS 2.3): `{"tenant", "proof" (base64), "request"}`. A request `{"session": "plain"}`
        mints a plain session (DESIGN 5.10); any other request is a scoped mint. Returns (answer, token or None)."""
        if not isinstance(m, dict) or not isinstance(m.get("tenant"), str) or not isinstance(m.get("proof"), str) \
                or not isinstance(m.get("request"), dict):
            return UNAVAILABLE, None
        try:
            proof = base64.b64decode(m["proof"], validate=True)
        except (binascii.Error, ValueError):
            return UNAVAILABLE, None
        req = m["request"]
        if not self._admit(m["tenant"]):   # both mint forms: a closed tenant mints nothing (review F1)
            return UNAVAILABLE, None
        if req == {"session": "plain"}:
            tok = self._sessions.mint(m["tenant"], proof)
            return ({"ok": True, "token": tok}, tok) if tok else (UNAVAILABLE, None)
        out = self.mint(m["tenant"], proof, req)
        return out, out.get("token")

    def _token_op(self, op: str, token: str, rec, request: dict) -> dict:
        from .sessions import TokenError
        try:
            if op == "token_renew":
                return {"ok": True, "exp": self._sessions.renew(token)}
            scope = request.get("scope")
            if not isinstance(scope, dict) or not all(isinstance(v, list) for v in scope.values()):
                return UNAVAILABLE
            return {"ok": True, "token": self._sessions.delegate(token, {k: frozenset(v) for k, v in scope.items()}, request.get("ttl_s"))}
        except (TokenError, ValueError, TypeError):
            return UNAVAILABLE

    def handle(self, token: str, request: Any) -> dict:
        """The answer alone (callers that do not send it); the socket server uses `serve`."""
        return self.serve(token, request).body

    def serve(self, token: str, request: Any) -> Reply:
        """The answer with what its hand-off must re-check: every successful pool authorization of the request is collected
        (`pool_authz.carried`). The ONE exemption from the token check is the receipt of a bearer revoking its own token (op
        `token_revoke` with no `token_id`, answered `ok`, and the token no longer resolving): it carries no pool and nothing the revoked
        token could read, and withholding it would only hide from the harness that its revoke landed (tests/unit/test_send_reply.py)."""
        with carried() as box:
            body = self._dispatch(token, request)
        needs = {pid: frozenset(ops) for pid, ops in box.items()}
        if (isinstance(request, dict) and request.get("op") == "token_revoke" and request.get("token_id") is None
                and isinstance(body, dict) and body.get("ok") is True and body.get("denied") is True and not needs
                and self._sessions.resolve(token) is None):
            return Reply(body, None, {})
        return Reply(body, token, needs)

    def send_ok(self, reply: Reply) -> bool:
        """POOLS 2.4 at the send hand-off, run by the server under the SHARED side of the tenant's send fence before every chunk:
        checks 1 to 3 for `reply.token` (it resolves: tenant prefix, `exp`, individual revocation, epoch, root and parent) and check 4
        for every pool in `reply.needs` with its operations, and the live visibility check for every pool it names (`VIEW`; the pool
        runtime's `send_ok`, `pool_authz.recheck`). Memory only: the session, epoch, pool
        directory and grant tables, each under its own leaf lock, which no thread holds while taking the fence (lock order: fence, then
        a leaf lock); no Elasticsearch, journal, disk or socket I/O, and the fence is never re-entered. Any failure or error is False,
        and the whole answer is aborted. Journal admission is not re-checked here: 2.4 names checks 1 to 4, and the administrator's lane
        is answered on a closed tenant."""
        try:
            if reply.token is None:
                return not reply.needs
            rec = self._sessions.resolve(reply.token)
            if rec is None:
                return False
            if not reply.needs:
                return True
            rt = self._pools(rec.tenant) if self._pools else None
            return rt is not None and rt.send_ok(reply.token, reply.needs) is True
        except Exception:
            return False

    def _dispatch(self, token: str, request: Any) -> dict:
        rec = self._sessions.resolve(token)
        if rec is None:
            return UNAVAILABLE
        op = request.get("op") if isinstance(request, dict) and isinstance(request.get("op"), str) else None
        if op not in ADMIN_OPS and not self._admit(rec.tenant):   # before any dispatch, class operations and ping included (F1)
            return UNAVAILABLE
        if not isinstance(request, dict) or FORBIDDEN_BODY_FIELDS & set(request):
            return REFUSED
        p = self._sessions.principal(rec)
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
            return self._token_op(op, token, rec, request) if self._runtime(rec.tenant) is not None else UNAVAILABLE
        if isinstance(op, str) and (op in POOL_OPS or op in POOL_TOOLS):
            svc = self._runtime(rec.tenant)
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
            want = [(class_index(tenant, cls, "leaf"), f"{ptr.id}:{ptr.version}")]
            try:   # the item must be exactly this leaf of this class (`reads.mget_items`); any other answer refuses (review round 6)
                doc, = mget_items(*self._es.client(tenant, f"read-{cls}").request(
                    "POST", "/_mget", {"docs": [{"_index": i, "_id": d} for i, d in want]}), want, alias=True)
            except IncompleteRead:
                return UNAVAILABLE
            if doc is not None:
                src = doc["_source"]
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
