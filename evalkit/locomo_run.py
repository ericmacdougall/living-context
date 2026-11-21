"""LoCoMo runner (docs/LOCOMO-PLAN.md). Two stages, both run on the host that holds Elasticsearch and the pinned e5 files:

  python -m evalkit.locomo_run prepare ...   no model call: one tenant per conversation (ingest through the real writer with real e5
                                             vectors), the turn to leaf bind check, the manifest (gold, clusters, pool sizes), the batch
                                             lists and arm A0 on ALL eligible questions (`a0.jsonl`)
  python -m evalkit.locomo_run tree ...      model calls only through the capped Claude CLI port: the tree of each conversation, then A1,
                                             A1-tree-only and SHUFFLED recalls on the batch (`done.jsonl`), with A0 recomputed at recall time

The numbers are computed from the run directory alone by `evalkit.locomo_report`. Nothing here prints a key; the Elasticsearch
credentials are passed by path and read by the repo's own client. Tenants are created with `registry_doc_only=True`, so no other
tenant's registry is touched, and the stage refuses to start if an index of the `lcmo` tenants already exists (unless told to reset)."""
from __future__ import annotations

import argparse
import json
import sys
import threading
import time
from pathlib import Path
from typing import Dict, List, Optional

from living_context.config import Constants
from living_context.embed import e5
from living_context.model.budget import Budget, BudgetExceeded
from living_context.model.port import PortRefused

from . import arms, locomo as lo, model_kind as mk, parallel as par, s2_stage
from .run_s2 import RC, write_state
from .s2_ports import Journal, ResilientPort, StageLimited, StagePaused, StageStopped, Tally, read_resumable

DEFAULT_SUBSET = 100
DEFAULT_SHUFFLED = 30
B2_SIZE = 300
WINDOW = arms.WINDOW


# ---- small helpers ----------------------------------------------------------------------------------------------------------
def jdump(path: Path, obj) -> None:
    Path(path).write_text(json.dumps(obj, indent=1, sort_keys=True))


def tenant_indices(admin, prefix: str) -> List[str]:
    st, rows = admin.request("GET", "/_cat/indices/lc-*?format=json&h=index")
    return [r["index"] for r in rows if st == 200 and isinstance(rows, list) and r["index"].startswith(prefix)] if st == 200 else []


def make_tenant(a, conv: lo.Conversation, run_dir: Path, consts: Constants, pport, qport, *, attach: bool):
    from .eval_tenant import EvalTenant
    from .flat import A0_FIELDS_STRONG
    t = conv.tenant()
    return EvalTenant(a.es_url, a.es_ca, a.es_pw_file, t, run_dir / "tenant", consts=consts, embed=pport.for_tenant(t),
                      query_embed=qport.for_tenant(t), a0_fields=A0_FIELDS_STRONG, attach=attach, registry_doc_only=True)


class Timing:
    """One recall's view of its scoped port: counts its calls and the wall seconds the recall spent inside them. The wall time
    includes waiting for a concurrency slot and for a throttle back-off; pure model latency per task kind is in calls.jsonl."""
    def __init__(self, port):
        self._port, self.calls, self.call_wall_s = port, 0, 0.0
        self._lock = threading.Lock()

    def call(self, task, inputs, **kw):
        t0 = time.monotonic()
        try:
            return self._port.call(task, inputs, **kw)
        finally:
            with self._lock:
                self.calls += 1
                self.call_wall_s += time.monotonic() - t0

    def stop_requested(self) -> bool:
        return self._port.stop_requested()


class Rec:
    """A retriever that keeps its last output, its wall seconds and (for a tree recall) its call timing and failure tally."""
    def __init__(self, fn, timing: Optional[Timing] = None, tally: Optional[Tally] = None):
        self.fn, self.timing, self.tally, self.last, self.wall_s = fn, timing, tally, None, 0.0

    def __call__(self, q):
        t0 = time.monotonic()
        try:
            self.last = self.fn(q)
        finally:
            self.wall_s = time.monotonic() - t0
        return self.last


# ---- prepare ----------------------------------------------------------------------------------------------------------------
def cluster_ids(q: lo.LQuestion) -> Dict[str, str]:
    return {"cluster": "%s|%s" % (q.sample_id, lo.first_session(q)), "cluster_conv": q.sample_id}


