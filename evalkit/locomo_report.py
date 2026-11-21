"""Turns a LoCoMo run directory into the results tables (markdown and JSON); docs/LOCOMO-PLAN.md defines every figure.

    python -m evalkit.locomo_report <run-dir> [--out-md FILE] [--out-json FILE] [--resamples 10000]

Reads only files the run wrote (`manifest.json`, `leafmaps/`, `a0.jsonl`, `done.jsonl`, `done_B2.jsonl`, `subset_B1.json`, `calls.jsonl`,
`STATE.json`, `STAMP.json`, `PREPARE.json`, `BUILT_*.json`): no Elasticsearch, no model, no dataset. Intervals are the registered
percentile 95% clustered bootstrap (`evalkit.stats`, seed 16096, 10,000 resamples); the primary cluster is conversation x session of the
first evidence, and the conversation-only cluster is shown beside it. Nothing is rounded before it is printed to three places."""
from __future__ import annotations

import argparse
import json
import statistics
import sys
from pathlib import Path
from typing import Dict, List, Mapping, Optional, Sequence

from . import locomo as lo, stats

ARMS = ("A0", "A1", "A1-tree-only")
HEAD_METRICS = ("leaf_r5", "leaf_r10", "sess_r5", "mrr10", "allgold10")
ALL_METRICS = lo.METRICS
LABEL = {"leaf_r5": "gold-leaf recall@5", "leaf_r10": "gold-leaf recall@10", "sess_r5": "gold-session recall@5",
         "sess_r10": "gold-session recall@10", "mrr10": "MRR@10 (leaf)", "allgold10": "all-gold@10 (leaf, >=2 gold leaves)",
         "allgold10_sess": "all-gold@10 (session, >=2 gold sessions)"}


def read_jsonl(p: Path) -> List[dict]:
    if not p.exists():
        return []
    out = []
    for line in p.read_text().splitlines():
        if line.strip():
            try:
                out.append(json.loads(line))
            except ValueError:
                pass            # a torn last line of an interrupted run
    return out


def read_json(p: Path, default=None):
    return json.loads(p.read_text()) if p.exists() else default


class Run:
    def __init__(self, d: Path):
        self.d = Path(d)
        m = read_json(self.d / "manifest.json", {"questions": {}, "pools": {}})
        self.q: Dict[str, dict] = m["questions"]
        self.pools = {int(k): v for k, v in m["pools"].items()}
        self.leaf_session: Dict[int, Dict[str, str]] = {}
        for i in self.pools:
            lm = read_json(self.d / "leafmaps" / ("%d.json" % i), {"leaves": []})
            self.leaf_session[i] = {l["id"]: l["session"] for l in lm["leaves"]}
        self.a0 = {r["qid"]: r for r in read_jsonl(self.d / "a0.jsonl")}
        self.done = {"B1": {r["qid"]: r for r in read_jsonl(self.d / "done.jsonl")},
                     "B2": {r["qid"]: r for r in read_jsonl(self.d / "done_B2.jsonl")}}
        self.subset = read_json(self.d / "subset_B1.json", {"order": [], "shuffled": []})
        self.calls = read_jsonl(self.d / "calls.jsonl")
        self.state = read_json(self.d / "STATE.json", {})
        self.stamp = read_json(self.d / "STAMP.json", {})
        self.prepare = read_json(self.d / "PREPARE.json", {})
        self.built = {int(p.stem.split("_")[1]): read_json(p) for p in sorted(self.d.glob("BUILT_*.json"))}

    def metrics(self, qid: str, leaves: Sequence[str], failed: bool = False) -> Dict[str, Optional[float]]:
        q = self.q[qid]
        return lo.question_metrics(leaves, q["gold_leaves"], q["gold_sessions"], self.leaf_session[q["conv"]], failed=failed)

    def arm_scores(self, qids: Sequence[str], arm: str) -> Dict[str, Dict[str, Optional[float]]]:
        out = {}
        for qid in qids:
            if arm == "A0-all":
                out[qid] = self.metrics(qid, self.a0[qid]["leaves"])
            else:
                a = self.done_row(qid)["arms"][arm]
                out[qid] = self.metrics(qid, a["leaves"], a["failed"])
        return out

    def done_row(self, qid: str) -> dict:
        return self.done["B1"].get(qid) or self.done["B2"][qid]

    def clusters(self, qids: Sequence[str], key: str) -> Dict[str, str]:
        return {qid: self.q[qid][key] for qid in qids}


