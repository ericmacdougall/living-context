"""The code-built title (DESIGN INV-7, 5.2, 8.2; BUILD-PLAN Step 6).

ONE function, `build_title`, builds every leaf and node title: the top `n` tf-idf terms of the
text with every token that contains a digit left out, so numeric dates, amounts and account
codes written with digits never reach a title (digit-free forms such as a month name can;
a limit, DESIGN 20). The choice between content-derived text and none is still the owner's
to confirm, so a documented alternative sits behind the same function:

    MODE_NO_CONTENT: the title is empty for every text (no descriptor at all); pointers then
    carry only their pointer, size, path, reason and confidence. Switch with `set_mode`.

A model never writes a title.
"""
from __future__ import annotations

import math
import re
from collections import Counter
from typing import Mapping, Optional

MODE_TFIDF = "tfidf"
MODE_NO_CONTENT = "no_content"
_mode = MODE_TFIDF

_WORD = re.compile(r"[^\W\d_]+(?:['’-][^\W\d_]+)*", re.UNICODE)
_STOP = frozenset("""a about above after again all also am an and any are as at be because been before being below
between both but by can could did do does doing down during each few for from further had has have having he her here
hers him his how i if in into is it its just me more most my no nor not now of off on once only or other our out over
own same she should so some such than that the their them then there these they this those through to too under until
up very was we were what when where which while who whom why will with would you your yours""".split())


def set_mode(mode: str) -> None:
    global _mode
    if mode not in (MODE_TFIDF, MODE_NO_CONTENT):
        raise ValueError("unknown title mode")
    _mode = mode


def get_mode() -> str:
    return _mode


def has_digit(tok: str) -> bool:
    return any(c.isdigit() for c in tok)


def build_title(text: str, df: Optional[Mapping[str, int]] = None, n_docs: int = 0, n: int = 4,
                mode: Optional[str] = None) -> str:
    """Top `n` tf-idf terms joined by spaces. `df` (document frequencies over the tenant's class)
    and `n_docs` give idf; without them idf is 1 and the order is by term frequency, then spelling.
    Tokens containing a digit are dropped BEFORE ranking."""
    if (mode or _mode) == MODE_NO_CONTENT:
        return ""
    raw = re.findall(r"\S+", text.lower())  # whitespace tokens: a token with a digit anywhere is dropped whole
    words = []
    for tok in raw:
        if has_digit(tok):
            continue
        words += [w for w in _WORD.findall(tok) if len(w) > 2 and w not in _STOP]
    tf = Counter(words)
    def score(w):
        idf = math.log((1 + n_docs) / (1 + (df or {}).get(w, 0))) + 1 if n_docs else 1.0
        return (-(tf[w] * idf), w)
    return " ".join(sorted(tf, key=score)[:n])
