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
import functools
import json
import os
import sys
import time
from pathlib import Path
from typing import Callable, List, Optional

from . import dataset as ds, model_kind as mk, parallel as par, s6h1_budget as bud, s6h1_freeze as fz, s6h1_stage as st

STAGE = "S6-H1"
PREBUILD_STAGE = "S6-H1-prebuild"   # EXPERIMENT 14.5: the whole-pool tenant and tree built before the freeze, questions sealed
PREBUILD_FILE = "PREBUILD.json"
TENANT = "s6h1"
REGISTERED_POOL = 18_464            # distinct session contents of all 500 questions (EXPERIMENT 3.3, MEASURED)
EXPECTED_LEAVES = 78_700            # 18,464 x 4.262 leaves per session (S3's dev static pool; counts only)
MEM_MARGIN_MB = 2_048               # free memory beyond the proven peak that the host must have at the start
CLI_CHILD_MB = 256                  # one headless CLI process (MEASURED: 238 MB peak RSS each, 4 at once on the run host)
E5_WORKER_MB = 1_024                # one up-front embedding process (MEASURED: 761 to 800 MB RSS each, 3 at once, 6 threads)
RC = {"COMPLETE": 0, "STOPPED": 3, "CAP_REACHED": 4, "BUILD_FAILED": 5, "PAUSED_THROTTLE": 75, "LIMIT_REACHED": 76, "ERROR": 1,
      "REFUSED": 2, "PREBUILT": 0}
STAMP_KEYS = ("commit", "cap", "approval_ref", "release_ref", "freeze_sha256", "a0_fields", "model")
PREBUILD_KEYS = ("commit", "code_sha256", "cap", "approval_ref", "amendment_ref", "a0_fields", "threads")   # and mk.IDENTITY


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
    """Reasons the stage must not start; empty when every guard passed. Reads no dataset content. With `--prebuild` (EXPERIMENT 14.5)
    the release and the freeze are not needed, because no question is read: see `_prebuild_prereqs`."""
    from living_context.embed import e5
    why: List[str] = []
    if getattr(a, "stage", STAGE) != STAGE:
        why.append("this runner runs stage %s only" % STAGE)
    if not (a.approved_cap and 0 < a.approved_cap <= bud.APPROVED_CAP):
        why.append("--approved-cap must be the owner's S6-H1 cap, at most %d" % bud.APPROVED_CAP)
    prebuild = bool(getattr(a, "prebuild", False))
    refs = (("approval_ref", "the owner's approval of S6-H1"), ("commit", "the commit under test"),
            ("stamp_registry", "the host's stamp registry file"))
    refs += ((("amendment_ref", "the recorded amendment of the early build"),) if prebuild else
             (("release_ref", "the owner's release of the fresh set"), ("s3_run_dir", "S3's run directory")))
    for k, name in refs:
        if not str(getattr(a, k, "") or "").strip():
            why.append("%s is required (--%s)" % (name, k.replace("_", "-")))
    why += mk.check(a)
    try:
        e5.require_real(embed, stage=STAGE)
    except e5.PinError:
        why.append("the pinned real embedder (multilingual-e5-small) is required")
    rd = Path(a.run_dir)
    if prebuild:
        why += _prebuild_prereqs(a, rd, root)
    else:
        why += _stamped_prereqs(a, rd, manifest_path, root)
    return why + _shared_prereqs(a, rd, root, mem_available)


def _registry(a) -> List[dict]:
    return [r for r in _registry_rows(a.stamp_registry) if r.get("stage") in (STAGE, PREBUILD_STAGE)] \
        if str(getattr(a, "stamp_registry", "") or "").strip() else []


def prebuild_record(run_dir) -> Optional[dict]:
    p = Path(run_dir) / PREBUILD_FILE
    return json.loads(p.read_text()) if p.exists() else None


def _prebuild_now(a, root: Path) -> dict:
    return {"commit": a.commit, "code_sha256": fz.code_digest(root), "cap": a.approved_cap, "approval_ref": a.approval_ref,
            "amendment_ref": getattr(a, "amendment_ref", ""), "a0_fields": a.a0_fields, "threads": a.threads, **mk.record(a)}


