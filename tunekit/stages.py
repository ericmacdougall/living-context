"""TUNING's zero-call stages Z1 to Z6 as a registry: what each measures, which defaults it informs, its inputs and outputs, and
whether it can run today against the product code that exists. Adapters for product code that does not exist yet fail loudly
(`ProductCodeMissing`) instead of returning a plausible stand-in.

`status`:
  RUNS            built and run in this kit against code that exists
  PARTIAL         the part that needs only existing code is built and run; the rest is named in `missing`
  WAITS           generator and harness interface built; the measurement waits on product code named in `missing`
"""
from __future__ import annotations

import importlib.util
from dataclasses import dataclass
from typing import Callable, List, Optional, Sequence


class ProductCodeMissing(RuntimeError):
    """The adapter's product module is not on this branch. Raised, never swallowed: a stage must not report a number from a stand-in."""


# product module a stage needs, with the interface the adapter expects of it
EXPECTED = {
    "adaptive_signals": ("living_context.adaptive.signals", "evaluate(pool_state, window) -> dict of ADAPTIVE 4.1 signals"),
    "adaptive_machine": ("living_context.adaptive.machine", "Evaluator(pool_state).step(now, signals) -> proposal | None (ADAPTIVE 4.3, 4.4)"),
    "adaptive_probation": ("living_context.adaptive.probation", "Probation.record(unit) / Probation.decide() (ADAPTIVE 5.3)"),
    "pool_select": ("living_context.pools.select", "select(granted, hint, query_vec, cards, p_all, p_max, tau_card) -> list of pool ids (POOLS 4.2)"),
    "pool_merge": ("living_context.pools.merge", "merge(lists_by_pool, k_pool, k_total, i_rank, d_floor) -> ranked items (POOLS 5.1)"),
    "inject": ("living_context.pools.inject", "inject_check(session_state, turn) -> offers (POOLS 6.2, 6.3)"),
    "rollover": ("living_context.adaptive.rollover", "roll(index_stats, b_shard_target, age_roll) -> action (ADAPTIVE 4.5)"),
    "tree_arm": ("living_context.recall.fanout", "needs a model port: not zero-call"),
}


def product_available(key: str) -> bool:
    mod = EXPECTED[key][0]
    try:
        return importlib.util.find_spec(mod) is not None
    except (ImportError, ValueError):
        return False


def require_product(key: str):
    """Import the product module for `key` or raise ProductCodeMissing naming the expected module and interface."""
    mod, iface = EXPECTED[key]
    if not product_available(key):
        raise ProductCodeMissing(f"{mod} is not on this branch (adapter for '{key}' expects: {iface})")
    return importlib.import_module(mod)


class Adapter:
    """Base of an adapter: `call` imports the product module on first use, and raises ProductCodeMissing if it is not there."""
    key = ""

    def module(self):
        return require_product(self.key)


class SignalsAdapter(Adapter):
    key = "adaptive_signals"

    def evaluate(self, pool_state, window):
        return self.module().evaluate(pool_state, window)


class MachineAdapter(Adapter):
    key = "adaptive_machine"

    def run_trace(self, trace, params):
        return self.module().Evaluator(params).run(trace)


class ProbationAdapter(Adapter):
    key = "adaptive_probation"

    def run(self, units, params):
        return self.module().Probation(params).run(units)


class SelectionAdapter(Adapter):
    key = "pool_select"

    def select(self, *a, **kw):
        return self.module().select(*a, **kw)


class MergeAdapter(Adapter):
    key = "pool_merge"

    def merge(self, *a, **kw):
        return self.module().merge(*a, **kw)


class InjectionAdapter(Adapter):
    key = "inject"

    def inject_check(self, *a, **kw):
        return self.module().inject_check(*a, **kw)


class RolloverAdapter(Adapter):
    key = "rollover"

    def roll(self, *a, **kw):
        return self.module().roll(*a, **kw)


@dataclass(frozen=True)
class Stage:
    id: str
    content: str
    measures: str
    informs: str
    inputs: str
    outputs: str
    status: str                   # RUNS | PARTIAL | WAITS
    built: str                    # what exists in this kit
    missing: str                  # what is not available (product code, data)
    needs: Sequence[str] = ()     # EXPECTED keys the full stage needs


