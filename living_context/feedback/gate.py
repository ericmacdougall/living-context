"""The replay gate on adopting a reorganisation (DESIGN 13.3; BUILD-PLAN Step 19, the statistical core and the replay set).

Replay queries are grouped into clusters (one per operation of the candidate, operations that share a query merged); per replay
query d_q = sign(r_cand - r_curr); D_k is the cluster mean, T the mean of the D_k and p the exact one-sided sign-flip p-value over
the 2^K sign vectors, computed by counting signed integer sums. Adopt only if K >= 8, p < 0.05 and the general-replay guard passes;
the test is evaluated once per candidate. The gate is an inspectable heuristic, not a bounded false-adoption guarantee (13.3).

`spread` builds the replay set: up to 60 queries over as many clusters as possible (one per cluster in turn, held-out searches
first), at most 6 per cluster. Gate-only synthetic queries are tagged synthetic and are never stored: `replay_queries` takes no
store and never calls one. `broken_per_query` is the BROKEN variant of the tests: every query is its own cluster."""
from __future__ import annotations

import math
from dataclasses import dataclass
from fractions import Fraction
from typing import Callable, Mapping, Optional, Sequence

K_MIN = 8
ALPHA = Fraction(5, 100)
CLUSTER_MAX = 6
QUERY_MAX = 60


def exact_p(means: Sequence[Fraction]) -> Fraction:
    """Exact one-sided sign-flip p-value: the share of sign vectors e with (1/K) sum e_k D_k >= T, counted over integer sums."""
    lcm = 1
    for x in means:
        lcm = lcm * x.denominator // math.gcd(lcm, x.denominator)
    ints = [int(x * lcm) for x in means]
    observed = sum(ints)
    groups: dict = {}
    for v in ints:
        groups[abs(v)] = groups.get(abs(v), 0) + 1
    counts = {0: 1}
    for v, m in groups.items():
        if v == 0:
            continue   # a zero mean gives the same sum under both signs: a factor 2^m that cancels in the ratio
        binom = [math.comb(m, j) for j in range(m + 1)]
        new: dict = {}
        for s, c in counts.items():
            for j in range(m + 1):
                t = s + v * (2 * j - m)
                new[t] = new.get(t, 0) + c * binom[j]
        counts = new
    nonzero = sum(m for v, m in groups.items() if v != 0)
    return Fraction(sum(c for s, c in counts.items() if s >= observed), 2 ** nonzero)


@dataclass(frozen=True)
class Decision:
    adopt: bool
    k: int
    p: Optional[Fraction]
    reason: str


def decide(clusters: Mapping[str, Sequence[int]], *, general_guard_ok: bool = True, broken_per_query: bool = False) -> Decision:
    """`clusters`: cluster id -> the d_q in {-1, 0, +1} of its queries. One decision, no repeated looks."""
    if broken_per_query:   # BROKEN: correlated paraphrases are counted as independent units
        clusters = {f"{c}#{i}": [d] for c, ds in clusters.items() for i, d in enumerate(ds)}
    means = [Fraction(sum(ds), len(ds)) for ds in clusters.values() if ds]
    k = len(means)
    if k < K_MIN:
        return Decision(False, k, None, "fewer than 8 clusters: no decision, keep current")
    p = exact_p(means)
    if p >= ALPHA:
        return Decision(False, k, p, "p is not below 0.05: keep current")
    if not general_guard_ok:
        return Decision(False, k, p, "the general-replay guard failed: keep current")
    return Decision(True, k, p, "adopt")


def spread(held_out: Mapping[str, Sequence[str]], synthetic: Mapping[str, Sequence[str]], *, total: int = QUERY_MAX,
           cluster_max: int = CLUSTER_MAX) -> dict:
    """Up to `total` queries over as many clusters as possible: one query per cluster in turn, held-out searches first, before any
    cluster gets a second; a cluster holds at most `cluster_max`. Returns cluster id -> [(query id, synthetic flag)]."""
    pools = {c: [(q, False) for q in held_out.get(c, ())] + [(q, True) for q in synthetic.get(c, ())]
             for c in sorted(set(held_out) | set(synthetic))}
    out: dict = {c: [] for c in pools}
    n = 0
    while n < total:
        moved = False
        for c, pool in pools.items():
            if n >= total:
                break
            if pool and len(out[c]) < cluster_max:
                out[c].append(pool.pop(0))
                n += 1
                moved = True
        if not moved:
            break
    return {c: v for c, v in out.items() if v}


def replay_queries(held_out: Mapping[str, Sequence[str]], make_synthetic: Callable[[str], Sequence[str]],
                   clusters_leaves: Mapping[str, Sequence[str]], **kw) -> dict:
    """The replay set of a candidate. Synthetic queries are generated per cluster from its changed leaves and are tagged: they are
    returned to the caller for the replay and never written anywhere (this function takes no store)."""
    synth = {c: [q for leaf in leaves for q in make_synthetic(leaf)] for c, leaves in clusters_leaves.items()}
    return spread(held_out, synth, **kw)