def _prebuild_prereqs(a, rd: Path, root: Path) -> List[str]:
    """The early build (EXPERIMENT 14.5): before any stamped S6-H1 run, in one run directory only; a rerun continues it (journal
    replay) only with the same commit, code, cap, references, fields, model and thread count. No release, freeze or question."""
    why: List[str] = []
    if (rd / "STAMP.json").exists():
        why.append("a stamped S6-H1 run exists in the run directory: the early build is only before it")
    here = os.path.realpath(a.run_dir)
    rows = _registry(a)
    if any(r.get("run_dir") != here for r in rows):
        why.append("the stamp registry holds an S6-H1 run or early build in another run directory: refused")
    if any(r.get("stage") == STAGE for r in rows):
        why.append("the stamp registry holds a stamped S6-H1 run: the early build is only before it")
    old = prebuild_record(rd)
    if old is not None:
        now = _prebuild_now(a, root)
        for k in PREBUILD_KEYS:
            if old.get(k) != now[k]:
                why.append("the early build must continue with the same %s as %s" % (k, PREBUILD_FILE))
        for k in mk.differs(old, a):
            why.append("the early build must continue with the same %s as %s" % (k, PREBUILD_FILE))
    elif any(r.get("stage") == PREBUILD_STAGE for r in rows):
        why.append("the stamp registry has an early build row for this run directory but no %s" % PREBUILD_FILE)
    elif (rd / "BUILT.json").exists() or (rd / "counter").exists():     # an ingested tenant refused before any call is attached
        why.append("the run directory holds a built tree or charged calls without %s" % PREBUILD_FILE)
    return why


def _stamped_prereqs(a, rd: Path, manifest_path: Path, root: Path) -> List[str]:
    why: List[str] = []
    why += fz.check(commit=a.commit, s3_dir=a.s3_run_dir, model=a.model, a0_fields=a.a0_fields, path=manifest_path, root=root)
    fsha = fz.manifest_sha256(manifest_path) if Path(manifest_path).exists() else ""
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
        for k in mk.differs(old, a):
            if k != "model":                # compared with STAMP_KEYS above
                why.append("--resume must continue the same run: %s differs from the stamp" % k)
    if a.resume and not stamp.exists():
        why.append("--resume needs the stamp of an earlier start in the run directory")
    if str(getattr(a, "stamp_registry", "") or "").strip():
        here = os.path.realpath(a.run_dir)
        both = _registry(a)
        rows = [r for r in both if r.get("stage") == STAGE]
        if any(r.get("run_dir") != here for r in both):
            why.append("the stamp registry holds a stamped S6-H1 run or early build in another run directory: a second run is refused")
        if rows and not a.resume:
            why.append("the stamp registry already holds this run directory's S6-H1 run (start once; --resume continues)")
        if a.resume and stamp.exists() and not rows:
            why.append("the stamp registry has no row for this run; refusing to resume an unregistered run")
    pre = prebuild_record(rd)
    if pre is not None:                 # the early build (14.5) must be the code, fields and model under test
        now = _prebuild_now(a, root)
        for k in ("commit", "code_sha256", "a0_fields", "threads", "cap", "approval_ref"):
            if pre.get(k) != now[k]:
                why.append("the early build's %s (%s) is not this run's: the prebuilt tree is not the one under test" % (k, PREBUILD_FILE))
        for k in mk.differs(pre, a):
            why.append("the early build's %s (%s) is not this run's: the prebuilt tree is not the one under test" % (k, PREBUILD_FILE))
    elif (rd / "BUILT.json").exists() and not stamp.exists():
        why.append("the run directory holds a built tree with neither a stamp nor %s" % PREBUILD_FILE)
    return why


