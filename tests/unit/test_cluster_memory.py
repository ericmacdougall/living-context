"""Balanced clustering must not build the n x k x d difference tensor (63 GB at 18,120 leaves, d 384, k 1,133), and must return exactly
what the one-shot version returned. `reference_groups` is that earlier version, kept verbatim as the oracle and as the BROKEN VARIANT of
the memory test. Pure; numpy only."""
import tracemalloc
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest import mock

import numpy as np

from living_context.structure import cluster


def reference_groups(vectors, f_target, rounds=3):
    """The one-shot implementation (n x k x d float64 temporary, all sorted pairs as Python lists)."""
    x = np.asarray(vectors, dtype=np.float64)
    n = len(x)
    k = cluster.group_count(n, f_target)
    if k == 1:
        return [list(range(n))]
    quota = cluster._sizes(n, k)
    centres = [0]
    d = np.linalg.norm(x - x[0], axis=1)
    for _ in range(k - 1):
        nxt = int(np.argmax(d))
        centres.append(nxt)
        d = np.minimum(d, np.linalg.norm(x - x[nxt], axis=1))
    c = x[centres].copy()
    groups = [[] for _ in range(k)]
    for _ in range(rounds):
        dist = ((x[:, None, :] - c[None, :, :]) ** 2).sum(axis=2)
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


def vectors(n, d=384, seed=1, dup=0):
    rng = np.random.default_rng(seed)
    x = rng.standard_normal((n, d)).astype(np.float32)
    x /= np.linalg.norm(x, axis=1, keepdims=True)
    if dup:                                   # exact duplicates force ties in the sorted distances
        x[dup:2 * dup] = x[:dup]
    return x.astype(np.float64).tolist()


class SameGroups(unittest.TestCase):
    def test_identical_to_the_one_shot_version(self):
        for n, seed, dup in ((40, 1, 0), (257, 2, 0), (700, 3, 0), (700, 4, 60), (1001, 5, 100)):
            x = vectors(n, seed=seed, dup=dup)
            self.assertEqual(cluster.balanced_groups(x, 16), reference_groups(x, 16), (n, seed, dup))

    def test_identical_across_block_and_chunk_boundaries(self):
        x = vectors(500, seed=9, dup=40)
        with mock.patch.object(cluster, "_BLOCK_ELEMS", 384 * 32 * 7 + 5), mock.patch.object(cluster, "_PAIR_CHUNK", 997):
            got = cluster.balanced_groups(x, 16)
        self.assertEqual(got, reference_groups(x, 16))

    def test_identical_distances(self):
        x = np.array(vectors(300, seed=3))
        c = x[::10].copy()
        with mock.patch.object(cluster, "_BLOCK_ELEMS", 384 * 30 * 3):
            got = cluster._sq_dist(x, c)
        self.assertTrue(np.array_equal(got, ((x[:, None, :] - c[None, :, :]) ** 2).sum(axis=2)))


def lattice(n, d=24, seed=1):
    """Vectors on a coarse integer lattice: many exactly equal distances, so every tie-break is exercised."""
    rng = np.random.default_rng(seed)
    return rng.integers(-1, 2, size=(n, d)).astype(np.float64).tolist()


def greedy(dist, quota):
    """The assignment loop of the one-shot version over a given distance matrix (the oracle for `_assign_par`)."""
    n, k = dist.shape
    groups, left, done, placed = [[] for _ in range(k)], list(quota), np.zeros(n, dtype=bool), 0
    for pos in np.argsort(dist, axis=None, kind="stable").tolist():
        i, g = divmod(pos, k)
        if not done[i] and left[g] > 0:
            groups[g].append(i)
            left[g] -= 1
            done[i] = True
            placed += 1
            if placed == n:
                break
    return groups


SMALL = dict(_BLOCK_ELEMS=24 * 9 * 5 + 3, _FF_ROWS=7, _SAMPLE=97, _FIRST_BUCKET=1)   # many blocks, spans and value buckets


