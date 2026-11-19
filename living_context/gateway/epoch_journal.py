"""Bounded per-tenant epoch journal, skeleton (DESIGN 4.4; BUILD-PLAN Step 5).

A preallocated file of fixed size `j_size` plus a spare of the same size,
checksummed length-prefixed records, `fsync` of the file and of its directory.
This step builds only the files, append, replay with torn-record detection and
provisioning of the spare; the durable start record, compaction, the clean
stop and break-glass recovery are Step 15.
"""
from __future__ import annotations

import json
import os
import struct
import zlib
from pathlib import Path

HEADER = struct.Struct(">II")  # length, crc32 of the body


class JournalFull(Exception):
    pass


def preallocate(path: Path, size: int) -> None:
    """Create a file of exactly `size` zero bytes with its blocks allocated (no sparse file)."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.posix_fallocate(fd, 0, size)
        os.fsync(fd)
    finally:
        os.close(fd)
    dfd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(dfd)
    finally:
        os.close(dfd)


def provision(directory: Path, size: int) -> tuple[Path, Path]:
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    j, s = directory / "epochs.journal", directory / "epochs.spare"
    preallocate(j, size)
    preallocate(s, size)
    return j, s


class Journal:
    def __init__(self, path: Path):
        self.path = path
        self.size = path.stat().st_size
        self.records, self.end, self.torn = self._scan()
        self._fd = os.open(path, os.O_RDWR)

    def _scan(self):
        data = self.path.read_bytes()
        pos, out = 0, []
        while pos + HEADER.size <= len(data):
            length, crc = HEADER.unpack_from(data, pos)
            if length == 0 and crc == 0:
                return out, pos, False  # clean end: zeros
            body = data[pos + HEADER.size: pos + HEADER.size + length]
            if len(body) != length or zlib.crc32(body) != crc:
                return out, pos, True  # torn or corrupt record
            out.append(json.loads(body))
            pos += HEADER.size + length
        return out, pos, False

    def append(self, record: dict) -> None:
        body = json.dumps(record, sort_keys=True).encode()
        frame = HEADER.pack(len(body), zlib.crc32(body)) + body
        if self.end + len(frame) > self.size:
            raise JournalFull()
        os.pwrite(self._fd, frame, self.end)
        os.fsync(self._fd)
        self.end += len(frame)
        self.records.append(record)

    def close(self) -> None:
        os.close(self._fd)
