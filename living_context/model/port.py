"""The model port (DESIGN 18.1; BUILD-PLAN Step 8). Stub backend only: a call to any other backend is refused.

Every call: refuse a non-stub backend, look up the cache, charge the persistent budget BEFORE the call, wait
in the per-tenant fair queue, run the stub, check the output against the task's schema (a failure is a typed
failure, and still counts), store the output in the class cache. `port_calls` and the stub's own
`invocations` are incremented independently so the S0 report can compare them.
"""
from __future__ import annotations

import threading
from dataclasses import dataclass
from enum import Enum
from typing import Any, Mapping, Optional

from ..policy import ClassLattice
from . import cache as cachemod
from .budget import Budget
from .fairq import FairLimiter

STUB_ID = "stub-extractive-v1"


class Task(str, Enum):
    BUILD_L1 = "build_l1"
    BUILD_UPPER = "build_upper"
    ROUTE = "route"
    ORGANISE = "organise"
    TYPE_EDGES = "type_edges"
    JUDGE_RELEVANCE = "judge_relevance"
    QUERY_VARIANTS = "query_variants"
    SYNTHETIC_QUERIES = "synthetic_queries"
    ASK = "ask"
    READER_ANSWER = "reader_answer"
    ANSWER_JUDGE = "answer_judge"


PROMPT_VERSION = {t.value: "v1" for t in Task}
SCHEMA: Mapping[str, Mapping[str, type]] = {
    "build_l1": {"entries": list, "summary": str}, "build_upper": {"summary": str},
    "route": {"selected": list, "reasons": dict, "confidence": dict}, "organise": {"outline": list},
    "type_edges": {"edges": list}, "judge_relevance": {"score": float, "relevant": bool},
    "query_variants": {"variants": list}, "synthetic_queries": {"queries": list},
    "ask": {"answer": str}, "reader_answer": {"answer": str}, "answer_judge": {"correct": bool},
}


class PortRefused(Exception):
    pass


@dataclass(frozen=True)
class CallContext:
    tenant: str
    cls: str                      # the channel's class
    principal: str                # the principal's token
    input_classes: tuple = ()     # classes of every input to the call
    model_id: str = STUB_ID
    task_id: str = ""
    lin_leaf_ids: tuple = ()      # lineage of the call: what the output was derived from (erasure reaches it)
    lin_nodes: tuple = ()
    lin_subjects: tuple = ()
    lin_queries: tuple = ()


@dataclass(frozen=True)
class CallResult:
    ok: bool
    output: Optional[dict] = None
    error: str = ""
    cached: bool = False


def schema_ok(task: str, out: Any) -> bool:
    spec = SCHEMA[task]
    return isinstance(out, dict) and all(isinstance(out.get(k), t) for k, t in spec.items())


class ModelPort:
    def __init__(self, backend, budget: Budget, lattice: ClassLattice, store: cachemod.CacheStore,
                 limiter: Optional[FairLimiter] = None, *, charge_before: bool = True, class_in_cache: bool = True,
                 principal_in_scope: bool = True, join_class: bool = True):
        """The last four keywords are the broken variants of the tests (each must turn a probe red)."""
        self._b, self._budget, self._lat, self._store = backend, budget, lattice, store
        self._lim = limiter or FairLimiter(2)
        self._charge_before, self._class_in_cache = charge_before, class_in_cache
        self._principal_in_scope, self._join = principal_in_scope, join_class
        self.port_calls, self._lock = 0, threading.Lock()

    def _place(self, task: str, ctx: CallContext) -> tuple[str, str]:
        cls = cachemod.cache_class(self._lat, ctx.cls, ctx.input_classes) if self._join else (
            ctx.input_classes[0] if ctx.input_classes else ctx.cls)
        scope = cachemod.scope_for(task, ctx.principal) if self._principal_in_scope else cachemod.SERVICE_SCOPE
        return cls, scope

    def call(self, task: Task, inputs: Mapping[str, Any], *, ctx: CallContext) -> CallResult:
        if getattr(self._b, "name", None) != "stub" or ctx.model_id != STUB_ID:
            raise PortRefused("only the stub backend is wired in this build")
        with self._lock:
            self.port_calls += 1
        name = task.value
        lineage = {"lin_leaf_ids": ctx.lin_leaf_ids, "lin_nodes": ctx.lin_nodes, "lin_queries": ctx.lin_queries,
                   "lin_subjects": tuple(ctx.lin_subjects) + tuple(inputs.get("subjects", []) if name in cachemod.PROMPT_BEARING else [])}
        if getattr(self._store, "requires_lineage", False) and not any(lineage.values()):
            raise PortRefused("a call without lineage cannot be cached inside the erasure closure")
        cls, scope = self._place(name, ctx)
        key = cachemod.cache_key(ctx.tenant, cls if self._class_in_cache else "", name, PROMPT_VERSION[name],
                                 ctx.model_id, scope, inputs)
        hit = self._store.get(cls, key)
        if hit is not None:
            return CallResult(True, hit, cached=True)
        if self._charge_before:
            self._budget.charge()
        out, err = None, ""

        def work():
            nonlocal out, err
            try:
                out = self._b.run(name, inputs)
            except Exception:
                err = "backend_error"

        self._lim.run(ctx.tenant, work)
        if not self._charge_before and not err:
            self._budget.charge()  # broken variant: a failed call is never counted
        if err:
            return CallResult(False, error=err)
        if not schema_ok(name, out):
            return CallResult(False, error="schema")
        self._store.put(cls, key, out, lineage)
        return CallResult(True, out)
