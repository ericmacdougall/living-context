"""The question loop, the per-arm result files and the stage summaries of S2 (EXPERIMENT 10; H1 arms only).

Everything here is pure with respect to the model and Elasticsearch: the retrievers are passed in, so the same code runs on the real
stack (`run_s2`) and, in tests, on the in-memory corpus with a fake port.

  * `Ledger`: one JSON line per COMPLETED question (opaque question reference, top-10 sessions of every arm, failed flags, the source
    of each gold session in A1). A killed run resumes after the last completed question; a question that was in flight is asked
    again and its already journaled calls are answered from the journal (`s2_ports`), so no charged call is repeated.
  * `write_arm_results`: one file per arm in the registered row schema (score.REQUIRED_FIELDS), per-question rows included.
  * `summaries`: calls by task kind, tokens, latency median and p90, throttles, failure rate, and the largest number of calls in any
    5-hour window (the S2 gate's "sustained calls per 5-hour window").

Outputs carry opaque references only (a salted hash of the question id, session content hashes mapped through `dataset.opaque_ref`):
no question text, no session text, no gold id."""
from __future__ import annotations

import hashlib
import json
import os
import statistics
from pathlib import Path
from typing import Callable, Dict, List, Mapping, Optional, Sequence

from . import arms, dataset as ds, stats
from .score import question_scores, write_result
from .parallel import run_ordered
from .s2_ports import StageStopped, read_resumable

ARMS_H1 = ("A0", "A1", "A1-tree-only", "SHUFFLED")


class CapReached(Exception):
    """The call cap was reached while a recall was running: the question is incomplete and is not recorded."""


def qref(qid: str) -> str:
    return "q" + hashlib.sha256(("%d:%s" % (ds.SPLIT_SEED, qid)).encode()).hexdigest()[:12]


class Ledger:
    def __init__(self, path: Path, extra: Optional[dict] = None):
        self.path = Path(path)
        self.extra = dict(extra or {})          # recorded in every row (the model kind and model id)
        self.rows: Dict[str, dict] = {}
        for r in read_resumable(self.path):     # a torn last line is set aside; an unreadable earlier one refuses
            self.rows[r["qref"]] = r

    def append(self, row: dict) -> None:
        row = {**self.extra, **row}
        with open(self.path, "a") as f:
            f.write(json.dumps(row, sort_keys=True) + "\n")
            f.flush()
            os.fsync(f.fileno())
        self.rows[row["qref"]] = row


class Recorder:
    """Wraps a retriever and keeps its last output (ids only), so the source of each gold session can be attributed; `tally` is
    the `s2_ports.Tally` its recall's calls went through (the question's own), when there is one."""
    def __init__(self, fn, tally=None):
        self.fn, self.last, self.tally = fn, None, tally

    def __call__(self, q):
        self.last = self.fn(q)
        return self.last


def port_counts(**recs) -> dict:
    """{arm: {timeouts, errors, invalid}} of the recalls that ran with their own tally (F7: how often a finished question's arm
    fell back because a call timed out or failed)."""
    return {arm: r.tally.counts() for arm, r in recs.items() if r is not None and getattr(r, "tally", None) is not None}


def tallied(port, fn) -> "Recorder":
    """A recorder over `fn(port')`, where port' counts the failed calls of this recall only."""
    from .s2_ports import Tally
    t = Tally(port)
    return Recorder(fn(t), tally=t)


