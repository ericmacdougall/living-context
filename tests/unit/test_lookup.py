"""BUILD-PLAN Step 10: typed pointer resolution and the gateway-logged `opened` (AC-8)."""
import unittest

from living_context.ids import KIND_LEAF, KIND_NODE, Pointer, encode_pointer
from living_context.recall.lookup import Resolver

A, B, N = "a" * 32, "b" * 32, "c" * 32


class Store:
    leaves = {(A, 1): {"cls": "base", "text": "old text"}, (A, 2): {"cls": "base", "text": "new text"},
              (B, 1): {"cls": "restricted", "text": "secret"}}

    def leaf(self, i, v):
        return self.leaves.get((i, v))

    def current_lv(self, i):
        return max((v for (x, v) in self.leaves if x == i), default=None)

    def gen_status(self, g):
        return {1: "retired", 2: "live", 3: "expired"}.get(g)

    def forwarding(self, g, n):
        return ["d" * 32] if (g, n) == (1, N) else None


def leaf_ptr(i, v, gen=2):
    return encode_pointer(Pointer(KIND_LEAF, gen, i, v))


class Lookup(unittest.TestCase):
    def resolver(self, erased=(), classes=("base",), **kw):
        self.log = []
        return Resolver(Store(), classes, lambda i: i in erased, self.log.append, **kw)

    def test_typed_answers(self):
        r = self.resolver()
        self.assertEqual(r.resolve(leaf_ptr(A, 2)), {"status": "ok", "text": "new text"})
        sup = r.resolve(leaf_ptr(A, 1))
        self.assertEqual((sup["status"], sup["newer"]), ("superseded", leaf_ptr(A, 2)))
        self.assertEqual(r.resolve(encode_pointer(Pointer(KIND_NODE, 1, N, 1)))["status"], "moved")
        self.assertEqual(r.resolve(encode_pointer(Pointer(KIND_NODE, 1, B, 1)))["status"], "expired")
        self.assertEqual(r.resolve(encode_pointer(Pointer(KIND_NODE, 3, N, 1)))["status"], "expired")
        self.assertEqual(r.resolve(encode_pointer(Pointer(KIND_NODE, 2, N, 1)))["status"], "ok")

    def test_erased_forbidden_and_nonexistent_are_one_byte_identical_answer(self):
        r = self.resolver(erased={A})
        answers = [r.resolve(leaf_ptr(A, 2)),               # erased
                   r.resolve(leaf_ptr(B, 1)),               # forbidden (class outside the read set)
                   r.resolve(leaf_ptr("e" * 32, 1)),        # nonexistent
                   r.resolve("not-a-pointer")]
        self.assertTrue(all(a == {"status": "unavailable"} for a in answers), answers)
        self.assertEqual(len({repr(a) for a in answers}), 1)

    def test_broken_resolver_without_the_class_check_leaks_a_forbidden_leaf(self):
        r = self.resolver(check_class=False)
        self.assertEqual(r.resolve(leaf_ptr(B, 1))["status"], "ok")  # what the class check prevents
        self.assertEqual(self.resolver().resolve(leaf_ptr(B, 1)), {"status": "unavailable"})

    def test_a_relabelled_newer_version_is_unavailable_not_superseded(self):
        s = Store()
        s.leaves = dict(s.leaves)
        s.leaves[(A, 3)] = {"cls": "restricted", "text": "relabelled"}
        r = Resolver(s, ["base"], lambda i: False, lambda e: None)
        self.assertEqual(r.resolve(leaf_ptr(A, 1)), {"status": "unavailable"})
        self.assertEqual(r.resolve(leaf_ptr(A, 3)), {"status": "unavailable"})

    def test_opened_is_logged_by_the_resolver_on_ok_only(self):
        r = self.resolver()
        r.resolve(leaf_ptr(A, 1))   # superseded: no text returned, not opened
        r.resolve(leaf_ptr(B, 1))   # forbidden
        self.assertEqual(self.log, [])
        r.resolve(leaf_ptr(A, 2), actor="delegate")
        self.assertEqual(self.log, [{"leaf_id": A, "lv": 2, "gen": 2, "actor": "delegate"}])


if __name__ == "__main__":
    unittest.main()
