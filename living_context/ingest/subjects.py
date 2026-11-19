"""Subject detection and the shared tombstone guard (DESIGN 7.3, INV-8, 14.3; BUILD-PLAN Step 6).

Detection finds identifiers in text by shape (email addresses, @handles) and bare person names
(two capitalised words). Tokens are HMACs from the keystore, so the guard keeps no plaintext.
Outcomes, identical for every writer path:
  * an erased identifier (email, handle: declared with context)  -> REFUSED, the same typed answer as any policy refusal
  * a bare-name match of an erased identity (no declared context) -> HELD (quarantine), the same
    typed answer as any other hold; never silently refused, and neither reveals that a name was erased
  * otherwise OK
Mention detection misses spellings it was never told about: a stated limit (DESIGN 20).
"""
from __future__ import annotations

import hashlib
import hmac
import re
from typing import Iterable

from ..keystore.canon import canonicalise

OK, HELD, REFUSED = "ok", "held", "refused"
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
_HANDLE = re.compile(r"(?<![\w@])@[A-Za-z0-9_]{3,32}")
_NAME = re.compile(r"(?=\b([A-Z][a-z]{2,}\s+[A-Z][a-z]{2,})\b)")  # overlapping pairs


def identifiers(text: str) -> list[str]:
    return _EMAIL.findall(text) + _HANDLE.findall(text)


def bare_names(text: str) -> list[str]:
    return _NAME.findall(text)


class TokenSource:
    """Wraps a keystore's HMAC key: one purpose per prefix so a name token never equals an identifier token."""

    def __init__(self, keystore):
        self._ks = keystore

    def identifier(self, text: str) -> str:
        return self._ks.subject_token(text)

    def bare(self, name: str) -> str:
        key = self._ks._hmac_key("subject")
        return hmac.new(key, b"bare:" + canonicalise(name).encode(), hashlib.sha256).digest()[:16].hex()


class Guard:
    """The shared guard consulted by every path that can write, rebuild, replay, restore or re-ingest."""

    def __init__(self, tokens: TokenSource):
        self._t = tokens
        self._erased_ids: set[str] = set()
        self._erased_bare: set[str] = set()

    def erase_identifier(self, identifier: str, *, bare_name: str | None = None) -> None:
        self._erased_ids.add(self._t.identifier(identifier))
        if bare_name:
            self._erased_bare.add(self._t.bare(bare_name))

    def tokens_in(self, text: str) -> list[str]:
        """Subject tokens (identifier and bare-name) found in `text`: the lineage of anything stored from it."""
        return sorted({self._t.identifier(i) for i in identifiers(text)} | {self._t.bare(n) for n in bare_names(text)})

    def check(self, text: str, *, check_variants: Iterable[str] = ()) -> str:
        for ident in identifiers(text):
            if self._t.identifier(ident) in self._erased_ids:
                return REFUSED
        for name in bare_names(text):
            if self._t.bare(name) in self._erased_bare:
                return HELD
        return OK


class Idempotency:
    """Idempotency keys scoped to (tenant, principal, key) with the body hash (DESIGN 7.1)."""

    class KeyReuse(Exception):
        pass

    def __init__(self):
        self._seen: dict[tuple[str, str, str], tuple[str, object]] = {}

    @staticmethod
    def body_hash(body: bytes) -> str:
        return hashlib.sha256(body).hexdigest()

    def run(self, tenant: str, principal: str, key: str, body: bytes, fn):
        """Return the first result for the same key and body; refuse the same key with another body."""
        k, h = (tenant, principal, key), self.body_hash(body)
        if k in self._seen:
            first_hash, first = self._seen[k]
            if first_hash != h:
                raise Idempotency.KeyReuse()
            return first
        result = fn()
        self._seen[k] = (h, result)
        return result
