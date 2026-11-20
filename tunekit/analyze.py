"""Aggregate a run directory's cell files into the small tables that are committed: curves by size and complexity, fall points by a
pre-stated rule, signal tables with AUC intervals, memory-signal crossings. Aggregates only: no question or leaf text.

  python -m tunekit.analyze --run DIR [--run DIR2] --out OUTDIR [--resamples N]
"""
from __future__ import annotations

import argparse
import csv
import gzip
import json
from collections import defaultdict
from pathlib import Path
from typing import Dict, List

import numpy as np

from . import grid, signals
from .stats import percentile, wilson


def load_cells(runs: List[Path]) -> List[dict]:
    out = []
    for run in runs:
        for f in sorted((run / "cells").glob("*.json")):
            d = json.loads(f.read_text())
            d["_run"] = str(run)
            out.append(d)
    return out


def group_key(name: str):
    c = grid.Cell.parse(name)
    return (c.growth, c.level, c.over)


def curve_rows(cells: List[dict], tag: str) -> List[dict]:
    """One row per (growth, level, overrides, size), pooled over seeds; pooled Wilson intervals (questions treated as independent
    given the corpus: optimistic, so the per-seed spread is reported beside)."""
    g: Dict[tuple, List[dict]] = defaultdict(list)
    for d in cells:
        c = grid.Cell.parse(d["cell"])
        g[(c.growth, c.level, c.over, c.n)].append(d)
    rows = []
    for (growth, level, over, n), ds in sorted(g.items()):
        row = {"set": tag, "growth": growth, "level": level, "overrides": ";".join(f"{k}={v}" for k, v in over), "n_leaves": n,
               "seeds": len(ds), "n_questions": sum(d["n_questions"] for d in ds)}
        for k in ("hit1", "hit5", "hit10", "bm25_hit10", "knn_hit10"):
            kk, nn = sum(d[k]["k"] for d in ds), sum(d[k]["n"] for d in ds)
            p, lo, hi = wilson(kk, nn)
            row[k] = round(p, 4)
            if k in ("hit10", "hit1"):
                row[k + "_lo"], row[k + "_hi"] = round(lo, 4), round(hi, 4)
        sd = [d["set"] for d in ds if "set" in d]
        if sd:
            ka, na = sum(x["all10"]["k"] for x in sd), sum(x["all10"]["n"] for x in sd)
            pa, la, ha = wilson(ka, na)
            ki, ni = sum(x["found10"] for x in sd), sum(x["gold_items"] for x in sd)
            pi, li, hi_ = wilson(ki, ni)
            row.update({"set_n": na, "set_mean_gold": round(ni / na, 3), "set_all10": round(pa, 4), "set_all10_lo": round(la, 4),
                        "set_all10_hi": round(ha, 4), "set_item_recall10": round(pi, 4), "set_item_recall10_lo": round(li, 4),
                        "set_item_recall10_hi": round(hi_, 4)})
        per_seed = [d["hit10"]["p"] for d in ds]
        row["hit10_seed_min"], row["hit10_seed_max"] = round(min(per_seed), 4), round(max(per_seed), 4)
        row["mrr10"] = round(float(np.mean([d["mrr10"] for d in ds])), 4)
        sf_k, sf_n = sum(d["stale_first"]["k"] for d in ds), sum(d["stale_first"]["n"] for d in ds)
        row["stale_first"] = round(sf_k / sf_n, 4) if sf_n else ""
        row["lat_p50_ms"] = round(float(np.median([d["latency_ms"]["p50"] for d in ds])), 2)
        row["lat_p95_ms"] = round(float(np.median([d["latency_ms"]["p95"] for d in ds])), 2)
        row["took_p50_ms"] = round(float(np.median([d["took_ms"]["p50"] for d in ds])), 2)
        row["took_p95_ms"] = round(float(np.median([d["took_ms"]["p95"] for d in ds])), 2)
        row["knn_exact_overlap10"] = round(float(np.mean([d["knn_exact_check"]["mean_top10_overlap_with_exact"] for d in ds])), 4)
        row["knn_max_cos_err"] = max(d["knn_exact_check"]["max_abs_cosine_error"] for d in ds)
        row["question_drop_rate"] = round(float(np.mean([d["question_drop_rate"] for d in ds])), 4)
        row["ingest_leaves_per_s_median"] = round(float(np.median([d["ingest"]["leaves_per_s"] for d in ds])), 2)
        row["index_bytes_per_leaf"] = round(float(np.median([d["ingest"]["leaf_index_bytes"] / n for d in ds])), 1)
        rows.append(row)
    return rows


