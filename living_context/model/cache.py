"""Model-output cache keys and class placement (DESIGN 5.8, 18.1; BUILD-PLAN Step 8).

key   = sha256 over (tenant, cls, task, prompt_version, pinned model id, scope, inputs)
cls   = the lattice join of the channel's class and the class of every input, so a restricted-channel route over
        a base node is stored in (and readable only from) the restricted cache
scope = the principal's token for prompt-bearing tasks (route, organise, ask, reader_answer, query_variants),
        so one reader's cache hits reveal nothing about another's queries; otherwise the class's service scope.
The store is an interface: a dict in unit tests, the class's cache index (`EsCacheStore`) in the product.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any, Iterable, Mapping, Optional, Protocol

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
    def put(self, cls: str, key: str, value: dict, subjects: Iterable[str]) -> None: ...


class DictStore:
    def __init__(self):
        self.data: dict[tuple[str, str], dict] = {}

    def get(self, cls, key):
        return self.data.get((cls, key))

    def put(self, cls, key, value, subjects):
        self.data[(cls, key)] = value
