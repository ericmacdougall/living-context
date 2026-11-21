"""The S6-H1 question loop, its ledger and the H1 decision (EXPERIMENT 2.1, 2.2, 5.1, 5.2, 6, 7.1). Pure with respect to the model and
Elasticsearch: the retrievers are passed in, so the same code runs on the real stack (`run_s6h1`) and, in tests, on the in-memory corpus.

Ledger row per completed question (`done.jsonl`; opaque references only, no question or session text, no gold id):
  qref, stratum (primary | dev_exposed | abstention), qtype, gold (opaque session refs), every arm's top-10 sessions and failed flag
  (A0, A0+LEN with the frozen beta, A1, A1-tree-only, and SHUFFLED on the registered 50), the source of each gold session in A1's top 10,
  holder calls and fallbacks, and the two input lists cut at 50: `direct50` ([leaf, session ref, score, session tokens], so A0 and A0+LEN
  can be recomputed for any beta) and `tree50` ([leaf, session ref], so A1 and the source check can be recomputed).

The decision (`decide`), exactly as registered:
  (a) A1 minus A0 and (b) A1 minus A0+LEN on gold-session recall@10 over the PRIMARY questions (293: non-abstention, not DEV-EXPOSED),
      clustered paired bootstrap over content clusters, percentile 95%, 10,000 resamples, seed 16096 (BCa beside it as a sensitivity);
  (c) among primary questions with at least one gold session in A1's top 10, the share where one came only from the tree (its leaves in
      the tree list and none in the direct list's top 50), Wilson 95%; its lower bound must be above 0;
  word: the worse of (a) and (b) by the outcome words of 2.1 (stats.decide_h1; HARM on (a) only); (c) failing makes it NOT ESTABLISHED.
  SHUFFLED-SUMMARIES: A1-tree-only minus SHUFFLED on its 50 questions must drop with point >= 0.10 and lower bound > 0 (section 6). A
  control that does not fail voids the arm it controls (2.2); reading R3 (ASSUMPTION, recorded in the result): SHUFFLED controls the
  tree list that (c) rests on, so a SHUFFLED that does not fail makes H1 NOT ESTABLISHED (void); the rule's own word is kept beside it.
Reported beside, never deciding: the same on the 299 (DEV-EXPOSED included), the DEV-EXPOSED stratum, the abstention stratum (13: no
recall; offered sessions and holder calls), per-type means, and top-10 gold by source."""
from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from . import arms, dataset as ds, stats
from .parallel import run_ordered
from .s2_ports import StageStopped
from .s2_stage import CapReached, Ledger, Recorder, _scores, port_counts, qref
from .score import write_result

STAGE = "S6-H1"
ARMS_S6 = ("A0", "A0+LEN", "A1", "A1-tree-only", "SHUFFLED")
TREE_ARMS = ("A1", "A1-tree-only", "SHUFFLED")


def seeded_order(qids: Sequence[str], seed: int = ds.SPLIT_SEED) -> List[str]:
    return sorted(qids, key=lambda q: hashlib.sha256(("%d:%s" % (seed, q)).encode()).hexdigest())


def shuffled_subset(fs: ds.FreshSet, n: int = 50) -> List[str]:
    """SHUFFLED-SUMMARIES runs on the first n PRIMARY questions in the seeded order (ASSUMPTION R4: the registration says "on 50
    questions"; S3 took the first 50 of its seeded order the same way)."""
    return seeded_order(fs.primary)[:n]


def stratum_of(fs: ds.FreshSet) -> Dict[str, str]:
    out = {q: "primary" for q in fs.primary}
    out.update({q: "dev_exposed" for q in fs.dev_exposed})
    out.update({q: "abstention" for q in fs.abstention})
    return out


