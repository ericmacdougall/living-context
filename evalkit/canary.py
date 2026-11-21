"""Stage S1, the canary (EXPERIMENT 10): every task kind with the real model on the fixture, hard cap 25 calls in total.

Run on the fleet host from a committed tree: `python -m evalkit.canary --phase probe|rest --run-dir D --binary B --model M --commit C`.
All model calls go through `claude_cli.CappedPort` over ONE durable counter file in the run directory; there is no other path to
a model. The plan is fixed here before any run (22 planned calls, 3 held in reserve):

  probe   1  query_variants (the smallest prompt) -> the raw envelope is written for inspection, then the process stops
  rest   21  build_l1 x4 (the real builder over 64 fixture leaves) + build_upper x1 (over those nodes' summaries)
             + 2 full A1 recalls (route x4 and organise x1 each) + type_edges, judge_relevance, synthetic_queries, ask,
             reader_answer, answer_judge, one call each

No dataset content is used (a synthetic fixture); the leaves are nonsense-word notes, so token counts per character are higher than
English text and the figures are labelled as fixture-sized in the report. A reserve call is used only to repeat a kind whose call
failed structurally, and the report says so.
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from pathlib import Path

from living_context.config import Constants
from living_context.model.budget import Budget
from living_context.model.port import CallContext, Task
from living_context.recall.view import MemView
from living_context.structure import build

from . import arms, claude_cli, corpus, fixture
from .score import write_result

CAP = 25
STAGE = "S1"
CTX = CallContext("canary", "base", "px", ("base",))


def make_port(run_dir: Path, binary: str, model: str) -> claude_cli.CappedPort:
    runner = claude_cli.CliRunner(binary, model, run_dir / "cwd")
    return claude_cli.CappedPort(runner, Budget(run_dir / "counter", CAP), run_dir / "calls.jsonl", model_id=model,
                                 argv_record=runner.argv())


def probe(run_dir: Path, port: claude_cli.CappedPort) -> None:
    res = port.call(Task.QUERY_VARIANTS, {"query": "tomato plants wilt in the afternoon heat"}, ctx=CTX)
    row = port.rows[-1]
    (run_dir / "probe_row.json").write_text(json.dumps(row, indent=1, sort_keys=True))
    print("probe:", json.dumps({k: row.get(k) for k in ("n", "task", "prompt_chars", "input_tokens", "cache_creation_input_tokens",
                                                       "cache_read_input_tokens", "output_tokens", "models_reported", "num_turns",
                                                       "latency_s", "rc", "parse_ok", "schema_ok", "strict_ok", "error")}))
    print("ok:", res.ok)


def rest(run_dir: Path, port: claude_cli.CappedPort) -> None:
    fx = fixture.make("advantage", 4)
    mc = corpus.MemCorpus(fx.sessions)
    c = Constants()
    tree = build.build_tree(mc.leaves, port, "canary", "base", consts=c)               # build_l1 x4
    nodes = list(tree.nodes.values())
    port.call(Task.BUILD_UPPER, {"children": [{"summary": n.summary, "facts": n.facts, "title": n.title} for n in nodes]}, ctx=CTX)
    view = MemView(tree)
    tr = arms.TreeRetriever(port, view, "canary", "base", "px", c)
    for qid in ("q000", "q001"):                                                         # route x4 + organise x1, twice
        out = tr(fx.queries[qid])
        print("recall", qid, "ok" if out.ok else "FAILED", "tree leaves", len(out.ids))
    leaves = mc.leaves
    q = fx.queries["q000"]
    items = [{"label": "c0", "text": leaves[0].text, "entities": []}] + [
        {"label": "c%d" % n, "text": l.text, "entities": []} for n, l in enumerate(leaves[1:6], 1)]
    port.call(Task.TYPE_EDGES, {"items": items}, ctx=CTX)
    port.call(Task.JUDGE_RELEVANCE, {"query": q, "text": leaves[0].text}, ctx=CTX)
    port.call(Task.SYNTHETIC_QUERIES, {"text": leaves[0].text}, ctx=CTX)
    port.call(Task.ASK, {"question": q, "context": [leaves[0].text, leaves[1].text]}, ctx=CTX)
    port.call(Task.READER_ANSWER, {"question": q, "context": [leaves[0].text]}, ctx=CTX)
    port.call(Task.ANSWER_JUDGE, {"gold": "plants wilt when it is hot", "answer": "heat makes tomato plants wilt"}, ctx=CTX)


def report(run_dir: Path, commit: str, model: str) -> dict:
    rows = [json.loads(l) for l in (run_dir / "calls.jsonl").read_text().splitlines() if l.strip()]
    kinds = {}
    for r in rows:
        k = kinds.setdefault(r["task"], [])
        k.append(r)
    per = {}
    for task, rs in sorted(kinds.items()):
        tot_in = [r.get("input_tokens", 0) + r.get("cache_creation_input_tokens", 0) + r.get("cache_read_input_tokens", 0) for r in rs]
        per[task] = {"calls": len(rs), "schema_valid": sum(bool(r.get("schema_ok")) for r in rs),
                     "consumer_strict_valid": sum(bool(r.get("strict_ok")) for r in rs),
                     "input_tokens_total_per_call": tot_in, "input_tokens_uncached_per_call": [r.get("input_tokens", 0) for r in rs],
                     "cache_creation_per_call": [r.get("cache_creation_input_tokens", 0) for r in rs],
                     "cache_read_per_call": [r.get("cache_read_input_tokens", 0) for r in rs],
                     "output_tokens_per_call": [r.get("output_tokens", 0) for r in rs],
                     "prompt_chars_per_call": [r["prompt_chars"] for r in rs],
                     "latency_s_per_call": [r["latency_s"] for r in rs],
                     "latency_s_median": round(statistics.median([r["latency_s"] for r in rs]), 2),
                     "throttled": sum(bool(r.get("throttled")) for r in rs),
                     "errors": [r.get("error") for r in rs if r.get("error")],
                     "strict_problems": [p for r in rs for p in r.get("strict_problems", [])][:6]}
    return {"stage": STAGE, "commit": commit, "model_requested": model, "calls_total": len(rows),
            "counter": Budget(run_dir / "counter", CAP).count(), "cap": CAP,
            "models_reported": sorted({m for r in rows for m in r.get("models_reported", [])}),
            "num_turns": sorted({str(r.get("num_turns")) for r in rows}), "argv": rows[0]["argv"] if rows else [],
            "per_task": per, "fixture": "synthetic nonsense-word notes; token counts per character are higher than English"}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--phase", choices=["probe", "rest", "report"], required=True)
    ap.add_argument("--run-dir", required=True)
    ap.add_argument("--binary", required=True)
    ap.add_argument("--model", required=True)
    ap.add_argument("--commit", required=True)
    a = ap.parse_args(argv)
    run_dir = Path(a.run_dir)
    run_dir.mkdir(parents=True, exist_ok=True)
    if a.phase == "report":
        rep = report(run_dir, a.commit, a.model)
        (run_dir / "s1_report.json").write_text(json.dumps(rep, indent=1, sort_keys=True))
        row = {"commit": a.commit, "stage": STAGE, "arm": "canary", "pool": "synthetic fixture (64 leaves)", "n": rep["calls_total"],
               "metric": "schema validity, tokens and latency per task kind", "interval_method": "none (descriptive)", "seed": 0,
               "call_counter": rep["counter"], "product_cache_hits": 0, "routing": "agent-routed"}
        write_result(str(run_dir / "s1_result.json"), row, [{"task": t, **{k: v for k, v in d.items() if k in ("calls", "schema_valid")}} for t, d in rep["per_task"].items()])
        print(json.dumps(rep["per_task"], indent=1, sort_keys=True)[:6000])
        return 0
    port = make_port(run_dir, a.binary, a.model)
    try:
        (probe if a.phase == "probe" else rest)(run_dir, port)
    except claude_cli.StagePaused as e:
        print("STAGE PAUSED:", e)
        return 3
    print("counter:", Budget(run_dir / "counter", CAP).count(), "of", CAP)
    return 0


if __name__ == "__main__":
    sys.exit(main())
