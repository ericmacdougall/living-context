"""Run one grid cell end to end and write its aggregates and per-question rows under the run directory.

  python -m tunekit.run_grid --run DIR --es-url URL --ca CA --pw-file FILE --cells @cells.txt --workers 3

Stage A (`embed_cells`) must have produced the corpus and vectors. No model call is made anywhere."""
from __future__ import annotations

import gzip
import hashlib
import json
import time
from pathlib import Path
from typing import Optional

import numpy as np

from . import baseline, grid, signals
from .stats import percentile, wilson


def tenant_id(cell_name: str) -> str:
    return "tk" + hashlib.sha1(cell_name.encode()).hexdigest()[:8]


def commit_stamp() -> str:
    """The commit the code tree was exported from (a file written by the export step), or "unstamped"."""
    f = Path(__file__).resolve().parent.parent / "COMMIT"
    return f.read_text().strip() if f.exists() else "unstamped"


def run_cell(env: baseline.Env, name: str, run: Path, *, check_n: int = 20, passes: int = 1) -> dict:
    out_json = run / "cells" / (name + ".json")
    if out_json.exists():
        return json.loads(out_json.read_text())
    corpus = grid.load_corpus(run / "corp" / (name + ".json.gz"))
    P, Q = grid.load_vectors(run / "emb" / (name + ".npz"))
    leaves, questions = corpus.leaves, corpus.questions
    vec = {L["text"]: P[i].tolist() for i, L in enumerate(leaves)}
    t = baseline.Tenant(env, tenant_id(name), vec)
    try:
        ing = baseline.ingest(t, leaves)
        doc_ids = ing.pop("doc_ids")
        if ing["refused"] or ing["docs"] != len(leaves):
            raise RuntimeError("ingest incomplete: refused=%s docs=%s of %s" % (ing["refused"], ing["docs"], len(leaves)))
        all_rows = baseline.run_queries(t, questions, Q, passes=passes)
        rows = all_rows[0]
        chk = baseline.check_against_product(t, questions, Q, rows, n=check_n)
        exact = baseline.exact_knn_check(t, P, doc_ids, Q, rows)
        mapping = baseline.embedding_mapping(t)
        scored = baseline.score(questions, rows, doc_ids)
    finally:
        t.close()
    session_of = {doc_ids[i]: L["session"] for i, L in enumerate(leaves)}
    per_q = []
    for q, r, s in zip(questions, rows, scored):
        f = signals.query_features(r.bm25, r.knn, r.knn_cos, r.fused, session_of)
        per_q.append({"cell": name, "qid": q["qid"], "cluster": f"{name}:{q['topic']}", "kind": q["kind"], "para": q["para"],
                      "alias": q["alias"], "nw": (len(q["text"].split(" regarding ")[1].rstrip("?").split()) if " regarding " in q["text"] else 0), "hit1": s["hit1"], "hit5": s["hit5"], "hit10": s["hit10"], "rr": s["rr"],
                      "rank": s["rank"], "bm25_hit10": s["bm25_hit10"], "knn_hit10": s["knn_hit10"],
                      "stale_first": s["stale_first"], "has_stale": s["has_stale"], "ms": r.ms, **f})
    n = len(per_q)
    agg = {"cell": name, "knobs": corpus.manifest["knobs"], "stats": corpus.manifest["stats"], "n_questions": n,
           "dropped_ambiguous": corpus.manifest["questions_dropped_ambiguous"], "content_sha256": corpus.manifest["content_sha256"]}
    for k in ("hit1", "hit5", "hit10", "bm25_hit10", "knn_hit10"):
        kk = sum(1 for r in per_q if r[k])
        p, lo, hi = wilson(kk, n)
        agg[k] = {"k": kk, "n": n, "p": round(p, 4), "lo": round(lo, 4), "hi": round(hi, 4)}
    agg["mrr10"] = round(float(np.mean([r["rr"] for r in per_q])), 4)
    st = [r for r in per_q if r["has_stale"]]
    agg["stale_first"] = {"k": sum(r["stale_first"] for r in st), "n": len(st)}
    ms = [r["ms"] for r in per_q]
    tk = [r.took for r in rows]
    agg["latency_ms"] = {"p50": round(percentile(ms, 50), 2), "p95": round(percentile(ms, 95), 2), "mean": round(float(np.mean(ms)), 2),
                         "n": len(ms), "what": "client wall time of one _msearch through the product's EsClient (a new TLS connection per request)"}
    agg["took_ms"] = {"p50": round(percentile(tk, 50), 2), "p95": round(percentile(tk, 95), 2), "n": len(tk),
                      "what": "Elasticsearch's own took for the _msearch (server side)"}
    if passes > 1:
        agg["latency_passes"] = [{"wall_p50": round(percentile([r.ms for r in rs], 50), 2), "wall_p95": round(percentile([r.ms for r in rs], 95), 2),
                                  "took_p50": round(percentile([r.took for r in rs], 50), 2), "took_p95": round(percentile([r.took for r in rs], 95), 2)}
                                 for rs in all_rows]
    agg["knn_exact_check"] = exact
    agg["embedding_mapping"] = mapping
    agg["meta"] = {"stage": "Z2", "commit": commit_stamp(), "model_port_calls": 0, "interval": "Wilson 95% (rates); cluster bootstrap seed 16096 (AUC)",
                   "embedder": "multilingual-e5-small@614241f622f53c4eeff9890bdc4f31cfecc418b3", "arm": "plain hybrid retrieval (flat)", "passes": passes}
    agg["question_drop_rate"] = corpus.manifest.get("question_drop_rate")
    agg["ingest"] = {k: (round(v, 3) if isinstance(v, float) else v) for k, v in ing.items()}
    agg["product_path_check"] = chk
    agg["memory_signals"] = signals.memory_signals(leaves)
    agg["crossings"] = signals.crossings(agg["memory_signals"])
    agg["finished"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    (run / "rows").mkdir(parents=True, exist_ok=True)
    with gzip.open(run / "rows" / (name + ".jsonl.gz"), "wt") as f:
        for r in per_q:
            f.write(json.dumps(r) + "\n")
    out_json.parent.mkdir(parents=True, exist_ok=True)
    out_json.write_text(json.dumps(agg, indent=1))
    return agg
