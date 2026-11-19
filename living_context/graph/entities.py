"""Entity extraction by code (the stub's stand-in for a model-extracted entity list; DESIGN 7.7 "leaves sharing an entity").

An entity is a capitalised word of at least four letters that does not start a sentence, lower-cased. It is a deterministic
function of the leaf text, so the same text always gives the same entities (a stub reading; a model would replace it).
"""
from __future__ import annotations

import re

_SENT = re.compile(r"(?<=[.!?])\s+")
_WORD = re.compile(r"[A-Za-z][A-Za-z0-9]*")


def extract(text: str, limit: int = 12) -> list[str]:
    found: list[str] = []
    for sent in _SENT.split(text):
        words = _WORD.findall(sent)
        for w in words[1:]:
            if len(w) >= 4 and w[0].isupper() and not w.isupper():
                e = w.lower()
                if e not in found:
                    found.append(e)
    return found[:limit]
