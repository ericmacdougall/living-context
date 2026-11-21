"""The registered statistics for H1 (EXPERIMENT 2.1, 2.2, 7.1, 7.2; BUILD-PLAN Step 20), plain Python.

- Clustered paired bootstrap over questions: whole content clusters are resampled with replacement (questions that share
  gold content move together), percentile 95% interval, 10,000 resamples, seed 16096; BCa beside it as a sensitivity.
  The statistic is the mean paired difference over the questions in the resampled clusters. The number of clusters
  (effective n) is returned with every interval.
- Wilson intervals only for measures declared binary; a count or mean measure is refused.
- Outcome words of 2.1 and the H1 decision of 2.2.

Readings made here (the registered text leaves them open; each is reversible without changing the method):
  R1. "the worse of the two" for (a) and (b) is ordered HOLDS < HOLDS, BELOW MINIMUM < NOT ESTABLISHED < DID NOT HOLD
      < HARM; a firm negative is worse for the claim than an unresolved one. HARM is defined for H1 on (a) only
      (2.2), so a (b) interval entirely below 0 reads DID NOT HOLD.
  R2. The H1 minimum effect is 0.05 (7.2).
Holm adjustment and the Tango and Newcombe score intervals (H2, H4) are not needed for H1 and are not built here.
"""
from __future__ import annotations

import math
import random
from dataclasses import dataclass
from statistics import NormalDist
from typing import Dict, List, Mapping, Optional, Sequence, Tuple

SEED = 16096
RESAMPLES = 10_000
H1_MIN_EFFECT = 0.05
_N = NormalDist()

HOLDS, BELOW_MIN, NOT_ESTABLISHED, DID_NOT_HOLD, HARM = ("HOLDS", "HOLDS, BELOW MINIMUM", "NOT ESTABLISHED",
                                                         "DID NOT HOLD", "HARM")
_SEVERITY = {HOLDS: 0, BELOW_MIN: 1, NOT_ESTABLISHED: 2, DID_NOT_HOLD: 3, HARM: 4}


class StatsError(Exception):
    pass


@dataclass(frozen=True)
class Interval:
    point: float
    lo: float
    hi: float
    n_questions: int
    n_clusters: int
    method: str
    resamples: int = 0
    seed: int = 0


def _cluster_sums(diffs: Mapping[str, float], clusters: Mapping[str, str]) -> Tuple[List[float], List[int]]:
    sums: Dict[str, float] = {}
    cnt: Dict[str, int] = {}
    for q, d in diffs.items():
        c = clusters.get(q, q)
        sums[c] = sums.get(c, 0.0) + d
        cnt[c] = cnt.get(c, 0) + 1
    keys = sorted(sums)
    return [sums[k] for k in keys], [cnt[k] for k in keys]


def paired_differences(a: Mapping[str, float], b: Mapping[str, float]) -> Dict[str, float]:
    """Paired by question id. Both arms must score the same questions (an arm missing a question is an error, never a
    silent drop)."""
    if set(a) != set(b):
        raise StatsError("the two arms do not score the same questions")
    return {q: a[q] - b[q] for q in a}


def clustered_bootstrap(diffs: Mapping[str, float], clusters: Mapping[str, str], *, resamples: int = RESAMPLES,
                        seed: int = SEED, bca: bool = False, broken_per_question: bool = False) -> Interval:
    """Percentile (or BCa) 95% interval of the mean paired difference, resampling whole clusters.
    `broken_per_question` resamples questions as if independent (the variant the tests must turn red)."""
    if not diffs:
        raise StatsError("no questions")
    if broken_per_question:
        clusters = {q: q for q in diffs}
    sums, cnt = _cluster_sums(diffs, clusters)
    k = len(sums)
    total, n = sum(sums), sum(cnt)
    point = total / n
    rng = random.Random(seed)
    stats: List[float] = []
    for _ in range(resamples):
        idx = rng.choices(range(k), k=k)
        s = c = 0
        for i in idx:
            s += sums[i]
            c += cnt[i]
        stats.append(s / c)
    stats.sort()
    if not bca:
        lo, hi = stats[int(0.025 * resamples)], stats[min(resamples - 1, int(math.ceil(0.975 * resamples)) - 1)]
        return Interval(point, lo, hi, n, k, "clustered paired bootstrap, percentile", resamples, seed)
    below = sum(1 for s in stats if s < point) / resamples
    z0 = _N.inv_cdf(min(max(below, 1e-9), 1 - 1e-9))
    jack = [((total - sums[i]) / (n - cnt[i])) if n > cnt[i] else point for i in range(k)]
    jm = sum(jack) / k
    num = sum((jm - j) ** 3 for j in jack)
    den = 6.0 * (sum((jm - j) ** 2 for j in jack) ** 1.5)
    acc = num / den if den else 0.0

    def adj(alpha: float) -> float:
        z = _N.inv_cdf(alpha)
        return _N.cdf(z0 + (z0 + z) / (1 - acc * (z0 + z)))

    lo = stats[min(resamples - 1, max(0, int(adj(0.025) * resamples)))]
    hi = stats[min(resamples - 1, max(0, int(adj(0.975) * resamples)))]
    return Interval(point, lo, hi, n, k, "clustered paired bootstrap, BCa", resamples, seed)


