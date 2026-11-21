"""The exact call-count budget of stage S2 (H1 arms) from the REAL leaf counts of the registered question set. Makes no model call, needs
no Elasticsearch and no embedder: leaf counts depend only on the segmenter, and the number of build calls only on the leaf count (the
tree is balanced to a fixed fan-out, `cluster.group_count`), so both are computed exactly.

  python3 -I -m evalkit.s2_budget --dataset FILE [--n-questions 30] [--json OUT]

What is exact and what is a bound:
  * build calls: exact for the count rule (one level-1 call per group of at most F_TARGET leaves, upper levels until the top has at most
    F_MAX nodes; one rule not modelled, the token term of `too_big`, which could only ADD an upper level if the summaries were
    implausibly long); every failed build call may be retried up to twice (a new charged call), shown as a separate allowance;
  * route and organise calls per recall: bounded exactly by the fan-out code: every top holder is asked (T route calls at least), at
    most T + B_HOLDERS_EXTRA holder calls in all, and at most B_ORGANISE organise calls. Where in that band a recall lands depends on
    what the model selects, so the S2 total is a band, not a number, and the band is stated per arm;
  * A1 and A1-tree-only share ONE tree recall per question; SHUFFLED is a second recall per question. A0 makes no model call.
Prompt sizes of the level-1 build calls are computed from the real leaf text (the sum is exact; the per-call figure is the mean, since
which leaves share a group depends on the embeddings)."""
from __future__ import annotations

import argparse
import json
import math
import sys
from typing import Dict, List, Optional

from living_context.config import Constants
from living_context.ingest import title as title_mod
from living_context.structure import cluster

from . import claude_cli, corpus, dataset as ds

REGISTERED_S2 = (2391, 5454)           # EXPERIMENT 10.3
# mean latency per call kind measured in S1 (EXPERIMENT 14.2), seconds: a FLOOR for real-size prompts
LATENCY_S = {"route": 2.54, "organise": 3.80, "build_l1": 10.21, "build_upper": 3.00}
# mean total input and output tokens per call measured in S1 (EXPERIMENT 14.2; fixture-sized inputs, so lower bounds for input)
MEASURED_TOKENS = {"route": (2865, 98), "organise": (1255, 404), "build_l1": (2415, 1724), "build_upper": (2090, 176)}
REGISTERED_PARTS = {"build": (381, 574), "edges": (330, 1100), "A1 recalls": (750, 1800), "SHUFFLED recalls": (750, 1800), "judge": (180, 180)}


def pick(qs, n):
    from .run_h1 import pick_questions
    return pick_questions(ds.development_questions(qs, ("pilot",)), n)


def tree_shape(n_leaves: int, c: Constants) -> dict:
    """Build calls and the top width of the tree `build.build_tree` makes over n_leaves leaves (count rule only)."""
    level = cluster.group_count(n_leaves, c.f_target)
    calls_l1, upper, depth, widths = level, 0, 1, [level]
    prev = level
    while level > c.f_max and level > 1:
        k = cluster.group_count(level, c.f_target)
        upper += k
        prev, level, depth = level, k, depth + 1
        widths.append(level)
    top = level
    if level == 1 and depth >= 2:       # a single upper node is dropped: its children are the top
        top, depth = prev, depth - 1
    return {"l1_calls": calls_l1, "upper_calls": upper, "levels": depth, "top": top, "level_widths": widths}


def recall_band(top: int, levels: int, c: Constants) -> dict:
    """Route and organise calls of ONE recall. Every top holder is asked; when the top holders' children are nodes (levels >= 2)
    expansion adds at most B_HOLDERS_EXTRA more holder calls. Leaves at level 1 cannot be expanded."""
    extra = c.b_holders_extra if levels >= 2 else 0
    return {"route_min": top, "route_max": top + extra, "organise_min": 0, "organise_max": c.b_organise,
            "total_min": top, "total_max": top + extra + c.b_organise}


