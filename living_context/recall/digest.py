"""The pointer field contract and the descriptor checks (DESIGN INV-7, 8.2 to 8.4; BUILD-PLAN Step 10).

A returned pointer, and every digest entry, carries exactly: pointer, title, size, path, reason, confidence. No body,
gist or fact field is ever copied into it (a one-sentence leaf's gist is the whole leaf). Titles are code-built
(`ingest/title.py`), reasons and outline labels are at most 12 words and share no 5-gram with the call's input.
"""
from __future__ import annotations

import re
from typing import Iterable, Mapping

from ..ingest import title as titlemod

POINTER_FIELDS = frozenset({"pointer", "title", "size", "path", "reason", "confidence"})
DIGEST_KEYS = frozenset({"outline", "pointers", "sizes", "node_pointers", "pinned", "sources", "counts"})
NODE_POINTER_FIELDS = frozenset({"pointer", "title", "size", "total", "path"})
OUTLINE_FIELDS = frozenset({"label", "pointers", "reason"})
FORBIDDEN = frozenset({"text", "body", "gist", "facts", "fact", "summary", "content", "snippet"})


class ContractViolation(Exception):
    pass


def words(text: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", text.lower())


def ngrams(text: str, n: int) -> set[tuple]:
    w = words(text)
    return {tuple(w[i:i + n]) for i in range(len(w) - n + 1)}


def shares_ngram(reason: str, sources: Iterable[str], n: int = 5) -> bool:
    mine = ngrams(reason, n)
    return bool(mine) and any(mine & ngrams(s, n) for s in sources)


def reason_ok(reason, sources: Iterable[str], max_words: int = 12, n: int = 5) -> bool:
    return isinstance(reason, str) and len(words(reason)) <= max_words and not shares_ngram(reason, sources, n)


def make_pointer(pointer: str, title: str, size: int, path: str, reason: str, confidence: int) -> dict:
    return {"pointer": pointer, "title": title, "size": int(size), "path": path, "reason": reason, "confidence": int(confidence)}


def check_pointer(p: Mapping, allowed=POINTER_FIELDS) -> None:
    extra = set(p) - set(allowed)
    if extra or FORBIDDEN & set(p):
        raise ContractViolation(f"pointer carries a field outside the contract: {sorted(extra)}")
    if set(p) != set(allowed):
        raise ContractViolation("pointer is missing a contract field")


def check_digest(d: Mapping) -> None:
    """Walk every entry of a digest; any field outside the contract anywhere is a violation."""
    if set(d) - DIGEST_KEYS:
        raise ContractViolation("digest carries a key outside the contract")
    for p in d["pointers"]:
        check_pointer(p)
    for p in d["node_pointers"]:
        check_pointer(p, NODE_POINTER_FIELDS)
    for sec in d["sources"].values():
        for p in sec:
            check_pointer(p)
    for o in d["outline"]:
        if set(o) - OUTLINE_FIELDS:
            raise ContractViolation("outline entry carries a field outside the contract")


def code_title(text: str, n: int = 4) -> str:
    """The only title path: top terms by code, no token containing a digit."""
    return titlemod.build_title(text, n=n)
