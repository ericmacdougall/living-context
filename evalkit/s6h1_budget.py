"""The S6-H1 call estimate against the approved cap (EXPERIMENT 10, 10.3; approved hard cap 35,574). No model call, no dataset text.

  python3 -I -m evalkit.s6h1_budget --leaves N --rates-from <S2 run dir> [<S3 run dir> ...] [--cap 35574] [--json OUT]

Calls of the stage = build + recalls. Build is exact for the leaf count (one level-1 call per group of at most F_TARGET leaves, upper
levels until the top has at most F_MAX nodes, `s2_budget.tree_shape`), plus at most one more upper level that the token term of the
index rule could add. Recalls: A1 (A1-tree-only is the same recall) on every fresh question, and SHUFFLED-SUMMARIES on 50; A0 and A0+LEN
make no model call.

  * expected: build + recalls x the MEASURED route-and-organise calls per tree recall of the earlier stages (S2 measured 67.1);
  * top: build + recalls x the CODE BOUND per recall (every top holder is asked, at most B_HOLDERS_EXTRA more holder calls, at most
    B_ORGANISE organise calls; the top never has more than F_MAX nodes, so the bound is F_MAX + 36 + 12 = 72 whatever the tree's shape;
    the measured rate is used instead if it were ever higher), plus allowances for build retries (each failed build call is asked again
    up to twice), throttle retries (each a charged call) and allowance refusals (the refused call is asked again on resume), at the 95%
    Wilson upper bound of the rates measured in the earlier stages. With `concurrency` N the throttle and refusal allowances are
    multiplied by N: one throttle or refusal can meet every call then in flight (the shared gate stops further charges, not those
    already made). A call lost in flight is never asked again, so it adds nothing.

`refusal` returns the reason to refuse when the top exceeds the cap: the run then charges nothing."""
from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path
from typing import Dict, List, Optional, Sequence

from living_context.config import Constants
from living_context.structure import cluster

from . import s2_budget, stats

APPROVED_CAP = 35_574                 # the owner's S6-H1 cap (2026-11-19)
N_QUESTIONS = 312                     # A1 on every fresh question (abstention included, EXPERIMENT 10.3)
SHUFFLED_N = 50
_BIN = stats.Measure("call failed or throttled", "binary")


def measured_rates(run_dirs: Sequence) -> dict:
    """Per-recall route and organise calls, and the build-failure and throttle shares, from the call logs and ledgers of earlier stages
    (calls.jsonl rows carry the task kind; done.jsonl has one row per question, with a SHUFFLED arm when that recall ran)."""
    route = organise = recalls = build = build_failed = calls = throttled = limited = 0
    for d in run_dirs:
        d = Path(d)
        for line in (d / "calls.jsonl").read_text().splitlines():
            if not line.strip():
                continue
            r = json.loads(line)
            calls += 1
            throttled += bool(r.get("throttled"))
            limited += bool(r.get("limited"))
            t = r.get("task")
            if t == "route":
                route += 1
            elif t == "organise":
                organise += 1
            elif t in ("build_l1", "build_upper"):
                build += 1
                build_failed += bool(r.get("error")) or r.get("schema_ok") is False
        for line in (d / "done.jsonl").read_text().splitlines():
            if line.strip():
                row = json.loads(line)
                recalls += 1 + ("SHUFFLED" in row.get("arms", {}))
    if not recalls or not calls:
        raise ValueError("no measured recalls or calls in %s" % [str(x) for x in run_dirs])
    up = lambda k, n: stats.wilson(_BIN, k, n)[1] if n else 1.0
    return {"recalls": recalls, "route": route, "organise": organise, "per_recall": (route + organise) / recalls, "calls": calls,
            "build_calls": build, "build_failed": build_failed, "build_fail_upper": up(build_failed, build),
            "throttled": throttled, "throttle_upper": up(throttled, calls), "limited": limited, "limit_upper": up(limited, calls)}


def estimate(n_leaves: int, rates: dict, *, n_questions: int = N_QUESTIONS, shuffled_n: int = SHUFFLED_N,
             consts: Optional[Constants] = None, concurrency: int = 1) -> dict:
    c = consts or Constants()
    n_par = max(1, int(concurrency))
    shape = s2_budget.tree_shape(n_leaves, c)
    build = shape["l1_calls"] + shape["upper_calls"]
    build_top = build + cluster.group_count(c.f_max, c.f_target)        # one more upper level from the token term, at most
    recalls = n_questions + shuffled_n
    band = s2_budget.recall_band(shape["top"], shape["levels"], c)
    code_bound = max(band["total_max"], c.f_max + c.b_holders_extra + c.b_organise)
    per_top = max(code_bound, rates["per_recall"])
    base_top = build_top + recalls * per_top
    build_retry = math.ceil(2 * build_top * rates["build_fail_upper"])
    throttle = n_par * math.ceil(base_top * rates["throttle_upper"])
    refused = n_par * math.ceil(base_top * rates.get("limit_upper", 0.0))
    return {"concurrency": n_par, "allowance_refused_reasked": refused, "leaves": n_leaves, "tree": shape, "build_calls": build, "build_calls_top": build_top, "recalls": recalls,
            "per_recall_measured": rates["per_recall"], "per_recall_code_bound": code_bound,
            "expected": round(build + recalls * min(rates["per_recall"], per_top)),
            "top": base_top + build_retry + throttle + refused, "allowance_build_retry": build_retry, "allowance_throttle": throttle}


def refusal(est: dict, cap: int) -> List[str]:
    if est["top"] > cap:
        return ["the call estimate's top %d exceeds the approved cap %d (expected %d): the stage is not started" % (est["top"], cap,
                                                                                                                 est["expected"])]
    return []


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--leaves", type=int, required=True)
    ap.add_argument("--rates-from", nargs="+", required=True)
    ap.add_argument("--cap", type=int, default=APPROVED_CAP)
    ap.add_argument("--concurrency", type=int, default=1)
    ap.add_argument("--json", default="")
    a = ap.parse_args(argv)
    est = estimate(a.leaves, measured_rates(a.rates_from), concurrency=a.concurrency)
    print(json.dumps(est, indent=1, sort_keys=True))
    why = refusal(est, a.cap)
    print("REFUSED: " + why[0] if why else "within the cap %d (top %d, expected %d)" % (a.cap, est["top"], est["expected"]))
    if a.json:
        Path(a.json).write_text(json.dumps(est, indent=1, sort_keys=True))
    return 2 if why else 0


if __name__ == "__main__":
    sys.exit(main())
