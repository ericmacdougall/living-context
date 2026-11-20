"""Stage S6-H1: the confirmatory H1 stage on the fresh set over the whole pool (EXPERIMENT 10, row S6-H1). docs/RUN-S6H1.md is the
operator page. Nothing here runs without every guard of `check_prereqs`:

  * the owner's approval of S6-H1 with its cap (`--approval-ref`, `--approved-cap` at most 35,574) and the owner's release of the fresh
    set (`--release-ref`, and RELEASE.json in the run directory naming it and the freeze manifest's sha256);
  * the freeze (`s6h1_freeze.check`): the manifest exists, its code commit is `--commit`, the code under test has the frozen digest,
    every registered value recomputed from the code equals the frozen one, the model and A0 fields are the frozen ones, and S3's run
    directory is COMPLETE with the files the freeze recorded;
  * one stamped run: no stamp in the run directory (or `--resume` of the same stamp), and no S6-H1 row for ANOTHER run directory in the
    host's stamp registry (`--stamp-registry`, append-only), so a second run directory cannot start a second stamped S6-H1 run;
  * the real model port, the pinned real embedder, no stop file, and the call estimate's top within the cap (`s6h1_budget`).

The body (`run`): load the dataset, take the fresh set through `dataset.fresh_questions` (the release gate), build one tenant over the
WHOLE POOL (every question's haystack sessions, deduplicated by content hash: 18,464 registered contents), refuse before any call if
the leaf count exceeds the size whose peak memory was proven (`--mem-proof`, written by `s6h1_mem`) or if the call estimate's top at the
exact leaf count exceeds the cap, build the tree through the journaled, capped port, run the question loop (`s6h1_stage`), and on
completion write the per-arm results and the H1 decision. Run control, resume and exit codes are those of S2 (docs/RUN-S2.md), plus
exit 2 with STATE `REFUSED` for a refusal after the start (memory or budget)."""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Callable, List, Optional

from . import dataset as ds, s6h1_budget as bud, s6h1_freeze as fz, s6h1_stage as st

STAGE = "S6-H1"
TENANT = "s6h1"
REGISTERED_POOL = 18_464            # distinct session contents of all 500 questions (EXPERIMENT 3.3, MEASURED)
EXPECTED_LEAVES = 78_700            # 18,464 x 4.262 leaves per session (S3's dev static pool; counts only)
MEM_MARGIN_MB = 2_048               # free memory beyond the proven peak that the host must have at the start
RC = {"COMPLETE": 0, "STOPPED": 3, "CAP_REACHED": 4, "BUILD_FAILED": 5, "PAUSED_THROTTLE": 75, "LIMIT_REACHED": 76, "ERROR": 1,
      "REFUSED": 2}
STAMP_KEYS = ("commit", "cap", "approval_ref", "release_ref", "freeze_sha256", "a0_fields", "model")


def _registry_rows(path) -> List[dict]:
    p = Path(path)
    if not p.exists():
        return []
    out = []
    for line in p.read_text().splitlines():
        if line.strip():
            out.append(json.loads(line))
    return out


def mem_available_mb() -> Optional[float]:
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            if line.startswith("MemAvailable:"):
                return int(line.split()[1]) / 1024.0
    except OSError:
        return None
    return None


def mem_proof(path, root: Path = fz.ROOT) -> dict:
    """The memory proof written by `s6h1_mem` on the code under test, at a leaf count that covers the expected pool."""
    p = Path(path or "")
    if not str(path or "").strip() or not p.exists():
        raise ValueError("no memory proof (--mem-proof): run evalkit.s6h1_mem on the run host at >= %d leaves first" % EXPECTED_LEAVES)
    m = json.loads(p.read_text())
    if m.get("tool") != "evalkit.s6h1_mem" or m.get("dim") != 384:
        raise ValueError("the memory proof is not an s6h1_mem report at 384 dimensions")
    if m.get("code_sha256") != fz.code_digest(root):
        raise ValueError("the memory proof was measured on other code than the code under test")
    if int(m.get("leaves", 0)) < EXPECTED_LEAVES:
        raise ValueError("the memory proof covers %s leaves, fewer than the expected %d" % (m.get("leaves"), EXPECTED_LEAVES))
    return m


