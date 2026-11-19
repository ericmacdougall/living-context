"""Null coverage and power of the replay gate (EXPERIMENT.md 7.3, DESIGN.md 13.3).

The gate, as pre-registered:

  Replay queries are grouped into clusters k = 1..K. One cluster per operation of the candidate (a move, an added
  membership, a re-summary, a split or merge, an edge change), holding every replay query generated from a leaf
  that operation changed and every held-out search whose judged-relevant items include such a leaf; operations
  that share a replay query are merged into one cluster. A cluster contributes at most 6 queries. Per query q, r(q) is tree-source recall of judged-relevant items within the top 10, and the
  paired difference is scored by its sign, d_q = sign(r_cand(q) - r_curr(q)) in {-1, 0, +1} (the candidate
  offered more, the same, or fewer of the judged-relevant items).

      D_k = (1 / n_k) * sum_{q in k} d_q          (cluster-mean paired difference)
      T   = (1 / K)   * sum_k D_k                 (mean over clusters)
      p   = #{ e in {-1, +1}^K : (1/K) sum_k e_k D_k >= T } / 2^K   (exact one-sided sign-flip p-value)

  Adopt only if K >= 8 and p < 0.05 (and the general-replay count guard passes; not simulated here).
  The test is evaluated once per candidate: no repeated looks.

The sign-flip test is exact under the null that cluster means are symmetric about 0 and clusters are mutually
independent; any dependence inside a cluster is allowed. The ASYMMETRIC NULL section shows what happens when
symmetry fails, in both directions: rare large gains with frequent small losses (conservative), and frequent
small gains with rare large losses (anti-conservative at small K). p is computed exactly by dynamic programming over integer sums after scaling every D_k by
the least common multiple of their denominators (at most 60, because n_k <= 6 and d_q is an integer), so there
are at most 2 * 60 * K + 1 distinct sums. Scoring d_q by sign keeps this grid small: with raw fractional recall
differences the common denominator grows with the data and one exact evaluation at K = 60 took up to 100 s in a
trial, so the sign is the pre-registered score.

Plain Python 3, standard library only, seeded. Run: python3 -I gate_power.py
"""
import math
import random
import time
from fractions import Fraction

ALPHA = 0.05
K_MIN = 8
SIMS = 4000
SEED = 16096


def exact_p(cluster_means):
    """Exact one-sided sign-flip p-value for the sum of the cluster means (Fractions)."""
    k = len(cluster_means)
    lcm = 1
    for x in cluster_means:
        lcm = lcm * x.denominator // math.gcd(lcm, x.denominator)
    ints = [int(x * lcm) for x in cluster_means]
    observed = sum(ints)
    groups = {}
    for v in ints:
        groups[abs(v)] = groups.get(abs(v), 0) + 1
    counts = {0: 1}
    for v, m in groups.items():
        if v == 0:
            continue  # a zero mean gives the same sum under both signs: factor 2^m, cancels in the ratio
        binom = [math.comb(m, j) for j in range(m + 1)]
        new = {}
        for s, c in counts.items():
            for j in range(m + 1):  # j of the m equal values get sign +1
                t = s + v * (2 * j - m)
                new[t] = new.get(t, 0) + c * binom[j]
        counts = new
    nonzero = sum(m for v, m in groups.items() if v != 0)
    ge = sum(c for s, c in counts.items() if s >= observed)
    return Fraction(ge, 2 ** nonzero)


def adopt(cluster_means):
    return len(cluster_means) >= K_MIN and exact_p(cluster_means) < ALPHA


def draw_d(rng, pp, pm):
    u = rng.random()
    return 1 if u < pp else (-1 if u < pp + pm else 0)


def clusters_correlated(rng, sizes, pp, pm, rho):
    """Each cluster: with probability rho every query shares one draw (fully correlated), else independent draws."""
    out = []
    for n in sizes:
        if rng.random() < rho:
            d = draw_d(rng, pp, pm)
            ds = [d] * n
        else:
            ds = [draw_d(rng, pp, pm) for _ in range(n)]
        out.append(Fraction(sum(ds), n))
    return out


def clusters_skewed(rng, k):
    """Mean-zero but asymmetric cluster means: +4/5 with probability 0.2, -1/5 with probability 0.8."""
    return [Fraction(4, 5) if rng.random() < 0.2 else Fraction(-1, 5) for _ in range(k)]


def clusters_skewed_mirror(rng, k):
    """The mirror case: +1/5 with probability 0.8, -4/5 with probability 0.2 (frequent small gains, rare large losses)."""
    return [Fraction(-4, 5) if rng.random() < 0.2 else Fraction(1, 5) for _ in range(k)]


def rate(fn, seed):
    rng = random.Random(seed)
    return sum(adopt(fn(rng)) for _ in range(SIMS)) / SIMS


def per_query_naive_rate(sizes, pp, pm, seed):
    """Contrast: the same exact test applied per query (each query its own 'cluster'), fully correlated clusters."""
    rng = random.Random(seed)
    wins = 0
    for _ in range(SIMS):
        q = []
        for n in sizes:
            q += [Fraction(draw_d(rng, pp, pm))] * n
        wins += len(q) >= K_MIN and exact_p(q) < ALPHA
    return wins / SIMS


