"""The H1 runner: the development smoke on the stub today, and stage S2 (H1 arms only) once the owner approves it.

  python -m evalkit.run_h1 --stage DEV-STUB ...   no model call; stub model and stub embedder; a smoke of the data path on development
                                                  questions, never a stage result
  python -m evalkit.run_h1 --stage S2 ...         refuses to start until ALL of: the owner's approval of S2 with its call cap is passed in
                                                  (`--approved-cap`, `--approval-ref`), a real embedding port exists (it does not yet: the
                                                  stub's hashed bag of words would hand flat search a weakened baseline and bias H1 toward A1),
                                                  the commit is named, the run directory holds no earlier stamp, and the questions are
                                                  development (pilot) questions

Scope of the S2 runner: arms A0, A1, A1-tree-only and SHUFFLED-SUMMARIES on the pilot haystack pool, with the registered statistics
labelled EXPLORATORY (EXPERIMENT 7.4: development numbers decide nothing). A2, the relevance judge on three pools and C1/C2 of the S2
row are not part of this runner (A2 and the judge belong to the other build stream; C1 and C2 are Step 21). A0+LEN is reported only when
a beta fitted on development is passed (S3 fits it and freezes it).

All model calls go through `claude_cli.CappedPort` over one durable counter whose cap is the approved cap. The held-out and fresh
questions are never loaded for a run: `dataset.development_questions` refuses the test split."""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
import tempfile
import time
from pathlib import Path
from typing import List, Optional

from living_context.embed import stub as embed_stub

from . import arms, dataset as ds, stats
from .score import question_scores, write_result

STAGES = ("DEV-STUB", "S2")
SEED = ds.SPLIT_SEED


def check_prereqs(a, *, embed=embed_stub.embed) -> List[str]:
    """Reasons the run must not start. An empty list means every guard passed."""
    why: List[str] = []
    if a.stage not in STAGES:
        why.append("unknown stage")
    if a.splits != ["pilot"]:
        why.append("only the pilot split may be run here; the held-out and fresh questions are never loaded")
    if a.stage == "S2":
        if not (a.approved_cap and a.approved_cap > 0):
            why.append("S2 needs the owner's approval of the stage with its call cap (--approved-cap)")
        if not (a.approval_ref or "").strip():
            why.append("S2 needs a reference to the owner's approval (--approval-ref)")
        if embed is embed_stub.embed:
            why.append("PREREQUISITE MISSING: the real embedding port (multilingual-e5-small at the pinned revision, BUILD-PLAN 1.3) is not "
                       "built; the stub embedder weakens flat search and biases H1")
        if not (a.commit or "").strip():
            why.append("S2 needs the commit under test (--commit)")
        if a.model_kind != "claude":
            why.append("S2 uses the real model port (--model-kind claude)")
        if a.n_questions > 30:
            why.append("S2 is registered on 30 pilot questions")
    if (Path(a.run_dir) / "STAMP.json").exists() and not a.resume:
        why.append("a stamped run already exists in the run directory (one stamped run per stage; --resume continues the same run)")
    return why


def pick_questions(qs: List[ds.Question], n: int) -> List[ds.Question]:
    """The first n non-abstention pilot questions with gold, in the order of sha256(seed:qid)."""
    cand = [q for q in qs if not q.is_abstention and q.gold_session_ids]
    cand.sort(key=lambda q: hashlib.sha256(("%d:%s" % (SEED, q.qid)).encode()).hexdigest())
    return cand[:n]


class Recorder:
    """Wraps a retriever and keeps its last output (ids only) so the source of each gold session can be attributed."""
    def __init__(self, fn):
        self.fn, self.last = fn, None

    def __call__(self, q):
        self.last = self.fn(q)
        return self.last