class ParallelSame(unittest.TestCase):
    """Several workers return exactly what one worker returns (and so what the one-shot version returned)."""

    def small(self):
        return mock.patch.multiple(cluster, **SMALL)

    def test_same_groups_as_one_worker(self):
        cases = [(vectors(40, seed=1), 16), (vectors(257, seed=2), 16), (vectors(700, seed=4, dup=60), 16),
                 (vectors(1001, seed=5, dup=100), 16), (lattice(600, seed=6), 8), (lattice(333, seed=7), 5)]
        for x, f in cases:
            want = cluster.balanced_groups(x, f)
            for w in (2, 5):
                self.assertEqual(cluster.balanced_groups(x, f, workers=w), want, (len(x), f, w))
                with self.small():
                    self.assertEqual(cluster.balanced_groups(x, f, workers=w), want, (len(x), f, w, "small"))

    def test_same_distances_bit_for_bit(self):
        x = np.array(vectors(300, seed=3))
        c = x[::10].copy()
        with ThreadPoolExecutor(3) as pool, mock.patch.object(cluster, "_BLOCK_ELEMS", 384 * 30 * 3 + 1):
            got = cluster._sq_dist_par(x, c, pool, 3)
            want = cluster._sq_dist(x, c)
        self.assertEqual(got.tobytes(), want.tobytes())

    def test_same_assignment_under_ties_and_nan(self):
        rng = np.random.default_rng(12)
        for n, k in ((50, 7), (200, 13), (97, 97)):
            dist = rng.integers(0, 4, size=(n, k)).astype(np.float64)       # four distinct values: ties everywhere
            dist[3, :2] = np.nan
            quota = cluster._sizes(n, k)
            with ThreadPoolExecutor(3) as pool, self.small():
                self.assertEqual(cluster._assign_par(dist, quota, pool, 3), greedy(dist, quota), (n, k))

    def test_same_centres_and_norms(self):
        for x, k in ((np.array(lattice(400, d=6, seed=8)), 30), (np.array(vectors(500, seed=13, dup=50)), 40)):
            centres = [0]
            d = np.linalg.norm(x - x[0], axis=1)
            for _ in range(k - 1):
                nxt = int(np.argmax(d))
                centres.append(nxt)
                d = np.minimum(d, np.linalg.norm(x - x[nxt], axis=1))
            for w in (1, 3, 4):
                with ThreadPoolExecutor(w) as pool, self.small():
                    self.assertEqual(cluster._farthest_first_par(x, k, pool, w), centres, (len(x), w))
        x = np.array(vectors(300, seed=14))
        out = np.empty(300)
        cluster._norms_into(x, 0, 300, x[7], np.empty((11, 384)), out)
        self.assertEqual(out.tobytes(), np.linalg.norm(x - x[7], axis=1).tobytes())


class Memory(unittest.TestCase):
    n, d, f = 1200, 384, 16

    def peak(self, fn):
        x = vectors(self.n, self.d, seed=11)
        tracemalloc.start()
        tracemalloc.reset_peak()
        try:
            fn(x, self.f)
            return tracemalloc.get_traced_memory()[1]
        finally:
            tracemalloc.stop()

    def test_peak_is_far_below_the_difference_tensor(self):
        tensor = self.n * (-(-self.n // self.f)) * self.d * 8          # n x k x d float64
        self.assertLess(self.peak(cluster.balanced_groups), tensor // 5)

    def test_parallel_peak_is_far_below_the_difference_tensor(self):
        tensor = self.n * (-(-self.n // self.f)) * self.d * 8
        bufs = 4 * cluster._BLOCK_ELEMS * 8                              # one reused 16 MB block buffer per worker, whatever n is
        self.assertLess(self.peak(lambda x, f: cluster.balanced_groups(x, f, workers=4)) - bufs, tensor // 5)

    def test_broken_variant_is_caught(self):
        tensor = self.n * (-(-self.n // self.f)) * self.d * 8
        self.assertGreater(self.peak(reference_groups), tensor // 5)


if __name__ == "__main__":
    unittest.main()
