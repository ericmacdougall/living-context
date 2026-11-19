"""Client-side reciprocal rank fusion (DESIGN 8.5; BUILD-PLAN Step 7): k = 60, window = 50."""
from __future__ import annotations

from typing import Hashable, Sequence

K_RRF = 60
WINDOW = 50


def rrf(rankings: Sequence[Sequence[Hashable]], k: int = K_RRF, window: int = WINDOW) -> list[tuple[Hashable, float]]:
    """Fuse ranked id lists. Only the first `window` entries of each list count; an id appearing twice
    in one list counts once, at its first rank. Ties break by id so the order is deterministic."""
    score: dict[Hashable, float] = {}
    for ranking in rankings:
        seen = set()
        for rank, ident in enumerate(ranking[:window], start=1):
            if ident in seen:
                continue
            seen.add(ident)
            score[ident] = score.get(ident, 0.0) + 1.0 / (k + rank)
    return sorted(score.items(), key=lambda kv: (-kv[1], str(kv[0])))