# ---- Wilson, binary measures only -------------------------------------------------------------------------------
@dataclass(frozen=True)
class Measure:
    name: str
    kind: str    # "binary" | "mean" | "count"


def wilson(measure: Measure, successes: int, n: int, z: float = 1.959964) -> Tuple[float, float]:
    """Wilson 95% interval for a rate of binary outcomes. Refused for a measure not declared binary (EXPERIMENT 7.1: a
    count per recall is not a binary trial; its interval is the bootstrap over recalls)."""
    if measure.kind != "binary":
        raise StatsError("Wilson interval refused: measure %r is not declared binary" % measure.name)
    if n <= 0 or not 0 <= successes <= n:
        raise StatsError("bad counts")
    p = successes / n
    den = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / den
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / den
    return max(0.0, centre - half), min(1.0, centre + half)


# ---- outcome words ----------------------------------------------------------------------------------------------
def outcome(lo: float, hi: float, m: float = H1_MIN_EFFECT, *, harm_defined: bool = True) -> str:
    """2.1. HARM is tested first: hi < 0 also satisfies lo <= 0 and hi < m."""
    if harm_defined and hi < 0:
        return HARM
    if lo > 0:
        return BELOW_MIN if hi < m else HOLDS
    return DID_NOT_HOLD if hi < m else NOT_ESTABLISHED


def worse(*words: str) -> str:
    return max(words, key=lambda w: _SEVERITY[w])


@dataclass(frozen=True)
class H1Decision:
    word: str
    word_a: str
    word_b: str
    tree_only_share: Optional[Tuple[int, int, float, float]]    # (k, n, wilson lo, wilson hi)
    reason: str


def decide_h1(a: Interval, b: Interval, tree_only_k: int, tree_only_n: int, m: float = H1_MIN_EFFECT) -> H1Decision:
    """2.2: HOLDS only if (a) A1 minus A0 has lo > 0, (b) A1 minus A0+LEN has lo > 0 and (c) the share of questions with at
    least one gold session in A1's top 10 whose gold came only from the tree has a Wilson lower bound above 0. The word
    comes from (a) and (b) together (the worse); (c) failing makes it NOT ESTABLISHED."""
    wa = outcome(a.lo, a.hi, m)
    wb = outcome(b.lo, b.hi, m, harm_defined=False)
    word = worse(wa, wb)
    share = None
    reason = "word from (a) and (b)"
    if tree_only_n > 0:
        lo, hi = wilson(Measure("tree_only_share", "binary"), tree_only_k, tree_only_n)
        share = (tree_only_k, tree_only_n, lo, hi)
    if word in (HOLDS, BELOW_MIN) and (share is None or share[2] <= 0):
        word, reason = NOT_ESTABLISHED, "(c) failed: the gain is not shown to come from the tree"
    return H1Decision(word, wa, wb, share, reason)


@dataclass(frozen=True)
class ControlVerdict:
    passed: bool
    point: float
    lo: float
    reason: str


def shuffled_control(drop: Interval, *, min_point: float = 0.10) -> ControlVerdict:
    """SHUFFLED-SUMMARIES must show a drop in A1-tree-only recall@10 with point at least `min_point` (0.10 registered for
    the real run on 50 questions; 0.30 for the stub gate on the fixture) and a lower bound above 0. `drop` is the interval
    of (A1-tree-only minus SHUFFLED)."""
    ok = drop.point >= min_point and drop.lo > 0
    return ControlVerdict(ok, drop.point, drop.lo, "drop point %.3f lo %.3f against minimum %.2f" % (drop.point, drop.lo, min_point))