def check_prereqs(a, *, embed=None, manifest_path: Path = fz.MANIFEST, root: Path = fz.ROOT,
                  mem_available: Callable[[], Optional[float]] = mem_available_mb) -> List[str]:
    """Reasons the stage must not start; empty when every guard passed. Reads no dataset content."""
    from living_context.embed import e5
    why: List[str] = []
    if getattr(a, "stage", STAGE) != STAGE:
        why.append("this runner runs stage %s only" % STAGE)
    if not (a.approved_cap and 0 < a.approved_cap <= bud.APPROVED_CAP):
        why.append("--approved-cap must be the owner's S6-H1 cap, at most %d" % bud.APPROVED_CAP)
    for k, name in (("approval_ref", "the owner's approval of S6-H1"), ("release_ref", "the owner's release of the fresh set"),
                    ("commit", "the commit under test"), ("stamp_registry", "the host's stamp registry file")):
        if not str(getattr(a, k, "") or "").strip():
            why.append("%s is required (--%s)" % (name, k.replace("_", "-")))
    if a.model_kind != "claude":
        why.append("S6-H1 uses the real model port (--model-kind claude)")
    try:
        e5.require_real(embed, stage=STAGE)
    except e5.PinError:
        why.append("the pinned real embedder (multilingual-e5-small) is required")
    why += fz.check(commit=a.commit, s3_dir=a.s3_run_dir, model=a.model, a0_fields=a.a0_fields, path=manifest_path, root=root)
    fsha = fz.manifest_sha256(manifest_path) if Path(manifest_path).exists() else ""
    rd = Path(a.run_dir)
    try:
        rec = ds.read_release(rd)
    except ds.Unreleased as e:
        why.append(str(e))
    else:
        if rec["stage"] != STAGE or rec["release_ref"] != a.release_ref:
            why.append("RELEASE.json does not name stage %s with this release reference" % STAGE)
        if rec["freeze_manifest_sha256"] != fsha:
            why.append("RELEASE.json names another freeze manifest: the release follows the freeze")
    stamp = rd / "STAMP.json"
    if stamp.exists() and not a.resume:
        why.append("a stamped S6-H1 run already exists in the run directory (one stamped run; --resume continues it)")
    if stamp.exists() and a.resume:
        old = json.loads(stamp.read_text())
        now = {"commit": a.commit, "cap": a.approved_cap, "approval_ref": a.approval_ref, "release_ref": a.release_ref,
               "freeze_sha256": fsha, "a0_fields": a.a0_fields, "model": a.model}
        for k in STAMP_KEYS:
            if old.get(k) != now[k]:
                why.append("--resume must continue the same run: %s differs from the stamp" % k)
    if a.resume and not stamp.exists():
        why.append("--resume needs the stamp of an earlier start in the run directory")
    if str(getattr(a, "stamp_registry", "") or "").strip():
        here = os.path.realpath(a.run_dir)
        rows = [r for r in _registry_rows(a.stamp_registry) if r.get("stage") == STAGE]
        if any(r.get("run_dir") != here for r in rows):
            why.append("the stamp registry holds a stamped S6-H1 run in another run directory: a second stamped run is refused")
        if rows and not a.resume:
            why.append("the stamp registry already holds this run directory's S6-H1 run (start once; --resume continues)")
        if a.resume and stamp.exists() and not rows:
            why.append("the stamp registry has no row for this run; refusing to resume an unregistered run")
    if (rd / "STOP").exists():
        why.append("a stop file is present in the run directory; remove it to start or resume")
    try:
        mp = mem_proof(getattr(a, "mem_proof", ""), root)
    except (OSError, ValueError) as e:
        why.append(str(e))
    else:
        free = mem_available()
        if free is not None and free < float(mp["peak_rss_mb"]) + MEM_MARGIN_MB:
            why.append("the host has %.0f MB available, less than the proven build peak %.0f MB plus %d MB" % (free, mp["peak_rss_mb"],
                                                                                                         MEM_MARGIN_MB))
    try:
        est = bud.estimate(EXPECTED_LEAVES, bud.measured_rates(a.rates_from))
        why += bud.refusal(est, a.approved_cap or 0)
    except (OSError, ValueError) as e:
        why.append("the call estimate needs the measured rates of earlier stages (--rates-from): %s" % e)
    return why