def main(argv: Optional[list] = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--stage", required=True)
    ap.add_argument("--dataset", required=True)
    ap.add_argument("--es-url", required=True)
    ap.add_argument("--es-ca", required=True)
    ap.add_argument("--es-pw-file", required=True)
    ap.add_argument("--tenant", default="h1run")
    ap.add_argument("--run-dir", required=True)
    ap.add_argument("--n-questions", type=int, default=3)
    ap.add_argument("--splits", nargs="+", default=["pilot"])
    ap.add_argument("--model-kind", choices=["stub", "claude"], default="stub")
    ap.add_argument("--binary", default="")
    ap.add_argument("--model", default="")
    ap.add_argument("--approved-cap", type=int, default=0)
    ap.add_argument("--approval-ref", default="")
    ap.add_argument("--commit", default="")
    ap.add_argument("--beta", type=float, default=None)
    ap.add_argument("--resume", action="store_true")
    a = ap.parse_args(argv)
    why = check_prereqs(a)
    if why:
        print("REFUSED:\n  " + "\n  ".join(why))
        return 2
    run_dir = Path(a.run_dir)
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "STAMP.json").write_text(json.dumps({"stage": a.stage, "commit": a.commit, "started": time.time()}))

    from living_context.config import Constants
    from living_context.model.budget import Budget
    from living_context.model.cache import DictStore
    from living_context.model.port import ModelPort
    from living_context.model.stub import StubBackend
    from living_context.policy import ClassLattice
    from . import claude_cli
    from .eval_tenant import CLS, EvalTenant

    qs = ds.load_questions(a.dataset)
    dev = pick_questions(ds.development_questions(qs, tuple(a.splits)), a.n_questions)
    hashes = ds.session_hashes(qs)
    sessions = {}
    for q in dev:
        for sid, turns in q.sessions:
            sessions.setdefault(hashes[sid], turns)
    consts = Constants()
    et = EvalTenant(a.es_url, a.es_ca, a.es_pw_file, a.tenant, run_dir / "tenant", consts=consts)
    t0 = time.time()
    leaves = et.ingest(list(sessions.items()))
    print("ingested %d sessions as %d leaves in %.0fs; es leaf count %d" % (len(sessions), leaves, time.time() - t0, et.leaf_count_in_es()))
    if a.model_kind == "claude":
        runner = claude_cli.CliRunner(a.binary, a.model, run_dir / "cwd")
        port = claude_cli.CappedPort(runner, Budget(run_dir / "counter", a.approved_cap), run_dir / "calls.jsonl", model_id=a.model,
                                     argv_record=runner.argv())
    else:
        port = ModelPort(StubBackend(), Budget(run_dir / "counter", 10 ** 9), ClassLattice([CLS], []), DictStore())
    tree = et.build_and_publish(port)
    print("tree: %d nodes, %d levels, top %d" % (len(tree.nodes), tree.levels, len(tree.top)))
    view = et.view()
    world = et.world
    direct = Recorder(et.direct())
    tr = Recorder(arms.TreeRetriever(port, view, a.tenant, CLS, "px", consts))
    sh = arms.TreeRetriever(port, arms.PermutedView(view), a.tenant, CLS, "px", consts)
    outs = {x: {} for x in ("A0", "A1", "A1-tree-only", "SHUFFLED")}
    gold, rows, tree_only_k, tree_only_n = {}, [], 0, 0
    for q in dev:
        g = sorted({hashes[s] for s in q.gold_session_ids})
        gold[q.qid] = g
        o = arms.run_question(q.question, direct, tr, world, beta=0.0, shuffled_tree=sh)
        for k in outs:
            outs[k][q.qid] = o[k]
        src = arms.source_of_gold(o["A1"], tr.last.ids, [i for i, _ in direct.last], g, world) if o["A1"].sessions else {}
        if src:
            tree_only_n += 1
            tree_only_k += "tree_only" in src.values()
        rows.append({"qid": q.qid, "gold_n": len(g), "sources": sorted(src.values())})
    sc = {k: question_scores(v, gold) for k, v in outs.items()}
    mean = lambda d: sum(d.values()) / len(d) if d else float("nan")
    print("EXPLORATORY (development; decides nothing):")
    for k, (s, failed) in sc.items():
        print("  %-13s recall@10 %.3f over %d questions, failed recalls %d" % (k, mean(s), len(s), failed))
    clusters = ds.content_clusters(qs, hashes)
    if len(dev) >= 2:
        iv = stats.clustered_bootstrap(stats.paired_differences(sc["A1"][0], sc["A0"][0]), clusters, resamples=2000)
        drop = stats.clustered_bootstrap(stats.paired_differences(sc["A1-tree-only"][0], sc["SHUFFLED"][0]), clusters, resamples=2000)
        print("  A1 - A0: %.3f [%.3f, %.3f] (n %d, clusters %d); tree-only minus SHUFFLED: %.3f [%.3f, %.3f]" % (
            iv.point, iv.lo, iv.hi, iv.n_questions, iv.n_clusters, drop.point, drop.lo, drop.hi))
    row = {"commit": a.commit or "dev", "stage": a.stage, "arm": "A0,A1,A1-tree-only,SHUFFLED", "pool": "pilot haystacks (%d questions)" % len(dev),
           "n": len(dev), "metric": "gold-session recall@10 (EXPLORATORY)", "interval_method": "clustered paired bootstrap, seed %d" % SEED,
           "seed": SEED, "call_counter": Budget(run_dir / "counter", max(a.approved_cap, 1)).count(), "product_cache_hits": 0,
           "routing": "agent-routed"}
    write_result(str(run_dir / "result.json"), row, rows)
    et.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
