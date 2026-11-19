"""BUILD-PLAN Step 2 failing-first tests for ids and pointers."""
import random
import time
import unittest

from living_context import ids


class TimeSeededSource:
    """Broken variant: seeded from the clock in whole seconds (restarts collide)."""

    def __init__(self):
        self._rng = random.Random(int(time.time()))

    def __call__(self, n):
        return self._rng.getrandbits(n * 8).to_bytes(n, "big")


def restart_simulation(make_source, restarts=20, per_run=5_000):
    """Create a fresh generator per 'process start' within one clock second."""
    seen, dup = set(), 0
    for _ in range(restarts):
        g = ids.IdGenerator(make_source())
        for _ in range(per_run):
            i = g.new_id()
            dup += i in seen
            seen.add(i)
    return dup


class IdUniqueness(unittest.TestCase):
    def test_one_million_ids_are_unique(self):
        g = ids.IdGenerator()
        seen = {g.new_id() for _ in range(1_000_000)}
        self.assertEqual(len(seen), 1_000_000)

    def test_restart_simulation_has_no_duplicates(self):
        self.assertEqual(restart_simulation(lambda: __import__("os").urandom), 0)

    def test_broken_time_seeded_generator_is_caught(self):
        # Failing-first: this variant must produce duplicates across restarts.
        self.assertGreater(restart_simulation(TimeSeededSource), 0)

    def test_short_entropy_is_refused(self):
        with self.assertRaises(ids.IdError):
            ids.IdGenerator(lambda n: b"\0" * (n - 1)).new_id()

    def test_id_shape(self):
        i = ids.new_leaf_id()
        self.assertEqual(ids.check_id(i), i)
        for bad in ("", "A" * 32, "g" * 32, "ab" * 15, 7):
            with self.assertRaises(ids.IdError):
                ids.check_id(bad)


class EdgeIds(unittest.TestCase):
    def test_deterministic_and_sensitive_to_every_field(self):
        a, b = ids.new_leaf_id(), ids.new_leaf_id()
        base = ids.edge_id("ta", a, b, "related")
        self.assertEqual(base, ids.edge_id("ta", a, b, "related"))
        for other in (ids.edge_id("tb", a, b, "related"), ids.edge_id("ta", b, a, "related"),
                      ids.edge_id("ta", a, b, "supersedes")):
            self.assertNotEqual(base, other)
        self.assertEqual(len(base), 32)

    def test_field_boundaries_cannot_be_shifted(self):
        # Broken variant would concatenate without length prefixes.
        self.assertNotEqual(ids.edge_id("ab", "c", "d", "e"), ids.edge_id("a", "bc", "d", "e"))
        self.assertNotEqual(ids.edge_id("a", "b", "cd", "e"), ids.edge_id("a", "b", "c", "de"))


class PointerCodec(unittest.TestCase):
    def test_round_trip(self):
        for kind in (ids.KIND_LEAF, ids.KIND_NODE):
            p = ids.Pointer(kind, 7, ids.new_leaf_id(), 3)
            self.assertEqual(ids.decode_pointer(ids.encode_pointer(p)), p)

    def test_extremes(self):
        p = ids.Pointer(ids.KIND_NODE, (1 << 64) - 1, "f" * 32, (1 << 32) - 1)
        self.assertEqual(ids.decode_pointer(ids.encode_pointer(p)), p)
        for gen, ver in ((1 << 64, 0), (-1, 0), (0, 1 << 32), (0, -1)):
            with self.assertRaises(ids.IdError):
                ids.Pointer(ids.KIND_LEAF, gen, "0" * 32, ver)

    def test_damage_and_forgery_forms_are_refused(self):
        s = ids.encode_pointer(ids.Pointer(ids.KIND_LEAF, 1, ids.new_leaf_id(), 1))
        flipped = s[:-1] + ("a" if s[-1] != "a" else "b")
        for bad in ("", s.upper(), s + "=", s[:-2], s + "aa", flipped, "!!!!", None, 5):
            with self.assertRaises(ids.IdError, msg=repr(bad)):
                ids.decode_pointer(bad)

    def test_unknown_kind_refused(self):
        with self.assertRaises(ids.IdError):
            ids.Pointer(9, 1, "0" * 32, 1)

    def test_encoding_is_opaque_text(self):
        i = "0123456789abcdef0123456789abcdef"
        s = ids.encode_pointer(ids.Pointer(ids.KIND_LEAF, 1, i, 1))
        self.assertNotIn(i, s)
        self.assertRegex(s, r"^[a-z2-7]+$")


if __name__ == "__main__":
    unittest.main()