LAYOUTS = [
    ("60 single-query clusters", [1] * 60),
    ("20 clusters x 3", [3] * 20),
    ("10 clusters x 6", [6] * 10),
    ("8 clusters x 6 (minimum K)", [6] * 8),
    ("10 held-out singletons + 10 leaf clusters x 5", [1] * 10 + [5] * 10),
]

print("Replay gate: exact one-sided cluster sign-flip test, alpha %.2f, K >= %d, %d runs per cell, seed %d"
      % (ALPHA, K_MIN, SIMS, SEED))
print()
print("NULL COVERAGE (adoption probability when the candidate changes nothing; must be <= 0.05)")
cell = 0
for name, sizes in LAYOUTS:
    for pp, rho in ((0.05, 1.0), (0.20, 1.0), (0.30, 1.0), (0.20, 0.0)):
        cell += 1
        r = rate(lambda rng, s=sizes, a=pp, c=rho: clusters_correlated(rng, s, a, a, c), SEED + cell)
        print("  %-46s p+ = p- = %.2f, %s: %.4f" % (
            name, pp, "fully correlated within cluster" if rho == 1.0 else "independent within cluster", r))
cell += 1
print("  %-46s p+ = p- = 0.20, fully correlated: %.4f (K = 1 < 8: never adopts)" % (
    "1 cluster of 60 paraphrases of one leaf",
    rate(lambda rng: clusters_correlated(rng, [60], 0.2, 0.2, 1.0), SEED + cell)))
cell += 1
print("  %-46s p+ = p- = 0.20: %.4f (contrast: what treating the 60 as independent would do)" % (
    "same 60 paraphrases tested per query", per_query_naive_rate([60], 0.2, 0.2, SEED + cell)))
cell += 1
print("  %-46s p+ = p- = 0.20: %.4f (contrast: 10 clusters x 6 tested per query)" % (
    "10 x 6 fully correlated, tested per query", per_query_naive_rate([6] * 10, 0.2, 0.2, SEED + cell)))
skewed = []
for k in (8, 10, 20, 60):
    cell += 1  # computed here so that every later cell keeps its seed; printed in ASYMMETRIC NULL below
    skewed.append((k, rate(lambda rng, kk=k: clusters_skewed(rng, kk), SEED + cell)))

print()
print("POWER (adoption probability; p- = 0.05, net effect = p+ - p-)")
for name, sizes in LAYOUTS:
    for rho, label in ((1.0, "fully correlated"), (0.0, "independent")):
        if len(set(sizes)) == 1 and sizes[0] == 1 and rho == 0.0:
            continue  # same as fully correlated for single-query clusters
        row = []
        for pp in (0.20, 0.30, 0.40, 0.60):
            cell += 1
            row.append("p+ %.2f: %.2f" % (pp, rate(
                lambda rng, s=sizes, a=pp, c=rho: clusters_correlated(rng, s, a, 0.05, c), SEED + cell)))
        print("  %-46s %-17s %s" % (name, label, "  ".join(row)))

print()
print("ORACLE-LIKE CANDIDATE (each cluster improves with probability 0.8, never worsens; fully correlated)")
for k in (8, 16, 40):
    cell += 1
    print("  K = %-3d clusters x 3: %.2f" % (k, rate(
        lambda rng, kk=k: clusters_correlated(rng, [3] * kk, 0.8, 0.0, 1.0), SEED + cell)))

print()
print("REPEATED CANDIDATES (one evaluation each; each null candidate is adopted with the probability above)")
for size in (0.01, 0.02, 0.03, 0.05):
    print("  per-candidate null adoption %.2f: P(at least one false adoption) over 18 / 30 null candidates = %.2f / %.2f"
          % (size, 1 - (1 - size) ** 18, 1 - (1 - size) ** 30))

print()
print("ASYMMETRIC NULL (cluster means with mean 0 but not symmetric about 0: the sign-flip test's assumption fails)")
for k, r in skewed:
    print("  rare large gains (+0.8 w.p. 0.2, -0.2 otherwise), K = %-3d %.4f" % (k, r))
for k in (8, 10, 20, 60):
    print("  rare large losses (+0.2 w.p. 0.8, -0.8 otherwise), K = %-3d %.4f" % (
        k, rate(lambda rng, kk=k: clusters_skewed_mirror(rng, kk), SEED + 10000 + k)))

print()
rng = random.Random(SEED)
worst = 0.0
for _ in range(200):
    means = []
    for _k in range(60):
        n = rng.randint(1, 6)
        means.append(Fraction(sum(rng.choice((-1, 0, 1)) for _q in range(n)), n))
    t0 = time.perf_counter()
    exact_p(means)
    worst = max(worst, time.perf_counter() - t0)
print("EXACT P COST: K = 60 clusters of 1 to 6 queries, sign-scored: slowest of 200 evaluations = %.3f s (depends on the machine)" % worst)

# A test valid for every mean-zero distribution of cluster means in [-1, 1] (Hoeffding: P(T >= t) <= exp(-K t^2 / 2))
# needs T >= sqrt(2 ln(1 / alpha) / K); printed for comparison with the sign-flip test (no random numbers used).
print("HOEFFDING ALTERNATIVE: mean T needed to adopt at alpha %.2f: K = 8: %.2f, K = 20: %.2f, K = 60: %.2f"
      % (ALPHA, math.sqrt(2 * math.log(1 / ALPHA) / 8), math.sqrt(2 * math.log(1 / ALPHA) / 20),
         math.sqrt(2 * math.log(1 / ALPHA) / 60)))
