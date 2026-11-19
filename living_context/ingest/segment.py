"""Segmentation (DESIGN 7.2, 17; BUILD-PLAN Step 6): hard leaf cap, never exceeded.

Order: cut at session boundaries and time gaps over 30 minutes; within a session cut at
embedding-similarity valleys between adjacent round windows; then anything still over the cap is
split at the round boundary nearest the middle, then at a turn boundary, then at a sentence
boundary, and a single sentence over the cap at a token (character) boundary. Pieces of a split
are reported as `continues` pairs. Token counts use `ceil(chars / 2.4)`.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Optional

from ..embed.stub import cosine, embed

GAP_SECONDS = 30 * 60  # ASSUMPTION


class Empty(ValueError):
    """Empty or whitespace-only input (a typed refusal)."""


@dataclass(frozen=True)
class Turn:
    role: str
    content: str
    ts: Optional[float] = None


def tokens_of(text: str, chars_per_token: float = 2.4) -> int:
    return math.ceil(len(text) / chars_per_token)


def max_chars(cap_tokens: int, chars_per_token: float = 2.4) -> int:
    return int(cap_tokens * chars_per_token)  # tokens_of(text) <= cap  iff  len(text) <= this


def render(turns: list[Turn]) -> str:
    return "\n".join(f"{t.role}: {t.content}" for t in turns)


def split_text(text: str, limit: int) -> list[str]:
    """Split one piece of text into pieces of at most `limit` characters: nearest-to-middle sentence
    boundary first, then a hard character boundary."""
    if len(text) <= limit:
        return [text]
    mid = len(text) // 2
    cuts = [m.end() for m in re.finditer(r"(?<=[.!?])\s+|\n", text)]
    cuts = [c for c in cuts if 0 < c < len(text)]
    if cuts:
        c = min(cuts, key=lambda x: abs(x - mid))
        return split_text(text[:c], limit) + split_text(text[c:], limit)
    return split_text(text[:limit], limit) + split_text(text[limit:], limit)


def split_turns(turns: list[Turn], limit: int, *, rounds_only: bool = False) -> list[list[Turn]]:
    """Recursively split a list of turns so that render() of each piece fits `limit` characters."""
    if len(render(turns)) <= limit:
        return [turns]
    if len(turns) > 1:  # round (turn pair) boundary nearest the middle, then any turn boundary
        mid = len(turns) // 2
        cut = mid if mid % 2 == 0 else mid + 1 if mid + 1 < len(turns) else mid
        cut = max(1, min(cut, len(turns) - 1))
        return split_turns(turns[:cut], limit, rounds_only=rounds_only) + split_turns(turns[cut:], limit, rounds_only=rounds_only)
    if rounds_only:
        return [turns]  # broken variant: cannot split inside a turn
    t = turns[0]
    room = limit - len(f"{t.role}: ")
    return [[Turn(t.role, piece, t.ts)] for piece in split_text(t.content, max(1, room))]


def valleys(turns: list[Turn], window: int = 4, drop: float = 0.15) -> list[int]:
    """Indices where adjacent round windows are markedly less similar than the session average
    (TextTiling-style, stub embeddings; threshold ASSUMPTION)."""
    if len(turns) < 2 * window:
        return []
    sims = []
    for i in range(window, len(turns) - window + 1):
        a = embed(render(turns[i - window:i]))
        b = embed(render(turns[i:i + window]))
        sims.append((i, cosine(a, b)))
    mean = sum(s for _, s in sims) / len(sims)
    cuts, last = [], -window
    for i, s in sims:
        if s < mean - drop and i - last >= window:
            cuts.append(i)
            last = i
    return cuts


def segment(sessions: list[list[Turn]], cap_tokens: int = 1536, *, rounds_only: bool = False
            ) -> tuple[list[list[Turn]], list[tuple[int, int]]]:
    """Return (leaves as lists of turns, continues pairs of leaf indices). Raises Empty."""
    if not any(t.content.strip() for s in sessions for t in s):
        raise Empty("empty input")
    limit = max_chars(cap_tokens)
    leaves: list[list[Turn]] = []
    continues: list[tuple[int, int]] = []
    for s in sessions:
        s = [t for t in s if t.content.strip()]
        if not s:
            continue
        cuts = {0, len(s)}
        for i in range(1, len(s)):  # time gaps
            if s[i].ts is not None and s[i - 1].ts is not None and s[i].ts - s[i - 1].ts > GAP_SECONDS:
                cuts.add(i)
        cuts |= set(valleys(s))
        bounds = sorted(cuts)
        for a, b in zip(bounds, bounds[1:]):
            pieces = split_turns(s[a:b], limit, rounds_only=rounds_only)
            first = len(leaves)
            leaves += pieces
            continues += [(first + k, first + k + 1) for k in range(len(pieces) - 1)]
    return leaves, continues
