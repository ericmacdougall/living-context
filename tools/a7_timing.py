"""A7 wall-clock timing harness (EXPERIMENT 9.2): two samples of request latencies, taken interleaved against two corpora, are
compared by the two-sample Kolmogorov-Smirnov statistic and the relative difference of the medians. The result is REPORTED and
never gates: a failure is a disclosed timing channel (expected cause: shared node load, DESIGN 20). The A4 pair (a forbidden and
a nonexistent pointer) goes through the same comparison.

    python -m tools.a7_timing   (prints the self-check: identical samples pass, a delayed corpus is flagged)

`measure` runs the two callables interleaved (a, b, a, b, ...) so slow drift of the node hits both samples alike.
"""
from __future__ import annotations

import bisect
import statistics
import time
from typing import Callable, Sequence

KS_LIMIT = 0.0515        # alpha 0.01 at 2,000 per sample (EXPERIMENT 9.2)
MEDIAN_LIMIT = 0.02      # relative


def ks_statistic(a: Sequence[float], b: Sequence[float]) -> float:
    """The two-sample KS statistic: the largest gap between the two empirical distribution functions."""
    if not a or not b:
        raise ValueError("both samples must be non-empty")
    sa, sb = sorted(a), sorted(b)
    d = 0.0
    for x in sorted(set(sa) | set(sb)):
        fa = bisect.bisect_right(sa, x) / len(sa)
        fb = bisect.bisect_right(sb, x) / len(sb)
        d = max(d, abs(fa - fb))
    return d


def median_difference(a: Sequence[float], b: Sequence[float]) -> float:
    ma, mb = statistics.median(a), statistics.median(b)
    return abs(ma - mb) / max(ma, mb) if max(ma, mb) > 0 else 0.0


def compare(a: Sequence[float], b: Sequence[float], *, ks_limit: float = KS_LIMIT, median_limit: float = MEDIAN_LIMIT) -> dict:
    ks, md = ks_statistic(a, b), median_difference(a, b)
    return {"n_a": len(a), "n_b": len(b), "ks": round(ks, 4), "median_diff": round(md, 4), "ks_limit": ks_limit,
            "median_limit": median_limit, "disclosed_timing_channel": bool(ks >= ks_limit or md >= median_limit)}


def measure(fa: Callable[[int], object], fb: Callable[[int], object], n: int, clock: Callable[[], float] = time.perf_counter) -> tuple:
    """`n` interleaved requests per side; each callable gets the request index. Returns (times of a, times of b)."""
    ta, tb = [], []
    for i in range(n):
        for f, out in ((fa, ta), (fb, tb)):
            t0 = clock()
            f(i)
            out.append(clock() - t0)
    return ta, tb


if __name__ == "__main__":
    import random
    rng = random.Random(1)
    same = [rng.gauss(10, 1) for _ in range(2000)], [rng.gauss(10, 1) for _ in range(2000)]
    slow = same[0], [x * 1.1 for x in same[1]]
    print("identical:", compare(*same))
    print("delayed corpus:", compare(*slow))
