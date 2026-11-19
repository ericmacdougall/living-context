"""Per-question scoring and the result file (EXPERIMENT 5.2, 11; BUILD-PLAN Step 20).

`question_scores` turns arm outputs into per-question gold-session recall@10: abstention questions are excluded, a question
whose recall call failed scores 0 and is counted (INV-11), gold is the question's gold CONTENT HASHES. The `broken_*`
switches are the variants the tests must turn red."""
from __future__ import annotations

import json
import os
from typing import Dict, Mapping, Optional, Sequence, Set, Tuple

from .arms import ArmOut, K, recall_at_k

REQUIRED_FIELDS = ("commit", "stage", "arm", "pool", "n", "metric", "interval_method", "seed", "call_counter",
                   "product_cache_hits", "routing")   # EXPERIMENT 11; routing = agent-routed | proxy | COMPARISON
ROUTING = ("agent-routed", "proxy", "COMPARISON")


def question_scores(outs: Mapping[str, ArmOut], gold: Mapping[str, Sequence[str]], abstention: Set[str] = frozenset(), *,
                    broken_include_abstention: bool = False, broken_skip_failed: bool = False) -> Tuple[Dict[str, float], int]:
    scores: Dict[str, float] = {}
    failed = 0
    for qid, out in outs.items():
        if qid in abstention and not broken_include_abstention:
            continue
        g = gold.get(qid, [])
        if not g and not broken_include_abstention:
            continue
        if out.failed:
            failed += 1
            if broken_skip_failed:
                continue
            scores[qid] = 0.0
            continue
        scores[qid] = recall_at_k(out.sessions, g, K) if g else 0.0
    return scores, failed


class ResultError(Exception):
    pass


def check_result(row: Mapping) -> None:
    missing = [f for f in REQUIRED_FIELDS if f not in row or row[f] in (None, "")]
    if missing:
        raise ResultError("result row is missing required fields: %s" % ", ".join(missing))
    if row["routing"] not in ROUTING:
        raise ResultError("routing must be one of %s" % (ROUTING,))


def write_result(path: str, row: Mapping, per_question: Optional[Sequence[Mapping]] = None) -> None:
    """Refuses a row missing any required field; ranked lists and per-question rows go in the same file so every figure
    can be recomputed from rows."""
    check_result(row)
    doc = dict(row)
    if per_question is not None:
        doc["rows"] = list(per_question)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(doc, f, indent=1, sort_keys=True)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)
