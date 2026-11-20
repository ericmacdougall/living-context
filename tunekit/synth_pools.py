"""G6, multi-pool layouts (TUNING 2.1): several memory pools of one principal, each with its own vocabulary domain and a card; the
ground truth says which pool holds the answer to each question.

Pool roles:
  real     holds answers; granted to the principal
  decoy    granted, no answers, and a LOOK-ALIKE card (it repeats half of a real pool's card words)
  leak     NOT granted, holds copies of facts that real pools hold (a selection that includes it is a leakage failure, INV-12)
A question's `kind` is `one` (answer in one granted pool), `two` (the same fact is held by two granted pools) or `none` (the answer
is in no pool). Cards are written by a card generator with its own template and word choice, separate from the question generator:
a card lists a `card_coverage` share of the pool's topic words, with filler from a card-only vocabulary.

Template text again: cards and questions share an invented vocabulary, so a card-versus-question cosine is easier to separate
than between a real description of a notebook and a real question (see docs/tuning). No model call."""
from __future__ import annotations

import random
from dataclasses import asdict, dataclass
from typing import Dict, List

from . import synth

CARD_OPENERS = ("Notes and decisions about", "A running record covering", "Everything filed under", "Shared memory on")
CARD_CLOSERS = ("kept by the team", "updated weekly", "mostly short entries", "owned by the project lead")


@dataclass(frozen=True)
class PoolKnobs:
    seed: int
    n_pools: int = 8               # real pools
    leaves_per_pool: int = 200
    n_decoys: int = 4
    n_leak: int = 1
    card_coverage: float = 0.4
    two_pool_frac: float = 0.10
    none_frac: float = 0.10
    n_questions: int = 400
    level: float = 0.5


@dataclass
class PoolWorld:
    knobs: PoolKnobs
    pools: List[dict]
    questions: List[dict]
    manifest: dict


def _card(rng: random.Random, words: List[str], coverage: float, k_max: int = 8) -> str:
    k = max(2, min(k_max, round(len(words) * coverage)))
    pick = rng.sample(words, min(k, len(words)))
    return f"{rng.choice(CARD_OPENERS)} {', '.join(pick[:-1])} and {pick[-1]}; {rng.choice(CARD_CLOSERS)}."


def generate(k: PoolKnobs) -> PoolWorld:
    if k.n_pools + k.n_decoys + k.n_leak + 1 > 39:
        raise ValueError("at most 39 pools including the withheld one")
    rng = random.Random(f"{synth.GEN_VERSION}|pools|{k.seed}")
    ids = [f"pl{i:02d}" for i in range(k.n_pools + k.n_decoys + k.n_leak)]
    corpora: Dict[str, synth.Corpus] = {}
    pools: List[dict] = []
    for i, pid in enumerate(ids):
        role = "real" if i < k.n_pools else ("decoy" if i < k.n_pools + k.n_decoys else "leak")
        kn = synth.knobs_at(k.leaves_per_pool, k.seed * 100 + i, k.level, domain=i + 1, n_questions=60 if role == "real" else 0)
        c = synth.generate(kn)
        corpora[pid] = c
        pools.append({"id": pid, "role": role, "granted": role != "leak", "leaves": c.leaves, "card": ""})
    withheld = synth.generate(synth.knobs_at(k.leaves_per_pool, k.seed * 100 + 99, k.level, domain=39, n_questions=60))
    vocab = {p["id"]: sorted({w for t in corpora[p["id"]].topics for w in t if w not in synth.COMMON}) or sorted({w for t in corpora[p["id"]].topics for w in t})
             for p in pools}
    real = [p for p in pools if p["role"] == "real"]
    for p in pools:
        if p["role"] == "real":
            p["card"] = _card(rng, vocab[p["id"]], k.card_coverage)
    for j, p in enumerate([q for q in pools if q["role"] == "decoy"]):
        target = real[j % len(real)]
        shared = rng.sample(vocab[target["id"]], max(2, round(len(vocab[target["id"]]) * k.card_coverage / 2)))
        own = rng.sample(vocab[p["id"]], min(len(vocab[p["id"]]), len(shared)))
        p["card"] = _card(rng, shared + own, 1.0, k_max=len(shared) + len(own))
        p["lookalike_of"] = target["id"]
    for j, p in enumerate([q for q in pools if q["role"] == "leak"]):
        target = real[j % len(real)]
        p["card"] = _card(rng, vocab[target["id"]], k.card_coverage)
        p["lookalike_of"] = target["id"]

    qs: List[dict] = []
    pool_by = {p["id"]: p for p in pools}
    pools_real = [p["id"] for p in real]
    for qi in range(k.n_questions):
        u = rng.random()
        if u < k.none_frac:
            q = rng.choice(withheld.questions)
            qs.append({"qid": qi, "text": q["text"], "kind": "none", "answer_pools": [], "gold": {}})
            continue
        pid = rng.choice(pools_real)
        q = rng.choice(corpora[pid].questions)
        gold = {pid: list(q["gold"])}
        kind = "one"
        if u < k.none_frac + k.two_pool_frac and len(pools_real) > 1:
            other = rng.choice([x for x in pools_real if x != pid])
            pool_by[other]["leaves"] = pool_by[other]["leaves"] + [dict(corpora[pid].leaves[q["gold"][0]], id=len(pool_by[other]["leaves"]), copy_of=pid)]
            gold[other] = [len(pool_by[other]["leaves"]) - 1]
            kind = "two"
        for p in pools:
            if p["role"] == "leak" and p.get("lookalike_of") == pid:
                p["leaves"] = p["leaves"] + [dict(corpora[pid].leaves[q["gold"][0]], id=len(p["leaves"]), copy_of=pid)]
        qs.append({"qid": qi, "text": q["text"], "kind": kind, "answer_pools": sorted(gold), "gold": gold})
    man = {"generator": synth.GEN_VERSION, "knobs": asdict(k), "n_pools_total": len(pools), "roles": {r: sum(p["role"] == r for p in pools)
           for r in ("real", "decoy", "leak")}, "kinds": {kd: sum(q["kind"] == kd for q in qs) for kd in ("one", "two", "none")},
           "pool_sizes": {p["id"]: len(p["leaves"]) for p in pools}, "text_caveat": "template text and template cards; not natural language"}
    return PoolWorld(k, pools, qs, man)
