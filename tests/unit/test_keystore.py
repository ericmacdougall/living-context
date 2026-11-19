"""BUILD-PLAN Step 4: keystore, byte scans with positive controls, broken variants."""
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path

from living_context.keystore import LookalikeRefused, KeyDestroyed, Keystore, KeystoreError, KeystoreSet, canonicalise
from living_context.keystore.store import _TENANT

N = 1000


class WalKeepingKeystore(Keystore):
    """Test double: no automatic checkpoint, so every committed page stays in the -wal file
    until destroy (the positive control needs the key bytes to be there)."""

    def _configure(self, db):
        super()._configure(db)
        db.execute("PRAGMA wal_autocheckpoint=1000000")


class SecureDeleteOff(WalKeepingKeystore):
    def _configure(self, db):
        super()._configure(db)
        db.execute("PRAGMA secure_delete=OFF")

    def _after_destroy(self, db):  # the production step also vacuums, which would hide what this variant shows
        db.execute("PRAGMA wal_checkpoint(TRUNCATE)")


class NoCheckpointAfterDestroy(WalKeepingKeystore):
    def _after_destroy(self, db):
        pass


def scan(path: Path, needles) -> dict:
    """For each of the database, -wal and -journal files, the needles found in it."""
    out = {}
    for suffix in ("", "-wal", "-journal"):
        f = Path(str(path) + suffix)
        data = f.read_bytes() if f.exists() else b""
        out[suffix or "db"] = {n for n in needles if n in data}
    return out


class Fixture:
    """N leaf versions; half carry subject token `victim`, which will be destroyed."""

    def __init__(self, cls, root):
        self.ks = cls(Path(root) / "tena.db", "tenaaa")
        self.victim = self.ks.subject_token("victim@example.com")
        self.other = self.ks.subject_token("other@example.com")
        self.doomed, self.kept = [], []
        for i in range(N):
            leaf_id = f"{i:032x}"
            tok = self.victim if i % 2 == 0 else self.other
            self.ks.encrypt(leaf_id, 1, b"secret text %d" % i, [tok])
            (self.doomed if i % 2 == 0 else self.kept).append(leaf_id)
        reader = sqlite3.connect(str(self.ks.path))
        rows = reader.execute("SELECT leaf_id, key, key_ref FROM leaf_keys").fetchall()
        reader.close()
        self.material = {lid: (bytes(k), ref.encode()) for lid, k, ref in rows}

    def needles(self, leaf_ids):
        out = set()
        for lid in leaf_ids:
            k, ref = self.material[lid]
            out |= {k, ref}
        return out


