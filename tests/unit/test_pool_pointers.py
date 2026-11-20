"""POOLS 3.6: sealed pointers open only for their tenant and key version; any change makes them unopenable."""
import tempfile
import unittest
from pathlib import Path

from living_context.gateway import pointers
from living_context.ids import KIND_LEAF

P, ID = "a" * 16, "ab" * 16


class Sealed(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.keys = pointers.PointerKeys(Path(self.tmp.name) / "k")
        self.s = pointers.Sealed(P, KIND_LEAF, 0, ID, 3)

    def test_round_trip_and_stability(self):
        t = pointers.seal(self.keys, "tenaaa", self.s)
        self.assertEqual(pointers.open_(self.keys, "tenaaa", t), self.s)
        self.assertEqual(t, pointers.seal(self.keys, "tenaaa", self.s))
        self.assertNotIn(P, t)

    def test_other_tenant_lookalike_tamper_and_retired_key_do_not_open(self):
        t = pointers.seal(self.keys, "tenaaa", self.s)
        for tenant in ("tenaab", "tenaaax", "tenaa"):
            self.assertIsNone(pointers.open_(self.keys, tenant, t))
        for i in range(2, len(t)):
            alt = t[:i] + ("a" if t[i] != "a" else "b") + t[i + 1:]
            self.assertIsNone(pointers.open_(self.keys, "tenaaa", alt), i)
        for junk in ("", None, 5, "x" * 500, t.upper(), t[:-3]):
            self.assertIsNone(pointers.open_(self.keys, "tenaaa", junk))
        self.keys.rotate()
        self.assertEqual(pointers.open_(self.keys, "tenaaa", t), self.s)   # an old key version still verifies
        self.keys.retire(1)
        self.assertIsNone(pointers.open_(self.keys, "tenaaa", t))

    def test_keys_persist_across_restart(self):
        t = pointers.seal(self.keys, "tenaaa", self.s)
        again = pointers.PointerKeys(Path(self.tmp.name) / "k")
        self.assertEqual(pointers.open_(again, "tenaaa", t), self.s)


if __name__ == "__main__":
    unittest.main()