def write_state(run_dir: Path, state: str, **extra) -> int:
    (run_dir / "STATE.json").write_text(json.dumps(dict(state=state, at=time.time(), **extra), indent=1, sort_keys=True))
    return RC[state]


def _default_tenant(a, embedder, attach: bool):
    from living_context.config import Constants
    from living_context.embed import e5
    from .eval_tenant import EvalTenant
    from .flat import A0_FIELDS_STRONG, A0_FIELDS_TEXT
    pport, qport = e5.ports(embedder, limit=2)
    return EvalTenant(a.es_url, a.es_ca, a.es_pw_file, TENANT, Path(a.run_dir) / "tenant", consts=Constants(),
                      embed=pport.for_tenant(TENANT), query_embed=qport.for_tenant(TENANT),
                      a0_fields=A0_FIELDS_TEXT if a.a0_fields == "text" else A0_FIELDS_STRONG, attach=attach,
                      registry_doc_only=True)      # the Elasticsearch is shared with S2/S3 tenants: never delete their registry rows


def whole_pool(qs) -> List[tuple]:
    """(content hash, turns) of every haystack session of every question, first copy of each content, in dataset order."""
    hashes = ds.session_hashes(qs)
    seen, out = set(), []
    for q in qs:
        for sid, turns in q.sessions:
            h = hashes[sid]
            if h not in seen:
                seen.add(h)
                out.append((h, turns))
    return out


