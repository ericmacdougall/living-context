"""Stage S2 for H1: A0, A1, A1-tree-only and SHUFFLED-SUMMARIES on the 30 pilot questions' own haystacks (EXPERIMENT 10).

Started only through `python -m evalkit.run_h1 --stage S2 ...` (whose guards must all pass: the owner's approval reference and call cap,
the real model port, the pinned real embedder, the commit under test, the pilot split only, a clean or resumable run directory).
This module is the body of that stage.

Run control (all in `s2_ports`): every call outcome is journaled and a resumed run never repeats a charged call; a stop file ends the run
cleanly before the next charge; throttle responses back off and never retry past the cap; the cap is the one approved at start and is
the durable counter's hard limit. Exit codes: 0 complete, 3 stopped by the stop file, 4 call cap reached, 5 a build call failed after its
retries, 75 paused by throttling (all of 3, 4, 75 are resumable, 4 only after a new approval of a higher cap, which needs a new run).

Resuming re-ingests the pool and rebuilds the tree from the journal (no model call: every build call is answered from it), skips the
questions already in the ledger, and continues. Recalls run with one worker so that the call order is deterministic.

Not covered here, on purpose (see docs/RUN-S2.md): A2 and edge typing, the relevance judge on three pools, C1 and C2, the holder-log
check that SHUFFLED fails for its stated reason."""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from typing import Optional

from living_context.config import Constants
from living_context.embed import e5
from living_context.model.budget import Budget, BudgetExceeded
from living_context.model.port import PortRefused

from . import arms, claude_cli, dataset as ds, s2_stage
from .s2_ports import Journal, ResilientPort, StageLimited, StagePaused, StageStopped

RC = {"COMPLETE": 0, "STOPPED": 3, "CAP_REACHED": 4, "BUILD_FAILED": 5, "PAUSED_THROTTLE": 75, "LIMIT_REACHED": 76, "ERROR": 1}


def write_state(run_dir: Path, state: str, **extra) -> int:
    (run_dir / "STATE.json").write_text(json.dumps(dict(state=state, at=time.time(), **extra), indent=1, sort_keys=True))
    return RC[state]