def prepare(a) -> int:
    run_dir = Path(a.run_dir)
    run_dir.mkdir(parents=True, exist_ok=True)
    convs = lo.load(a.data)
    consts = Constants()
    emb = e5.E5Embedder(a.embed_dir, threads=a.threads)
    e5.require_real(emb, stage="LOCOMO")
    pport, qport = e5.ports(emb, limit=2)
    (run_dir / "leafmaps").mkdir(exist_ok=True)
    manifest: Dict[str, dict] = {}
    pools: Dict[int, int] = {}
    ets = {}
    t_start = time.time()
    for conv in convs:
        et = make_tenant(a, conv, run_dir, consts, pport, qport, attach=False) if not _exists(a, conv) else None
        if et is None:
            print("REFUSED: indices of tenant %s already exist; remove them or use a new run (nothing was changed)" % conv.tenant(), flush=True)
            return 2
        ets[conv.idx] = et
        t0 = time.time()
        n = et.ingest([(conv.session_hash(s), s.role_content()) for s in conv.sessions])
        et.save_state(run_dir / ("tenant_state_%d.json" % conv.idx))
        if et.leaf_count_in_es() != n:
            raise RuntimeError("tenant %s: the index holds %d leaves, ingest wrote %d" % (conv.tenant(), et.leaf_count_in_es(), n))
        lm = lo.bind(conv, [(l.leaf_id, l.text) for l in et.leaves], consts.leaf_cap)      # raises on any mismatch
        jdump(run_dir / "leafmaps" / ("%d.json" % conv.idx), lm.to_json())
        pools[conv.idx] = n
        for q in conv.questions:
            gl, gs = lo.gold_of(q, lm)
            manifest[q.qid] = {"qid": q.qid, "conv": conv.idx, "sample_id": q.sample_id, "category": q.category,
                               "gold_turns": list(q.gold_turns), "gold_leaves": gl, "gold_sessions": gs, "n_unresolved": q.n_unresolved,
                               "pool_leaves": n, **cluster_ids(q)}
        print("conv %d (%s): %d sessions, %d leaves, bound and checked, %.0fs" % (conv.idx, conv.sample_id, len(conv.sessions), n,
                                                                              time.time() - t0), flush=True)
    jdump(run_dir / "manifest.json", {"questions": manifest, "pools": {str(k): v for k, v in pools.items()},
                                      "excluded_no_evidence": sum(c.n_excluded for c in convs)})
    allq = [q for c in convs for q in c.questions]
    b1 = lo.select_subset(allq, a.n_subset)
    jdump(run_dir / "subset_B1.json", {"order": [q.qid for q in b1], "n": len(b1), "seed": lo.SEED, "shuffled_n": a.shuffled_n,
                                       "shuffled": [q.qid for q in b1[:a.shuffled_n]]})
    # arm A0 on every eligible question, zero model calls
    n_done = 0
    with open(run_dir / "a0.jsonl", "w") as f:
        for conv in convs:
            dfn = ets[conv.idx].direct()
            for q in conv.questions:
                t0 = time.monotonic()
                hits = dfn(q.question)[:WINDOW]
                f.write(json.dumps({"qid": q.qid, "leaves": [i for i, _ in hits], "wall_s": round(time.monotonic() - t0, 4)}) + "\n")
                n_done += 1
        f.flush()
    jdump(run_dir / "PREPARE.json", {"commit": a.commit, "data_sha256": lo.DATA_SHA256, "embedder": e5.MODEL_ID, "es_url": a.es_url,
                                     "tenants": [c.tenant() for c in convs], "leaves": sum(pools.values()), "eligible": len(allq),
                                     "a0_questions": n_done, "seconds": round(time.time() - t_start, 1), "model_calls": 0,
                                     "a0_fields": list(ets[0].a0_fields)})
    for et in ets.values():
        et.close()
    print("prepared: %d tenants, %d leaves, A0 on %d questions, 0 model calls, %.0fs" % (len(convs), sum(pools.values()), n_done,
                                                                                         time.time() - t_start), flush=True)
    return 0


def _exists(a, conv: lo.Conversation) -> bool:
    """True when an index of this tenant is already there (checked before anything is created or reset)."""
    from living_context.gateway.es import EsClient
    admin = EsClient(a.es_url, a.es_ca, a.es_pw_file, scheme="basic", user="elastic")
    return not tenant_indices(admin, "lc-%s-" % conv.tenant()) == []


# ---- tree -------------------------------------------------------------------------------------------------------------------
class CapReached(Exception):
    pass


def batch_questions(a, convs, run_dir: Path) -> List[lo.LQuestion]:
    b1 = json.loads((run_dir / "subset_B1.json").read_text())
    by = {q.qid: q for c in convs for q in c.questions}
    if a.batch == "B1":
        return [by[x] for x in b1["order"]]
    allq = [q for c in convs for q in c.questions]
    return lo.select_subset(allq, a.b2_size, exclude=b1["order"])