# ---- statistics -------------------------------------------------------------------------------------------------------------
def ci(values: Mapping[str, float], clusters: Mapping[str, str], resamples: int) -> Optional[dict]:
    if not values:
        return None
    iv = stats.clustered_bootstrap(values, clusters, resamples=resamples)
    return {"mean": iv.point, "lo": iv.lo, "hi": iv.hi, "n": iv.n_questions, "clusters": iv.n_clusters}


def metric_ci(run: Run, scores: Mapping[str, Mapping[str, Optional[float]]], metric: str, resamples: int) -> Optional[dict]:
    vals = {q: s[metric] for q, s in scores.items() if s[metric] is not None}
    if not vals:
        return None
    p, c = ci(vals, run.clusters(list(vals), "cluster"), resamples), ci(vals, run.clusters(list(vals), "cluster_conv"), resamples)
    return {**p, "conv_lo": c["lo"], "conv_hi": c["hi"], "conv_clusters": c["clusters"]}


def diff_ci(run: Run, sa, sb, metric: str, resamples: int) -> Optional[dict]:
    d = {q: sa[q][metric] - sb[q][metric] for q in sa if sa[q][metric] is not None and sb[q][metric] is not None}
    if not d:
        return None
    p, c = ci(d, run.clusters(list(d), "cluster"), resamples), ci(d, run.clusters(list(d), "cluster_conv"), resamples)
    return {**p, "conv_lo": c["lo"], "conv_hi": c["hi"], "conv_clusters": c["clusters"]}


def chance(run: Run, qids: Sequence[str]) -> dict:
    return {"leaf_r%d" % k: sum(lo.chance_leaf_recall(run.q[q]["pool_leaves"], k) for q in qids) / len(qids) for k in (5, 10)}


# ---- tables -----------------------------------------------------------------------------------------------------------------
def f3(x: Optional[float]) -> str:
    return "n/a" if x is None else "%.3f" % x


def cell(c: Optional[dict], conv: bool = False) -> str:
    if c is None:
        return "n/a"
    lo_, hi_ = (c["conv_lo"], c["conv_hi"]) if conv else (c["lo"], c["hi"])
    return "%s [%s, %s]" % (f3(c["mean"]), f3(lo_), f3(hi_))


def block(run: Run, qids: Sequence[str], arms: Sequence[str], resamples: int, metrics=ALL_METRICS) -> dict:
    """Per-arm metric intervals and paired differences over `qids` (every arm scores the same questions)."""
    sc = {a: run.arm_scores(qids, a) for a in arms}
    res: dict = {"n": len(qids), "arms": {a: {m: metric_ci(run, sc[a], m, resamples) for m in metrics} for a in arms}, "paired": {}}
    pairs = [(x, y) for x, y in (("A1", "A0"), ("A1-tree-only", "A0"), ("A1", "A1-tree-only")) if x in arms and y in arms]
    for x, y in pairs:
        res["paired"]["%s minus %s" % (x, y)] = {m: diff_ci(run, sc[x], sc[y], m, resamples) for m in metrics}
    res["chance"] = chance(run, qids)
    res["_scores"] = sc
    return res


def md_block(title: str, b: dict, arms: Sequence[str], names: Mapping[str, str], metrics=ALL_METRICS) -> List[str]:
    L = ["### %s" % title, "", "n = %d questions. Cells are mean [95%% CI], percentile clustered bootstrap, primary cluster (conversation x session of first "
         "evidence), 10,000 resamples, seed 16096." % b["n"], "", "| Metric (n with the metric) | " + " | ".join(names[a] for a in arms) + " |",
         "|---|" + "---|" * len(arms)]
    for m in metrics:
        ns = {a: (b["arms"][a][m] or {}).get("n") for a in arms}
        L.append("| %s (n=%s) | %s |" % (LABEL[m], "/".join(sorted({str(v) for v in ns.values()})), " | ".join(cell(b["arms"][a][m]) for a in arms)))
    if "leaf_r5" in b["chance"]:
        L.append("| chance reference, gold-leaf recall@5 / @10 | %s |" % " | ".join("%s / %s" % (f3(b["chance"]["leaf_r5"]), f3(b["chance"]["leaf_r10"]))
                                                                                    for _ in arms))
    L += ["", "Conversation-only clusters (10 clusters at most), the sensitivity interval:", "", "| Metric | " + " | ".join(names[a] for a in arms) + " |",
          "|---|" + "---|" * len(arms)]
    for m in metrics:
        L.append("| %s | %s |" % (LABEL[m], " | ".join(cell(b["arms"][a][m], True) for a in arms)))
    if b["paired"]:
        L += ["", "Paired differences (mean of per-question differences, same clustering):", "",
              "| Metric | " + " | ".join(b["paired"]) + " |", "|---|" + "---|" * len(b["paired"])]
        for m in metrics:
            L.append("| %s | %s |" % (LABEL[m], " | ".join(cell(b["paired"][k][m]) + " (conv: [%s, %s])" % (f3((b["paired"][k][m] or {}).get("conv_lo")),
                                                                                                        f3((b["paired"][k][m] or {}).get("conv_hi")))
                                                          for k in b["paired"])))
    return L + [""]


