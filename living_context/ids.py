"""Identifiers and the pointer codec (DESIGN 5.2, 5.4, 5.5, 8.4; BUILD-PLAN Step 2).

* Leaf and node ids: 128 random bits from the operating system's generator,
  stable for life and never reused. The randomness source is injectable so a
  test can show that a generator seeded from the clock is caught.
* Edge ids: deterministic, `sha256(tenant, src_id, dst_id, type)` truncated to
  128 bits, over length-prefixed fields so no two different tuples share an
  encoding.
* Pointers: `(gen, id, version)` for a leaf or a node, with an opaque external
  form. The form is NOT a credential: a forged or altered pointer is accepted
  or refused by the resolver (`unavailable`), never trusted. The checksum only
  catches transcription damage.
"""
from __future__ import annotations

import base64
import hashlib
import os
import struct
from dataclasses import dataclass
from typing import Callable

ID_BYTES = 16
EntropySource = Callable[[int], bytes]

KIND_LEAF = 1
KIND_NODE = 2
_KINDS = {KIND_LEAF: "leaf", KIND_NODE: "node"}


class IdError(ValueError):
    """A malformed identifier or pointer."""


class IdGenerator:
    """Generates 128-bit random ids. `source` returns n random bytes."""

    def __init__(self, source: EntropySource = os.urandom):
        self._source = source

    def new_id(self) -> str:
        raw = self._source(ID_BYTES)
        if len(raw) != ID_BYTES:
            raise IdError("entropy source returned the wrong number of bytes")
        return raw.hex()


def new_leaf_id(gen: IdGenerator | None = None) -> str:
    return (gen or IdGenerator()).new_id()


def new_node_id(gen: IdGenerator | None = None) -> str:
    return (gen or IdGenerator()).new_id()


def check_id(value: str) -> str:
    if not isinstance(value, str) or len(value) != ID_BYTES * 2 or value != value.lower():
        raise IdError("an id is 32 lowercase hex characters")
    try:
        bytes.fromhex(value)
    except ValueError as e:
        raise IdError("an id is 32 lowercase hex characters") from e
    return value


def _field(value: str) -> bytes:
    raw = value.encode("utf-8")
    return struct.pack(">I", len(raw)) + raw


def edge_id(tenant: str, src_id: str, dst_id: str, edge_type: str) -> str:
    """Deterministic edge id; the same endpoints and type give the same id."""
    h = hashlib.sha256()
    for part in (tenant, src_id, dst_id, edge_type):
        h.update(_field(part))
    return h.digest()[:ID_BYTES].hex()


@dataclass(frozen=True)
class Pointer:
    kind: int  # KIND_LEAF or KIND_NODE
    gen: int
    id: str
    version: int

    def __post_init__(self):
        if self.kind not in _KINDS:
            raise IdError("unknown pointer kind")
        if not 0 <= self.gen < 1 << 64 or not 0 <= self.version < 1 << 32:
            raise IdError("pointer number out of range")
        check_id(self.id)


_FMT = ">BBQ16sI"  # format version, kind, gen, id, version
_FORMAT_VERSION = 1
_CHECK_LEN = 4


def encode_pointer(p: Pointer) -> str:
    body = struct.pack(_FMT, _FORMAT_VERSION, p.kind, p.gen, bytes.fromhex(p.id), p.version)
    check = hashlib.sha256(b"lc-pointer" + body).digest()[:_CHECK_LEN]
    return base64.b32encode(body + check).decode("ascii").rstrip("=").lower()


def decode_pointer(text: str) -> Pointer:
    """Strict decoder: one canonical spelling per pointer, anything else is refused."""
    if not isinstance(text, str) or not text or text != text.lower() or "=" in text:
        raise IdError("not a pointer")
    pad = "=" * (-len(text) % 8)
    try:
        raw = base64.b32decode(text.upper() + pad)
    except (ValueError, base64.binascii.Error) as e:
        raise IdError("not a pointer") from e
    size = struct.calcsize(_FMT)
    if len(raw) != size + _CHECK_LEN:
        raise IdError("not a pointer")
    body, check = raw[:size], raw[size:]
    if hashlib.sha256(b"lc-pointer" + body).digest()[:_CHECK_LEN] != check:
        raise IdError("not a pointer")
    fmt, kind, gen, id_bytes, version = struct.unpack(_FMT, body)
    if fmt != _FORMAT_VERSION:
        raise IdError("not a pointer")
    p = Pointer(kind, gen, id_bytes.hex(), version)
    if encode_pointer(p) != text:  # reject non-canonical encodings
        raise IdError("not a pointer")
    return p
