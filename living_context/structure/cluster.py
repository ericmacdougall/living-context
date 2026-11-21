"""Balanced clustering to the fan-out target (DESIGN 5.4, 7.6; BUILD-PLAN Step 9).

`balanced_groups` splits n vectors into k = ceil(n / f_target) groups whose sizes differ by at most one, so no
group exceeds `f_max` (when f_target <= f_max) and none is below `f_min` unless n itself is (a single short group).
The assignment is capacity-constrained k-means: farthest-first initial centres (deterministic), then a few rounds of
global nearest-pair assignment under the exact size quotas. Pure and deterministic; numpy only.

`workers > 1` runs the same algorithm on threads (numpy releases the GIL in its loops) and returns exactly what one worker returns:
the distances use the same row blocks and per-element operations into reused buffers, farthest-first takes the first maximum over
row blocks, and the global stable pair order is produced lazily in value buckets (all pairs of one value in one bucket, each bucket
stably sorted, pairs of a placed row or a full group dropped first; both states only ever grow), so the greedy sees the same pairs in
the same order up to the last placement. `workers == 1` is the original code, unchanged.
"""
from __future__ import annotations

import math
import threading
from concurrent.futures import ThreadPoolExecutor

import numpy as np


def group_count(n: int, f_target: int) -> int:
    return max(1, math.ceil(n / f_target))


