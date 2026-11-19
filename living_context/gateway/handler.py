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
from .es import EsClient, EsError
from .indices import class_index
from .sessions import Sessions

FORBIDDEN_BODY_FIELDS = {"tenant", "principal", "principal_token", "session", "roles", "read_set", "write_set"}
REFUSED = {"ok": False, "error": "refused"}
UNAVAILABLE = {"ok": False, "error": "unavailable"}
HELD = {"ok": False, "error": "held"}


class EsGate:
    """Per-tenant Elasticsearch access with the role-scoped keys of the tenant."""

    def __init__(self, url: str, ca_path: str, keys: Mapping[str, Mapping[str, str]]):
        self._url, self._ca, self._keys = url, ca_path, keys
        self._clients: dict[tuple[str, str], EsClient] = {}

    def client(self, tenant: str, role: str) -> EsClient:
        k = (tenant, role)
        if k not in self._clients:
            self._clients[k] = EsClient(self._url, self._ca, self._keys[tenant][role], scheme="apikey")
        return self._clients[k]

    def drop_tenant(self, tenant: str) -> None:
        """Tenant-wide revocation: forget every client of the tenant at once (DESIGN 4.4)."""
        for k in [k for k in self._clients if k[0] == tenant]:
            del self._clients[k]


class Gateway:
    def __init__(self, es: EsGate, sessions: Sessions, lattices: Mapping[str, ClassLattice],
                 writers: Optional[Callable[[str], Any]] = None):
        self._es, self._sessions, self._lattices = es, sessions, lattices
        self._writers = writers

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

    def handle(self, token: str, request: Any) -> dict:
        rec = self._sessions.resolve(token)
        if rec is None:
            return UNAVAILABLE
        if not isinstance(request, dict) or FORBIDDEN_BODY_FIELDS & set(request):
            return REFUSED
        p = self._sessions.principal(rec)
        lattice = self._lattices.get(rec.tenant)
        if lattice is None:
            return UNAVAILABLE
        try:
            op = request.get("op")
            if op == "ping":
                return {"ok": True}
            if op == "search":
                return self._search(rec.tenant, p, lattice, request)
            if op == "lookup":
                return self._lookup(rec.tenant, p, lattice, request)
            if op == "write":
                return self._write(rec.tenant, p, lattice, request)
        except (EsError, ValueError, KeyError, TypeError):
            return UNAVAILABLE
        return REFUSED

    def _search(self, tenant, p, lattice, req):
        cls, text = req.get("cls"), req.get("text")
        if not isinstance(cls, str) or not isinstance(text, str):
            return REFUSED
        if not decide(p, Op.RECALL, tenant=tenant, lattice=lattice, cls=cls):
            return UNAVAILABLE  # no distinction between forbidden and missing
        st, body = self._es.client(tenant, f"read-{cls}").request(
            "POST", f"/{class_index(tenant, cls, 'leaf')}/_search",
            {"size": 10, "_source": ["title", "size_tokens"],
             "query": {"multi_match": {"query": text, "fields": ["text", "title", "entities"]}}})
        if st != 200 or not isinstance(body, dict):
            return UNAVAILABLE
        hits = [h for h in (self._shape_hit(tenant, cls, x) for x in body.get("hits", {}).get("hits", [])) if h]
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
