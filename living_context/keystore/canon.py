"""Identifier canonicalisation (DESIGN 5, review M1).

NFKC, then case folding, then accents folded, then a confusables skeleton,
then whitespace collapsed, so a variant spelling of an erased identifier maps
to the same subject token.

The confusables table below is a compact SUBSET of the Unicode TR39
confusables data (https://www.unicode.org/reports/tr39/): the Cyrillic and
Greek letters that look like Latin letters (lower case, since case folding
runs first), the digit look-alikes 0 and 1, and the multi-character shapes
`rn`, `vv` and `cl`. It over-matches by design (for example `clark` and
`dark`); identifiers are declared with context and a bare-name match is held
for review, not refused (DESIGN 7.3). A full table is a
later refinement; the limit is stated in DESIGN 20 ("mentions under an
undeclared spelling"). Folding accents is an ASSUMPTION needed by BUILD-PLAN
Step 4's variant table (case, width, accents, homoglyphs).
"""
from __future__ import annotations

import re
import unicodedata

# source character -> prototype (applied after NFKC, casefold and accent folding)
_CONFUSABLE_PAIRS = {
    # Cyrillic
    "а": "a", "в": "b", "е": "e", "к": "k", "м": "m", "н": "h", "о": "o", "р": "p", "с": "c", "т": "t",
    "у": "y", "х": "x", "ѕ": "s", "і": "l", "ј": "j", "ԁ": "d", "ԛ": "q", "ԝ": "w", "ӏ": "l",
    # Greek
    "α": "a", "β": "b", "ε": "e", "ι": "l", "κ": "k", "ν": "v", "ο": "o", "ρ": "p", "τ": "t",
    "υ": "u", "χ": "x", "ω": "w",
    # Latin look-alikes and digits (TR39 maps 1, l and I together, and O with 0)
    "ı": "l", "ɡ": "g", "ɑ": "a", "ɩ": "l", "ʋ": "u", "ᴏ": "o", "ᴠ": "v", "ᴡ": "w", "ᴢ": "z",
    "0": "o", "1": "l", "ǀ": "l", "|": "l",
}
_MULTI = (("rn", "m"), ("vv", "w"), ("cl", "d"))  # shapes that read alike

_TABLE = str.maketrans(_CONFUSABLE_PAIRS)
_SPACE = re.compile(r"\s+")


def _fold_accents(text: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFD", text) if unicodedata.category(c) != "Mn")


def canonicalise(identifier: str, *, confusables: bool = True) -> str:
    """Return the canonical form used before HMAC. `confusables=False` is the
    broken variant that BUILD-PLAN Step 4 requires the tests to catch."""
    s = unicodedata.normalize("NFKC", identifier)
    s = s.casefold()
    s = _fold_accents(s)
    s = unicodedata.normalize("NFKC", s)
    if confusables:
        s = s.translate(_TABLE)
        for shape, proto in _MULTI:
            s = s.replace(shape, proto)
    return _SPACE.sub(" ", s).strip()