def run_questions(fs: ds.FreshSet, hashes: Mapping[str, str], direct: Optional[Recorder], tree: Optional[Recorder],
                  shuffled: Optional[Recorder], world: arms.World, ledger: Ledger, *, beta: float, shuffled_qids: Sequence[str],
                  stop: Callable[[], bool] = lambda: False, progress: Callable[[str], None] = lambda s: None,
                  scope: Callable[[Optional[str]], None] = lambda s: None,
                  retrievers: Optional[Callable[[str], Tuple[Recorder, Recorder, Recorder]]] = None, workers: int = 1) -> None:
    """Every fresh question not yet in the ledger, in the seeded order; SHUFFLED only on `shuffled_qids`. A question is recorded only
    when it completed; the stop file and the cap end the loop before recording (as in S2).

    One worker: the given recorders, and `scope(ref)` is called before each question (the run sets the journal scope of its port, so a
    resume replays only the in-flight question's own calls). Several workers: `retrievers(ref)` gives each question its own
    (direct, tree, shuffled) recorders over a port scoped to that question, questions run concurrently, and their rows are appended
    strictly in the seeded order (`parallel.run_ordered`), so the ledger is byte-identical at any concurrency."""
    if workers > 1 and retrievers is None:
        raise ValueError("several workers need per-question retrievers (a shared recorder is shared state)")
    by = {q.qid: q for q in fs.questions}
    strata = stratum_of(fs)
    shuf = set(shuffled_qids)
    order = seeded_order(list(by))
    todo = [(n, qid) for n, qid in enumerate(order, 1) if qref(qid) not in ledger.rows]

    def one(item) -> dict:
        n, qid = item
        q = by[qid]
        ref = qref(qid)
        if retrievers is not None:
            d, t, sh = retrievers(ref)
        else:
            scope(ref)
            d, t, sh = direct, tree, shuffled
        g = sorted({hashes[s] for s in q.gold_session_ids}) if not q.is_abstention else []
        o = arms.run_question(q.question, d, t, world, beta=beta, shuffled_tree=sh if qid in shuf else None)
        for rec in (t, sh if qid in shuf else None):
            if rec is not None and rec.last is not None and not rec.last.ok and rec.last.error == "BudgetExceeded":
                raise CapReached("call cap reached during question %d" % n)
        dlist = list(d.last)[:arms.WINDOW]
        tids = list(t.last.ids)[:arms.WINDOW] if t.last is not None and t.last.ok else []
        src = arms.source_of_gold(o["A1"], tids, [i for i, _ in dlist], g, world) if (g and o["A1"].sessions) else {}
        sess = lambda lid: ds.opaque_ref(world.leaf_session[lid]) if lid in world.leaf_session else None
        return {
            "qref": ref, "stratum": strata[qid], "qtype": q.qtype, "gold_n": len(g), "gold": [ds.opaque_ref(x) for x in g],
            "arms": {k: {"sessions": [ds.opaque_ref(s) for s in v.sessions], "failed": bool(v.failed), "offered": len(v.leaves)}
                     for k, v in o.items() if k in ARMS_S6},
            "sources": sorted(src.values()), "holder_calls": t.last.holder_calls, "fallbacks": t.last.fallbacks,
            **({"shuffled_fallbacks": sh.last.fallbacks} if qid in shuf and sh is not None and sh.last is not None else {}),
            **({"port": pc} if (pc := port_counts(A1=t, SHUFFLED=sh if qid in shuf else None)) else {}),
            "direct50": [[lid, sess(lid), float(score), int(world.session_tokens.get(world.leaf_session.get(lid, ""), 0))]
                         for lid, score in dlist],
            "tree50": [[lid, sess(lid)] for lid in tids]}

    def commit(_i, item, row) -> None:
        ledger.append(row)
        progress("question %d/%d done" % (item[0], len(order)))

    run_ordered(todo, one, commit, workers=workers, stop=stop, stop_exc=lambda: StageStopped("stop file present"))


