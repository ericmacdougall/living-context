"""The flat search of A0 and A1's direct list: the BM25 field set is one parameter, the default is the stronger reading
(EXPERIMENT 4 does not name the fields; DESIGN 8.5 names text, title, entities), and the request shape is the product's."""
import unittest

from evalkit import flat
from living_context.policy import ClassLattice, Principal, Role
from living_context.recall.direct import DirectSearch

LAT = ClassLattice(["base"], [])
P = Principal("tenant1", "p", frozenset([Role.READER]), frozenset(["base"]), frozenset())


class Rec:
    def __init__(self):
        self.sent = []

    def request(self, method, path, body=None, ndjson=False):
        self.sent.append((method, path, body))
        return 200, {"responses": [{"hits": {"hits": [{"_id": "L1:1", "_source": {"title": "x", "size_tokens": 5}}]}},
                                   {"hits": {"hits": [{"_id": "L2:1", "_source": {"title": "y", "size_tokens": 6}}]}}]}


class FlatTests(unittest.TestCase):
    def test_default_is_the_strong_reading(self):
        self.assertEqual(flat.A0_FIELDS_DEFAULT, ("text", "title", "entities"))

    def test_default_request_equals_the_product_direct_search(self):
        a, b = Rec(), Rec()
        emb = lambda t: [0.5, 0.5]
        flat.FlatSearch(lambda c: a, "tenant1", embed=emb).search(P, LAT, "hello world")
        DirectSearch(lambda c: b, "tenant1", embed=emb).search(P, LAT, "hello world")
        self.assertEqual(a.sent, b.sent)

    def test_text_only_changes_only_the_bm25_fields(self):
        a, b = Rec(), Rec()
        emb = lambda t: [0.5, 0.5]
        flat.FlatSearch(lambda c: a, "tenant1", embed=emb, fields=flat.A0_FIELDS_TEXT).search(P, LAT, "q")
        flat.FlatSearch(lambda c: b, "tenant1", embed=emb).search(P, LAT, "q")
        la, lb = a.sent[0][2], b.sent[0][2]
        self.assertEqual(la[1]["query"]["multi_match"]["fields"], ["text"])
        self.assertEqual(lb[1]["query"]["multi_match"]["fields"], ["text", "title", "entities"])
        self.assertEqual(la[3], lb[3])           # the kNN half is identical
        self.assertEqual(la[0], lb[0])

    def test_unknown_field_sets_are_refused(self):
        for f in (("title",), ("text", "title"), ()):
            with self.assertRaises(ValueError):
                flat.FlatSearch(lambda c: Rec(), "tenant1", fields=f)

    def test_returns_fused_hits(self):
        hits = flat.FlatSearch(lambda c: Rec(), "tenant1", embed=lambda t: [1.0]).search(P, LAT, "q")
        self.assertEqual({h.doc_id for h in hits}, {"L1:1", "L2:1"})


if __name__ == "__main__":
    unittest.main()
