"""A REFERENCE reimplementation of the POOLS 4.2 selection rule, written from the text of the design and nothing else, so that D9
can be measured before the product's rule exists. It is not product code: `stages.SelectionAdapter` is the seam, and when
`living_context.pools.select` exists the same harness runs against it and this module is only a cross-check.

Rule (POOLS 4.2, zero model calls): given the granted taint-neutral set G0 and an optional hint H:
  1. a hint is the selection (every pool of the hint must be granted, else the whole call fails);
  2. else if |G0| <= P_ALL, all of G0;
  3. else rank G0 by cosine(query embedding, card embedding), keep the top P_MAX with cosine at least TAU_CARD; if none clears
     the bar, keep the top 1."""
from __future__ import annotations

from typing import Dict, Iterable, List, Optional, Sequence

import numpy as np

P_ALL, P_MAX, TAU_CARD = 4, 8, 0.2


class NotGranted(Exception):
    pass


def select(q_vec: np.ndarray, card_vecs: Dict[str, np.ndarray], granted: Iterable[str], hint: Optional[Sequence[str]] = None, *,
           p_all: int = P_ALL, p_max: int = P_MAX, tau_card: float = TAU_CARD) -> List[str]:
    g0 = sorted(granted)
    if hint:
        if not set(hint) <= set(g0):
            raise NotGranted("a hint names a pool outside the grant")
        return list(hint)
    if len(g0) <= p_all:
        return g0
    cos = {p: float(np.dot(q_vec, card_vecs[p])) for p in g0}
    ranked = sorted(g0, key=lambda p: (-cos[p], p))
    keep = [p for p in ranked[:p_max] if cos[p] >= tau_card]
    return keep or ranked[:1]
