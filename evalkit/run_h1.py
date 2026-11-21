"""The H1 runner: the development smoke on the stub today, and stage S2 (H1 arms only) once the owner approves it.

  python -m evalkit.run_h1 --stage DEV-STUB ...   no model call; stub model and stub embedder; a smoke of the data path on development
                                                  questions, never a stage result
  python -m evalkit.run_h1 --stage S2 ...         refuses to start until ALL of: the owner's approval of S2 with its call cap is passed in
                                                  (`--approved-cap`, `--approval-ref`), the pinned real embedder is in use (`--embed-dir`; the
                                                  stub's hashed bag of words would hand flat search a weakened baseline and bias H1 toward A1),
                                                  the commit is named, the run directory holds no earlier stamp (or `--resume` of the same stamp:
                                                  same commit, cap and question count) and no stop file, and the questions are development
                                                  (pilot) questions; the body of the stage is `run_s2`

Scope of the S2 runner: arms A0, A1, A1-tree-only and SHUFFLED-SUMMARIES on the pilot haystack pool, with the registered statistics
labelled EXPLORATORY (EXPERIMENT 7.4: development numbers decide nothing). A2, the relevance judge on three pools and C1/C2 of the S2
row are not part of this runner (docs/RUN-S2.md says what each would take). A0+LEN is reported only when
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

from living_context.embed import e5, stub as embed_stub

from . import arms, dataset as ds, parallel as par, stats
from .score import question_scores, write_result

STAGES = ("DEV-STUB", "S2", "S3")
REGISTERED_N = {"S2": 30, "S3": 102}      # pilot questions per stage (EXPERIMENT 10); S3 scores the non-abstention ones with gold
SHUFFLED_N = {"S3": 50}                    # S3 runs SHUFFLED-SUMMARIES on the first 50 questions of the seeded order
SEED = ds.SPLIT_SEED


def check_prereqs(a, *, embed=embed_stub.embed) -> List[str]:
    """`embed` is the embedder object the stage would use (anything but a pinned real embedder is refused in S2)."""
    """Reasons the run must not start. An empty list means every guard passed."""
    why: List[str] = []
    if a.stage not in STAGES:
        why.append("unknown stage")
    if a.stage == "DEV-STUB" and getattr(a, "model_kind", "") == "muse":
        why.append("--model-kind muse runs only in S2 or S3")
    if a.splits != ["pilot"]:
        why.append("only the pilot split may be run here; the held-out and fresh questions are never loaded")
    if a.stage in ("S2", "S3"):
        if not (a.approved_cap and a.approved_cap > 0):
            why.append("%s needs the owner's approval of the stage with its call cap (--approved-cap)" % a.stage)
        if not (a.approval_ref or "").strip():
            why.append("%s needs a reference to the owner's approval (--approval-ref)" % a.stage)
        try:
            e5.require_real(embed, stage=a.stage)
        except e5.PinError:
            why.append("the pinned real embedder (multilingual-e5-small, BUILD-PLAN 1.3) is required; the stub embedder weakens flat search and "
                       "biases H1")
        if not (a.commit or "").strip():
            why.append("%s needs the commit under test (--commit)" % a.stage)
        from . import model_kind as mk
        why += ["%s: %s" % (a.stage, w) for w in mk.check(a)]
        if a.n_questions != REGISTERED_N[a.stage]:
            why.append("%s is registered on %d pilot questions" % (a.stage, REGISTERED_N[a.stage]))
        if int(getattr(a, "shuffled_n", 0) or 0) != SHUFFLED_N.get(a.stage, 0):
            why.append("%s runs SHUFFLED-SUMMARIES on %s questions (--shuffled-n)" % (a.stage, SHUFFLED_N.get(a.stage, "all")))
        if getattr(a, "a0_fields", "strong") not in ("strong", "text"):
            why.append("--a0-fields must be strong or text")
    stamp = Path(a.run_dir) / "STAMP.json"
    if stamp.exists() and not a.resume:
        why.append("a stamped run already exists in the run directory (one stamped run per stage; --resume continues the same run)")
    if stamp.exists() and a.resume and a.stage in ("S2", "S3"):
        try:
            old = json.loads(stamp.read_text())
        except ValueError:
            old = {}
        rd = Path(a.run_dir)
        charged = any((rd / f).exists() for f in ("counter", "calls.jsonl", "journal.jsonl"))
        # a recorded amendment may move a charged run to a new commit only between its build and its first question: the tree is
        # complete (BUILT.json) and no question is in the ledger, so every recorded result comes from one commit per phase
        done = rd / "done.jsonl"
        amended = bool((getattr(a, "amendment_ref", "") or "").strip())
        if amended and old.get("commit") != a.commit:
            if not (rd / "BUILT.json").exists():
                why.append("--amendment-ref moves a run to a new commit only after its build is complete (BUILT.json)")
            if done.exists() and done.read_text().strip():
                why.append("--amendment-ref moves a run to a new commit only before its first question is recorded")
        for k, now in (("commit", a.commit), ("cap", a.approved_cap), ("n_questions", a.n_questions), ("a0_fields", getattr(a, "a0_fields", "strong")),
                         ("shuffled_n", int(getattr(a, "shuffled_n", 0) or 0))):
            if k == "commit" and (not charged or amended):
                continue      # a run that never charged a call may continue under a corrected commit, and a built run with no question
                #               done may continue under an amendment; the stamp records the earlier commit (and the amendment)
            if old.get(k, 0 if k == "shuffled_n" else None) != now:
                why.append("--resume must continue the same run: %s differs from the stamp (a new cap needs a new approval and a new run)" % k)
        from . import model_kind as mk
        for k in mk.differs(old, a):
            why.append("--resume must continue the same run: %s differs from the stamp (a new model or setting is a new run)" % k)
    if a.stage in ("S2", "S3"):
        n_par = getattr(a, "concurrency", 1)
        why += par.check_concurrency(n_par, getattr(a, "concurrency_reason", ""))
        rd = Path(a.run_dir)
        if isinstance(n_par, int) and n_par > 1 and (rd / "journal.jsonl").exists() and not (rd / "BUILT.json").exists():
            why.append("an unfinished build journaled without scopes resumes at --concurrency 1 only (its keys are unscoped)")
    if a.stage in ("S2", "S3") and (Path(a.run_dir) / "STOP").exists():
        why.append("a stop file is present in the run directory; remove it to start or resume")
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
    ap.add_argument("--model-kind", choices=["stub", "claude", "muse"], default="stub")
    ap.add_argument("--binary", default="")
    ap.add_argument("--model", default="")
    ap.add_argument("--approved-cap", type=int, default=0)
    ap.add_argument("--approval-ref", default="")
    ap.add_argument("--commit", default="")
    ap.add_argument("--beta", type=float, default=None)
    ap.add_argument("--resume", action="store_true")
    ap.add_argument("--embed-dir", default="", help="directory of the pinned multilingual-e5-small files (S2; checksums are verified)")
    ap.add_argument("--threads", type=int, default=6)
    ap.add_argument("--a0-fields", default="strong", choices=["strong", "text"],
                    help="BM25 fields of A0 and of A1's direct list: strong = text,title,entities (default, the stronger baseline); text = text only")
    ap.add_argument("--shuffled-n", type=int, default=0, help="run SHUFFLED-SUMMARIES on the first N questions only (0 = all; S3 requires 50)")
    ap.add_argument("--max-retries", type=int, default=3, help="throttle retries per call before the stage pauses")
    ap.add_argument("--base-delay", type=float, default=30.0, help="first throttle back-off in seconds (doubles, capped at 600)")
    ap.add_argument("--concurrency", type=int, default=par.REGISTERED_CONCURRENCY,
                    help="model calls at once (registered %d); questions run concurrently, outputs do not depend on it" % par.REGISTERED_CONCURRENCY)
    ap.add_argument("--concurrency-reason", default="", help="required above the registered value; recorded in starts.jsonl")
    ap.add_argument("--fixed-concurrency", action="store_true",
                    help="keep --concurrency fixed (default above 1: start at 2, halve on a throttle or allowance refusal, double after "
                         "a clean window, never above --concurrency)")
    from . import model_kind as mk
    mk.add_arguments(ap, default_kind="stub")
    ap.add_argument("--amendment-ref", default="", help="a recorded deviation amendment that lets a built run with no question done "
                    "resume under a new commit")
    a = ap.parse_args(argv)
    embedder = None
    if a.stage in ("S2", "S3") and a.embed_dir:
        try:
            embedder = e5.E5Embedder(a.embed_dir, threads=a.threads)
        except e5.PinError as e:
            print("REFUSED:\n  the embedder files are not the pinned ones: %s" % e)
            return 2
    why = check_prereqs(a, **({"embed": embedder} if a.stage in ("S2", "S3") else {}))
    if why:
        print("REFUSED:\n  " + "\n  ".join(why))
        return 2
    if a.stage in ("S2", "S3"):
        from . import run_s2
        return run_s2.run(a, pick_questions, embedder=embedder)
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
