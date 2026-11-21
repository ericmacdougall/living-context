"""Peak memory of the S6-H1 whole-pool tree build, measured on a SYNTHETIC pool of the expected size (no dataset content, no model call).

The whole pool (EXPERIMENT 3.3) is 18,464 distinct session contents. Its leaf count is not known before the fresh set is released, so it
is estimated from counts only: S3's dev static pool segmented 4,600 sessions into 19,605 leaves (4.262 per session), and the registered
token figures give the whole pool the same tokens per session as the dev static pool (47.04M chars/4 over 18,464 against 11.8M over
4,600), so about 78,700 leaves of about 2,390 characters each. This tool builds the real tree (`build.build_tree`, the clustering of
`cluster.balanced_groups` included) with the stub model over N synthetic leaves shaped like that pool (384-dimensional unit vectors around
topic centres, held as Python lists as ingest holds them; text of the same mean length) and reports the peak resident memory and the wall
time of each phase. Its JSON output is the run's memory proof (`run_s6h1 --mem-proof`): it records the digest of the code that was
measured, and the run refuses to start unless that digest is the code under test, the proven leaf count covers the expected pool and the
host has the measured peak free; after ingest it refuses to build a pool with more leaves than were proven.

  python -I -m evalkit.s6h1_mem --leaves 85000 [--dim 384] [--chars 2390] [--json OUT]

Run it on the run host, not a laptop; it needs about 11 GB at 85,000 leaves."""
from __future__ import annotations

import argparse
import json
import random
import resource
import sys
import tempfile
import time
from pathlib import Path

import numpy as np

EXPECTED_LEAVES = 78_700          # 18,464 sessions x 4.262 leaves per session (counts only; see the module text)
LEAF_CHARS = 2_390                # 47.04M chars/4 tokens x 4 / 78,700 leaves


def peak_mb() -> float:
    r = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return r / 1024.0 if sys.platform.startswith("linux") else r / (1024.0 * 1024.0)


def current_mb() -> float:
    try:
        for line in Path("/proc/self/status").read_text().splitlines():
            if line.startswith("VmRSS:"):
                return int(line.split()[1]) / 1024.0
    except OSError:
        pass
    return float("nan")


def synthetic_leaves(n: int, dim: int, chars: int, seed: int = 16096, topics: int = 2_000):
    from living_context.structure.build import LeafRec
    rng = np.random.default_rng(seed)
    centres = rng.standard_normal((topics, dim))
    vocab = ["w%04d" % i for i in range(5_000)]
    pr = random.Random(seed)
    out = []
    for lo in range(0, n, 5_000):
        m = min(5_000, n - lo)
        v = centres[rng.integers(0, topics, m)] + 0.6 * rng.standard_normal((m, dim))
        v /= np.linalg.norm(v, axis=1, keepdims=True)
        for j in range(m):
            words, size = [], 0
            while size < chars:
                w = pr.choice(vocab)
                words.append(w)
                size += len(w) + 1
            text = " ".join(words)[:chars]
            out.append(LeafRec("%032x" % (lo + j + 1), 1, text, " ".join(words[:4]), v[j].tolist(), chars // 4, (), lo + j + 1))
    return out


def measure(n: int, dim: int = 384, chars: int = LEAF_CHARS, cluster_workers: int = 1) -> dict:
    from living_context.config import Constants
    from living_context.model.budget import Budget
    from living_context.model.cache import DictStore
    from living_context.model.port import ModelPort
    from living_context.model.stub import StubBackend
    from living_context.policy import ClassLattice
    from living_context.structure import build, cluster

    rep = {"leaves": n, "dim": dim, "leaf_chars": chars, "cluster_workers": cluster_workers, "rss_start_mb": current_mb()}
    t0 = time.time()
    leaves = synthetic_leaves(n, dim, chars)
    rep["make_s"], rep["rss_after_leaves_mb"] = time.time() - t0, current_mb()
    c = Constants()
    timing = {}
    real = cluster.balanced_groups

    def timed(vectors, f_target):
        t = time.time()
        g = real(vectors, f_target, workers=cluster_workers)
        timing.setdefault("cluster_s", []).append(round(time.time() - t, 1))
        timing.setdefault("cluster_n", []).append(len(vectors))
        return g
    with tempfile.TemporaryDirectory() as d:
        port = ModelPort(StubBackend(), Budget(Path(d) / "counter", 10 ** 9), ClassLattice(["base"], []), DictStore())
        t1 = time.time()
        tree = build.build_tree(leaves, port, "t", "base", consts=c, grouper=timed)
        rep["build_s"] = time.time() - t1
    rep.update(timing)
    rep["nodes"], rep["levels"], rep["top"], rep["build_calls"] = len(tree.nodes), tree.levels, len(tree.top), tree.calls
    rep["level1_groups"] = sum(1 for x in tree.nodes.values() if x.level == 1)
    rep["peak_rss_mb"] = peak_mb()
    from . import s6h1_freeze
    rep["code_sha256"], rep["tool"] = s6h1_freeze.code_digest(), "evalkit.s6h1_mem"
    return rep


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--leaves", type=int, default=EXPECTED_LEAVES)
    ap.add_argument("--dim", type=int, default=384)
    ap.add_argument("--chars", type=int, default=LEAF_CHARS)
    ap.add_argument("--json", default="")
    ap.add_argument("--cluster-workers", type=int, default=1, help="clusterer threads, as the run will use them")
    a = ap.parse_args(argv)
    rep = measure(a.leaves, a.dim, a.chars, a.cluster_workers)
    print(json.dumps(rep, sort_keys=True), flush=True)
    if a.json:
        Path(a.json).write_text(json.dumps(rep, indent=1, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