def tree_stage(a, *, runner=None) -> int:
    from living_context.structure.build import BuildFailed
    from .eval_tenant import CLS
    run_dir = Path(a.run_dir)
    convs = lo.load(a.data)
    qs = batch_questions(a, convs, run_dir)
    shuffled_n = a.shuffled_n if a.batch == "B1" else 0
    done_path = run_dir / ("done.jsonl" if a.batch == "B1" else "done_B2.jsonl")
    try:
        prepared = mk.prepare(a, run_dir, runner=runner)
    except mk.PortUnavailable as e:
        print("REFUSED: %s" % e, flush=True)
        return 2
    stamp = run_dir / "STAMP.json"
    if not stamp.exists():
        stamp.write_text(json.dumps({"commit": a.commit, "cap": a.approved_cap, "approval_ref": a.approval_ref, "data_sha256": lo.DATA_SHA256,
                                     "n_subset": a.n_subset, "shuffled_n": a.shuffled_n, **mk.stamp_record(a, prepared),
                                     "concurrency": a.concurrency, "started": time.time()}, sort_keys=True))
    else:
        old = json.loads(stamp.read_text())
        if old.get("commit") != a.commit or old.get("cap") != a.approved_cap:
            print("REFUSED: the run was stamped with another commit or cap", flush=True)
            return 2
    consts = Constants()
    emb = e5.E5Embedder(a.embed_dir, threads=a.threads)
    e5.require_real(emb, stage="LOCOMO")
    pport, qport = e5.ports(emb, limit=2)
    budget = Budget(run_dir / "counter", a.approved_cap)
    read_resumable(run_dir / "calls.jsonl")
    inner = mk.capped_port(a, run_dir, budget, prepared=prepared)
    journal = Journal(run_dir / "journal.jsonl")
    n_par = int(a.concurrency)
    port = ResilientPort(inner, budget, journal, run_dir / "STOP", max_retries=a.max_retries, base_delay_s=a.base_delay, concurrency=n_par,
                         adaptive=n_par > 1, start_concurrency=min(n_par, 2) if n_par > 1 else None, require_scope=True)
    with open(run_dir / "starts.jsonl", "a") as f:
        f.write(json.dumps({"at": time.time(), "commit": a.commit, "batch": a.batch, "concurrency": n_par, "adaptive": n_par > 1}, sort_keys=True) + "\n")
    rows = {r["qid"]: r for r in read_resumable(done_path)}
    state, extra = "COMPLETE", {}
    ets: Dict[int, object] = {}
    try:
        need = sorted({q.conv for q in qs})
        env: Dict[int, dict] = {}
        for ci in need:
            conv = convs[ci]
            et = make_tenant(a, conv, run_dir, consts, pport, qport, attach=True)
            n_leaves = et.load_state(run_dir / ("tenant_state_%d.json" % ci))
            if et.leaf_count_in_es() != n_leaves:
                print("REFUSED: tenant %s no longer holds its leaves; run prepare again" % conv.tenant(), flush=True)
                return 2
            et.load_leaves(n_leaves)
            ets[ci] = et
            built = run_dir / ("BUILT_%d.json" % ci)
            if not built.exists():
                t0 = time.time()
                tree = et.build_and_publish(port, pmap=par.ordered_map(n_par), port_for=lambda k, ci=ci: port.scoped("build|%d|%s" % (ci, k)))
                jdump(built, {"at": time.time(), "nodes": len(tree.nodes), "levels": tree.levels, "top": len(tree.top), "build_calls": tree.calls,
                              "leaves": n_leaves, "seconds": round(time.time() - t0, 1)})
                print("conv %d tree: %d nodes, %d levels, top %d, build calls %d, counter %d" % (ci, len(tree.nodes), tree.levels, len(tree.top),
                                                                                              tree.calls, budget.count()), flush=True)
            view = et.view()
            env[ci] = {"view": view, "pview": arms.PermutedView(view), "dfn": et.direct(), "world": et.world}
        todo = [(n, q) for n, q in enumerate(qs, 1) if q.qid not in rows]

        def one(item) -> dict:
            n, q = item
            e = env[q.conv]
            t_start = time.time()
            qp = port.scoped(q.qid)
            tm = Timing(qp)
            tally = Tally(tm)
            tree = Rec(arms.TreeRetriever(tally, e["view"], conv_tenant(convs, q), CLS, "px", consts, workers=1), tm, tally)
            d = Rec(e["dfn"])
            sh = None
            if shuffled_n and n <= shuffled_n:
                tm2 = Timing(qp)
                tl2 = Tally(tm2)
                sh = Rec(arms.TreeRetriever(tl2, e["pview"], conv_tenant(convs, q), CLS, "px", consts, workers=1), tm2, tl2)
            o = arms.run_question(q.question, d, tree, e["world"], beta=0.0, shuffled_tree=sh)
            for rec in (tree, sh):
                if rec is not None and rec.last is not None and not rec.last.ok and rec.last.error == "BudgetExceeded":
                    raise CapReached("call cap reached during question %d" % n)
            row = {"qid": q.qid, "batch": a.batch, "order": n, "started": t_start, "finished": time.time(),
                   "arms": {k: {"leaves": v.leaves[:WINDOW], "failed": bool(v.failed)} for k, v in o.items() if k != "A0+LEN"},
                   "direct_wall_s": round(d.wall_s, 4), "tree_wall_s": round(tree.wall_s, 3), "tree_calls": tm.calls,
                   "tree_call_wall_s": round(tm.call_wall_s, 3), "holder_calls": tree.last.holder_calls, "fallbacks": tree.last.fallbacks,
                   "tree_ok": tree.last.ok, "tree_failed_calls": tally.counts()}
            if sh is not None:
                row["shuffled"] = {"wall_s": round(sh.wall_s, 3), "calls": sh.timing.calls, "call_wall_s": round(sh.timing.call_wall_s, 3),
                                   "fallbacks": sh.last.fallbacks, "ok": sh.last.ok, "failed_calls": sh.tally.counts()}
            return row

        def commit(_i, item, row) -> None:
            with open(done_path, "a") as f:
                f.write(json.dumps(row, sort_keys=True) + "\n")
                f.flush()
            rows[row["qid"]] = row
            print("question %d/%d done (%s) tree %.1fs, %d calls, counter %d" % (item[0], len(qs), row["qid"], row["tree_wall_s"], row["tree_calls"],
                                                                               budget.count()), flush=True)

        par.run_ordered(todo, one, commit, workers=n_par, stop=port.stop_requested, stop_exc=lambda: StageStopped("stop file present"))
    except StageStopped:
        state = "STOPPED"
    except (BudgetExceeded, CapReached):
        state = "CAP_REACHED"
    except StagePaused as e:
        state, extra = "PAUSED_THROTTLE", {"error": str(e)}
    except StageLimited as e:
        state, extra = "LIMIT_REACHED", {"error": str(e)}
    except (BuildFailed, PortRefused) as e:
        state, extra = "BUILD_FAILED", {"error": "%s: %s" % (type(e).__name__, e)}
    except Exception as e:
        write_state(run_dir, "ERROR", error=type(e).__name__, counter=budget.count())
        raise
    finally:
        for et in ets.values():
            et.close()
    extra.update(batch=a.batch, counter=budget.count(), cap=a.approved_cap, questions_done=len(rows), questions=len(qs), replayed=port.replayed,
                 fresh=port.fresh, lost_in_flight=port.lost, throttled=port.throttled, limited=port.limited,
                 models_reported=sorted(inner.models_reported), concurrency_now=port.slots.limit, concurrency_changes=port.slots.changes)
    rc = write_state(run_dir, state, **extra)
    print("state", state, json.dumps(extra), flush=True)
    return rc


