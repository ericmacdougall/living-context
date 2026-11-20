"""D9 on generated multi-pool layouts with the REFERENCE selection rule (tunekit.pool_reference): the share of questions whose granted
answer pool is in the selection S, by layout and P_MAX, with Wilson intervals; selections naming an ungranted pool; mean pools
queried. Needs only cards and questions (the pools' leaves are not read), embedded with the pinned local embedder. Zero model calls.

  python -m tunekit.run_pools --model MODEL_DIR --out FILE --seeds 1,2,3
"""
from __future__ import annotations

import argparse
import json
from collections import defaultdict

import numpy as np

from . import pool_reference, synth_pools
from .stats import wilson


def run(embedder, seeds, layouts, pmaxes, taus):
    acc = defaultdict(lambda: defaultdict(int))
    cos_stats = defaultdict(list)
    for (n_real, n_decoy, cov) in layouts:
        for seed in seeds:
            w = synth_pools.generate(synth_pools.PoolKnobs(seed=seed, n_pools=n_real, n_decoys=n_decoy, n_leak=1, leaves_per_pool=40,
                                                          card_coverage=cov, n_questions=400))
            ids = [p["id"] for p in w.pools]
            cv = embedder.passages([p["card"] for p in w.pools])
            qv = embedder.queries([q["text"] for q in w.questions])
            cards = dict(zip(ids, cv))
            granted = [p["id"] for p in w.pools if p["granted"]]
            for pmax in pmaxes:
                for tau in taus:
                    key = (n_real, n_decoy, cov, pmax, tau)
                    for q, v in zip(w.questions, qv):
                        if q["kind"] == "none":
                            continue
                        sel = pool_reference.select(v, cards, granted, p_max=pmax, tau_card=tau)
                        a = acc[key]
                        a["n"] += 1
                        a["hit_any"] += any(p in sel for p in q["answer_pools"])
                        a["hit_all"] += all(p in sel for p in q["answer_pools"])
                        a["pools_queried"] += len(sel)
                        a["ungranted_selected"] += any(p not in granted for p in sel)
            for q, v in zip(w.questions, qv):
                if q["kind"] != "none":
                    cs = sorted((float(np.dot(v, cards[p])) for p in granted), reverse=True)
                    cos_stats[(n_real, n_decoy, cov)].append((float(np.dot(v, cards[q["answer_pools"][0]])), cs[0], cs[-1]))
    rows = []
    for key, a in sorted(acc.items()):
        p, lo, hi = wilson(a["hit_any"], a["n"])
        rows.append({"n_real": key[0], "n_decoy": key[1], "card_coverage": key[2], "p_max": key[3], "tau_card": key[4], "n": a["n"],
                     "answer_pool_in_S": round(p, 4), "lo": round(lo, 4), "hi": round(hi, 4),
                     "all_answer_pools_in_S": round(a["hit_all"] / a["n"], 4), "mean_pools_queried": round(a["pools_queried"] / a["n"], 2),
                     "ungranted_selected": a["ungranted_selected"]})
    cs = [{"n_real": k[0], "n_decoy": k[1], "card_coverage": k[2], "mean_cos_answer_card": round(float(np.mean([x[0] for x in v])), 4),
           "mean_cos_top_card": round(float(np.mean([x[1] for x in v])), 4), "mean_cos_lowest_card": round(float(np.mean([x[2] for x in v])), 4)}
          for k, v in sorted(cos_stats.items())]
    return rows, cs


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--seeds", default="1,2,3")
    a = ap.parse_args(argv)
    from .e5 import BatchEmbedder
    emb = BatchEmbedder(a.model, threads=4)
    layouts = [(n, n // 2 + 1, cov) for n in (6, 12, 24) for cov in (0.3, 0.6)]
    rows, cs = run(emb, [int(x) for x in a.seeds.split(",")], layouts, (1, 2, 4, 8), (0.2, 0.8, 0.85))
    json.dump({"rows": rows, "cosines": cs, "seeds": a.seeds, "rule": "reference reimplementation of POOLS 4.2 (not product code)"},
              open(a.out, "w"), indent=1)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