def run_questions(dev: Sequence[ds.Question], hashes: Mapping[str, str], direct: Optional[Recorder], tree: Optional[Recorder],
                  shuffled: Optional[Recorder], world: arms.World, ledger: Ledger, *, beta: Optional[float] = None,
                  stop: Callable[[], bool] = lambda: False, progress: Callable[[str], None] = lambda s: None, shuffled_n: int = 0,
                  retrievers: Optional[Callable[[str], tuple]] = None, workers: int = 1) -> None:
    """Completes every question not yet in the ledger, in the given order. Raises `StageStopped` (stop file) or `CapReached`
    before recording a question that did not complete; whatever the model port raised (`StagePaused`, `BudgetExceeded` outside a
    recall) propagates, again without recording the question.

    Several workers: `retrievers(ref)` gives each question its own (direct, tree, shuffled) recorders over a port scoped to it; the
    questions run concurrently and their rows are appended strictly in the given order (`parallel.run_ordered`)."""
    if workers > 1 and retrievers is None:
        raise ValueError("several workers need per-question retrievers (a shared recorder is shared state)")
    todo = [(n, q) for n, q in enumerate(dev, 1) if qref(q.qid) not in ledger.rows]

    def one(item) -> dict:
        n, q = item
        ref = qref(q.qid)
        d, t, sh = retrievers(ref) if retrievers is not None else (direct, tree, shuffled)
        g = sorted({hashes[s] for s in q.gold_session_ids})
        shf = sh if (not shuffled_n or n <= shuffled_n) else None      # S3: SHUFFLED on the first `shuffled_n` questions only
        o = arms.run_question(q.question, d, t, world, beta=beta or 0.0, shuffled_tree=shf)
        for rec in (t, shf):
            if rec is None:
                continue
            if rec.last is not None and not rec.last.ok and rec.last.error == "BudgetExceeded":
                raise CapReached("call cap reached during question %d" % n)
        src = arms.source_of_gold(o["A1"], t.last.ids, [i for i, _ in d.last], g, world) if o["A1"].sessions else {}
        return {"qref": ref, "gold_n": len(g), "gold": [ds.opaque_ref(x) for x in g],
                "arms": {k: {"sessions": [ds.opaque_ref(s) for s in v.sessions], "failed": bool(v.failed)} for k, v in o.items()
                         if k in ARMS_H1 or (beta is not None and k == "A0+LEN")},
                "sources": sorted(src.values()), "holder_calls": t.last.holder_calls, "fallbacks": t.last.fallbacks,
                **({"shuffled_fallbacks": shf.last.fallbacks} if shf is not None and shf.last is not None else {}),
                **({"port": pc} if (pc := port_counts(A1=t, SHUFFLED=shf)) else {})}

    def commit(_i, item, row) -> None:
        ledger.append(row)
        progress("question %d/%d done" % (item[0], len(dev)))

    run_ordered(todo, one, commit, workers=workers, stop=stop, stop_exc=lambda: StageStopped("stop file present"))


# ---- results ----------------------------------------------------------------------------------------------------------
def _scores(ledger: Ledger, arm: str, qids: Sequence[str]) -> tuple:
    outs, gold = {}, {}
    for q in qids:
        r = ledger.rows[qref(q)]
        a = r["arms"].get(arm)
        if a is None:
            continue
        outs[q] = arms.ArmOut([], a["sessions"], failed=a["failed"])
        gold[q] = r["gold"]
    return question_scores(outs, gold)


def write_arm_results(out_dir: Path, ledger: Ledger, qids: Sequence[str], *, commit: str, stage: str, pool: str, counter: int,
                      cache_hits: int, a0_fields: Sequence[str], clusters: Mapping[str, str], resamples: int = 10_000) -> dict:
    """One file per arm in the registered row schema; returns {arm: mean}. Intervals are the clustered bootstrap of the mean over
    questions (EXPLORATORY: development numbers decide nothing, EXPERIMENT 7.4)."""
    out_dir.mkdir(parents=True, exist_ok=True)
    means = {}
    present = sorted({a for r in ledger.rows.values() for a in r["arms"]})
    for arm in present:
        sc, failed = _scores(ledger, arm, qids)
        if not sc:
            continue
        iv = stats.clustered_bootstrap(sc, clusters, resamples=resamples) if len(sc) >= 2 else None
        rows = [{"qref": qref(q), "gold_n": ledger.rows[qref(q)]["gold_n"], "recall_at_10": sc[q], "failed": ledger.rows[qref(q)]["arms"][arm]["failed"],
                 "sessions_top10": ledger.rows[qref(q)]["arms"][arm]["sessions"]} for q in qids if q in sc]
        means[arm] = sum(sc.values()) / len(sc)
        row = {"commit": commit, "stage": stage, "arm": arm, "pool": pool, "n": len(sc), "metric": "gold-session recall@10 (EXPLORATORY)",
               "interval_method": "clustered bootstrap of the mean over questions, percentile 95%%, %d resamples" % resamples,
               "seed": stats.SEED, "call_counter": counter, "product_cache_hits": cache_hits,
               "routing": "agent-routed" if arm in ("A1", "A1-tree-only", "SHUFFLED") else "agent-routed",
               "a0_fields": list(a0_fields), "mean": means[arm], "interval": [iv.lo, iv.hi] if iv else None, "failed_recalls": failed,
               "uses_model": arm in ("A1", "A1-tree-only", "SHUFFLED"), "label": "EXPLORATORY, pilot haystacks, decides nothing"}
        write_result(str(out_dir / ("arm_%s.json" % arm.replace("+", "_plus_"))), row, rows)
    return means


