"""A0 sanity on real data with the real embedder and NO model call (a development check of the data path and its scale).

Ingests the haystacks of the pilot split through the real writer (real E5 vectors, the per-tenant fair queue), runs the flat hybrid
search (arm A0) only, and reports gold-session recall@10 on the pilot questions with gold. The figure is EXPLORATORY: pilot split
only, development only, never a confirmatory number and never a test-split number (`dataset.development_questions` refuses the test
split). Also reports what S2's planning needs: leaves per session, ingest time and throughput, embedding throughput, index sizes.

  python -m evalkit.a0_sanity --dataset F --es-url U --es-ca CA --es-pw-file PW --embed-dir D --run-dir R [--n-questions N]

No model is called: there is no model port in this module (a test checks the import list)."""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Dict, List, Optional

from living_context.config import Constants
from living_context.embed import e5

from . import arms, dataset as ds, stats
from .flat import A0_FIELDS_STRONG, A0_FIELDS_TEXT
from .score import question_scores


def index_sizes(admin, tenant: str) -> Dict[str, dict]:
    st, rows = admin.request("GET", f"/_cat/indices/lc-{tenant}-*?format=json&bytes=b&h=index,docs.count,store.size,pri.store.size")
    return {r["index"]: {"docs": int(r["docs.count"] or 0), "store_bytes": int(r["store.size"] or 0)}
            for r in (rows if st == 200 and isinstance(rows, list) else [])}


def main(argv: Optional[list] = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dataset", required=True)
    ap.add_argument("--es-url", required=True)
    ap.add_argument("--es-ca", required=True)
    ap.add_argument("--es-pw-file", required=True)
    ap.add_argument("--embed-dir", required=True)
    ap.add_argument("--run-dir", required=True)
    ap.add_argument("--tenant", default="a0san")
    ap.add_argument("--n-questions", type=int, default=0, help="0 = every pilot question")
    ap.add_argument("--threads", type=int, default=6)
    a = ap.parse_args(argv)

    from .eval_tenant import EvalTenant
    run_dir = Path(a.run_dir)
    run_dir.mkdir(parents=True, exist_ok=True)
    qs = ds.load_questions(a.dataset)
    pilot = ds.development_questions(qs, ("pilot",))
    if a.n_questions:
        pilot = pilot[:a.n_questions]
    hashes = ds.session_hashes(qs)
    sessions: Dict[str, tuple] = {}
    for q in pilot:
        for sid, turns in q.sessions:
            sessions.setdefault(hashes[sid], turns)
    emb = e5.E5Embedder(a.embed_dir, threads=a.threads)
    e5.require_real(emb, stage="A0-SANITY")
    pport, qport = e5.ports(emb, limit=2)
    consts = Constants()
    et = EvalTenant(a.es_url, a.es_ca, a.es_pw_file, a.tenant, run_dir / "tenant", consts=consts,
                    embed=pport.for_tenant(a.tenant), query_embed=qport.for_tenant(a.tenant))
    t0 = time.time()
    done = 0
    items = list(sessions.items())
    for i in range(0, len(items), 100):
        et.ingest(items[i:i + 100])
        done = min(len(items), i + 100)
        print("ingested %d/%d sessions, %d leaves, %.0fs, %d windows" % (done, len(items), len(et.leaves), time.time() - t0,
                                                                        emb.stats["windows"]), flush=True)
    ingest_s = time.time() - t0
    n_es = et.leaf_count_in_es()
    world = et.world
    per_session = {}
    for lid, h in et.leaf_session.items():
        per_session[h] = per_session.get(h, 0) + 1

    out: Dict[str, dict] = {}
    for label, fields in (("strong (text,title,entities)", A0_FIELDS_STRONG), ("text only", A0_FIELDS_TEXT)):
        direct = et.direct(fields=fields)
        outs, gold = {}, {}
        t1 = time.time()
        for q in pilot:
            if q.is_abstention or not q.gold_session_ids:
                continue
            gold[q.qid] = sorted({hashes[s] for s in q.gold_session_ids})
            d = direct(q.question)
            a0 = arms.a0_leaves(d)
            outs[q.qid] = arms.ArmOut(a0, arms.distinct_sessions(a0, world))
        sc, failed = question_scores(outs, gold)
        clusters = ds.content_clusters(qs, hashes)
        iv = stats.clustered_bootstrap({k: v for k, v in sc.items()}, clusters, resamples=2000) if len(sc) >= 2 else None
        out[label] = {"n_questions": len(sc), "recall_at_10_mean": sum(sc.values()) / len(sc) if sc else None,
                      "interval_95_clustered_bootstrap": [iv.lo, iv.hi] if iv else None, "failed": failed,
                      "search_seconds": round(time.time() - t1, 1)}
        print(label, json.dumps(out[label]), flush=True)
    sizes = index_sizes(et.admin, a.tenant)
    report = {"label": "EXPLORATORY: pilot split only, development only, decides nothing", "no_model_calls": True,
              "embedder": e5.MODEL_ID, "pilot_questions": len(pilot), "distinct_sessions": len(sessions), "leaves": len(et.leaves),
              "leaves_in_es": n_es, "leaves_per_session_mean": len(et.leaves) / max(1, len(sessions)),
              "leaves_per_session_max": max(per_session.values()) if per_session else 0,
              "session_leaf_counts": {h: n for h, n in sorted(per_session.items())},
              "ingest_seconds": round(ingest_s, 1), "ingest_leaves_per_second": round(len(et.leaves) / ingest_s, 2),
              "embed_stats": emb.stats, "index_sizes": sizes, "a0": out}
    (run_dir / "a0_sanity.json").write_text(json.dumps(report, indent=1, sort_keys=True))
    et.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
