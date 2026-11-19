"""BUILD-PLAN Step 6: tombstone guard outcomes and idempotency."""
import tempfile
import unittest
from pathlib import Path

from living_context.ingest import subjects
from living_context.ingest.subjects import Guard, HELD, Idempotency, OK, REFUSED, TokenSource
from living_context.keystore import Keystore


def make():
    d = tempfile.TemporaryDirectory()
    ks = Keystore(Path(d.name) / "tena.db", "tenaaa")
    return d, ks, Guard(TokenSource(ks))


class GuardTests(unittest.TestCase):
    def test_erased_identifier_is_refused_in_any_canonical_variant(self):
        d, ks, g = make()
        try:
            g.erase_identifier("marta.silva@example.com", bare_name="Marta Silva")
            self.assertEqual(g.check("please email marta.silva@example.com today"), REFUSED)
            self.assertEqual(g.check("Marta.Silva@EXAMPLE.com wrote"), REFUSED)
            self.assertEqual(g.check("мarta.silva@example.com"), REFUSED)  # Cyrillic em
            self.assertEqual(g.check("nobody@example.com"), OK)
        finally:
            ks.close(); d.cleanup()

    def test_bare_name_match_is_held_not_refused(self):
        d, ks, g = make()
        try:
            g.erase_identifier("marta.silva@example.com", bare_name="Marta Silva")
            self.assertEqual(g.check("Yesterday Marta Silva called the office."), HELD)
            self.assertEqual(g.check("yesterday marta silva called"), OK)  # not shaped like a name: not detected (limit)
            self.assertEqual(g.check("Marta Costa called"), OK)
        finally:
            ks.close(); d.cleanup()

    def test_held_outcome_does_not_reveal_an_erasure(self):
        d, ks, g = make()
        try:
            g.erase_identifier("marta.silva@example.com", bare_name="Marta Silva")
            held_for_erased = g.check("Marta Silva said hello")
            self.assertEqual(held_for_erased, HELD)
            # the writer-visible outcome is the single word `held`, the same value any hold produces
            self.assertEqual(held_for_erased, subjects.HELD)
        finally:
            ks.close(); d.cleanup()

    def test_broken_guard_that_ignores_bare_names_is_caught(self):
        d, ks, g = make()
        try:
            g.erase_identifier("marta.silva@example.com", bare_name="Marta Silva")

            class IdentifiersOnly(Guard):
                def check(self, text, **kw):
                    return REFUSED if any(self._t.identifier(i) in self._erased_ids for i in subjects.identifiers(text)) else OK
            b = IdentifiersOnly(g._t)
            b._erased_ids, b._erased_bare = g._erased_ids, g._erased_bare
            self.assertEqual(b.check("Marta Silva said hello"), OK)  # slipped through: the table below catches it
            self.assertEqual(g.check("Marta Silva said hello"), HELD)
        finally:
            ks.close(); d.cleanup()

    def test_name_and_identifier_tokens_differ(self):
        d, ks, g = make()
        try:
            t = g._t
            self.assertNotEqual(t.identifier("Marta Silva"), t.bare("Marta Silva"))
        finally:
            ks.close(); d.cleanup()


class IdempotencyTests(unittest.TestCase):
    def test_same_key_same_body_returns_the_first_result_without_running_again(self):
        idem, calls = Idempotency(), []
        r1 = idem.run("tena", "p1", "k1", b"body", lambda: calls.append(1) or "leaf-1")
        r2 = idem.run("tena", "p1", "k1", b"body", lambda: calls.append(1) or "leaf-2")
        self.assertEqual((r1, r2, len(calls)), ("leaf-1", "leaf-1", 1))

    def test_same_key_different_body_is_refused_and_broken_returns_the_first_result(self):
        idem = Idempotency()
        idem.run("tena", "p1", "k1", b"body", lambda: "leaf-1")
        with self.assertRaises(Idempotency.KeyReuse):
            idem.run("tena", "p1", "k1", b"other", lambda: "leaf-2")

        class Broken(Idempotency):
            def run(self, tenant, principal, key, body, fn):
                k = (tenant, principal, key)
                if k in self._seen:
                    return self._seen[k][1]  # ignores the body hash
                self._seen[k] = (self.body_hash(body), fn())
                return self._seen[k][1]
        b = Broken()
        b.run("tena", "p1", "k1", b"body", lambda: "leaf-1")
        self.assertEqual(b.run("tena", "p1", "k1", b"other", lambda: "leaf-2"), "leaf-1")  # silently wrong

    def test_keys_are_scoped_by_tenant_and_principal(self):
        idem = Idempotency()
        a = idem.run("tena", "p1", "k", b"x", lambda: "a")
        b = idem.run("tenb", "p1", "k", b"y", lambda: "b")
        c = idem.run("tena", "p2", "k", b"z", lambda: "c")
        self.assertEqual((a, b, c), ("a", "b", "c"))


if __name__ == "__main__":
    unittest.main()
