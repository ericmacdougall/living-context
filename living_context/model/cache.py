"""Model-output cache keys and class placement (DESIGN 5.8, 18.1; BUILD-PLAN Step 8).

key   = sha256 over (tenant, cls, task, prompt_version, pinned model id, scope, inputs)
cls   = the lattice join of the channel's class and the class of every input, so a restricted-channel route over
        a base node is stored in (and readable only from) the restricted cache
scope = the principal's token for prompt-bearing tasks (route, organise, ask, reader_answer, query_variants),
        so one reader's cache hits reveal nothing about another's queries; otherwise the class's service scope.
The store is an interface: a dict in unit tests, the class's cache index (`EsCacheStore`) in the product.
"""
from __future__ import annotations

import datetime
import hashlib
import json
from typing import Any, Callable, Iterable, Mapping, Optional, Protocol

from ..gateway import validator
from ..gateway.es import EsClient
from ..gateway.indices import class_index
from ..policy import ClassLattice

PROMPT_BEARING = frozenset({"route", "organise", "ask", "reader_answer", "query_variants"})
SERVICE_SCOPE = "service"


def scope_for(task: str, principal: str) -> str:
    return principal if task in PROMPT_BEARING else SERVICE_SCOPE


def cache_class(lattice: ClassLattice, channel_cls: str, input_classes: Iterable[str]) -> str:
    return lattice.join(channel_cls, *input_classes)


def cache_key(tenant: str, cls: str, task: str, prompt_version: str, model_id: str, scope: str,
              inputs: Mapping[str, Any]) -> str:
    blob = json.dumps([tenant, cls, task, prompt_version, model_id, scope, inputs], sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode()).hexdigest()


class CacheStore(Protocol):
    def get(self, cls: str, key: str) -> Optional[dict]: ...
    def put(self, cls: str, key: str, value: dict, lineage: Mapping[str, Iterable[str]]) -> None: ...


class DictStore:
    def __init__(self):
        self.data: dict[tuple[str, str], dict] = {}

    def get(self, cls, key):
        return self.data.get((cls, key))

    requires_lineage = False

    def put(self, cls, key, value, lineage):
        self.data[(cls, key)] = value


LINEAGE_FIELDS = ("lin_leaves", "lin_leaf_ids", "lin_nodes", "lin_subjects", "lin_queries")


class StoreError(Exception):
    """An Elasticsearch step of the cache failed (typed; no body or index name is carried)."""


class EsCacheStore:
    """The class's cache index `lc-<t>-<c>-cache` (DESIGN 5.8). Every entry carries the lineage of the call that
    produced it (leaves, nodes, query subjects), so the erasure closure (14.2) reaches model outputs too; an entry
    without lineage is refused by the document validator, and the port refuses the call before it spends budget."""
    requires_lineage = True

    def __init__(self, es: Callable[[], EsClient], tenant: str):
        self._es_fn, self.tenant = es, tenant

    def get(self, cls: str, key: str) -> Optional[dict]:
        st, body = self._es_fn().request("GET", f"/{class_index(self.tenant, cls, 'cache')}/_doc/{key}")
        if st == 404:
            return None
        if st != 200 or not isinstance(body, dict):
            raise StoreError(f"cache read failed ({st})")
        return body["_source"]["value"]

    def put(self, cls: str, key: str, value: dict, lineage: Mapping[str, Iterable[str]]) -> None:
        doc = {"tenant": self.tenant, "cls": cls, "kind": "cache", "derivation": "model", "erasure_rewrite": 0,
               "created_at": datetime.datetime.now(datetime.timezone.utc).isoformat(), "schema_v": 1, "value": value}
        for f in LINEAGE_FIELDS:
            doc[f] = sorted({x for x in lineage.get(f, ()) if x})
        idx = class_index(self.tenant, cls, "cache")
        validator.validate(doc, session_tenant=self.tenant, index=idx, index_tenant=self.tenant, index_cls=cls)
        st, _ = self._es_fn().request("PUT", f"/{idx}/_doc/{key}", doc)
        if st not in (200, 201):
            raise StoreError(f"cache write failed ({st})")