def main(argv: Optional[list] = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dataset", required=True)
    ap.add_argument("--n-questions", type=int, default=30)
    ap.add_argument("--json", default="")
    a = ap.parse_args(argv)
    c = Constants()
    qs = ds.load_questions(a.dataset)
    dev = pick(qs, a.n_questions)
    hashes = ds.session_hashes(qs)
    sessions: Dict[str, tuple] = {}
    for q in dev:
        for sid, turns in q.sessions:
            sessions.setdefault(hashes[sid], turns)
    texts: List[str] = []
    for h, turns in sessions.items():
        texts += corpus.session_leaf_texts(turns, c.leaf_cap)
    n = len(texts)
    shape = tree_shape(n, c)
    band = recall_band(shape["top"], shape["levels"], c)
    nq = len(dev)
    build_calls = shape["l1_calls"] + shape["upper_calls"]
    recalls = 2 * nq                                    # A1 (shared with A1-tree-only) and SHUFFLED
    lo = build_calls + recalls * band["total_min"]
    hi = build_calls + recalls * band["total_max"]
    retry_allow = build_calls * 2                       # worst case: every build call fails twice (a new charged call each)
    # level-1 prompt sizes from the real leaf text: the sum over all groups is exact
    leaf_json = [json.dumps({"label": "c1", "title": title_mod.build_title(t, n=c.title_terms), "text": t}, ensure_ascii=False) for t in texts]
    per_group_chars = sum(len(x) for x in leaf_json) / max(1, shape["l1_calls"]) + len(claude_cli.render("build_l1", {"leaves": [{"text": "", "title": ""}]}))
    secs_lo = shape["l1_calls"] * LATENCY_S["build_l1"] + shape["upper_calls"] * LATENCY_S["build_upper"] + recalls * band["route_min"] * LATENCY_S["route"]
    secs_hi = (shape["l1_calls"] * LATENCY_S["build_l1"] + shape["upper_calls"] * LATENCY_S["build_upper"]
               + recalls * (band["route_max"] * LATENCY_S["route"] + band["organise_max"] * LATENCY_S["organise"]))
    rep = {"hours_floor_concurrency_1": [round(secs_lo / 3600, 2), round(secs_hi / 3600, 2)],
           "hours_floor_concurrency_2": [round(secs_lo / 7200, 2), round(secs_hi / 7200, 2)], "questions": nq, "distinct_sessions": len(sessions), "leaves": n, "leaves_per_session_mean": n / max(1, len(sessions)),
           "tree": shape, "recall_band_per_recall": band, "build_calls": build_calls, "recalls": recalls,
           "s2_h1_calls_low": lo, "s2_h1_calls_high": hi, "build_retry_allowance": retry_allow, "cap_suggestion": hi + retry_allow,
           "registered_s2_range": REGISTERED_S2, "registered_h1_part": [381 + 750 + 750, 574 + 1800 + 1800],
           "level1_prompt_chars_mean": round(per_group_chars), "level1_prompt_chars_total": round(per_group_chars * shape["l1_calls"]),
           "leaf_chars_mean": round(sum(len(t) for t in texts) / max(1, n))}
    print("S2 call-count budget, H1 arms (no model call made to produce this)")
    print("  questions %d, distinct sessions %d, leaves %d (%.2f per session)" % (nq, len(sessions), n, rep["leaves_per_session_mean"]))
    print("  tree: level-1 groups %d, upper calls %d, levels %d, top holders %d (level widths %s)" % (
        shape["l1_calls"], shape["upper_calls"], shape["levels"], shape["top"], shape["level_widths"]))
    print("  build calls: %d exact (retry allowance for failed build calls, worst case: +%d)" % (build_calls, retry_allow))
    print("  per recall: route %d to %d, organise 0 to %d (total %d to %d)" % (band["route_min"], band["route_max"], band["organise_max"],
                                                                              band["total_min"], band["total_max"]))
    print("  recalls: %d (A1 and A1-tree-only share %d, SHUFFLED %d)" % (recalls, nq, nq))
    print("  S2 H1 calls: %d to %d; with the retry allowance up to %d" % (lo, hi, hi + retry_allow))
    print("  registered S2 row (all arms, edges, judge): %d to %d; its H1 part (build + two recall sets): %d to %d" % (
        REGISTERED_S2[0], REGISTERED_S2[1], rep["registered_h1_part"][0], rep["registered_h1_part"][1]))
    print("  by kind (calls low to high | S1-measured mean input/output tokens and latency per call):")
    by = {"build_l1": (shape["l1_calls"], shape["l1_calls"]), "build_upper": (shape["upper_calls"], shape["upper_calls"]),
          "route": (recalls * band["route_min"], recalls * band["route_max"]), "organise": (0, recalls * band["organise_max"])}
    for k, (l, h) in by.items():
        print("    %-12s %5d to %5d | %5d in, %5d out | %.2f s" % (k, l, h, MEASURED_TOKENS[k][0], MEASURED_TOKENS[k][1], LATENCY_S[k]))
    rep["by_kind"] = {k: {"low": l, "high": h} for k, (l, h) in by.items()}
    print("  wall time floor at the S1-measured latencies: %.1f to %.1f h at concurrency 1 (the port serialises calls), %.1f to %.1f h at the "
          "registered concurrency 2; real prompts are larger than S1's, so these are floors" % (
              rep["hours_floor_concurrency_1"][0], rep["hours_floor_concurrency_1"][1], rep["hours_floor_concurrency_2"][0],
              rep["hours_floor_concurrency_2"][1]))
    print("  level-1 prompt: mean %d chars per call over the real leaf text (registered assumption 18,000 to 37,000 tokens)" % rep["level1_prompt_chars_mean"])
    if a.json:
        with open(a.json, "w") as f:
            json.dump(rep, f, indent=1, sort_keys=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