def per_category(run: Run, qids: Sequence[str], arms: Sequence[str], resamples: int) -> dict:
    out = {}
    for cat in lo.CATEGORIES:
        qs = [q for q in qids if run.q[q]["category"] == cat]
        if not qs:
            continue
        sc = {a: run.arm_scores(qs, a) for a in arms}
        row = {"n": len(qs), "arms": {a: {m: metric_ci(run, sc[a], m, resamples) for m in ("leaf_r10", "sess_r5")} for a in arms}, "paired": {}}
        if "A1" in arms and "A0" in arms:
            row["paired"]["A1 minus A0"] = {m: diff_ci(run, sc["A1"], sc["A0"], m, resamples) for m in ("leaf_r10", "sess_r5")}
        out[cat] = row
    return out


def md_category(title: str, pc: dict, arms: Sequence[str], names: Mapping[str, str]) -> List[str]:
    L = ["### %s" % title, "", "Categories are labelled by number (the LoCoMo repository does not name them). Category 3 has very few questions in the subset: its "
         "intervals are printed but are not interpretable.", "",
         "| Category | n | " + " | ".join("%s leaf@10" % names[a] for a in arms) + " | " + " | ".join("%s sess@5" % names[a] for a in arms)
         + (" | A1 minus A0 leaf@10 |" if "A1" in arms and "A0" in arms else " |"), "|---|---|" + "---|" * (2 * len(arms) + (1 if "A1" in arms and "A0" in arms else 0))]
    for cat, r in pc.items():
        cells = [cell(r["arms"][a]["leaf_r10"]) for a in arms] + [cell(r["arms"][a]["sess_r5"]) for a in arms]
        if r["paired"]:
            cells.append(cell(r["paired"]["A1 minus A0"]["leaf_r10"]))
        L.append("| %d | %d | %s |" % (cat, r["n"], " | ".join(cells)))
    return L + [""]


def pct(xs: Sequence[float], p: float) -> float:
    s = sorted(xs)
    return s[min(len(s) - 1, int(p * len(s)))]


def timing(rows: Sequence[dict], key: str) -> Optional[dict]:
    xs = [r[key] for r in rows if key in r]
    if not xs:
        return None
    return {"n": len(xs), "median": statistics.median(xs), "mean": statistics.mean(xs), "p90": pct(xs, 0.9), "min": min(xs), "max": max(xs)}


