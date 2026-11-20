"""Write the independence evidence as a small JSON table: complexity statistics of generated corpora at two sizes per level (wide
growth), the same under deep growth (the control that must move), and the statistics at one size across levels.

  python -m tunekit.independence_report OUT.json"""
from __future__ import annotations

import json
import sys

from . import synth

KEYS = ("topic_jaccard", "entity_reuse", "dup_frac", "old_frac", "mean_session_len", "mean_words", "leaves_per_topic", "q_alias_frac",
        "q_para_frac", "q_mean_topic_words", "set_mean_gold")


def avg(n, level, seeds=(1, 2, 3), **over):
    acc = {k: 0.0 for k in KEYS}
    drop = 0.0
    for s in seeds:
        c = synth.generate(synth.knobs_at(n, s, level, **over))
        for k in KEYS:
            acc[k] += c.manifest["stats"][k] / len(seeds)
        drop += c.manifest["question_drop_rate"] / len(seeds)
    acc["question_drop_rate"] = drop
    return {k: round(v, 4) for k, v in acc.items()}


def main(out):
    rep = {"generator": synth.GEN_VERSION, "seeds": [1, 2, 3], "wide": [], "deep_control": [], "across_levels_at_2000": []}
    for lv in (0.0, 0.5, 1.0):
        for n in (600, 6000):
            rep["wide"].append({"level": lv, "n_leaves": n, **avg(n, lv)})
    for n in (600, 6000):
        rep["deep_control"].append({"level": 0.5, "growth": "deep", "fixed_topics": 24, "n_leaves": n, **avg(n, 0.5, growth="deep", fixed_topics=24)})
    for lv in (0.0, 0.5, 1.0):
        rep["across_levels_at_2000"].append({"level": lv, "n_leaves": 2000, **avg(2000, lv)})
    with open(out, "w") as f:
        json.dump(rep, f, indent=1)


if __name__ == "__main__":
    main(sys.argv[1])