def _sizes(n: int, k: int) -> list[int]:
    return [n // k + (1 if i < n % k else 0) for i in range(k)]


_BLOCK_ELEMS = 1 << 21  # float64 elements in one block of the difference tensor (16 MB)
_PAIR_CHUNK = 1 << 20


def _sq_dist(x, c):
    """Squared distances n x k with the arithmetic of `((x[:, None, :] - c[None, :, :]) ** 2).sum(axis=2)`, over blocks of rows, so
    the n x k x d difference tensor (tens of GB at 18k leaves) never exists. Each element is the same last-axis reduction."""
    n, k, d = len(x), len(c), x.shape[1]
    out = np.empty((n, k), dtype=np.float64)
    step = max(1, _BLOCK_ELEMS // max(1, k * d))
    for lo in range(0, n, step):
        out[lo:lo + step] = ((x[lo:lo + step, None, :] - c[None, :, :]) ** 2).sum(axis=2)
    return out


def balanced_groups(vectors, f_target: int, rounds: int = 3, workers: int = 1) -> list[list[int]]:
    """Return k lists of indices; sizes are exact quotas. Group order and member order are deterministic and do not depend on
    `workers`."""
    x = np.asarray(vectors, dtype=np.float64)
    n = len(x)
    k = group_count(n, f_target)
    if k == 1:
        return [list(range(n))]
    quota = _sizes(n, k)
    if workers > 1:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            return _balanced_par(x, k, quota, rounds, pool, workers)
    centres = [0]
    d = np.linalg.norm(x - x[0], axis=1)
    for _ in range(k - 1):  # farthest-first
        nxt = int(np.argmax(d))
        centres.append(nxt)
        d = np.minimum(d, np.linalg.norm(x - x[nxt], axis=1))
    c = x[centres].copy()
    groups: list[list[int]] = [[] for _ in range(k)]
    for _ in range(rounds):
        dist = _sq_dist(x, c)  # n x k, the values of the one-shot expression, computed in row blocks
        order = np.argsort(dist, axis=None, kind="stable")  # flat C-order positions i * k + g
        del dist
        groups, left, done, placed = [[] for _ in range(k)], quota[:], np.zeros(n, dtype=bool), 0
        for lo in range(0, order.size, _PAIR_CHUNK):  # sorted pairs in chunks: a full list of Python pairs is gigabytes
            for pos in order[lo:lo + _PAIR_CHUNK].tolist():
                i, g = divmod(pos, k)
                if not done[i] and left[g] > 0:
                    groups[g].append(i)
                    left[g] -= 1
                    done[i] = True
                    placed += 1
                    if placed == n:
                        break
            if placed == n:
                break
        del order
        new_c = np.array([x[g].mean(axis=0) for g in groups])
        if np.allclose(new_c, c):
            break
        c = new_c
    return [sorted(g) for g in groups]


_FF_ROWS = 256           # rows per farthest-first buffer (cache-sized)
_FIRST_BUCKET = 4        # the first value bucket holds about this many pairs per row
_SAMPLE = 1 << 20        # distances sampled (strided, deterministic) to place the bucket bounds


def _spans(n: int, step: int, parts: int) -> list[tuple[int, int]]:
    """Contiguous runs of whole `step`-row blocks, about `parts` per worker, so each task keeps the serial block boundaries."""
    blocks = -(-n // step)
    per = max(1, -(-blocks // parts))
    return [(b * step, min(n, (b + per) * step)) for b in range(0, blocks, per)]


def _sq_dist_par(x, c, pool, workers: int):
    """`_sq_dist` on threads: the same row blocks and the same element operations (subtract, square, last-axis sum), written into a
    per-thread buffer instead of a fresh temporary per block."""
    n, k, d = len(x), len(c), x.shape[1]
    out = np.empty((n, k), dtype=np.float64)
    step = max(1, _BLOCK_ELEMS // max(1, k * d))
    local = threading.local()

    def run(span):
        buf = getattr(local, "buf", None)
        if buf is None:
            buf = local.buf = np.empty((step, k, d), dtype=np.float64)
        for lo in range(span[0], span[1], step):
            hi = min(span[1], lo + step)
            b = buf[:hi - lo]
            np.subtract(x[lo:hi, None, :], c[None, :, :], out=b)
            np.square(b, out=b)
            b.sum(axis=2, out=out[lo:hi])
    list(pool.map(run, _spans(n, step, 4 * workers)))
    return out


def _norms_into(x, lo: int, hi: int, centre, buf, out):
    """`np.linalg.norm(x[lo:hi] - centre, axis=1)` (numpy's own steps for a real 2-norm along one axis: multiply by itself,
    add.reduce, sqrt) into `out`, through a reused cache-sized buffer."""
    for a in range(lo, hi, len(buf)):
        b = min(hi, a + len(buf))
        v, o = buf[:b - a], out[a - lo:b - lo]
        np.subtract(x[a:b], centre, out=v)
        np.multiply(v, v, out=v)
        np.add.reduce(v, axis=1, out=o)
        np.sqrt(o, out=o)


def _farthest_first_par(x, k: int, pool, workers: int) -> list[int]:
    """The farthest-first centres of `balanced_groups`: each worker keeps the running minimum distance of its rows and reports its
    first maximum; the earliest worker holding the overall first maximum (or first NaN) wins, as `np.argmax` over all rows would."""
    n = len(x)
    per = -(-n // workers)
    spans = [(lo, min(n, lo + per)) for lo in range(0, n, per)]
    d = np.empty(n, dtype=np.float64)
    local = threading.local()

    def run(span, nxt):
        lo, hi = span
        buf = getattr(local, "buf", None)
        if buf is None:
            buf = local.buf = (np.empty((_FF_ROWS, x.shape[1]), dtype=np.float64), np.empty(_FF_ROWS, dtype=np.float64))
        v, t = buf
        if nxt is None:
            _norms_into(x, lo, hi, x[0], v, d[lo:hi])
        else:
            for a in range(lo, hi, _FF_ROWS):
                b = min(hi, a + _FF_ROWS)
                _norms_into(x, a, b, x[nxt], v, t[:b - a])
                np.minimum(d[a:b], t[:b - a], out=d[a:b])
        j = int(np.argmax(d[lo:hi]))
        return lo + j, d[lo + j]
    centres = [0]
    best = list(pool.map(lambda s: run(s, None), spans))
    for _ in range(k - 1):
        nxt = best[int(np.argmax(np.array([v for _, v in best])))][0]
        centres.append(nxt)
        best = list(pool.map(lambda s: run(s, nxt), spans))
    return centres


def _assign_par(dist, quota: list[int], pool, workers: int) -> list[list[int]]:
    """The greedy of `balanced_groups` over pairs in global stable (value, flat position) order, produced bucket by bucket."""
    n, k = dist.shape
    flat = dist.reshape(-1)
    stride = max(1, flat.size // _SAMPLE)
    sample = np.sort(flat[::stride])
    groups: list[list[int]] = [[] for _ in range(k)]
    left, done, placed = list(quota), np.zeros(n, dtype=bool), 0
    rows = [(lo, min(n, lo + max(1, (1 << 22) // k))) for lo in range(0, n, max(1, (1 << 22) // k))]
    lo_v, target = None, _FIRST_BUCKET * n
    while placed < n:
        rank = target // stride
        hi_v = sample[rank] if rank < len(sample) and not np.isnan(sample[rank]) else None
        target *= 2
        if hi_v is not None and lo_v is not None and not hi_v > lo_v:
            continue
        open_rows, open_cols = ~done, np.array([v > 0 for v in left])

        def select(span, lo_v=lo_v, hi_v=hi_v, open_rows=open_rows, open_cols=open_cols):
            a, b = span
            blk = dist[a:b]
            m = open_rows[a:b, None] & open_cols[None, :]
            if hi_v is not None:
                m &= blk <= hi_v
            if lo_v is not None:
                m &= ~(blk <= lo_v)
            return np.flatnonzero(m) + a * k
        pos = np.concatenate(list(pool.map(select, rows)))
        pos = pos[np.argsort(flat[pos], kind="stable")]
        for p in pos.tolist():
            i, g = divmod(p, k)
            if not done[i] and left[g] > 0:
                groups[g].append(i)
                left[g] -= 1
                done[i] = True
                placed += 1
                if placed == n:
                    break
        if hi_v is None and placed < n:
            raise AssertionError("the last bucket left rows unplaced")
        lo_v = hi_v
    return groups


def _balanced_par(x, k: int, quota: list[int], rounds: int, pool, workers: int) -> list[list[int]]:
    c = x[_farthest_first_par(x, k, pool, workers)].copy()
    groups: list[list[int]] = [[] for _ in range(k)]
    for _ in range(rounds):
        dist = _sq_dist_par(x, c, pool, workers)
        groups = _assign_par(dist, quota, pool, workers)
        del dist
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
