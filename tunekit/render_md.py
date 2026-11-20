"""Turn the analysis outputs (curves.csv, fall_points.csv, signal_auc.json, latency_quiet.csv, memory_signals.json) into markdown
tables, so numbers in the results note are copied by a program and not by hand.

  python -m tunekit.render_md --in OUTDIR --out FILE.md
"""
from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path


def rd(path):
    with open(path) as f:
        return list(csv.DictReader(f))


def f(x, nd=3):
    try:
        return f"{float(x):.{nd}f}"
    except (TypeError, ValueError):
        return str(x) if x not in (None, "") else "-"


def interval(r, k):
    return f"{f(r.get(k))} ({f(r.get(k + '_lo'))} to {f(r.get(k + '_hi'))})" if r.get(k + "_lo") not in (None, "") else f(r.get(k))


def table(head, rows):
    out = ["| " + " | ".join(head) + " |", "|" + "|".join("---" for _ in head) + "|"]
    out += ["| " + " | ".join(str(c) for c in r) + " |" for r in rows]
    return "\n".join(out)


def main_curves(rows, growth, kind):
    sel = [r for r in rows if r["growth"] == growth and not r["overrides"]]
    sel.sort(key=lambda r: (float(r["level"]), int(r["n_leaves"])))
    if kind == "point":
        head = ["level", "leaves", "seeds", "questions", "recall@10 (95% Wilson)", "recall@1", "MRR@10", "BM25 alone @10", "kNN alone @10", "stale first", "seed range of recall@10"]
        body = [[r["level"], r["n_leaves"], r["seeds"], r["n_questions"], interval(r, "hit10"), f(r["hit1"]), f(r["mrr10"]), f(r["bm25_hit10"]), f(r["knn_hit10"]),
                 f(r["stale_first"]), f"{f(r['hit10_seed_min'])} to {f(r['hit10_seed_max'])}"] for r in sel]
    else:
        head = ["level", "leaves", "seeds", "set questions", "mean gold leaves", "all gold in top 10 (95% Wilson)", "item recall@10 (95% Wilson)"]
        body = [[r["level"], r["n_leaves"], r["seeds"], r.get("set_n", "-"), f(r.get("set_mean_gold")), interval(r, "set_all10"),
                 interval(r, "set_item_recall10")] for r in sel if r.get("set_n")]
    return table(head, body)


def contrasts(rows):
    sel = [r for r in rows if r["overrides"]]
    sel.sort(key=lambda r: r["overrides"])
    head = ["override (on c = 0.5, 4,000 leaves)", "seeds", "recall@10", "recall@1", "MRR@10", "set all10", "set item recall@10"]
    return table(head, [[r["overrides"], r["seeds"], interval(r, "hit10"), f(r["hit1"]), f(r["mrr10"]), f(r.get("set_all10")), f(r.get("set_item_recall10"))] for r in sel])


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--in", dest="inp", required=True)
    ap.add_argument("--out", required=True)
    a = ap.parse_args(argv)
    d = Path(a.inp)
    rows = rd(d / "curves.csv")
    parts = []
    for growth in ("wide", "deep"):
        if any(r["growth"] == growth and not r["overrides"] for r in rows):
            parts += [f"### Point questions, {growth} growth", main_curves(rows, growth, "point"), f"### Set questions, {growth} growth", main_curves(rows, growth, "set")]
    if any(r["overrides"] for r in rows):
        parts += ["### Knob contrasts", contrasts(rows)]
    if (d / "fall_points.csv").exists():
        fp = rd(d / "fall_points.csv")
        head = ["metric", "growth", "level", "smallest size", "value there", "separated fall at", "first below 0.95", "first below 0.90", "largest size", "value there"]
        parts += ["### Fall points (rule in the pre-statement)", table(head, [[r["metric"], r["growth"], r["level"], r["smallest_size"], f(r["smallest_value"]), r["separated_fall_at"] or "none",
                                                                          r["first_below_0.95"] or "none", r["first_below_0.90"] or "none", r["largest_size"], f(r["largest_value"])]
                                                                         for r in fp if not r["overrides"]])]
    if (d / "latency_quiet.csv").exists():
        la = rd(d / "latency_quiet.csv")
        head = ["leaves", "index bytes", "passes", "wall p50 ms (min to max)", "wall p95 ms", "server took p50 ms (min to max)", "server took p95 ms"]
        parts += ["### Latency, quiet run", table(head, [[r["n_leaves"], r["index_bytes"], r["passes"], f"{r['wall_p50_median']} ({r['wall_p50_min']} to {r['wall_p50_max']})",
                                                          r["wall_p95_median"], f"{r['took_p50_median']} ({r['took_p50_min']} to {r['took_p50_max']})", r["took_p95_median"]] for r in la])]
    if (d / "signal_auc.json").exists():
        sg = json.loads((d / "signal_auc.json").read_text())
        keys = ["agree (low = hard)", "margin (low = hard)", "spread (high = hard)", "rule not flat-sufficient (placeholders)"]
        head = ["questions", "level", "leaves", "n", "failure rate", "positives"] + [k.split(" (")[0] + " AUC (95%)" for k in keys] + ["share flat-sufficient"]

        def cell(v):
            return f"{v['auc']:.3f} ({v['lo']:.3f} to {v['hi']:.3f})" if v.get("auc") is not None else "NOT ESTABLISHED"
        body = []
        for r in sg:
            body.append([r["questions"], r["level"], r["n_leaves"], r["n"], f(r["failure_rate"]), r[keys[0]]["n_pos"]] + [cell(r[k]) for k in keys] + [f(r["share_flat_sufficient"])])
        parts += ["### Signal AUC against failure", table(head, body)]
        qh = ["questions", "level", "leaves", "agree q05 / q50 / q95", "margin q05 / q50 / q95", "spread q05 / q50 / q95"]
        qb = []
        for r in sg:
            if r["n_leaves"] == "all sizes":
                qb.append([r["questions"], r["level"], r["n_leaves"]] + [" / ".join(f(r[ft + "_quantiles"][q], 3) for q in ("q05", "q50", "q95")) for ft in ("agree", "margin", "spread")])
        parts += ["### Feature quantiles (all sizes pooled)", table(qh, qb)]
    Path(a.out).write_text("\n\n".join(parts) + "\n")


if __name__ == "__main__":
    main()