def fall_points(rows: List[dict], metric: str = "hit10") -> List[dict]:
    """Pre-stated rule (docs/tuning/RESULTS-Z2.md): per series, the smallest size whose pooled recall@10 Wilson upper bound is below
    the Wilson lower bound of the series' smallest size (a separated fall), and the first size whose point estimate is under 0.95 and
    under 0.90 (assumed practical bars)."""
    s: Dict[tuple, List[dict]] = defaultdict(list)
    for r in rows:
        s[(r["set"], r["growth"], r["level"], r["overrides"])].append(r)
    out = []
    for key, rs in sorted(s.items()):
        rs.sort(key=lambda r: r["n_leaves"])
        base = rs[0]
        if metric not in base or f"{metric}_lo" not in base:
            continue
        sep = next((r["n_leaves"] for r in rs[1:] if r[metric + "_hi"] < base[metric + "_lo"]), None)
        b95 = next((r["n_leaves"] for r in rs if r[metric] < 0.95), None)
        b90 = next((r["n_leaves"] for r in rs if r[metric] < 0.90), None)
        out.append({"metric": metric, "set": key[0], "growth": key[1], "level": key[2], "overrides": key[3], "smallest_size": base["n_leaves"],
                    "smallest_value": base[metric], "separated_fall_at": sep, "first_below_0.95": b95, "first_below_0.90": b90,
                    "largest_size": rs[-1]["n_leaves"], "largest_value": rs[-1][metric]})
    return out


def latency_rows(cells: List[dict]) -> List[dict]:
    """Quiet-run latency: per cell with several passes, the median and range over passes of the wall and server-side p50 and p95."""
    out = []
    for d in cells:
        ps = d.get("latency_passes")
        if not ps:
            continue
        c = grid.Cell.parse(d["cell"])
        row = {"cell": d["cell"], "n_leaves": c.n, "passes": len(ps), "index_bytes": d["ingest"]["leaf_index_bytes"]}
        for k in ("wall_p50", "wall_p95", "took_p50", "took_p95"):
            v = [p[k] for p in ps]
            row[k + "_median"], row[k + "_min"], row[k + "_max"] = round(float(np.median(v)), 2), min(v), max(v)
        out.append(row)
    return sorted(out, key=lambda r: r["n_leaves"])


def load_rows(run: Path, names: List[str]) -> List[dict]:
    rows = []
    for n in names:
        with gzip.open(run / "rows" / (n + ".jsonl.gz"), "rt") as f:
            rows += [json.loads(l) for l in f]
    return rows


def signal_tables(cells: List[dict], resamples: int) -> List[dict]:
    """AUC of each per-query feature against `gold not in the fused top 10`, per (run, growth, level, size) pooled over seeds, with
    cluster-bootstrap intervals; NOT ESTABLISHED below 30 positives or 30 negatives."""
    g: Dict[tuple, List[dict]] = defaultdict(list)
    for d in cells:
        c = grid.Cell.parse(d["cell"])
        if c.over:
            continue
        g[(d["_run"], c.growth, c.level, c.n)].append(d)
    out = []
    pooled: Dict[tuple, List[dict]] = defaultdict(list)
    for (run, growth, level, n), ds in sorted(g.items(), key=lambda kv: (kv[0][1], kv[0][2], kv[0][3])):
        rows = load_rows(Path(run), [d["cell"] for d in ds])
        for r in rows:
            r["n_leaves"] = n
        pooled[(growth, level)] += rows
        pooled[(growth, "all")] += rows
        for which in ("point", "set"):
            sub = [r for r in rows if (r["kind"] == "set") == (which == "set")]
            if not sub:
                continue
            p = signals.predictive_power(sub, resamples=resamples)
            out.append({"questions": which, "growth": growth, "level": level, "n_leaves": n, "seeds": len(ds), **p})
    for (growth, level), rows in sorted(pooled.items(), key=lambda kv: (kv[0][0], str(kv[0][1]))):
        for which in ("point", "set"):
            sub = [r for r in rows if (r["kind"] == "set") == (which == "set")]
            if not sub:
                continue
            p = signals.predictive_power(sub, resamples=resamples)
            out.append({"questions": which, "growth": growth, "level": level, "n_leaves": "all sizes", "seeds": "pooled", **p})
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", action="append", required=True)
    ap.add_argument("--tag", action="append", help="label per --run, same order")
    ap.add_argument("--out", required=True)
    ap.add_argument("--resamples", type=int, default=2000)
    ap.add_argument("--signals", action="store_true")
    a = ap.parse_args(argv)
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    rows = []
    cells_all = []
    fieldnames = None
    for i, r in enumerate(a.run):
        tag = a.tag[i] if a.tag else Path(r).name
        cells = load_cells([Path(r)])
        cells_all += cells
        rows += curve_rows(cells, tag)
    if rows:
        names = list(rows[0])
        for r in rows:
            names += [k for k in r if k not in names]
        with open(out / "curves.csv", "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=names, restval="")
            w.writeheader()
            w.writerows(rows)
        fp = fall_points(rows, "hit10") + fall_points(rows, "set_all10") + fall_points(rows, "set_item_recall10")
        with open(out / "fall_points.csv", "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(fp[0]))
            w.writeheader()
            w.writerows(fp)
    lat = latency_rows(cells_all)
    if lat:
        with open(out / "latency_quiet.csv", "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(lat[0]))
            w.writeheader()
            w.writerows(lat)
    mem = [{"cell": d["cell"], **d["memory_signals"], **d["crossings"]} for d in cells_all]
    (out / "memory_signals.json").write_text(json.dumps(mem, indent=1))
    chk = [{"cell": d["cell"], **d["product_path_check"]} for d in cells_all]
    (out / "product_path_check.json").write_text(json.dumps(chk, indent=1))
    if a.signals:
        (out / "signal_auc.json").write_text(json.dumps(signal_tables(cells_all, a.resamples), indent=1, default=float))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
