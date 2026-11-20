"""Sealed pointers (POOLS 3.6): `AEAD(k_t, (pool, kind, gen, id, version))` with the tenant id as associated data.

Agents see an opaque string. To resolve one the gateway opens it (a failed open is `unavailable`), decides authorization from the
sealed pool and the token alone, and only then reads. A forged or altered pointer, one of another tenant (or a look-alike tenant id,
which changes the associated data) and one sealed under a retired key version all fail to open. ASSUMPTION: AES-256-GCM-SIV (POOLS 3.6);
the key version is in a clear prefix. The key file holds one `<version>:<hex>` line per version, mode 0600, never logged.
"""
from __future__ import annotations

import base64
import hmac
import os
import struct
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCMSIV

from ..ids import KIND_LEAF, check_id
from .indices import POOL_ID

FORMAT = 1
_BODY = struct.Struct(">16sBQ16sI")


@dataclass(frozen=True)
class Sealed:
    pool: str
    kind: int
    gen: int
    id: str
    version: int


class PointerKeys:
    def __init__(self, path: Path):
        self._path = Path(path)
        self._keys: dict[int, bytes] = {}
        self._retired: set[int] = set()
        if self._path.exists():
            for line in self._path.read_text().split():
                v, _, h = line.partition(":")
                self._keys[int(v)] = bytes.fromhex(h)
        else:
            self.rotate()

    def rotate(self) -> int:
        v = max(self._keys, default=0) + 1
        self._keys[v] = os.urandom(32)
        self._path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        tmp = self._path.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write("\n".join(f"{k}:{x.hex()}" for k, x in sorted(self._keys.items())) + "\n")
        os.replace(tmp, self._path)
        return v

    def retire(self, version: int) -> None:
        self._retired.add(version)

    def current(self) -> tuple[int, bytes]:
        v = max(k for k in self._keys if k not in self._retired)
        return v, self._keys[v]

    def get(self, version: int) -> Optional[bytes]:
        return None if version in self._retired else self._keys.get(version)


def seal(keys: PointerKeys, tenant: str, s: Sealed, *, rand=None) -> str:
    v, k = keys.current()
    body = _BODY.pack(s.pool.encode(), s.kind, s.gen, bytes.fromhex(s.id), s.version)
    # SIV is nonce-misuse resistant: a nonce derived from the content makes a pointer stable, so the same leaf version is always the
    # same string (replays and comparisons) and nothing about the content is revealed beyond equality
    nonce = hmac.new(k, b"nonce" + tenant.encode() + b"\x00" + body, "sha256").digest()[:12] if rand is None else rand(12)
    ct = AESGCMSIV(k).encrypt(nonce, body, tenant.encode())
    return base64.b32encode(bytes([FORMAT, v]) + nonce + ct).decode().rstrip("=").lower()


def open_(keys: PointerKeys, tenant: str, text: str) -> Optional[Sealed]:
    """The sealed content, or None for anything that does not open (one answer: the caller returns `unavailable`)."""
    try:
        if not isinstance(text, str) or not text or text != text.lower() or len(text) > 400:
            return None
        raw = base64.b32decode(text.upper() + "=" * (-len(text) % 8))
        if len(raw) < 2 + 12 + _BODY.size + 16 or raw[0] != FORMAT:
            return None
        k = keys.get(raw[1])
        if k is None:
            return None
        body = AESGCMSIV(k).decrypt(raw[2:14], raw[14:], tenant.encode())
        pool, kind, gen, idb, ver = _BODY.unpack(body)
        pool = pool.decode()
        if not POOL_ID.match(pool):
            return None
        s = Sealed(pool, kind, gen, check_id(idb.hex()), ver)
        return s if kind == KIND_LEAF else None
    except (InvalidTag, ValueError, struct.error, UnicodeDecodeError):
        return None