def run(a, *, embedder=None, runner=None, loader: Optional[Callable] = None, tenant_factory: Optional[Callable] = None,
        registered_pool: int = REGISTERED_POOL, registered_fresh=None, manifest_path: Path = fz.MANIFEST, root: Path = fz.ROOT) -> int:
    """The body of S6-H1; `check_prereqs` must have passed. Tests pass a synthetic loader, an in-memory tenant and a fake runner."""
    from living_context.config import Constants
    from living_context.model.budget import Budget, BudgetExceeded
    from living_context.model.port import PortRefused
    from living_context.structure.build import BuildFailed
    from . import arms, claude_cli, s2_stage
    from .eval_tenant import CLS
    from .s2_ports import Journal, ResilientPort, StageLimited, StagePaused, StageStopped

    run_dir = Path(a.run_dir)
    run_dir.mkdir(parents=True, exist_ok=True)
    man = fz.load(manifest_path)
    fsha = fz.manifest_sha256(manifest_path)
    vals = man["values"]
    proven_max_leaves = int(mem_proof(a.mem_proof, root)["leaves"])
    stamp = run_dir / "STAMP.json"
    # nothing is stamped until every no-call gate has passed (data counts, pool count, memory, budget): a refusal leaves the run
    # directory and the host's stamp registry unspent, so a corrected start is still the stage's one stamped run
    try:
        qs = loader(a) if loader else ds.load_questions(a.dataset)
        fs = ds.fresh_questions(qs, stage=STAGE, run_dir=run_dir, release_ref=a.release_ref, freeze_sha256=fsha,
                                registered=registered_fresh if registered_fresh is not None else vals["registered_fresh"],
                                extra_dev_exposed=vals.get("dev_exposed_extra", ()))
    except ds.DatasetError as e:          # Unreleased is a DatasetError
        return write_state(run_dir, "REFUSED", error="%s: %s" % (type(e).__name__, e))
    hashes = ds.session_hashes(qs)
    clusters = ds.content_clusters(qs, hashes)
    pool = whole_pool(qs)
    if len(pool) != registered_pool:
        return write_state(run_dir, "REFUSED", error="the whole pool has %d contents, registered %d" % (len(pool), registered_pool))

    consts = Constants()
    built = run_dir / "BUILT.json"
    resumed_built = built.exists()
    ingested = (run_dir / "tenant_state.json").exists()
    make = tenant_factory or (lambda attach: _default_tenant(a, embedder, attach))
    et = make(resumed_built or ingested)
    if resumed_built or ingested:
        n_leaves = et.load_state(run_dir / "tenant_state.json")
        if et.leaf_count_in_es() != n_leaves:
            et.close()
            return write_state(run_dir, "REFUSED", error="the tenant no longer holds the leaves of the earlier run")
        if not resumed_built:
            et.load_leaves(n_leaves)
    else:
        t0 = time.time()
        n_leaves = et.ingest(pool)
        et.save_state(run_dir / "tenant_state.json")
        print("ingested %d contents as %d leaves in %.0fs" % (len(pool), n_leaves, time.time() - t0), flush=True)
    if n_leaves > proven_max_leaves:
        et.close()
        return write_state(run_dir, "REFUSED", leaves=n_leaves, error="%d leaves exceed the %d whose build memory was proven; prove the "
                           "larger size with evalkit.s6h1_mem first" % (n_leaves, proven_max_leaves))
    est = bud.estimate(n_leaves, bud.measured_rates(a.rates_from))
    (run_dir / "budget.json").write_text(json.dumps(est, indent=1, sort_keys=True))
    print("call estimate at %d leaves: expected %d, top %d, cap %d" % (n_leaves, est["expected"], est["top"], a.approved_cap), flush=True)
    why = bud.refusal(est, a.approved_cap)
    if why:
        et.close()
        return write_state(run_dir, "REFUSED", leaves=n_leaves, error=why[0])
    if not stamp.exists():
        stamp.write_text(json.dumps({"stage": STAGE, "commit": a.commit, "cap": a.approved_cap, "approval_ref": a.approval_ref,
                                     "release_ref": a.release_ref, "freeze_sha256": fsha, "a0_fields": a.a0_fields, "model": a.model,
                                     "beta": vals["beta"], "mem_proof_sha256": fz._sha(Path(a.mem_proof)), "leaves": n_leaves,
                                     "started": time.time()}, sort_keys=True))
        with open(a.stamp_registry, "a") as f:
            f.write(json.dumps({"stage": STAGE, "run_dir": os.path.realpath(a.run_dir), "commit": a.commit, "freeze_sha256": fsha,
                                "at": time.time()}, sort_keys=True) + "\n")

    runner = runner or claude_cli.CliRunner(a.binary, a.model, run_dir / "cwd")
    budget = Budget(run_dir / "counter", a.approved_cap)
    inner = claude_cli.CappedPort(runner, budget, run_dir / "calls.jsonl", model_id=a.model, argv_record=getattr(runner, "argv", lambda: [])())
    journal = Journal(run_dir / "journal.jsonl")
    port = ResilientPort(inner, budget, journal, run_dir / "STOP", max_retries=a.max_retries, base_delay_s=a.base_delay)
    ledger = s2_stage.Ledger(run_dir / "done.jsonl")
    shuffled_qids = st.shuffled_subset(fs, vals["thresholds"]["shuffled_n"])
    state, extra = "COMPLETE", {}
    try:
        if not resumed_built:
            tree = et.build_and_publish(port)
            (run_dir / "tree.json").write_text(json.dumps({"nodes": len(tree.nodes), "levels": tree.levels, "top": len(tree.top),
                                                            "build_calls": tree.calls, "leaves": n_leaves}, indent=1))
            built.write_text(json.dumps({"at": time.time()}))
            print("tree: %d nodes, %d levels, top %d, build calls %d" % (len(tree.nodes), tree.levels, len(tree.top), tree.calls), flush=True)
        view, world = et.view(), et.world
        direct = s2_stage.Recorder(et.direct())
        tr = s2_stage.Recorder(arms.TreeRetriever(port, view, TENANT, CLS, "px", consts, workers=1))
        sh = s2_stage.Recorder(arms.TreeRetriever(port, arms.PermutedView(view), TENANT, CLS, "px", consts, workers=1))
        st.run_questions(fs, hashes, direct, tr, sh, world, ledger, beta=vals["beta"], shuffled_qids=shuffled_qids,
                         stop=port.stop_requested, progress=lambda s: print(s, "counter", budget.count(), flush=True),
                         scope=lambda ref: setattr(port, "scope", ref))
    except StageStopped:
        state = "STOPPED"
    except (BudgetExceeded, s2_stage.CapReached):
        state = "CAP_REACHED"
    except StagePaused:
        state = "PAUSED_THROTTLE"
    except StageLimited as e:
        state, extra = "LIMIT_REACHED", {"error": str(e)}
    except (BuildFailed, PortRefused) as e:
        state, extra = "BUILD_FAILED", {"error": "%s: %s" % (type(e).__name__, e)}
    except Exception as e:
        write_state(run_dir, "ERROR", error=type(e).__name__, counter=budget.count())
        et.close()
        raise
    extra.update(counter=budget.count(), cap=a.approved_cap, questions_done=len(ledger.rows), questions=len(fs.questions),
                 leaves=n_leaves, replayed=port.replayed, fresh=port.fresh, lost_in_flight=port.lost, throttled=port.throttled)
    if state == "COMPLETE":
        dec = st.decide(ledger, fs, clusters, shuffled_qids=shuffled_qids, shuffled_min=vals["thresholds"]["shuffled_min_point"])
        pool_label = "whole pool (%d contents, %d leaves)" % (len(pool), n_leaves)
        st.write_results(run_dir / "results", ledger, fs, dec, commit=a.commit, counter=budget.count(), pool=pool_label,
                         a0_fields=[a.a0_fields], beta=vals["beta"], freeze_sha256=fsha, release_ref=a.release_ref)
        (run_dir / "results" / "summaries.json").write_text(json.dumps(s2_stage.summaries(run_dir / "calls.jsonl", run_dir / "journal.jsonl"),
                                                                       indent=1, sort_keys=True))
        extra["h1"] = dec["word"]
        print("H1:", dec["word"], "(rule word %s; (a) %s, (b) %s)" % (dec["word_registered_rule"], dec["word_a"], dec["word_b"]), flush=True)
    et.close()
    rc = write_state(run_dir, state, **extra)
    print("state", state, json.dumps(extra), flush=True)
    return rc


def parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description="Stage S6-H1 (confirmatory H1). See docs/RUN-S6H1.md.")
    ap.add_argument("--stage", default=STAGE)
    for k in ("--dataset", "--es-url", "--es-ca", "--es-pw-file", "--run-dir", "--s3-run-dir", "--stamp-registry", "--embed-dir",
              "--binary", "--model"):
        ap.add_argument(k, required=True)
    ap.add_argument("--rates-from", nargs="+", required=True, help="run directories of S2 and S3 (measured calls per recall)")
    ap.add_argument("--mem-proof", required=True, help="the JSON report of evalkit.s6h1_mem on this code at the expected pool size")
    ap.add_argument("--model-kind", choices=["claude"], default="claude")
    ap.add_argument("--approved-cap", type=int, required=True)
    ap.add_argument("--approval-ref", required=True)
    ap.add_argument("--release-ref", required=True)
    ap.add_argument("--commit", required=True)
    ap.add_argument("--a0-fields", default="strong", choices=["strong", "text"])
    ap.add_argument("--resume", action="store_true")
    ap.add_argument("--threads", type=int, default=6)
    ap.add_argument("--max-retries", type=int, default=3)
    ap.add_argument("--base-delay", type=float, default=30.0)
    return ap


def main(argv: Optional[list] = None) -> int:
    from living_context.embed import e5
    a = parser().parse_args(argv)
    try:
        embedder = e5.E5Embedder(a.embed_dir, threads=a.threads)
    except e5.PinError as e:
        print("REFUSED:\n  the embedder files are not the pinned ones: %s" % e)
        return 2
    why = check_prereqs(a, embed=embedder)
    if why:
        print("REFUSED:\n  " + "\n  ".join(why))
        return 2
    return run(a, embedder=embedder)


if __name__ == "__main__":
    sys.exit(main())
