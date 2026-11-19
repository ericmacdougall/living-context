"""Balanced clustering to the fan-out target (DESIGN 5.4, 7.6; BUILD-PLAN Step 9).

`balanced_groups` splits n vectors into k = ceil(n / f_target) groups whose sizes differ by at most one, so no
group exceeds `f_max` (when f_target <= f_max) and none is below `f_min` unless n itself is (a single short group).
The assignment is capacity-constrained k-means: farthest-first initial centres (deterministic), then a few rounds of
global nearest-pair assignment under the exact size quotas. Pure and deterministic; numpy only.
"""
from __future__ import annotations

import math

import numpy as np


def group_count(n: int, f_target: int) -> int:
    return max(1, math.ceil(n / f_target))


def _sizes(n: int, k: int) -> list[int]:
    return [n // k + (1 if i < n % k else 0) for i in range(k)]


def balanced_groups(vectors, f_target: int, rounds: int = 3) -> list[list[int]]:
    """Return k lists of indices; sizes are exact quotas. Group order and member order are deterministic."""
    x = np.asarray(vectors, dtype=np.float64)
    n = len(x)
    k = group_count(n, f_target)
    if k == 1:
        return [list(range(n))]
    quota = _sizes(n, k)
    centres = [0]
    d = np.linalg.norm(x - x[0], axis=1)
    for _ in range(k - 1):  # farthest-first
        nxt = int(np.argmax(d))
        centres.append(nxt)
        d = np.minimum(d, np.linalg.norm(x - x[nxt], axis=1))
    c = x[centres].copy()
    groups: list[list[int]] = [[] for _ in range(k)]
    for _ in range(rounds):
        dist = ((x[:, None, :] - c[None, :, :]) ** 2).sum(axis=2)  # n x k
        order = np.dstack(np.unravel_index(np.argsort(dist, axis=None, kind="stable"), dist.shape))[0]
        groups, left, done, placed = [[] for _ in range(k)], quota[:], np.zeros(n, dtype=bool), 0
        for i, g in order.tolist():
            if not done[i] and left[g] > 0:
                groups[g].append(i)
                left[g] -= 1
                done[i] = True
                placed += 1
                if placed == n:
                    break
        new_c = np.array([x[g].mean(axis=0) for g in groups])
        if np.allclose(new_c, c):
            break
        c = new_c
    return [sorted(g) for g in groups]


def kmeans_groups(vectors, f_target: int, rounds: int = 6) -> list[list[int]]:
    """BROKEN VARIANT (tests only): plain k-means with no size quotas; a dense topic yields a node over `f_max`."""
    x = np.asarray(vectors, dtype=np.float64)
    n = len(x)
    k = group_count(n, f_target)
    c = x[np.linspace(0, n - 1, k).astype(int)].copy()
    for _ in range(rounds):
        lab = ((x[:, None, :] - c[None, :, :]) ** 2).sum(axis=2).argmin(axis=1)
        for g in range(k):
            if (lab == g).any():
                c[g] = x[lab == g].mean(axis=0)
    out = [[int(i) for i in np.where(lab == g)[0]] for g in range(k)]
    return [g for g in out if g]