STAGES: List[Stage] = [
    Stage("Z1", "G1 size sweep on the host to its limit", "index size per leaf, ingest rate through the real write path, query latency, shard count at stage 0",
          "D8 (rollover), AT-8", "G1 corpora at sizes 64 to 20,000 (tunekit.synth)", "bytes per leaf, leaves per second, p50 and p95 latency per size",
          "PARTIAL", "tunekit.baseline and run_grid report these for every cell up to 20,000 leaves on one shard",
          "D8 needs shard sizes near 1 GiB and the rollover rule (B_SHARD_TARGET); the leaf of this generator is about 2.7 KB so 1 GiB is about 400,000 leaves, beyond the 15 GB budget with the real write path; the rollover code does not exist",
          ("rollover",)),
    Stage("Z2", "G2 to G5 on the flat arm, real embedder", "per-query features (agree, margin, spread), memory signals, their AUC against plain-retrieval failure",
          "D4 features, D3, trigger choice", "G2 to G5 cells (tunekit.synth)", "plain-retrieval curves by size and complexity; AUC with intervals per feature",
          "RUNS", "tunekit.baseline, tunekit.signals, tunekit.run_grid; the stub-embedding variant is not run (real embedder only)",
          "the second branch of hard(unit) (re-query within a conversation) needs recall streams; il_*, link_*, multi_rate, open_share need a tree arm and opens"),
    Stage("Z3", "state-machine simulations on generated traces", "false moves, attempts, spend, probation outcomes, N_FLOOR derivation",
          "D1, D5, D6, D7 (rollback side), AT-4", "traces from tunekit.traces", "false-move rate with Wilson bound, attempts, spend",
          "WAITS", "tunekit.traces (growth, stationary noise, oscillation, burst, bulk import, one flooding principal) and MachineAdapter/ProbationAdapter; the N_FLOOR check is arithmetic and is tested",
          "the evaluator and probation code (ADAPTIVE 4.3 to 5.3) is not on this branch", ("adaptive_machine", "adaptive_probation", "adaptive_signals")),
    Stage("Z4", "G6, G7, G8 with the stub: selection, merge, injection, token usage", "answer-pool hit rate, merge starvation, trigger recall and precision, injection bounds",
          "D9 to D13, AT-5, AT-6, AT-9", "G6 layouts (tunekit.synth_pools), G7 conversations (tunekit.synth_conv)", "Wilson bounds per layout",
          "PARTIAL", "G6 and G7 generators; D9 run with a REFERENCE reimplementation of the POOLS 4.2 rule as written (tunekit.pool_reference), to be replaced by the product's rule through SelectionAdapter",
          "product selection, merge and injection code; G8 usage scripts not built; the downloaded multi-session sets are not downloaded", ("pool_select", "pool_merge", "inject")),
    Stage("Z5", "ADAPT against TREE-ONLY and FLAT-ONLY on SYN growth with the stub", "recall difference and cost along a growth trace",
          "AT-2", "G1 growth cells, stub model port", "per-checkpoint recall differences with intervals",
          "WAITS", "the G1 growth cells and the flat arm", "the adaptive evaluator, the tree build and a model port with the stub", ("adaptive_machine", "tree_arm")),
    Stage("Z6", "the same features and labels on DEV and DL flat arms (A0)", "difficulty features and AUC on real text with real labels",
          "D3, D4 levels", "an external set in the BEIR-style layout (tunekit.external)", "the same tables as Z2",
          "WAITS", "tunekit.external converts a corpus, queries and qrels into the harness's cell format; tested on a tiny fixture",
          "no dataset is downloaded (the owner approves files one by one; docs/tuning/DATASETS.md lists what to verify); the DEV split is not in this kit"),
]


def table() -> List[dict]:
    out = []
    for s in STAGES:
        missing_code = [k for k in s.needs if not product_available(k)]
        out.append({"id": s.id, "status": s.status, "content": s.content, "informs": s.informs, "built": s.built,
                    "missing": s.missing, "product_modules_absent": [EXPECTED[k][0] for k in missing_code]})
    return out


# ---- derivation check Z3 can do now: D1 --------------------------------------------------------------------------------
def n_floor(f_max: int = 24, f_target: int = 16) -> int:
    """ADAPTIVE 4.3: N_FLOOR = F_MAX x F_TARGET."""
    return f_max * f_target