class DestroyByteScan(unittest.TestCase):
    def run_variant(self, cls):
        with tempfile.TemporaryDirectory() as d:
            fx = Fixture(cls, d)
            doomed, kept = fx.needles(fx.doomed), fx.needles(fx.kept)
            before = scan(fx.ks.path, doomed)
            destroyed = fx.ks.destroy(subject_token=fx.victim)
            after_doomed = scan(fx.ks.path, doomed)
            after_kept = scan(fx.ks.path, kept)
            self.assertEqual(len(destroyed), N // 2)
            self.assertEqual(fx.ks.count_keys(), N // 2)
            fx.ks.close()
            return before, after_doomed, after_kept, len(doomed)

    def test_positive_control_finds_every_key_and_id_in_the_wal_before_destroy(self):
        before, _, _, n = self.run_variant(WalKeepingKeystore)
        self.assertEqual(len(before["-wal"]), n)  # every key byte string and key id is in the -wal file

    def test_real_keystore_leaves_neither_key_bytes_nor_key_ids_after_destroy(self):
        _, after, after_kept, _ = self.run_variant(WalKeepingKeystore)
        self.assertEqual(after, {"db": set(), "-wal": set(), "-journal": set()})
        # the control that the scan still sees keys that were not destroyed
        self.assertEqual(sum(len(v) for v in after_kept.values()) > 0, True)

    def test_default_production_settings_also_leave_nothing(self):
        _, after, _, _ = self.run_variant(Keystore)
        self.assertEqual(after, {"db": set(), "-wal": set(), "-journal": set()})

    def test_broken_secure_delete_off_leaves_key_bytes_in_the_database_file(self):
        _, after, _, n = self.run_variant(SecureDeleteOff)
        self.assertGreater(len(after["db"]), 0)

    def test_broken_no_checkpoint_leaves_key_bytes_in_the_wal(self):
        _, after, _, n = self.run_variant(NoCheckpointAfterDestroy)
        self.assertGreater(len(after["-wal"]), 0)


class Behaviour(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.TemporaryDirectory()
        self.ks = Keystore(Path(self.d.name) / "tena.db", "tenaaa")

    def tearDown(self):
        self.ks.close()
        self.d.cleanup()

    def test_round_trip_and_destroy_make_decrypt_fail(self):
        ct, nonce, ref = self.ks.encrypt("a" * 32, 1, b"hello", ["tok1"])
        self.assertEqual(self.ks.decrypt("a" * 32, 1, ct, nonce), b"hello")
        self.assertEqual(self.ks.destroy(leaf_id="a" * 32), [ref])
        with self.assertRaises(KeyDestroyed):
            self.ks.decrypt("a" * 32, 1, ct, nonce)

    def test_versions_have_separate_keys_and_ciphertext_is_bound_to_its_version(self):
        c1, n1, r1 = self.ks.encrypt("b" * 32, 1, b"v1", [])
        c2, n2, r2 = self.ks.encrypt("b" * 32, 2, b"v2", [])
        self.assertNotEqual(r1, r2)
        with self.assertRaises(KeystoreError):
            self.ks.decrypt("b" * 32, 2, c1, n1)  # version 1 ciphertext under version 2 key
        self.assertEqual(self.ks.decrypt("b" * 32, 2, c2, n2), b"v2")

    def test_destroy_by_subject_reaches_every_version_naming_it_and_no_other(self):
        ks = self.ks
        ks.encrypt("c" * 32, 1, b"x", ["s1"])
        ks.encrypt("c" * 32, 2, b"y", ["s1", "s2"])
        ks.encrypt("d" * 32, 1, b"z", ["s2"])
        self.assertEqual(len(ks.destroy(subject_token="s1")), 2)
        self.assertEqual(ks.count_keys(), 1)
        self.assertEqual(ks.destroy(subject_token="s1"), [])  # idempotent
        with self.assertRaises(KeystoreError):
            ks.destroy()
        with self.assertRaises(KeystoreError):
            ks.destroy(subject_token="s2", leaf_id="d" * 32)

    def test_pragmas_are_set_explicitly_and_file_is_private(self):
        self.assertEqual(self.ks._db.execute("PRAGMA secure_delete").fetchone()[0], 1)
        self.assertEqual(self.ks._db.execute("PRAGMA journal_mode").fetchone()[0], "wal")
        self.assertEqual(os.stat(self.ks.path).st_mode & 0o077, 0)

    def test_tenant_ids_are_validated(self):
        for bad in ("ta", "TENANT", "ten-a", "../x", ""):
            with self.assertRaises(KeystoreError):
                Keystore(Path(self.d.name) / "x.db", bad)
        self.assertTrue(_TENANT.match("tbxx"))

    def test_tokens_are_per_tenant_and_per_purpose(self):
        other = Keystore(Path(self.d.name) / "tenb.db", "tenbbb")
        try:
            a = self.ks.subject_token("alice@example.com")
            self.assertEqual(a, self.ks.subject_token("alice@example.com"))
            self.assertNotEqual(a, other.subject_token("alice@example.com"))  # LAB-R10: no cross-tenant linking
            self.assertNotEqual(a, self.ks.principal_token("alice@example.com"))
            self.assertEqual(len(a), 32)
        finally:
            other.close()


class LookalikeFailsClosed(unittest.TestCase):
    """Canonicalisation over-matches (clark and dark share a token). That is acceptable only
    because registration fails closed and two existing identities are never conflated."""

    def test_lookalike_registration_is_refused_and_existing_identity_is_untouched(self):
        with tempfile.TemporaryDirectory() as d:
            ks = Keystore(Path(d) / "tena.db", "tenaaa")
            try:
                self.assertEqual(canonicalise("clark@example.com"), canonicalise("dark@example.com"))
                t1 = ks.register_identifier("clark@example.com")
                self.assertEqual(ks.register_identifier("Clark@Example.com"), t1)  # same identity, idempotent
                with self.assertRaises(LookalikeRefused):
                    ks.register_identifier("dark@example.com")
                with self.assertRaises(LookalikeRefused):
                    ks.register_identifier("\u0441lark@example.com")  # Cyrillic es
                self.assertEqual(ks._db.execute("SELECT COUNT(*) FROM identities").fetchone()[0], 1)
                ks.encrypt("a" * 32, 1, b"x", [t1])
                self.assertEqual(len(ks.destroy(subject_token=t1)), 1)  # erasure reaches only that identity
                self.assertNotEqual(ks.register_identifier("unrelated@example.org"), t1)
            finally:
                ks.close()


class Isolation(unittest.TestCase):
    def lock_wait(self, ks_set):
        a, b = ks_set.get("tenaaa"), ks_set.get("tenbbb")
        for k in (a, b):
            k._db.execute("PRAGMA busy_timeout=200")
        b.encrypt("e" * 32, 1, b"x", ["sub"])
        a._db.execute("BEGIN IMMEDIATE")  # tenant A's long write transaction
        try:
            b.destroy(leaf_id="e" * 32)
            return "done"
        except sqlite3.OperationalError as e:
            return str(e)
        finally:
            a._db.execute("ROLLBACK")

    def test_separate_files_let_tenant_b_destroy_while_a_holds_a_write_transaction(self):
        with tempfile.TemporaryDirectory() as d:
            s = KeystoreSet(d)
            try:
                self.assertEqual(self.lock_wait(s), "done")
            finally:
                s.close()

    def test_broken_shared_file_blocks_tenant_b(self):
        class Shared(KeystoreSet):
            def path_for(self, tenant):
                return self.root / "all.db"
        with tempfile.TemporaryDirectory() as d:
            s = Shared(d)
            try:
                self.assertIn("locked", self.lock_wait(s))
            finally:
                s.close()


class Canonicalisation(unittest.TestCase):
    ROWS = {
        "case": ["Alice@Example.COM", "ALICE@EXAMPLE.COM", "alice@example.com  "],
        "width": ["ＡＬＩＣＥ＠ＥＸＡＭＰＬＥ．ＣＯＭ", "alice＠example．com"],
        "accents": ["álice@example.com", "alicé@éxample.com", "alice@examplé.com"],
        "whitespace": ["alice   smith", "alice\tsmith", " Alice  Smith "],
        "homoglyph_cyrillic": ["аlice@example.com", "alice@exаmple.com", "аlicе@еxаmple.cоm"],
        "homoglyph_greek": ["alice@example.cοm", "αlice@example.com"],
        "homoglyph_digits": ["alice@examp1e.com", "a1ice@example.com", "alice@example.c0m"],
        "homoglyph_shapes": ["aliec@exarnple.com"],
    }

    def test_variants_map_to_one_token(self):
        with tempfile.TemporaryDirectory() as d:
            ks = Keystore(Path(d) / "tena.db", "tenaaa")
            try:
                canonical = {"case": "alice@example.com", "width": "alice@example.com",
                             "accents": "alice@example.com", "whitespace": "alice smith",
                             "homoglyph_cyrillic": "alice@example.com", "homoglyph_greek": "alice@example.com",
                             "homoglyph_digits": "alice@example.com"}
                for group, base in canonical.items():
                    want = ks.subject_token(base)
                    for v in self.ROWS[group]:
                        self.assertEqual(ks.subject_token(v), want, (group, v))
                self.assertNotEqual(ks.subject_token("alice@example.com"), ks.subject_token("alicf@example.com"))
                self.assertNotEqual(ks.subject_token("alice smith"), ks.subject_token("alicesmith"))
            finally:
                ks.close()

    def test_broken_without_the_confusables_map_fails_the_homoglyph_rows(self):
        with tempfile.TemporaryDirectory() as d:
            ks = Keystore(Path(d) / "tena.db", "tenaaa")
            try:
                want = ks.subject_token("alice@example.com", confusables=False)
                failed = [v for g in ("homoglyph_cyrillic", "homoglyph_greek", "homoglyph_digits")
                          for v in self.ROWS[g] if ks.subject_token(v, confusables=False) != want]
                self.assertEqual(len(failed), 8)  # every homoglyph row
                for g in ("case", "width", "accents"):  # the non-homoglyph rows still pass without the map
                    for v in self.ROWS[g]:
                        self.assertEqual(ks.subject_token(v, confusables=False), want if g != "whitespace" else None)
            finally:
                ks.close()

    def test_canonical_form_is_stable_text(self):
        self.assertEqual(canonicalise("  Ｊohn   Smith "), "john smith")


if __name__ == "__main__":
    unittest.main()
