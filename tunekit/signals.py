"""ADAPTIVE's zero-call signals (section 4.1 memory signals, section 6.2 per-query features), computed as written, and their power
to predict plain-retrieval failures.

Computed as written:
  * memory signals `leaf_count`, `token_total`, `session_count` (distinct sessions holding at least 2 leaves; one writer, so the
    per-principal cap does not apply), `entity_count` (distinct entities occurring in at least 2 leaves, entities from the
    product's own extractor `living_context.graph.entities.extract`);
  * per-recall features `agree` (|BM25 top 10 and kNN top 10| / 10), `margin` ((s1 - s10) / s1 over the kNN cosines of ranks 1 and
    10; the cosine is recovered from Elasticsearch's cosine score as 2 * score - 1), `spread` (distinct sessions in the fused top 10),
    and the flat-sufficiency rule `agree >= A_MIN and margin >= G_MIN and spread <= S_MAX`.

NOT computable here, and not redefined: the second branch of `hard(unit)` (a later recall of the same conversation with query cosine
at least TAU_RE) needs conversation streams of recalls, which a single-question harness does not have; `il_win`, `il_loss`,
`link_*`, `multi_rate` and `open_share` need a tree arm and opens. `hard_rate` over a window needs recall units issued in order by
sessions; here every question is a unit, so `hard_rate` is the share of questions the rule calls not flat-sufficient.

Session of a leaf: the generator's session id (the leaf index of the product has no `session_ref` field in this version; the
write path stores it in `source_ref`).
"""
from __future__ import annotations

import math
from typing import Dict, List, Sequence

import numpy as np

from living_context.graph import entities as ent_mod
from living_context.ingest import segment

from . import stats

PLACEHOLDERS = {"A_MIN": 0.4, "G_MIN": 0.1, "S_MAX": 6, "N_UP1": 2000, "T_UP1": 2_500_000, "S_UP1": 300, "E_UP1": 2000,
                "N_FLOOR": 384, "R_MIN": 50, "H_UP1": 0.20}


def memory_signals(leaves: Sequence[dict], chars_per_token_bound: float = 2.4) -> dict:
    sess: Dict[int, int] = {}
    ents: Dict[str, int] = {}
    tokens = 0
    for L in leaves:
        sess[L["session"]] = sess.get(L["session"], 0) + 1
        for e in set(ent_mod.extract(L["text"])):
            ents[e] = ents.get(e, 0) + 1
        tokens += segment.tokens_of(L["text"], chars_per_token_bound)
    return {"leaf_count": len(leaves), "token_total": tokens, "session_count": sum(1 for v in sess.values() if v >= 2),
            "entity_count": sum(1 for v in ents.values() if v >= 2)}


def crossings(sig: dict) -> dict:
    """Which memory-gate thresholds (placeholders) this memory has crossed, and the N_FLOOR condition."""
    p = PLACEHOLDERS
    return {"above_N_FLOOR": sig["leaf_count"] > p["N_FLOOR"], "N_UP1": sig["leaf_count"] >= p["N_UP1"],
            "T_UP1": sig["token_total"] >= p["T_UP1"], "S_UP1": sig["session_count"] >= p["S_UP1"],
            "E_UP1": sig["entity_count"] >= p["E_UP1"]}


def query_features(bm25: Sequence[str], knn: Sequence[str], knn_cos: Sequence[float], fused: Sequence[str],
                   session_of: Dict[str, int]) -> dict:
    agree = len(set(bm25[:10]) & set(knn[:10])) / 10.0
    margin = float("nan")
    if len(knn_cos) >= 10 and knn_cos[0] > 0:
        margin = (knn_cos[0] - knn_cos[9]) / knn_cos[0]
    spread = len({session_of[d] for d in fused[:10] if d in session_of})
    return {"agree": agree, "margin": margin, "spread": spread, "knn_cos1": knn_cos[0] if knn_cos else float("nan")}


def flat_sufficient(f: dict, a_min: float = PLACEHOLDERS["A_MIN"], g_min: float = PLACEHOLDERS["G_MIN"],
                    s_max: int = PLACEHOLDERS["S_MAX"]) -> bool:
    if math.isnan(f["margin"]):
        return False          # a feature that failed escalates (6.2 step 3)
    return f["agree"] >= a_min and f["margin"] >= g_min and f["spread"] <= s_max


def predictive_power(rows: Sequence[dict], *, resamples: int = 2000) -> dict:
    """AUC (cluster bootstrap over (cell, topic) clusters) of each feature, oriented so that higher means more likely a failure,
    and the binary rule, against the label `gold not in the fused top 10`. Rows carry agree, margin, spread, hit10, cluster."""
    y = np.array([0 if r["hit10"] else 1 for r in rows])
    cl = [r["cluster"] for r in rows]
    out = {"n": len(rows), "failure_rate": float(y.mean()) if len(y) else None}
    ok = np.array([not math.isnan(r["margin"]) for r in rows])
    out["margin_missing"] = int((~ok).sum())
    cand = {
        "agree (low = hard)": [-r["agree"] for r in rows],
        "margin (low = hard)": [-(r["margin"] if not math.isnan(r["margin"]) else 0.0) for r in rows],
        "spread (high = hard)": [float(r["spread"]) for r in rows],
        "rule not flat-sufficient (placeholders)": [0.0 if flat_sufficient(r) else 1.0 for r in rows],
    }
    for name, s in cand.items():
        out[name] = stats.auc_report(s, y, cl, resamples=resamples)
    for feat in ("agree", "margin", "spread"):
        v = np.array([r[feat] for r in rows if not (isinstance(r[feat], float) and math.isnan(r[feat]))], dtype=float)
        if len(v):
            out[feat + "_quantiles"] = {f"q{q:02d}": round(float(np.percentile(v, q)), 4) for q in (5, 25, 50, 75, 95)}
    out["share_flat_sufficient"] = float(np.mean([flat_sufficient(r) for r in rows])) if rows else None
    if y.sum() and len(y) - y.sum():
        fs = np.array([flat_sufficient(r) for r in rows])
        out["failures_among_flat_sufficient"] = int(((y == 1) & fs).sum())
        out["failures_total"] = int(y.sum())
    return out
