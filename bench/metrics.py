"""Metrics of one evalkit run, recomputed from its ledgers (stdlib only; no model, no Elasticsearch).

    python -m bench.metrics <run-dir> [--calls FILE] [--json]

`<run-dir>/done.jsonl` is the per-question ledger written by evalkit.run_h1 / run_s6h1 (`s2_stage.Ledger`): one row per completed
question with `qref`, `gold` (opaque gold session refs) and `arms: {arm: {sessions: [top-10 session refs], failed: bool}}`.
`<run-dir>/calls.jsonl` (optional, or `--calls`) is the capped port's call log: one row per charged call with `task`,
`cost_usd`, `input_tokens` (plus cache fields), `output_tokens`, `latency_s`.

Per arm, over the questions that have gold (abstention rows, with an empty `gold`, are skipped exactly as `evalkit.score` does):
  recall@10      |gold in top 10| / |gold|          (the registered metric; `evalkit.arms.recall_at_k`)
  precision@10   |gold in top 10| / 10              (the denominator is the cut-off, not the number returned)
  all-gold@10    1 if every gold session is in the top 10, else 0
  MRR@10         1 / rank of the first gold session in the top 10, else 0
A recall that failed (`failed: true`) scores 0 on every metric and is counted in `failed` (INV-11): it is never dropped.

Per task kind, from the call log: calls, cost (sum over rows that report one; `cost_usd_rows` says how many did), tokens (input counts
the uncached, cache-creation and cache-read tokens together, as `s2_stage.summaries` does), latency median and p90.
"""
from __future__ import annotations

import argparse
import json
import math
import statistics
import sys
from pathlib import Path
from typing import Dict, Iterable, List, Optional

K = 10


def read_jsonl(path) -> List[dict]:
    """Rows of a JSON-lines file. A torn LAST line (an interrupted run) is ignored; a bad earlier line is an error."""
    lines = [l for l in Path(path).read_text().splitlines() if l.strip()]
    rows = []
    for i, line in enumerate(lines):
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            if i == len(lines) - 1:
                break
            raise ValueError("%s: line %d is not JSON" % (path, i + 1))
    return rows


def question_metrics(sessions: List[str], gold: List[str], k: int = K) -> Dict[str, float]:
    top, g = list(sessions)[:k], set(gold)
    hits = len(g & set(top))
    rank = next((i + 1 for i, s in enumerate(top) if s in g), None)
    return {"recall": hits / len(g), "precision": hits / k, "all_gold": float(hits == len(g)), "mrr": 1.0 / rank if rank else 0.0}


def arm_metrics(rows: Iterable[dict], k: int = K) -> Dict[str, dict]:
    acc: Dict[str, dict] = {}
    for r in rows:
        gold = r.get("gold") or []
        if not gold:                                  # abstention: nothing to retrieve, not scored
            continue
        for arm, out in (r.get("arms") or {}).items():
            a = acc.setdefault(arm, {"n": 0, "failed": 0, "recall": 0.0, "precision": 0.0, "all_gold": 0.0, "mrr": 0.0})
            a["n"] += 1
            if out.get("failed"):
                a["failed"] += 1
                continue
            for name, v in question_metrics(out.get("sessions") or [], gold, k).items():
                a[name] += v
    return {arm: {"n": a["n"], "failed": a["failed"],
                  "recall@%d" % k: a["recall"] / a["n"], "precision@%d" % k: a["precision"] / a["n"],
                  "all-gold@%d" % k: a["all_gold"] / a["n"], "mrr@%d" % k: a["mrr"] / a["n"]} for arm, a in sorted(acc.items())}


def _pct(sorted_values: List[float], q: float) -> float:
    return sorted_values[min(len(sorted_values) - 1, int(q * len(sorted_values)))]


def call_stats(rows: Iterable[dict]) -> Dict[str, dict]:
    by: Dict[str, dict] = {}
    for r in rows:
        b = by.setdefault(r.get("task", "?"), {"calls": 0, "cost": 0.0, "cost_rows": 0, "in": 0, "out": 0, "lat": []})
        b["calls"] += 1
        cost = r.get("cost_usd")
        if isinstance(cost, (int, float)) and not isinstance(cost, bool) and math.isfinite(cost):
            b["cost"] += cost
            b["cost_rows"] += 1
        b["in"] += sum(int(r.get(f) or 0) for f in ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"))
        b["out"] += int(r.get("output_tokens") or 0)
        b["lat"].append(float(r.get("latency_s") or 0.0))
    out = {}
    for task, b in sorted(by.items()):
        lat = sorted(b["lat"])
        out[task] = {"calls": b["calls"], "cost_usd": round(b["cost"], 6), "cost_usd_rows": b["cost_rows"],
                     "input_tokens": b["in"], "output_tokens": b["out"],
                     "latency_median_s": statistics.median(lat), "latency_p90_s": _pct(lat, 0.9)}
    return out


def render(arms: Dict[str, dict], calls: Optional[Dict[str, dict]], k: int = K) -> str:
    cols = ["recall@%d" % k, "precision@%d" % k, "all-gold@%d" % k, "mrr@%d" % k]
    lines = ["%-14s %5s %6s " % ("arm", "n", "failed") + " ".join("%12s" % c for c in cols)]
    for arm, m in arms.items():
        lines.append("%-14s %5d %6d " % (arm, m["n"], m["failed"]) + " ".join("%12.4f" % m[c] for c in cols))
    if calls is not None:
        lines += ["", "%-16s %6s %10s %12s %12s %9s %9s" % ("task", "calls", "cost_usd", "input_tok", "output_tok", "lat_p50_s", "lat_p90_s")]
        for task, c in calls.items():
            note = "" if c["cost_usd_rows"] == c["calls"] else "  (cost on %d of %d calls)" % (c["cost_usd_rows"], c["calls"])
            lines.append("%-16s %6d %10.4f %12d %12d %9.2f %9.2f%s" % (task, c["calls"], c["cost_usd"], c["input_tokens"], c["output_tokens"],
                                                                      c["latency_median_s"], c["latency_p90_s"], note))
    return "\n".join(lines)


def main(argv: Optional[list] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("run_dir")
    ap.add_argument("--calls", help="call log (default: <run-dir>/calls.jsonl when it exists)")
    ap.add_argument("--json", action="store_true", help="print one JSON document instead of tables")
    a = ap.parse_args(argv)
    run = Path(a.run_dir)
    done = run / "done.jsonl"
    if not done.exists():
        print("no done.jsonl in %s" % run, file=sys.stderr)
        return 2
    arms = arm_metrics(read_jsonl(done))
    calls_path = Path(a.calls) if a.calls else run / "calls.jsonl"
    calls = call_stats(read_jsonl(calls_path)) if calls_path.exists() else None
    if a.json:
        print(json.dumps({"arms": arms, "calls_by_task": calls}, indent=1, sort_keys=True))
    else:
        print(render(arms, calls))
    return 0


if __name__ == "__main__":
    sys.exit(main())
