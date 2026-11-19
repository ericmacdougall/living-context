"""Exploration slots (DESIGN 9; BUILD-PLAN Step 11; EXPERIMENT 3.4, review F5).

A small share of digest slots (`exploration_share`, 1 in 10) is filled AFTER the ranked list, at random, from the reader's own
classes, excluding every leaf of the last `L` insert batches (so new leaves are not favoured by being new). The slot's
propensity is the probability the leaf was shown under this policy: k slots drawn without replacement from n eligible leaves
include each with probability k/n (1.0 for the ranked offers). That is the one field that cannot be reconstructed later.
The `broken_*` flags exist for the tests' broken variants only."""
from __future__ import annotations

import random
from dataclasses import dataclass
from typing import Iterable, Optional, Sequence

from .log import Offer


@dataclass(frozen=True)
class Candidate:
    leaf_id: str
    lv: int
    cls: str
    batch: int            # insert batch number of the leaf
    subjects: tuple = ()


def slots_for(n_ranked: int, share: float) -> int:
    return int(n_ranked * share / (1.0 - share) + 1e-9) if 0 <= share < 1 else 0   # share of the final digest, not of the ranked part


def place(ranked: Sequence[Offer], pool: Iterable[Candidate], *, readable_classes: Iterable[str], current_batch: int, lag: int,
          share: float, rng: random.Random, broken_include_recent: bool = False, broken_before_ranked: bool = False,
          broken_any_class: bool = False, broken_propensity_one: bool = False) -> list[Offer]:
    classes = set(readable_classes)
    taken = {(o.leaf_id, o.lv) for o in ranked}
    eligible = [c for c in pool
                if (c.leaf_id, c.lv) not in taken
                and (broken_any_class or c.cls in classes)
                and (broken_include_recent or c.batch <= current_batch - lag)]
    k = min(slots_for(len(ranked), share), len(eligible))
    if k <= 0:
        return list(ranked)
    chosen = rng.sample(sorted(eligible, key=lambda c: (c.leaf_id, c.lv)), k)   # sorted first: the draw depends on the seed only
    prop = 1.0 if broken_propensity_one else k / len(eligible)
    extra = [Offer(c.leaf_id, c.lv, c.cls, ("explore",), 0, subjects=c.subjects, exploration=True, propensity=prop) for c in chosen]
    out = (extra + list(ranked)) if broken_before_ranked else (list(ranked) + extra)
    return [Offer(o.leaf_id, o.lv, o.item_cls, o.sources, i, o.confidence, o.subjects, o.exploration, o.propensity, o.fallback)
            for i, o in enumerate(out)]