# ---- the decision -------------------------------------------------------------------------------------------------------------------
def _iv(iv: stats.Interval) -> dict:
    return {"point": iv.point, "lo": iv.lo, "hi": iv.hi, "n": iv.n_questions, "clusters": iv.n_clusters, "method": iv.method,
            "resamples": iv.resamples, "seed": iv.seed}


def _diff(ledger: Ledger, x: str, y: str, qids: Sequence[str], clusters: Mapping[str, str], resamples: int, *, bca: bool = False):
    a = _scores(ledger, x, qids)[0]
    b = _scores(ledger, y, qids)[0]
    return stats.clustered_bootstrap(stats.paired_differences(a, b), clusters, resamples=resamples, bca=bca)


def tree_only_share(ledger: Ledger, qids: Sequence[str]) -> tuple:
    rows = [ledger.rows[qref(q)] for q in qids]
    withgold = [r for r in rows if r["sources"]]
    return sum("tree_only" in r["sources"] for r in withgold), len(withgold)


def decide(ledger: Ledger, fs: ds.FreshSet, clusters: Mapping[str, str], *, shuffled_qids: Sequence[str],
           resamples: int = stats.RESAMPLES, shuffled_min: float = 0.10) -> dict:
    missing = [q for q in list(fs.primary) + list(fs.sensitivity) + list(fs.abstention) if qref(q) not in ledger.rows]
    if missing:
        raise ValueError("%d fresh questions are not in the ledger: the decision needs every one" % len(missing))
    prim = list(fs.primary)
    a = _diff(ledger, "A1", "A0", prim, clusters, resamples)
    b = _diff(ledger, "A1", "A0+LEN", prim, clusters, resamples)
    k, n = tree_only_share(ledger, prim)
    dec = stats.decide_h1(a, b, k, n)
    drop = _diff(ledger, "A1-tree-only", "SHUFFLED", list(shuffled_qids), clusters, resamples)
    ctrl = stats.shuffled_control(drop, min_point=shuffled_min)
    word = dec.word if ctrl.passed else stats.NOT_ESTABLISHED
    out = {
        "stage": STAGE, "hypothesis": "H1", "word": word, "word_registered_rule": dec.word, "word_a": dec.word_a, "word_b": dec.word_b,
        "reason": dec.reason if ctrl.passed else "void: SHUFFLED-SUMMARIES did not fail for its stated rule (reading R3); rule word %s"
                  % dec.word,
        "a_A1_minus_A0": _iv(a), "b_A1_minus_A0_plus_LEN": _iv(b),
        "a_bca": _iv(_diff(ledger, "A1", "A0", prim, clusters, resamples, bca=True)),
        "b_bca": _iv(_diff(ledger, "A1", "A0+LEN", prim, clusters, resamples, bca=True)),
        "c_tree_only_share": {"k": k, "n": n, "wilson_lo": dec.tree_only_share[2] if dec.tree_only_share else None,
                              "wilson_hi": dec.tree_only_share[3] if dec.tree_only_share else None},
        "shuffled_control": {"n": drop.n_questions, "clusters": drop.n_clusters, "drop_point": drop.point, "drop_lo": drop.lo,
                             "drop_hi": drop.hi, "min_point": shuffled_min, "passed": ctrl.passed, "rule": ctrl.reason,
                             "stated_reason_check": "not covered: holder logs are not stored (selections following the shuffled text)"},
        "n_primary": len(prim), "minimum_effect": stats.H1_MIN_EFFECT,
    }
    sens = list(fs.sensitivity)
    sa, sb = _diff(ledger, "A1", "A0", sens, clusters, resamples), _diff(ledger, "A1", "A0+LEN", sens, clusters, resamples)
    sk, sn = tree_only_share(ledger, sens)
    sd = stats.decide_h1(sa, sb, sk, sn)
    out["sensitivity_299"] = {"word_registered_rule": sd.word, "a": _iv(sa), "b": _iv(sb), "c": {"k": sk, "n": sn}}
    means = {}
    for name, qs in (("primary", prim), ("sensitivity", sens), ("dev_exposed", list(fs.dev_exposed))):
        means[name] = {}
        for arm in ARMS_S6:
            sc = _scores(ledger, arm, [q for q in qs if arm in ledger.rows[qref(q)]["arms"]])[0]
            means[name][arm] = {"mean": (sum(sc.values()) / len(sc)) if sc else None, "n": len(sc)}
    out["means"] = means
    by_type: Dict[str, Dict[str, List[float]]] = {}
    for q in prim:
        r = ledger.rows[qref(q)]
        for arm in ("A0", "A0+LEN", "A1"):
            sc = _scores(ledger, arm, [q])[0]
            by_type.setdefault(r["qtype"], {}).setdefault(arm, []).append(sc[q])
    out["per_type"] = {t: {arm: {"mean": sum(v) / len(v), "n": len(v)} for arm, v in d.items()} for t, d in sorted(by_type.items())}
    ab = [ledger.rows[qref(q)] for q in fs.abstention]
    out["abstention_stratum"] = {"n": len(ab), "a1_sessions_offered_mean": (sum(len(r["arms"]["A1"]["sessions"]) for r in ab) / len(ab))
                                 if ab else None, "holder_calls_mean": (sum(r["holder_calls"] for r in ab) / len(ab)) if ab else None,
                                 "note": "excluded from recall metrics (EXPERIMENT 3.1); the nothing-relevant path, reported only"}
    tot = {"tree_only": 0, "direct_only": 0, "both": 0, "neither": 0}
    for q in prim:
        for x in ledger.rows[qref(q)]["sources"]:
            tot[x] = tot.get(x, 0) + 1
    out["top10_gold_by_source_primary"] = tot
    return out


