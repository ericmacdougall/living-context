"""The per-tenant tombstone journal (DESIGN 5.9, 14.3, 14.6; BUILD-PLAN Step 14).

An append-only file of JSON lines, each carrying the sha256 of its predecessor's hash and its own content (a hash
chain), fsynced (file, and the directory on creation) before an append returns, and replicated to a second location.
It holds tombstones: scope (subject token, leaf ids), the redact-set node ids, and the erased identifier HMACs; never
content. On open the chain is verified: a broken chain or a record the replica lacks fails closed (`JournalCorrupt`);
a primary that lost its tail is repaired from the replica. The journal is the truth a restore replays (14.6)."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Callable, Optional

GENESIS = "0" * 64


class JournalCorrupt(Exception):
    pass


def _canon(seq: int, scope: dict, prev: str) -> bytes:
    return json.dumps({"seq": seq, "scope": scope, "prev": prev}, sort_keys=True, separators=(",", ":")).encode()


def _hash(seq: int, scope: dict, prev: str) -> str:
    return hashlib.sha256(_canon(seq, scope, prev)).hexdigest()


def _read(path: Path, chain: bool = True) -> list[dict]:
    out, prev = [], GENESIS
    if not path.exists():
        return out
    for n, line in enumerate(path.read_text().splitlines(), start=1):
        try:
            r = json.loads(line)
        except ValueError:
            raise JournalCorrupt("unreadable record") from None
        if chain and (r["prev"] != prev or r["hash"] != _hash(r["seq"], r["scope"], prev) or r["seq"] != n):
            raise JournalCorrupt("broken hash chain")
        out.append(r)
        prev = r["hash"]
    return out


class TombstoneJournal:
    def __init__(self, primary: Path, replica: Path, *, fsync: Callable[[int], None] = os.fsync, chain: bool = True):
        """`chain=False` is the BROKEN variant used by tests: records carry no verifiable chain."""
        self._p, self._r, self._fsync, self._chain = Path(primary), Path(replica), fsync, chain
        self._p.parent.mkdir(parents=True, exist_ok=True)
        self._r.parent.mkdir(parents=True, exist_ok=True)
        a, b = _read(self._p, chain), _read(self._r, chain)
        longer = a if len(a) >= len(b) else b
        if a != longer[: len(a)] or b != longer[: len(b)]:
            raise JournalCorrupt("primary and replica disagree")
        for path, have in ((self._p, a), (self._r, b)):  # repair a lost tail from the other copy
            if len(have) < len(longer):
                self._rewrite(path, longer)
        self._records = longer

    def _rewrite(self, path: Path, records: list[dict]) -> None:
        tmp = path.with_suffix(".new")
        with open(tmp, "w") as f:
            f.write("".join(json.dumps(r, sort_keys=True) + "\n" for r in records))
            f.flush()
            self._fsync(f.fileno())
        os.replace(tmp, path)
        self._sync_dir(path.parent)

    def _sync_dir(self, d: Path) -> None:
        fd = os.open(d, os.O_RDONLY)
        try:
            self._fsync(fd)
        finally:
            os.close(fd)

    @property
    def last_seq(self) -> int:
        return len(self._records)

    def records(self) -> list[dict]:
        return list(self._records)

    def append(self, scope: dict) -> int:
        seq = len(self._records) + 1
        prev = self._records[-1]["hash"] if self._records else GENESIS
        rec = {"seq": seq, "scope": scope, "prev": prev, "hash": _hash(seq, scope, prev)}
        line = json.dumps(rec, sort_keys=True) + "\n"
        for path in (self._p, self._r):
            new = not path.exists()
            with open(path, "a") as f:
                f.write(line)
                f.flush()
                self._fsync(f.fileno())
            if new:
                self._sync_dir(path.parent)
        self._records.append(rec)
        return seq
