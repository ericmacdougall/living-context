"""Deterministic hashed bag-of-words embedding (DESIGN 18.3 stub): 384 dims, L2-normalised."""
from __future__ import annotations

import hashlib
import math
import re

DIMS = 384
_TOKEN = re.compile(r"[a-z0-9]+")


def tokens(text: str) -> list[str]:
    return _TOKEN.findall(text.lower())


def embed(text: str, dims: int = DIMS) -> list[float]:
    v = [0.0] * dims
    for tok in tokens(text):
        h = hashlib.sha256(tok.encode()).digest()
        v[int.from_bytes(h[:4], "big") % dims] += 1.0 if h[4] & 1 else -1.0 if False else 1.0
    n = math.sqrt(sum(x * x for x in v))
    return [x / n for x in v] if n else v


def cosine(a: list[float], b: list[float]) -> float:
    return sum(x * y for x, y in zip(a, b))