def write_results(out_dir: Path, ledger: Ledger, fs: ds.FreshSet, decision: dict, *, commit: str, counter: int, pool: str,
                  a0_fields: Sequence[str], beta: float, freeze_sha256: str, release_ref: str) -> None:
    """One file per arm in the registered row schema (EXPERIMENT 11) over the primary questions, with per-question rows; and the
    decision file. Opaque references only."""
    import json
    out_dir.mkdir(parents=True, exist_ok=True)
    prim = list(fs.primary)
    for arm in ARMS_S6:
        qs = [q for q in prim if arm in ledger.rows[qref(q)]["arms"]]
        sc, failed = _scores(ledger, arm, qs)
        if not sc:
            continue
        rows = [{"qref": qref(q), "gold_n": ledger.rows[qref(q)]["gold_n"], "recall_at_10": sc[q],
                 "failed": ledger.rows[qref(q)]["arms"][arm]["failed"], "sessions_top10": ledger.rows[qref(q)]["arms"][arm]["sessions"]}
                for q in qs]
        row = {"commit": commit, "stage": STAGE, "arm": arm, "pool": pool, "n": len(sc), "metric": "gold-session recall@10",
               "interval_method": "clustered paired bootstrap over content clusters, percentile 95%%, %d resamples" % stats.RESAMPLES,
               "seed": stats.SEED, "call_counter": counter, "product_cache_hits": 0, "routing": "agent-routed",
               "uses_model": arm in TREE_ARMS, "a0_fields": list(a0_fields), "beta": beta, "mean": sum(sc.values()) / len(sc),
               "failed_recalls": failed, "freeze_manifest_sha256": freeze_sha256, "release_ref": release_ref,
               "label": "CONFIRMATORY, fresh set (primary %d)" % len(prim) if arm != "SHUFFLED" else "control, %d questions" % len(sc)}
        write_result(str(out_dir / ("arm_%s.json" % arm.replace("+", "_plus_"))), row, rows)
    doc = dict(decision, commit=commit, call_counter=counter, pool=pool, freeze_manifest_sha256=freeze_sha256, release_ref=release_ref,
               routing="agent-routed")
    (out_dir / "h1_decision.json").write_text(json.dumps(doc, indent=1, sort_keys=True))
