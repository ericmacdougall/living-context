"""Small statistics helpers in the conventions of EXPERIMENT 7 and 11 (percentile 95% intervals, seed 16096, Wilson for
single rates, cluster bootstrap over questions). Pure python plus numpy; no model call."""
from __future__ import annotations

import math
from typing import Iterable, Optional, Sequence, Tuple

import numpy as np

SEED = 16096
Z95 = 1.959963984540054


def wilson(k: int, n: int, z: float = Z95) -> Tuple[float, float, float]:
    """(point, lower, upper) Wilson score interval of k successes in n trials; (nan, nan, nan) for n = 0."""
    if n <= 0:
        return (float("nan"),) * 3
    p = k / n
    d = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / d
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return p, max(0.0, centre - half), min(1.0, centre + half)


def auc(score: Sequence[float], label: Sequence[int]) -> float:
    """Mann-Whitney AUC of `score` (higher = more likely positive) against 0/1 `label`; ties count one half."""
    s = np.asarray(score, dtype=float)
    y = np.asarray(label, dtype=int)
    npos, nneg = int(y.sum()), int((1 - y).sum())
    if npos == 0 or nneg == 0:
        return float("nan")
    order = np.argsort(s, kind="mergesort")
    ss = s[order]
    ranks = np.empty(len(s), dtype=float)
    i = 0
    while i < len(ss):  # average ranks over ties
        j = i
        while j + 1 < len(ss) and ss[j + 1] == ss[i]:
            j += 1
        ranks[order[i:j + 1]] = (i + j) / 2.0 + 1.0
        i = j + 1
    return float((ranks[y == 1].sum() - npos * (npos + 1) / 2.0) / (npos * nneg))


def cluster_bootstrap(stat, clusters: Sequence, arrays: Sequence[np.ndarray], *, resamples: int = 10_000, seed: int = SEED,
                      alpha: float = 0.05) -> Tuple[float, float, float, int]:
    """Cluster bootstrap of `stat(*arrays)`: whole clusters are resampled with replacement. Returns (point, lower, upper, effective n =
    number of clusters). Percentile interval. `arrays` are equal-length numpy arrays aligned with `clusters`."""
    cl = np.asarray(clusters)
    uniq, inv = np.unique(cl, return_inverse=True)
    idx_by = [np.nonzero(inv == i)[0] for i in range(len(uniq))]
    rng = np.random.default_rng(seed)
    point = float(stat(*arrays))
    vals = []
    for _ in range(resamples):
        pick = rng.integers(0, len(uniq), len(uniq))
        idx = np.concatenate([idx_by[i] for i in pick])
        v = stat(*[a[idx] for a in arrays])
        if not (isinstance(v, float) and math.isnan(v)):
            vals.append(v)
    if not vals:
        return point, float("nan"), float("nan"), len(uniq)
    lo, hi = np.percentile(vals, [100 * alpha / 2, 100 * (1 - alpha / 2)])
    return point, float(lo), float(hi), len(uniq)


def auc_report(score, label, clusters, *, resamples: int = 2000, min_each: int = 30) -> dict:
    """AUC with a cluster-bootstrap interval; NOT ESTABLISHED when fewer than `min_each` positives or negatives (TUNING 4.3)."""
    y = np.asarray(label, dtype=int)
    npos, nneg = int(y.sum()), int(len(y) - y.sum())
    if npos < min_each or nneg < min_each:
        return {"auc": None, "lo": None, "hi": None, "n_pos": npos, "n_neg": nneg, "clusters": None,
                "reading": "NOT ESTABLISHED (fewer than %d positives or negatives)" % min_each}
    p, lo, hi, nc = cluster_bootstrap(auc, clusters, [np.asarray(score, dtype=float), y], resamples=resamples)
    return {"auc": round(p, 4), "lo": round(lo, 4), "hi": round(hi, 4), "n_pos": npos, "n_neg": nneg, "clusters": nc,
            "reading": "exploratory"}


def percentile(values: Iterable[float], q: float) -> float:
    v = sorted(values)
    if not v:
        return float("nan")
    k = (len(v) - 1) * q / 100.0
    f, c = math.floor(k), math.ceil(k)
    return v[int(k)] if f == c else v[f] + (v[c] - v[f]) * (k - f)


def spearman(x: Sequence[float], y: Sequence[float]) -> Optional[float]:
    if len(x) < 3:
        return None
    rx = np.argsort(np.argsort(np.asarray(x, dtype=float))).astype(float)
    ry = np.argsort(np.argsort(np.asarray(y, dtype=float))).astype(float)
    if rx.std() == 0 or ry.std() == 0:
        return None
    return float(np.corrcoef(rx, ry)[0, 1])
