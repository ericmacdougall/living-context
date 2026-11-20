"""Per-tenant keystore (DESIGN 3 DEVIATION 1, 5.1; BUILD-PLAN Step 4).

One SQLite file per tenant (so one tenant's long write transaction cannot hold
another tenant's destroy), `secure_delete=ON` and `journal_mode=WAL` set
explicitly, `wal_checkpoint(TRUNCATE)` after every destroy batch so no page
image of a destroyed key stays in the `-wal` file. AES-256-GCM data keys per
leaf version; per-tenant HMAC keys for subject and principal tokens.

Limits (DESIGN 20): freed filesystem blocks and flash wear levelling can still
hold old bytes; this module controls the file layer only.
"""
from __future__ import annotations

import hashlib
import hmac
import os
import re
import sqlite3
import struct
import threading
from pathlib import Path
from typing import Iterable

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from .canon import canonicalise

TOKEN_BYTES = 16
_TENANT = re.compile(r"^[a-z0-9]{4,16}$")


class KeystoreError(Exception):
    pass


class LookalikeRefused(KeystoreError):
    """The identifier's canonical token equals that of a different existing identity."""


class KeyDestroyed(KeystoreError):
    """The data key of this leaf version no longer exists."""


def _aad(tenant: str, leaf_id: str, lv: int) -> bytes:
    out = b""
    for part in (tenant.encode(), leaf_id.encode()):
        out += struct.pack(">I", len(part)) + part
    return out + struct.pack(">Q", lv)