def _shared_prereqs(a, rd: Path, root: Path, mem_available) -> List[str]:
    why: List[str] = []
    if (rd / "STOP").exists():
        why.append("a stop file is present in the run directory; remove it to start or resume")
    n_par = getattr(a, "concurrency", par.REGISTERED_CONCURRENCY)
    why += par.check_concurrency(n_par, getattr(a, "concurrency_reason", ""))
    n_par = n_par if isinstance(n_par, int) and n_par >= 1 else 1
    from .s2_ports import UNSCOPED_REFUSAL, journal_unscoped
    if journal_unscoped(rd / "journal.jsonl"):           # review F2: one key scheme at every worker count
        why.append(UNSCOPED_REFUSAL % journal_unscoped(rd / "journal.jsonl"))
    try:
        mp = mem_proof(getattr(a, "mem_proof", ""), root)
    except (OSError, ValueError) as e:
        why.append(str(e))
    else:
        cw, proven_cw = int(getattr(a, "cluster_workers", 1) or 1), int(mp.get("cluster_workers", 1) or 1)
        if cw > proven_cw:                  # each clusterer worker holds its own block buffer: the proof must cover the count used
            why.append("the memory proof ran the clusterer with %d workers, fewer than --cluster-workers %d" % (proven_cw, cw))
        free = mem_available()
        need = float(mp["peak_rss_mb"]) + n_par * CLI_CHILD_MB + MEM_MARGIN_MB
        if free is not None and free < need:
            why.append("the host has %.0f MB available, less than the proven build peak %.0f MB plus %d CLI processes of %d MB plus %d MB"
                       % (free, mp["peak_rss_mb"], n_par, CLI_CHILD_MB, MEM_MARGIN_MB))
        ew = int(getattr(a, "embed_workers", 1) or 1)
        need_ingest = float(mp["peak_rss_mb"]) + (ew * E5_WORKER_MB if ew > 1 else 0) + MEM_MARGIN_MB   # the processes end before the build
        if free is not None and need_ingest > need and free < need_ingest:
            why.append("the host has %.0f MB available, less than the proven peak %.0f MB plus %d embedding processes of %d MB plus %d MB"
                       % (free, mp["peak_rss_mb"], ew, E5_WORKER_MB, MEM_MARGIN_MB))
    try:
        est = bud.estimate(EXPECTED_LEAVES, bud.measured_rates(a.rates_from), concurrency=n_par)
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