def cost(run: Run) -> dict:
    by: Dict[str, dict] = {}
    for r in run.calls:
        b = by.setdefault(r.get("task", "?"), {"calls": 0, "valid": 0, "schema": 0, "lat": [], "in": 0, "out": 0, "throttled": 0})
        b["calls"] += 1
        b["schema"] += bool(r.get("schema_ok"))
        b["valid"] += bool(r.get("strict_ok"))
        b["throttled"] += bool(r.get("throttled"))
        b["lat"].append(float(r.get("latency_s", 0.0)))
        b["in"] += sum(int(r.get(k, 0) or 0) for k in ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"))
        b["out"] += int(r.get("output_tokens", 0) or 0)
    tasks = {k: {"calls": b["calls"], "valid": b["valid"], "schema_ok": b["schema"], "throttled": b["throttled"], "input_tokens": b["in"], "output_tokens": b["out"],
                 "latency_median_s": statistics.median(b["lat"]), "latency_p90_s": pct(b["lat"], 0.9)} for k, b in by.items()}
    allrows = [r for rows in run.done.values() for r in rows.values()]
    shuf = [r["shuffled"] for r in allrows if "shuffled" in r]
    return {"calls_total": len(run.calls), "by_task": tasks,
            "cost_usd": (sum(float(r["cost_usd"]) for r in run.calls if r.get("cost_usd") is not None) if any(r.get("cost_usd") is not None for r in run.calls) else None),
            "build": {"conversations_built": len(run.built), "build_calls": sum(b["build_calls"] for b in run.built.values()),
                      "seconds": sum(b["seconds"] for b in run.built.values())},
            "recall": {"tree_wall_s": timing(allrows, "tree_wall_s"), "tree_calls": timing(allrows, "tree_calls"),
                       "tree_call_wall_s": timing(allrows, "tree_call_wall_s"), "holder_calls": timing(allrows, "holder_calls"),
                       "direct_wall_s_in_recall": timing(allrows, "direct_wall_s"), "shuffled_wall_s": timing(shuf, "wall_s"),
                       "shuffled_calls": timing(shuf, "calls")},
            "a0_all_query_wall_s": timing(list(run.a0.values()), "wall_s")}


def md_cost(c: dict) -> List[str]:
    def t(x, unit=""):
        return "n/a" if not x else "median %.2f%s, mean %.2f%s, p90 %.2f%s, min %.2f%s, max %.2f%s (n=%d)" % (
            x["median"], unit, x["mean"], unit, x["p90"], unit, x["min"], unit, x["max"], unit, x["n"])
    L = ["### Cost and latency", "", "Model calls charged (build plus recall, retries included): **%d**." % c["calls_total"]]
    if c["cost_usd"] is not None:
        L.append("Reported cost on the subscription CLI (`cost_usd` summed over rows that report one): $%.2f." % c["cost_usd"])
    L += ["", "| Task | calls | schema valid | strict valid | throttled | input tokens | output tokens | latency median s | latency p90 s |", "|---|---|---|---|---|---|---|---|---|"]
    for k, v in sorted(c["by_task"].items()):
        L.append("| %s | %d | %d | %d | %d | %d | %d | %.2f | %.2f |" % (k, v["calls"], v["schema_ok"], v["valid"], v["throttled"], v["input_tokens"], v["output_tokens"],
                                                                    v["latency_median_s"], v["latency_p90_s"]))
    b, r = c["build"], c["recall"]
    L += ["", "Schema valid means the reply parsed and matched the task's schema. Strict valid also applies the prompt's own rules (for route calls, each "
          "reason at most 12 words); the selections of a call that is schema valid but not strict valid are kept, as in S2.", "", "Tree build: %d conversations, %d build calls, %.0f s in total." % (b["conversations_built"], b["build_calls"], b["seconds"]), "",
          "Per tree recall (wall seconds around the recall, which includes waiting for a concurrency slot; calls inside a recall are serial): %s." % t(r["tree_wall_s"], " s"),
          "Calls per tree recall: %s." % t(r["tree_calls"]), "Holder (route) calls per recall: %s." % t(r["holder_calls"]),
          "Seconds the recall spent inside its calls: %s." % t(r["tree_call_wall_s"], " s"),
          "SHUFFLED recall wall: %s; calls: %s." % (t(r["shuffled_wall_s"], " s"), t(r["shuffled_calls"])),
          "A0 (flat) per query, including the query embedding, all eligible questions: %s." % t(c["a0_all_query_wall_s"], " s"), ""]
    return L


def build(run: Run, resamples: int) -> dict:
    allq = sorted(run.q)
    res: dict = {"plan": "docs/LOCOMO-PLAN.md", "resamples": resamples, "seed": stats.SEED}
    planned = run.subset.get("order", [])
    b1 = [q for q in planned if q in run.done["B1"]]
    sh_planned = run.subset.get("shuffled", [])
    sh_done = [q for q in sh_planned if "SHUFFLED" in run.done["B1"].get(q, {}).get("arms", {})]
    b2 = sorted(run.done["B2"])
    res["completion"] = {"eligible_questions": len(allq), "a0_all_rows": len(run.a0), "b1_planned": len(planned), "b1_completed": len(b1),
                         "shuffled_planned": len(sh_planned), "shuffled_completed": len(sh_done), "b2_completed": len(b2),
                         "state": run.state.get("state"), "counter": run.state.get("counter", len(run.calls)), "cap": run.state.get("cap", run.stamp.get("cap")),
                         "models_reported": run.state.get("models_reported"), "commit": run.stamp.get("commit"), "concurrency_changes": run.state.get("concurrency_changes"),
                         "excluded_no_evidence": None, "pools": run.pools}
    a0q = [q for q in allq if q in run.a0]
    res["a0_all"] = {"block": None}
    if a0q:
        sc = run.arm_scores(a0q, "A0-all")
        res["a0_all"] = {"n": len(a0q), "metrics": {m: metric_ci(run, sc, m, resamples) for m in ALL_METRICS}, "chance": chance(run, a0q),
                         "by_category": {}, "by_conversation": {}}
        for cat in lo.CATEGORIES:
            qs = [q for q in a0q if run.q[q]["category"] == cat]
            if qs:
                s = {q: sc[q] for q in qs}
                res["a0_all"]["by_category"][cat] = {"n": len(qs), **{m: metric_ci(run, s, m, resamples) for m in ("leaf_r10", "sess_r5")}}
        for ci_ in sorted(run.pools):
            qs = [q for q in a0q if run.q[q]["conv"] == ci_]
            if qs:
                res["a0_all"]["by_conversation"][ci_] = {"n": len(qs), "leaves": run.pools[ci_],
                                                         **{m: statistics.mean(sc[q][m] for q in qs) for m in ("leaf_r5", "leaf_r10", "sess_r5")}}
    res["batches"] = {}
    for name, qids in (("B1", b1), ("B2", b2), ("B1+B2", b1 + b2)):
        if not qids or (name == "B1+B2" and not b2):
            continue
        blk = block(run, qids, ARMS, resamples)
        blk["by_category"] = per_category(run, qids, ARMS, resamples)
        blk["tree_failed_recalls"] = sum(bool(run.done_row(q)["arms"]["A1"]["failed"]) for q in qids)
        # the A0 computed at recall time must be the A0 of the all-questions stage
        same = sum(run.done_row(q)["arms"]["A0"]["leaves"] == run.a0.get(q, {}).get("leaves") for q in qids)
        blk["a0_recall_time_equals_a0_all"] = {"same": same, "of": len(qids)}
        res["batches"][name] = blk
    if sh_done:
        sb = block(run, sh_done, ("A0", "A1", "A1-tree-only", "SHUFFLED"), resamples)
        sb["paired"]["A1-tree-only minus SHUFFLED"] = {m: diff_ci(run, sb["_scores"]["A1-tree-only"], sb["_scores"]["SHUFFLED"], m, resamples) for m in ALL_METRICS}
        sb["shuffled_empty_lists"] = sum(not run.done["B1"][q]["arms"]["SHUFFLED"]["leaves"] for q in sh_done)
        sb["shuffled_failed"] = sum(run.done["B1"][q]["arms"]["SHUFFLED"]["failed"] for q in sh_done)
        sb["shuffled_mean_leaves"] = statistics.mean(len(run.done["B1"][q]["arms"]["SHUFFLED"]["leaves"]) for q in sh_done)
        res["shuffled"] = sb
    tl = [statistics.mean(len(run.done_row(q)["arms"]["A1-tree-only"]["leaves"]) for q in b1)] if b1 else []
    res["tree_only_mean_list_length_B1"] = tl[0] if tl else None
    res["cost"] = cost(run)
    return res


def render(res: dict, run: Run) -> str:
    c = res["completion"]
    names = {"A0": "A0 (flat)", "A1": "A1 (tree + flat, RRF)", "A1-tree-only": "A1-tree-only", "SHUFFLED": "SHUFFLED-SUMMARIES"}
    L = ["# LoCoMo results", "", "EXPLORATORY. Plan, definitions and the rules fixed before any run: `docs/LOCOMO-PLAN.md`. Regenerate with "
         "`python -m evalkit.locomo_report <run-dir>`.", "", "## Completion", "",
         "| Planned | Completed |", "|---|---|",
         "| A0 on all eligible questions: %d | %d |" % (c["eligible_questions"], c["a0_all_rows"]),
         "| B1 (A1 and A1-tree-only on the seeded stratified subset): %d | %d |" % (c["b1_planned"], c["b1_completed"]),
         "| SHUFFLED-SUMMARIES on the first %d subset questions | %d |" % (c["shuffled_planned"], c["shuffled_completed"]),
         "| B2 (optional secondary batch) | %d |" % c["b2_completed"], "",
         "Run state: %s. Model calls charged: %s of cap %s. Models reported by the CLI: %s. Code commit: %s." % (c["state"], c["counter"], c["cap"], c["models_reported"], c["commit"]),
         "Pool sizes (leaves per conversation, file order): %s. These pools are small: see the chance reference in every table." % ", ".join(str(c["pools"][k]) for k in sorted(c["pools"])), ""]
    if res["a0_all"].get("n"):
        a = res["a0_all"]
        L += ["## A0 on all eligible questions (no model call)", "", "n = %d questions." % a["n"], "", "| Metric | A0 [95% CI, primary cluster] | conversation-cluster CI |", "|---|---|---|"]
        for m in ALL_METRICS:
            L.append("| %s | %s | %s |" % (LABEL[m], cell(a["metrics"][m]), cell(a["metrics"][m], True)))
        L += ["| chance reference, gold-leaf recall@5 / @10 | %s / %s | |" % (f3(a["chance"]["leaf_r5"]), f3(a["chance"]["leaf_r10"])), "",
              "### A0 by category", "", "| Category | n | gold-leaf recall@10 | gold-session recall@5 |", "|---|---|---|---|"]
        for cat, r in a["by_category"].items():
            L.append("| %d | %d | %s | %s |" % (cat, r["n"], cell(r["leaf_r10"]), cell(r["sess_r5"])))
        L += ["", "### A0 by conversation", "", "| Conversation | leaves | n | leaf@5 | leaf@10 | session@5 |", "|---|---|---|---|---|---|"]
        for ci_, r in a["by_conversation"].items():
            L.append("| %d | %d | %d | %s | %s | %s |" % (ci_, r["leaves"], r["n"], f3(r["leaf_r5"]), f3(r["leaf_r10"]), f3(r["sess_r5"])))
        L.append("")
    for name, b in res["batches"].items():
        L += ["## Tree arms, batch %s" % name, ""]
        L += md_block("A0, A1 and A1-tree-only on the same %d questions" % b["n"], b, ARMS, names)
        L += md_category("Per category", b["by_category"], ARMS, names)
        L += ["Tree recalls that failed (scored 0, kept): %d of %d. A0 at recall time equals the A0 of the all-questions stage on %d of %d questions." % (
            b["tree_failed_recalls"], b["n"], b["a0_recall_time_equals_a0_all"]["same"], b["a0_recall_time_equals_a0_all"]["of"]), ""]
    if res.get("tree_only_mean_list_length_B1") is not None:
        L += ["A1-tree-only returns on average %.2f leaves per question in B1 (it is scored as returned, not padded to 10)." % res["tree_only_mean_list_length_B1"], ""]
    if "shuffled" in res:
        s = res["shuffled"]
        L += ["## SHUFFLED-SUMMARIES negative control", "", "On the %d questions where it ran. The expectation about zero is not pre-registered for pools this small." % s["n"],
              "Lists returned empty: %d; failed recalls: %d; mean leaves returned: %.2f." % (s["shuffled_empty_lists"], s["shuffled_failed"], s["shuffled_mean_leaves"]), ""]
        L += md_block("A0, A1, A1-tree-only and SHUFFLED on those questions", s, ("A0", "A1", "A1-tree-only", "SHUFFLED"), names, metrics=("leaf_r5", "leaf_r10", "sess_r5", "mrr10"))
    L += md_cost(res["cost"])
    L += ["## Limits (fixed in the plan, section 10)", "",
          "- Pools are 25 to 60 leaves: a top-10 list covers a large share of a pool, A0 is already high, and the tree has one level of 2 to 4 nodes. "
          "These numbers do not transfer to the 4,000 to 78,000 leaf pools of the LongMemEval work.",
          "- Retrieval only: no answer generation and no end-to-end QA accuracy.",
          "- Model: Sonnet through the Claude CLI at the version installed on the host; not the model of S3 or S6-H1.",
          "- The session date is attached to the first leaf of a session only. Every figure is EXPLORATORY.", ""]
    return "\n".join(L) + "\n"


def strip(o):
    if isinstance(o, dict):
        return {k: strip(v) for k, v in o.items() if k != "_scores"}
    return o


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("run_dir")
    ap.add_argument("--out-md", default="")
    ap.add_argument("--out-json", default="")
    ap.add_argument("--resamples", type=int, default=stats.RESAMPLES)
    a = ap.parse_args(argv)
    run = Run(Path(a.run_dir))
    res = build(run, a.resamples)
    md = render(res, run)
    if a.out_md:
        Path(a.out_md).write_text(md)
    if a.out_json:
        Path(a.out_json).write_text(json.dumps(strip(res), indent=1, sort_keys=True, default=str))
    if not (a.out_md or a.out_json):
        print(md)
    return 0


if __name__ == "__main__":
    sys.exit(main())