class Keystore:
    """The keystore of one tenant."""

    BUSY_TIMEOUT_MS = 2000

    def __init__(self, path: str | os.PathLike, tenant: str):
        if not _TENANT.match(tenant):
            raise KeystoreError("bad tenant id")
        self.tenant = tenant
        self._lock = threading.RLock()  # one connection shared by the gateway's threads: calls are serialised
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fresh = not self.path.exists()
        self._db = sqlite3.connect(str(self.path), isolation_level=None, check_same_thread=False, timeout=self.BUSY_TIMEOUT_MS / 1000)
        self._configure(self._db)
        if fresh:
            os.chmod(self.path, 0o600)
        self._db.executescript(
            """
            CREATE TABLE IF NOT EXISTS leaf_keys(
                leaf_id TEXT NOT NULL, lv INTEGER NOT NULL, key_ref TEXT NOT NULL UNIQUE,
                key BLOB NOT NULL, PRIMARY KEY(leaf_id, lv));
            CREATE TABLE IF NOT EXISTS leaf_subjects(
                leaf_id TEXT NOT NULL, lv INTEGER NOT NULL, token TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS leaf_subjects_token ON leaf_subjects(token);
            CREATE TABLE IF NOT EXISTS tenant_keys(name TEXT PRIMARY KEY, key BLOB NOT NULL);
            CREATE TABLE IF NOT EXISTS identities(
                token TEXT NOT NULL, exact TEXT NOT NULL, PRIMARY KEY(token, exact));
            """)
        for name in ("subject", "principal", "idem", "content"):
            self._db.execute("INSERT OR IGNORE INTO tenant_keys(name, key) VALUES (?, ?)",
                             (name, os.urandom(32)))

    # --- the two knobs the broken variants of the tests override -----------------
    def _configure(self, db: sqlite3.Connection) -> None:
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA secure_delete=ON")
        db.execute(f"PRAGMA busy_timeout={self.BUSY_TIMEOUT_MS}")

    def _after_destroy(self, db: sqlite3.Connection) -> None:
        # VACUUM rewrites every page from the live rows: the unique index on key_ref keeps a copy of a deleted reference in an
        # interior page in about 3 percent of destroys (seen in 10 of 240 scans), which `secure_delete` does not clear.
        db.execute("VACUUM")
        db.execute("PRAGMA wal_checkpoint(TRUNCATE)")

    # --- tokens -----------------------------------------------------------------
    def _hmac_key(self, name: str) -> bytes:
        return self._db.execute("SELECT key FROM tenant_keys WHERE name=?", (name,)).fetchone()[0]

    def subject_token(self, identifier: str, *, confusables: bool = True) -> str:
        msg = canonicalise(identifier, confusables=confusables).encode()
        return hmac.new(self._hmac_key("subject"), msg, hashlib.sha256).digest()[:TOKEN_BYTES].hex()

    def digest(self, purpose: str, data: bytes) -> str:
        """Keyed digest of `data` (idempotency bodies, content hashes): an HMAC under a per-tenant
        key, so a stored digest is no oracle for guessing content."""
        return hmac.new(self._hmac_key(purpose), data, hashlib.sha256).hexdigest()

    def has_key(self, leaf_id: str, lv: int) -> bool:
        return self._db.execute("SELECT 1 FROM leaf_keys WHERE leaf_id=? AND lv=?", (leaf_id, lv)).fetchone() is not None

    def principal_token(self, identifier: str) -> str:
        msg = canonicalise(identifier).encode()
        return hmac.new(self._hmac_key("principal"), msg, hashlib.sha256).digest()[:TOKEN_BYTES].hex()

    def register_identifier(self, identifier: str) -> str:
        """Declare an identifier; returns its subject token. Fails closed: an identifier whose
        canonical token equals that of a DIFFERENT existing identity (a look-alike such as
        `clark` and `dark`) is refused, so two identities are never conflated. Registering the
        same identifier again is idempotent. `exact` is an HMAC of the identifier with only NFKC
        and case folding applied, so look-alikes are told apart without storing the identifier."""
        import unicodedata
        token = self.subject_token(identifier)
        exact_msg = unicodedata.normalize("NFKC", identifier).casefold().strip().encode()
        exact = hmac.new(self._hmac_key("subject"), b"exact:" + exact_msg, hashlib.sha256).digest()[:TOKEN_BYTES].hex()
        db = self._db
        db.execute("BEGIN IMMEDIATE")
        try:
            rows = db.execute("SELECT exact FROM identities WHERE token=?", (token,)).fetchall()
            if any(r[0] != exact for r in rows):
                db.execute("ROLLBACK")
                raise LookalikeRefused("look-alike of an existing identity")
            db.execute("INSERT OR IGNORE INTO identities(token, exact) VALUES (?,?)", (token, exact))
            db.execute("COMMIT")
        except LookalikeRefused:
            raise
        except Exception:
            db.execute("ROLLBACK")
            raise
        return token

    # --- data keys ----------------------------------------------------------------
    def encrypt(self, leaf_id: str, lv: int, plaintext: bytes, subject_tokens: Iterable[str]):
        """Create the data key of this leaf version; return (ciphertext, nonce, key_ref)."""
        key, key_ref, nonce = AESGCM.generate_key(256), os.urandom(16).hex(), os.urandom(12)
        ct = AESGCM(key).encrypt(nonce, plaintext, _aad(self.tenant, leaf_id, lv))
        db = self._db
        db.execute("BEGIN IMMEDIATE")
        try:
            db.execute("INSERT INTO leaf_keys(leaf_id, lv, key_ref, key) VALUES (?,?,?,?)",
                       (leaf_id, lv, key_ref, key))
            db.executemany("INSERT INTO leaf_subjects(leaf_id, lv, token) VALUES (?,?,?)",
                           [(leaf_id, lv, t) for t in set(subject_tokens)])
            db.execute("COMMIT")
        except Exception:
            db.execute("ROLLBACK")
            raise
        return ct, nonce, key_ref

    def decrypt(self, leaf_id: str, lv: int, ciphertext: bytes, nonce: bytes) -> bytes:
        row = self._db.execute("SELECT key FROM leaf_keys WHERE leaf_id=? AND lv=?", (leaf_id, lv)).fetchone()
        if row is None:
            raise KeyDestroyed("no data key for this leaf version")
        try:
            return AESGCM(row[0]).decrypt(nonce, ciphertext, _aad(self.tenant, leaf_id, lv))
        except InvalidTag as e:
            raise KeystoreError("ciphertext does not match this leaf version") from e

    # --- destroy ------------------------------------------------------------------
    def destroy_versions(self, versions: Iterable[tuple]) -> list[str]:
        """Destroy the data keys of the named (leaf id, highest version to destroy) pairs: every version of the leaf up to and
        including that one. A relabel's replay uses it so the keys of the new-class version, which has a higher number, stay."""
        db = self._db
        db.execute("BEGIN IMMEDIATE")
        try:
            rows = []
            for lid, max_lv in sorted(set(versions)):
                rows += db.execute("SELECT leaf_id, lv, key_ref FROM leaf_keys WHERE leaf_id=? AND lv<=?", (lid, int(max_lv))).fetchall()
            for lid, lv, _ in rows:
                db.execute("DELETE FROM leaf_keys WHERE leaf_id=? AND lv=?", (lid, lv))
                db.execute("DELETE FROM leaf_subjects WHERE leaf_id=? AND lv=?", (lid, lv))
            db.execute("COMMIT")
        except Exception:
            db.execute("ROLLBACK")
            raise
        if rows:
            self._after_destroy(db)
        return [r[2] for r in rows]

    def destroy_exact_version(self, leaf_id: str, lv: int) -> list[str]:
        """Destroy the data key of exactly one (leaf id, version). A writer that finds a key made for a version whose append never
        happened uses this, so the live earlier versions of the same leaf keep their keys."""
        db = self._db
        db.execute("BEGIN IMMEDIATE")
        try:
            rows = db.execute("SELECT key_ref FROM leaf_keys WHERE leaf_id=? AND lv=?", (leaf_id, int(lv))).fetchall()
            db.execute("DELETE FROM leaf_keys WHERE leaf_id=? AND lv=?", (leaf_id, int(lv)))
            db.execute("DELETE FROM leaf_subjects WHERE leaf_id=? AND lv=?", (leaf_id, int(lv)))
            db.execute("COMMIT")
        except Exception:
            db.execute("ROLLBACK")
            raise
        if rows:
            self._after_destroy(db)
        return [r[0] for r in rows]

    def destroy(self, *, subject_token: str | None = None, leaf_id: str | None = None,
                leaf_ids: Iterable[str] | None = None) -> list[str]:
        """Destroy every data key named by a subject token, by a leaf, or by a set of leaves (one VACUUM for the whole call,
        none when no key was found). Returns the destroyed key references (ids only), after the checkpoint.
        Cost limit: the VACUUM rewrites the whole file, measured 3 ms at 1,000 keys and 170 to 185 ms at 100,000 keys."""
        if sum(x is not None for x in (subject_token, leaf_id, leaf_ids)) != 1:
            raise KeystoreError("name exactly one of subject_token, leaf_id, leaf_ids")
        db = self._db
        db.execute("BEGIN IMMEDIATE")
        try:
            if subject_token is not None:
                rows = db.execute(
                    "SELECT k.leaf_id, k.lv, k.key_ref FROM leaf_keys k JOIN leaf_subjects s "
                    "ON s.leaf_id=k.leaf_id AND s.lv=k.lv WHERE s.token=?", (subject_token,)).fetchall()
            else:
                rows = []
                for lid in ([leaf_id] if leaf_id is not None else sorted(set(leaf_ids))):
                    rows += db.execute("SELECT leaf_id, lv, key_ref FROM leaf_keys WHERE leaf_id=?", (lid,)).fetchall()
            for lid, lv, _ in rows:
                db.execute("DELETE FROM leaf_keys WHERE leaf_id=? AND lv=?", (lid, lv))
                db.execute("DELETE FROM leaf_subjects WHERE leaf_id=? AND lv=?", (lid, lv))
            db.execute("COMMIT")
        except Exception:
            db.execute("ROLLBACK")
            raise
        if rows:
            self._after_destroy(db)
        return [r[2] for r in rows]

    def count_keys(self) -> int:
        return self._db.execute("SELECT COUNT(*) FROM leaf_keys").fetchone()[0]

    def close(self) -> None:
        self._db.close()


def _locked(fn):
    def wrapper(self, *a, **kw):
        with self._lock:
            return fn(self, *a, **kw)
    wrapper.__name__, wrapper.__doc__ = fn.__name__, fn.__doc__
    return wrapper


for _name in ("subject_token", "digest", "has_key", "principal_token", "register_identifier", "encrypt",
              "decrypt", "destroy", "destroy_versions", "count_keys"):
    setattr(Keystore, _name, _locked(getattr(Keystore, _name)))


class KeystoreSet:
    """One keystore file per tenant under `root`."""

    def __init__(self, root: str | os.PathLike, factory=Keystore):
        self.root = Path(root)
        self._factory = factory
        self._open: dict[str, Keystore] = {}

    def path_for(self, tenant: str) -> Path:
        return self.root / f"{tenant}.db"

    def get(self, tenant: str) -> Keystore:
        if tenant not in self._open:
            self._open[tenant] = self._factory(self.path_for(tenant), tenant)
        return self._open[tenant]

    def close(self) -> None:
        for k in self._open.values():
            k.close()
        self._open.clear()