def comparisons(ledger: Ledger, qids: Sequence[str], clusters: Mapping[str, str], resamples: int = 10_000) -> dict:
    """Exploratory comparisons of the S2 row: A1 minus A0, and the SHUFFLED-SUMMARIES control (A1-tree-only minus SHUFFLED), plus
    the tree-only source share of 2.2 (c). The stage gate itself is a human reading of these with the failure rate."""
    s = {a: _scores(ledger, a, qids)[0] for a in ARMS_H1 + ("A0+LEN",)}
    out: dict = {}
    # A1 minus A0 (and minus A0+LEN, the length control, when a beta was fitted) over the questions both arms answered; the SHUFFLED
    # control over the questions where SHUFFLED ran (S3 runs it on a subset), A1-tree-only restricted to the same ones
    def paired(x, y, keep=None):
        ks = sorted(set(s[x]) & set(s[y]) & (set(keep) if keep is not None else set(s[x])))
        return ({k: s[x][k] for k in ks}, {k: s[y][k] for k in ks})
    a, b = paired("A1", "A0")
    if len(a) >= 2:
        a1a0 = stats.clustered_bootstrap(stats.paired_differences(a, b), clusters, resamples=resamples)
        out["A1_minus_A0"] = {"point": a1a0.point, "lo": a1a0.lo, "hi": a1a0.hi, "n": a1a0.n_questions, "clusters": a1a0.n_clusters}
    a, b = paired("A1", "A0+LEN")
    if len(a) >= 2:
        d = stats.clustered_bootstrap(stats.paired_differences(a, b), clusters, resamples=resamples)
        out["A1_minus_A0_plus_LEN"] = {"point": d.point, "lo": d.lo, "hi": d.hi, "n": d.n_questions, "clusters": d.n_clusters}
    a, b = paired("A1-tree-only", "SHUFFLED")
    if len(a) >= 2:
        drop = stats.clustered_bootstrap(stats.paired_differences(a, b), clusters, resamples=resamples)
        ctrl = stats.shuffled_control(drop)
        out["SHUFFLED_control"] = {"drop_point": drop.point, "drop_lo": drop.lo, "drop_hi": drop.hi, "passes_registered_rule": ctrl.passed,
                                   "n": drop.n_questions, "rule": ctrl.reason + " (registered for 50 questions; exploratory)"}
    withgold = [r for r in ledger.rows.values() if r["sources"]]
    out["tree_only_share"] = {"questions_with_gold_in_A1_top10": len(withgold),
                              "of_which_tree_only": sum("tree_only" in r["sources"] for r in withgold)}
    tot = {"tree_only": 0, "direct_only": 0, "both": 0, "neither": 0}
    for r in withgold:
        for x in r["sources"]:
            tot[x] = tot.get(x, 0) + 1
    out["top10_gold_by_source"] = tot
    return out


def summaries(calls_path: Path, journal_path: Path, *, window_s: float = 5 * 3600) -> dict:
    """Calls by task kind, tokens, latency, failures and the largest count of calls in any 5-hour window, from the call log
    (written by the capped port) and the journal (timestamps)."""
    rows = [json.loads(l) for l in Path(calls_path).read_text().splitlines() if l.strip()] if Path(calls_path).exists() else []
    by: Dict[str, dict] = {}
    for r in rows:
        b = by.setdefault(r["task"], {"calls": 0, "ok": 0, "latency": [], "in": 0, "out": 0})
        b["calls"] += 1
        b["ok"] += bool(r.get("strict_ok"))
        b["latency"].append(r.get("latency_s", 0.0))
        b["in"] += sum(r.get(k, 0) for k in ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"))
        b["out"] += r.get("output_tokens", 0)
    tasks = {}
    for k, b in by.items():
        lat = sorted(b["latency"])
        tasks[k] = {"calls": b["calls"], "valid": b["ok"], "input_tokens": b["in"], "output_tokens": b["out"],
                    "latency_median_s": statistics.median(lat), "latency_p90_s": lat[min(len(lat) - 1, int(0.9 * len(lat)))]}
    total = len(rows)
    bad = sum(not r.get("strict_ok") for r in rows)
    ts = sorted(j["ts"] for j in (json.loads(l) for l in Path(journal_path).read_text().splitlines() if l.strip())
                if j.get("t") == "result" and "ts" in j and j.get("error") != "lost_in_flight") if Path(journal_path).exists() else []
    peak, lo = 0, 0
    for hi in range(len(ts)):
        while ts[hi] - ts[lo] > window_s:
            lo += 1
        peak = max(peak, hi - lo + 1)
    return {"calls": total, "invalid_or_failed": bad, "failure_rate": (bad / total) if total else None,
            "throttled": sum(bool(r.get("throttled")) for r in rows), "by_task": tasks,
            "wall_seconds_in_calls": sum(r.get("latency_s", 0.0) for r in rows),
            "max_calls_in_any_5h_window": peak, "span_hours": ((ts[-1] - ts[0]) / 3600) if len(ts) > 1 else 0.0}