class _Prebuilt(Exception):
    """The early build ends after the tree is published (EXPERIMENT 14.5)."""


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
        registered_pool: int = REGISTERED_POOL, registered_fresh=None, manifest_path: Path = fz.MANIFEST, root: Path = fz.ROOT,
        embed_factory: Optional[Callable] = None, pool_loader: Optional[Callable] = None) -> int:
    """The body of S6-H1; `check_prereqs` must have passed. Tests pass a synthetic loader, an in-memory tenant, a fake runner and a
    picklable embedder factory for the up-front embedding processes.

    With `--prebuild` (EXPERIMENT 14.5) the same body runs up to the built tree and stops in state PREBUILT: the pool comes from
    `dataset.pool_contents` (no question, answer or gold is parsed into any object), no release or freeze is read, PREBUILD.json and
    an `S6-H1-prebuild` registry row take the place of the stamp, and no question is asked. The stamped run later attaches to that
    tenant and tree only when its recomputed whole pool has the same ordered content hashes."""
    from living_context.config import Constants
    from living_context.model.budget import Budget, BudgetExceeded
    from living_context.model.port import PortRefused
    from living_context.structure import cluster
    from living_context.structure.build import BuildFailed
    from . import arms, claude_cli, s2_stage
    from .eval_tenant import CLS
    from .s2_ports import Journal, ResilientPort, StageLimited, StagePaused, StageStopped, read_resumable

    run_dir = Path(a.run_dir)
    run_dir.mkdir(parents=True, exist_ok=True)
    prebuild = bool(getattr(a, "prebuild", False))
    proven_max_leaves = int(mem_proof(a.mem_proof, root)["leaves"])
    stamp = run_dir / "STAMP.json"
    pre = prebuild_record(run_dir)
    try:                                  # the model port (and its key source) is built before any work, stamp or charge
        prepared = mk.prepare(a, run_dir, runner=runner)
    except mk.PortUnavailable as e:
        return write_state(run_dir, "REFUSED", error=str(e))
    # nothing is stamped until every no-call gate has passed (data counts, pool count, memory, budget): a refusal leaves the run
    # directory and the host's stamp registry unspent, so a corrected start is still the stage's one stamped run
    if prebuild:                          # the pool contents only: the questions, answers and gold stay sealed (EXPERIMENT 14.5)
        man = vals = fsha = fs = hashes = clusters = None
        try:
            pool = pool_loader(a) if pool_loader else ds.pool_contents(a.dataset)
        except ds.DatasetError as e:
            return write_state(run_dir, "REFUSED", error="%s: %s" % (type(e).__name__, e))
    else:
        man = fz.load(manifest_path)
        fsha = fz.manifest_sha256(manifest_path)
        vals = man["values"]
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
    pool_sha = ds.pool_sha256(pool)
    if pre is not None and pre.get("pool_sha256") != pool_sha:
        return write_state(run_dir, "REFUSED", error="the whole pool differs from the one the early build ingested (%s)" % PREBUILD_FILE)

    consts = Constants()
    ingest_writers, embed_workers = int(getattr(a, "ingest_writers", 1) or 1), int(getattr(a, "embed_workers", 1) or 1)
    ingest_info: dict = {}
    built = run_dir / "BUILT.json"
    resumed_built = built.exists()
    ingested = (run_dir / "tenant_state.json").exists()
    if (pre is not None or resumed_built) and not ingested:     # a fresh start resets the tenant: never over a prebuilt one
        return write_state(run_dir, "REFUSED", error="the run directory has %s but no tenant_state.json; refusing to reset the tenant"
                           % (PREBUILD_FILE if pre is not None else "BUILT.json"))
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
        reuse_from = (getattr(a, "reuse_from_tenant", "") or "").strip()
        if ingest_writers > 1 or embed_workers > 1 or reuse_from:
            from . import embed_pool
            factory = embed_factory or (embed_pool.E5Factory(a.embed_dir, a.threads) if embed_workers > 1 else (lambda: embedder))
            report = lambda i, n: (i % 2000 == 0 or i == n) and print("embedded %d/%d leaf texts in %.0fs" % (i, n, time.time() - t0),
                                                                      flush=True)

            def embed_many(texts):
                reuse = {}
                if reuse_from:          # another tenant's one-window vectors, read only, checked against the pinned embedder first
                    wanted = {embed_pool.text_key(t) for t in texts}
                    reuse = embed_pool.index_reuse(et.admin, reuse_from, keep=lambda t: embed_pool.text_key(t) in wanted
                                                   and embed_pool.one_window(embedder, t))
                    ingest_info.update(reused=len(reuse), reuse_checked=embed_pool.verify_reuse(reuse, texts, embedder),
                                       reuse_from=reuse_from)
                    print("reusing %d one-window vectors of tenant %s (%d re-embedded and equal)"
                          % (len(reuse), reuse_from, ingest_info["reuse_checked"]), flush=True)
                return embed_pool.passages(texts, factory, workers=embed_workers, reuse=reuse, progress=report)
            n_leaves = et.ingest_concurrent(pool, writers=ingest_writers, embed_many=embed_many)
        else:
            n_leaves = et.ingest(pool)
        et.save_state(run_dir / "tenant_state.json")
        print("ingested %d contents as %d leaves in %.0fs" % (len(pool), n_leaves, time.time() - t0), flush=True)
    if n_leaves > proven_max_leaves:
        et.close()
        return write_state(run_dir, "REFUSED", leaves=n_leaves, error="%d leaves exceed the %d whose build memory was proven; prove the "
                           "larger size with evalkit.s6h1_mem first" % (n_leaves, proven_max_leaves))
    n_par = int(getattr(a, "concurrency", par.REGISTERED_CONCURRENCY) or 1)
    cluster_workers = int(getattr(a, "cluster_workers", 1) or 1)
    est = bud.estimate(n_leaves, bud.measured_rates(a.rates_from), concurrency=n_par)
    (run_dir / "budget.json").write_text(json.dumps(est, indent=1, sort_keys=True))
    print("call estimate at %d leaves: expected %d, top %d, cap %d" % (n_leaves, est["expected"], est["top"], a.approved_cap), flush=True)
    why = bud.refusal(est, a.approved_cap)
    if why:
        et.close()
        return write_state(run_dir, "REFUSED", leaves=n_leaves, error=why[0])
    if prebuild and pre is None:
        pre = dict(mk.stamp_record(a, prepared), **_prebuild_now(a, root), stage=PREBUILD_STAGE, pool_contents=len(pool), pool_sha256=pool_sha,
                   leaves=n_leaves,
                   embed_workers=embed_workers, ingest_writers=ingest_writers, mem_proof_sha256=fz._sha(Path(a.mem_proof)),
                   started=time.time())
        (run_dir / PREBUILD_FILE).write_text(json.dumps(pre, sort_keys=True))
        with open(a.stamp_registry, "a") as f:
            f.write(json.dumps({"stage": PREBUILD_STAGE, "run_dir": os.path.realpath(a.run_dir), "commit": a.commit,
                                "code_sha256": pre["code_sha256"], "at": time.time()}, sort_keys=True) + "\n")
    if not prebuild and not stamp.exists():
        stamp.write_text(json.dumps({"stage": STAGE, "commit": a.commit, "cap": a.approved_cap, "approval_ref": a.approval_ref,
                                     "release_ref": a.release_ref, "freeze_sha256": fsha, "a0_fields": a.a0_fields, "model": a.model,
                                     "beta": vals["beta"], "mem_proof_sha256": fz._sha(Path(a.mem_proof)), "leaves": n_leaves,
                                     "concurrency": n_par, "concurrency_reason": getattr(a, "concurrency_reason", "") or "",
                                     "prebuild": pre, **mk.stamp_record(a, prepared), "started": time.time()}, sort_keys=True))
        with open(a.stamp_registry, "a") as f:
            f.write(json.dumps({"stage": STAGE, "run_dir": os.path.realpath(a.run_dir), "commit": a.commit, "freeze_sha256": fsha,
                                "at": time.time()}, sort_keys=True) + "\n")

    budget = Budget(run_dir / "counter", a.approved_cap)
    read_resumable(run_dir / "calls.jsonl")          # a torn last row of an interrupted run is set aside before new rows follow it
    inner = mk.capped_port(a, run_dir, budget, prepared=prepared)
    journal = Journal(run_dir / "journal.jsonl")
    adaptive = n_par > 1 and not getattr(a, "fixed_concurrency", False)
    port = ResilientPort(inner, budget, journal, run_dir / "STOP", max_retries=a.max_retries, base_delay_s=a.base_delay,
                         concurrency=n_par, adaptive=adaptive, start_concurrency=min(n_par, 2) if adaptive else None, require_scope=True)
    with open(run_dir / "starts.jsonl", "a") as f:      # every start or resume, with its concurrency (outputs do not depend on it)
        f.write(json.dumps({"at": time.time(), "commit": a.commit, "mode": "prebuild" if prebuild else "stamped",
                            "concurrency": n_par, "cluster_workers": cluster_workers,
                            "ingest_writers": ingest_writers, "embed_workers": embed_workers, **ingest_info, "adaptive": adaptive,
                            "concurrency_reason": getattr(a, "concurrency_reason", "") or ""}, sort_keys=True) + "\n")
    ledger = None if prebuild else s2_stage.Ledger(run_dir / "done.jsonl", extra=mk.record(a))
    shuffled_qids = None if prebuild else st.shuffled_subset(fs, vals["thresholds"]["shuffled_n"])
    state, extra = ("PREBUILT" if prebuild else "COMPLETE"), {}
    try:
        if not resumed_built:
            tree = et.build_and_publish(port, pmap=par.ordered_map(n_par), port_for=lambda k: port.scoped("build|" + k),
                                        grouper=functools.partial(cluster.balanced_groups, workers=cluster_workers))
            (run_dir / "tree.json").write_text(json.dumps({"nodes": len(tree.nodes), "levels": tree.levels, "top": len(tree.top),
                                                            "build_calls": tree.calls, "leaves": n_leaves}, indent=1))
            built.write_text(json.dumps({"at": time.time()}))
            print("tree: %d nodes, %d levels, top %d, build calls %d" % (len(tree.nodes), tree.levels, len(tree.top), tree.calls), flush=True)
        if prebuild:
            raise _Prebuilt()
        view, world = et.view(), et.world
        pview, dfn = arms.PermutedView(view), et.direct()

        def retrievers(ref):
            # one question's own recorders over a port scoped to it; each recall stays one worker (deterministic call order)
            qp = port.scoped(ref)
            return (s2_stage.Recorder(dfn),
                    s2_stage.tallied(qp, lambda p: arms.TreeRetriever(p, view, TENANT, CLS, "px", consts, workers=1)),
                    s2_stage.tallied(qp, lambda p: arms.TreeRetriever(p, pview, TENANT, CLS, "px", consts, workers=1)))
        st.run_questions(fs, hashes, None, None, None, world, ledger, beta=vals["beta"], shuffled_qids=shuffled_qids,
                         stop=port.stop_requested, progress=lambda s: print(s, "counter", budget.count(), flush=True),
                         retrievers=retrievers, workers=n_par)
    except _Prebuilt:
        pass
    except StageStopped:
        state = "STOPPED"
    except (BudgetExceeded, s2_stage.CapReached):
        state = "CAP_REACHED"
    except StagePaused as e:
        state, extra = "PAUSED_THROTTLE", {"error": str(e), "retry_after_s": getattr(e, "retry_after_s", None)}
    except StageLimited as e:
        state, extra = "LIMIT_REACHED", {"error": str(e)}
    except (BuildFailed, PortRefused) as e:
        state, extra = "BUILD_FAILED", {"error": "%s: %s" % (type(e).__name__, e)}
    except Exception as e:
        write_state(run_dir, "ERROR", error=type(e).__name__, counter=budget.count())
        et.close()
        raise
    if not prebuild:
        extra.update(questions_done=len(ledger.rows), questions=len(fs.questions))
    extra.update(counter=budget.count(), cap=a.approved_cap, leaves=n_leaves, replayed=port.replayed, fresh=port.fresh,
                 lost_in_flight=port.lost, throttled=port.throttled, limited=port.limited, concurrency_now=port.slots.limit,
                 models_reported=sorted(inner.models_reported), providers_reported=sorted(inner.providers_reported),
                 concurrency_changes=port.slots.changes)
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
    for k in ("--dataset", "--es-url", "--es-ca", "--es-pw-file", "--run-dir", "--stamp-registry", "--embed-dir", "--model"):
        ap.add_argument(k, required=True)
    ap.add_argument("--binary", default="", help="the CLI (--model-kind claude)")
    ap.add_argument("--s3-run-dir", default="", help="S3's run directory (required for the stamped run)")
    ap.add_argument("--rates-from", nargs="+", required=True, help="run directories of S2 and S3 (measured calls per recall)")
    ap.add_argument("--mem-proof", required=True, help="the JSON report of evalkit.s6h1_mem on this code at the expected pool size")
    ap.add_argument("--model-kind", choices=list(mk.KINDS), default="claude")
    mk.add_arguments(ap, default_kind="claude")
    ap.add_argument("--approved-cap", type=int, required=True)
    ap.add_argument("--approval-ref", required=True)
    ap.add_argument("--release-ref", default="", help="the owner's release of the fresh set (required for the stamped run)")
    ap.add_argument("--prebuild", action="store_true",
                    help="EXPERIMENT 14.5: ingest the whole pool and build the tree before the freeze, then stop (state PREBUILT); "
                         "reads the pool contents only, never a question, answer or gold; needs --amendment-ref")
    ap.add_argument("--amendment-ref", default="", help="the recorded amendment of the early build (with --prebuild)")
    ap.add_argument("--commit", required=True)
    ap.add_argument("--a0-fields", default="strong", choices=["strong", "text"])
    ap.add_argument("--resume", action="store_true")
    ap.add_argument("--threads", type=int, default=6)
    ap.add_argument("--max-retries", type=int, default=3)
    ap.add_argument("--base-delay", type=float, default=30.0)
    ap.add_argument("--concurrency", type=int, default=par.REGISTERED_CONCURRENCY,
                    help="model calls at once (registered %d; questions and build groups run concurrently, outputs do not depend on it)"
                    % par.REGISTERED_CONCURRENCY)
    ap.add_argument("--concurrency-reason", default="", help="required above the registered value; recorded in the stamp")
    ap.add_argument("--ingest-writers", type=int, default=1,
                    help="writer threads for the ingest (leaves, order and src_seq do not depend on it)")
    ap.add_argument("--embed-workers", type=int, default=1,
                    help="embedding processes of --threads torch threads each, before the writes (a vector depends on the text, the "
                         "model and --threads only)")
    ap.add_argument("--fixed-concurrency", action="store_true",
                    help="keep --concurrency fixed (default above 1: start at 2, halve on a throttle or allowance refusal, double after "
                         "a clean window, never above --concurrency)")
    ap.add_argument("--reuse-from-tenant", default="",
                    help="read this tenant's leaf index (read only) and reuse its vectors of one-window leaves with the same text")
    ap.add_argument("--cluster-workers", type=int, default=1,
                    help="clusterer threads (the groups do not depend on it); the memory proof must have used at least as many")
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