def conv_tenant(convs, q: lo.LQuestion) -> str:
    return convs[q.conv].tenant()


# ---- command line -----------------------------------------------------------------------------------------------------------
def parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("prepare", "tree"):
        p = sub.add_parser(name)
        p.add_argument("--data", required=True)
        p.add_argument("--es-url", required=True)
        p.add_argument("--es-ca", required=True)
        p.add_argument("--es-pw-file", required=True)
        p.add_argument("--embed-dir", required=True)
        p.add_argument("--run-dir", required=True)
        p.add_argument("--threads", type=int, default=6)
        p.add_argument("--commit", default="")
        p.add_argument("--n-subset", type=int, default=DEFAULT_SUBSET)
        p.add_argument("--shuffled-n", type=int, default=DEFAULT_SHUFFLED)
    t = sub.choices["tree"]
    t.add_argument("--batch", choices=["B1", "B2"], default="B1")
    t.add_argument("--b2-size", type=int, default=B2_SIZE)
    t.add_argument("--binary", required=True)
    t.add_argument("--model", required=True)
    t.add_argument("--model-kind", default="claude", choices=["claude"])
    t.add_argument("--approved-cap", type=int, required=True)
    t.add_argument("--approval-ref", required=True)
    t.add_argument("--concurrency", type=int, default=6)
    t.add_argument("--max-retries", type=int, default=3)
    t.add_argument("--base-delay", type=float, default=30.0)
    return ap


def main(argv=None) -> int:
    a = parser().parse_args(argv)
    if a.cmd == "tree" and (a.approved_cap < 1 or not a.commit or not a.approval_ref.strip()):
        print("REFUSED: tree needs --approved-cap, --commit and --approval-ref", flush=True)
        return 2
    return prepare(a) if a.cmd == "prepare" else tree_stage(a)


if __name__ == "__main__":
    sys.exit(main())