def run(a, pick_questions, *, embedder=None, runner=None, loader=None) -> int:
    """`loader(a)` returns (dev questions, content hash per session id, clusters); the default reads the registered dataset (tests pass a
    synthetic loader)."""
    from living_context.structure.build import BuildFailed
    from .eval_tenant import CLS, EvalTenant

    run_dir = Path(a.run_dir)
    run_dir.mkdir(parents=True, exist_ok=True)
    stamp = run_dir / "STAMP.json"
    if not stamp.exists():
        stamp.write_text(json.dumps({"stage": a.stage, "commit": a.commit, "cap": a.approved_cap, "approval_ref": a.approval_ref,
                                     "n_questions": a.n_questions, "a0_fields": a.a0_fields, "shuffled_n": int(getattr(a, "shuffled_n", 0) or 0), "embedder": e5.MODEL_ID,
                                     "started": time.time()}, sort_keys=True))
    if loader:
        dev, hashes, clusters = loader(a)
    else:
        qs = ds.load_questions(a.dataset)
        dev = pick_questions(ds.development_questions(qs, tuple(a.splits)), a.n_questions)
        hashes = ds.session_hashes(qs)
        clusters = ds.content_clusters(qs, hashes)
    sessions = {}
    for q in dev:
        for sid, turns in q.sessions:
            sessions.setdefault(hashes[sid], turns)

    emb = embedder or e5.E5Embedder(a.embed_dir, threads=a.threads)
    e5.require_real(emb, stage=a.stage)
    pport, qport = e5.ports(emb, limit=2)
    consts = Constants()
    from .flat import A0_FIELDS_STRONG, A0_FIELDS_TEXT
    fields = A0_FIELDS_TEXT if a.a0_fields == "text" else A0_FIELDS_STRONG
    built = run_dir / "BUILT.json"
    resumed_built = built.exists()
    et = EvalTenant(a.es_url, a.es_ca, a.es_pw_file, a.tenant, run_dir / "tenant", consts=consts, embed=pport.for_tenant(a.tenant),
                    query_embed=qport.for_tenant(a.tenant), a0_fields=fields, attach=resumed_built)
    t0 = time.time()
    if resumed_built:
        # the tenant, its leaves and the published tree are intact (ids included): attach, do not ingest or build again
        n_leaves = et.load_state(run_dir / "tenant_state.json")
        if et.leaf_count_in_es() != n_leaves:
            print("REFUSED: the tenant no longer holds the leaves of the earlier run; start a new run", flush=True)
            return 2
        print("attached to the built tenant: %d leaves" % n_leaves, flush=True)
    else:
        n_leaves = et.ingest(list(sessions.items()))
        et.save_state(run_dir / "tenant_state.json")
        print("ingested %d sessions as %d leaves in %.0fs; es leaf count %d" % (len(sessions), n_leaves, time.time() - t0,
                                                                              et.leaf_count_in_es()), flush=True)

    runner = runner or claude_cli.CliRunner(a.binary, a.model, run_dir / "cwd")
    budget = Budget(run_dir / "counter", a.approved_cap)
    inner = claude_cli.CappedPort(runner, budget, run_dir / "calls.jsonl", model_id=a.model, argv_record=getattr(runner, "argv", lambda: [])())
    journal = Journal(run_dir / "journal.jsonl")
    port = ResilientPort(inner, budget, journal, run_dir / "STOP", max_retries=a.max_retries, base_delay_s=a.base_delay)
    ledger = s2_stage.Ledger(run_dir / "done.jsonl")
    state, extra = "COMPLETE", {}
    try:
        if not resumed_built:
            tree = et.build_and_publish(port)       # on a resumed unfinished build every call is answered from the journal
            print("tree: %d nodes, %d levels, top %d, build calls %d (replayed %d so far)" % (len(tree.nodes), tree.levels, len(tree.top),
                                                                                             tree.calls, port.replayed), flush=True)
            (run_dir / "tree.json").write_text(json.dumps({"nodes": len(tree.nodes), "levels": tree.levels, "top": len(tree.top),
                                                            "build_calls": tree.calls, "leaves": n_leaves}, indent=1))
            built.write_text(json.dumps({"at": time.time()}))
        view, world = et.view(), et.world
        direct = s2_stage.Recorder(et.direct())
        tr = s2_stage.Recorder(arms.TreeRetriever(port, view, a.tenant, CLS, "px", consts, workers=1))
        sh = s2_stage.Recorder(arms.TreeRetriever(port, arms.PermutedView(view), a.tenant, CLS, "px", consts, workers=1))
        beta = a.beta
        if a.stage == "S3":
            # S3 fits the length-prior beta once, on the development questions' A0 lists (no model call), and freezes it in the run directory
            bp = run_dir / "beta.json"
            if bp.exists():
                beta = json.loads(bp.read_text())["beta"]
            else:
                dfn = et.direct()
                rows = [(list(dfn(q.question))[:arms.WINDOW], sorted({hashes[s] for s in q.gold_session_ids}))
                        for q in dev if q.gold_session_ids and not q.is_abstention]
                beta = arms.fit_beta(rows, world)
                bp.write_text(json.dumps({"beta": beta, "n_questions": len(rows), "grid": "evalkit.arms.fit_beta default"}))
            print("A0+LEN beta (frozen):", beta, flush=True)
        s2_stage.run_questions(dev, hashes, direct, tr, sh, world, ledger, beta=beta, shuffled_n=int(getattr(a, "shuffled_n", 0) or 0), stop=port.stop_requested,
                               progress=lambda s: print(s, "counter", budget.count(), flush=True))
    except StageStopped:
        state = "STOPPED"
    except (BudgetExceeded, s2_stage.CapReached):
        state = "CAP_REACHED"
    except StagePaused:
        state = "PAUSED_THROTTLE"
    except StageLimited as e:                     # allowance exhausted or the CLI keeps failing: stop at once, no retry, no back-off
        state, extra = "LIMIT_REACHED", {"error": str(e)}
    except (BuildFailed, PortRefused) as e:       # a refused prompt (a gold-style id or marker in it) stops the stage before any charge
        state, extra = "BUILD_FAILED", {"error": "%s: %s" % (type(e).__name__, e)}
    except Exception as e:                        # leave a state file for the operator, then show the traceback
        write_state(run_dir, "ERROR", error=type(e).__name__, counter=budget.count())
        et.close()
        raise
    extra.update(counter=budget.count(), cap=a.approved_cap, questions_done=len(ledger.rows), questions=len(dev), replayed=port.replayed,
                 fresh=port.fresh, lost_in_flight=port.lost, throttled=port.throttled)
    if state == "COMPLETE":
        qids = [q.qid for q in dev if q.gold_session_ids and not q.is_abstention]
        out = run_dir / "results"
        means = s2_stage.write_arm_results(out, ledger, qids, commit=a.commit, stage=a.stage, pool="pilot haystacks (%d questions)" % len(dev),
                                           counter=budget.count(), cache_hits=0, a0_fields=fields, clusters=clusters)
        (out / "comparisons.json").write_text(json.dumps(s2_stage.comparisons(ledger, qids, clusters), indent=1, sort_keys=True))
        (out / "summaries.json").write_text(json.dumps(s2_stage.summaries(run_dir / "calls.jsonl", run_dir / "journal.jsonl"), indent=1,
                                                        sort_keys=True))
        extra["means"] = means
        print("EXPLORATORY (development; decides nothing):", json.dumps(means), flush=True)
    et.close()
    rc = write_state(run_dir, state, **extra)
    print("state", state, json.dumps(extra), flush=True)
    return rc
