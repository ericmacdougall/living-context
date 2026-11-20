"""The reverse audit (DESIGN 12; BUILD-PLAN Step 18, minimal producer, stub only, no model call outside the judge).

Per cycle and class: candidate generation costs no model call (every leaf is percolated against the stored queries added since
the last cycle, and each such query's vector is a kNN over the leaf vectors; both are passed in as functions so the ES-backed and
the in-memory forms share this logic), candidates accumulate in a `Ledger` ACROSS cycles, leaves are ranked by a gold-blind sort,
at most `N_AUDIT` leaves are judged per cycle and the judge calls of a cycle never exceed `N_AUDIT` (the stricter reading of
DESIGN 12 and BUILD-PLAN Step 18, see design-readings), the judge confirms which candidate searches are relevant, the log answers
whether the tree offered the leaf for each (`offered_by_tree`), and a leaf is flagged for re-filing when at least `S_MIN`
distinct judged-relevant past searches were missed by the tree and its miss rate is at least `MISS_RATE_MIN`. The miss rate is
reported with a Wilson interval. Synthetic queries are a switch, off by default, tagged synthetic and measured apart: a leaf with
no search history is never re-filed with the switch off.

The `broken_*` flags are the BROKEN variants of the tests."""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Callable, Iterable, Mapping, Optional

from ..config import Constants


def wilson(k: int, n: int, z: float = 1.96) -> tuple:
    """Wilson score interval of a proportion k/n (n = 0 gives (0, 1))."""
    if n <= 0:
        return 0.0, 1.0
    p = k / n
    d = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / d
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return max(0.0, centre - half), min(1.0, centre + half)


@dataclass
class Cand:
    search_id: str
    score: float
    cycle: int
    synthetic: bool = False
    relevant: Optional[bool] = None          # the judge's answer; None = not judged (unknown, never irrelevant)
    offered_by_tree: Optional[bool] = None   # from the protocol log (real searches) or a replay (synthetic)


@dataclass
class Ledger:
    """Candidate searches per leaf, accumulated across cycles (they are stored as notes in the full system)."""
    by_leaf: dict = field(default_factory=dict)

    def add(self, leaf_id: str, c: Cand) -> None:
        cur = self.by_leaf.setdefault(leaf_id, {})
        old = cur.get(c.search_id)
        if old is None or c.score > old.score:
            if old is not None:
                c.relevant, c.offered_by_tree = old.relevant, old.offered_by_tree
            cur[c.search_id] = c

    def cands(self, leaf_id: str) -> list:
        return list(self.by_leaf.get(leaf_id, {}).values())


def collect(ledger: Ledger, leaves: Mapping[str, object], new_queries: Mapping[str, object], cycle: int, *,
            percolate: Callable[[object, Mapping], Mapping], knn: Callable[[object, Mapping], Mapping],
            offered_by_tree: Callable[[str, str], Optional[bool]], broken_current_cycle_only: bool = False) -> None:
    """Candidate generation, model-free: lexical (percolate) and semantic (kNN) scores per (leaf, query added since the last
    cycle). `offered_by_tree(search_id, leaf_id)` answers from the protocol log."""
    if broken_current_cycle_only:   # BROKEN variant: what earlier cycles found is forgotten
        ledger.by_leaf.clear()
    for leaf_id, leaf in leaves.items():
        scores: dict = {}
        for sid, sc in list(percolate(leaf, new_queries).items()) + list(knn(leaf, new_queries).items()):
            scores[sid] = max(scores.get(sid, 0.0), sc)
        for sid, sc in scores.items():
            ledger.add(leaf_id, Cand(sid, sc, cycle, offered_by_tree=offered_by_tree(sid, leaf_id)))


def priority(ledger: Ledger, leaf_id: str) -> tuple:
    """Gold-blind: (searches the tree did not offer this leaf for, then best candidate score), descending; the id breaks ties."""
    cs = ledger.cands(leaf_id)
    missed = sum(1 for c in cs if c.offered_by_tree is False)
    return (-missed, -max((c.score for c in cs), default=0.0), leaf_id)


@dataclass
class LeafReport:
    leaf_id: str
    relevant: int = 0
    missed: int = 0
    miss_rate: float = 0.0
    interval: tuple = (0.0, 1.0)
    missed_searches: tuple = ()
    flagged: bool = False


@dataclass
class CycleResult:
    judge_calls: int = 0
    audited: list = field(default_factory=list)
    reports: dict = field(default_factory=dict)


def run_cycle(ledger: Ledger, judge: Callable[[str, str], Optional[bool]], consts: Optional[Constants] = None, *,
              use_synthetic: bool = False,
              broken_uncapped: bool = False, broken_synthetic_only: bool = False, s_min: Optional[int] = None) -> CycleResult:
    """Audit the top `N_AUDIT` leaves. `judge(search_id, leaf_id)` returns True/False, or None when it cannot label (unknown).
    Synthetic candidates are in the ledger only if `add_synthetic` put them there, and count only with the switch on."""
    c = consts or Constants()
    s_min = c.s_min if s_min is None else s_min
    res = CycleResult()
    order = sorted(ledger.by_leaf, key=lambda l: priority(ledger, l))
    budget = None if broken_uncapped else c.n_audit
    for leaf_id in order if broken_uncapped else order[: c.n_audit]:
        res.audited.append(leaf_id)
        cs = [x for x in ledger.cands(leaf_id) if (x.synthetic if broken_synthetic_only else (not x.synthetic or use_synthetic))]
        for cand in sorted(cs, key=lambda x: (x.relevant is not None, -x.score, x.search_id))[: c.k_cand]:
            if cand.relevant is None:
                if budget is not None and res.judge_calls >= budget:
                    break
                res.judge_calls += 1
                cand.relevant = judge(cand.search_id, leaf_id)
        res.reports[leaf_id] = report(leaf_id, cs, c, s_min, use_synthetic or broken_synthetic_only)
    return res


def report(leaf_id: str, cands: list, c: Constants, s_min: int, use_synthetic: bool) -> LeafReport:
    rel = [x for x in cands if x.relevant and (use_synthetic or not x.synthetic)]
    missed = [x for x in rel if x.offered_by_tree is False]
    rate = len(missed) / len(rel) if rel else 0.0
    return LeafReport(leaf_id, len(rel), len(missed), rate, wilson(len(missed), len(rel)), tuple(sorted(x.search_id for x in missed)),
                      flagged=len(missed) >= s_min and rate >= c.miss_rate_min)


def add_synthetic(ledger: Ledger, leaf_id: str, queries: Iterable[tuple], cycle: int) -> None:
    """Synthetic queries (switch on): tagged synthetic, measured apart; `queries` are (query id, offered_by_tree from a replay)."""
    for qid, offered in queries:
        ledger.add(leaf_id, Cand(qid, 0.0, cycle, synthetic=True, offered_by_tree=offered))
