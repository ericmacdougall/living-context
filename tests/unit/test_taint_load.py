"""Round 5 (the F11 class on the conversation taint): the taint only rises and is durable (POOLS 2.8), and after a restart it is read
back from the tenant control index. At f16ea94 `es_load` read ANY 404 as "no taint yet" and any 200 dict as the stored record, so an
empty or foreign 404, or a 200 that is not this record found, lowered the conversation to the floor. Only a verified absence of exactly
this record (a 404 `found: false` about its `_index` and `_id`, through the control alias, or the index absent) reads as no taint now;
anything else refuses (`StateError`: the caller answers `unavailable`)."""
import unittest

from living_context.gateway import session_state
from living_context.gateway.session_state import StateError, Taint, es_load, es_persist
from tests.unit.fake_es import DocStore, behind, doc_answer

T = "tena"
KEY = (T, "pt-user-alice", "conv1")
CTL = f"lc-{T}-ctl"
DID = session_state._doc_id(KEY)
RAISED = Taint("restricted", frozenset({"p" * 16}))


class TaintLoad(unittest.TestCase):
    def setUp(self):
        self.es = DocStore()
        self.load = es_load(lambda: self.es, T)

    def test_a_stored_taint_reads_back(self):   # the control: written by the real compare-and-set persist
        self.assertEqual(es_persist(lambda: self.es, T)(KEY, lambda stored: RAISED), RAISED)
        self.assertEqual(self.load(KEY), RAISED)

    def test_a_verified_absence_is_no_taint(self):
        self.assertIsNone(self.load(KEY))                                       # 404 found false about exactly this record
        self.es.doc_answers[("GET", CTL, DID)] = [(404, {"error": {"type": "index_not_found_exception", "index": CTL,
                                                                   "resource.id": CTL}, "status": 404})]
        self.assertIsNone(self.load(KEY))                                       # the control alias itself is absent

    def test_an_answer_that_proves_no_absence_never_lowers_the_taint(self):
        es_persist(lambda: self.es, T)(KEY, lambda stored: RAISED)
        for name, answer in {
            "404 with an empty body": (404, {}),
            "404 found false for another _id": (404, {"_index": behind(CTL), "_id": DID + "x", "found": False}),
            "404 found false for another index": (404, {"_index": f"lc-{T}-other", "_id": DID, "found": False}),
            "404 index_not_found naming another index": (404, {"error": {"type": "index_not_found_exception", "index": f"lc-{T}-other",
                                                                         "resource.id": f"lc-{T}-other"}, "status": 404}),
            "404 that says found": (404, {"_index": behind(CTL), "_id": DID, "found": True, "_source": {}}),
            "200 without found": (200, {"_index": behind(CTL), "_id": DID, "_source": {}, "_seq_no": 1, "_primary_term": 1}),
            "200 found for another _id": doc_answer("GET", behind(CTL), DID + "x", True, {}),
            "200 with an empty body": (200, {}),
        }.items():
            with self.subTest(name):
                self.es.doc_answers[("GET", CTL, DID)] = [answer]
                with self.assertRaises(StateError):
                    self.load(KEY)
        self.assertEqual(self.load(KEY), RAISED)


if __name__ == "__main__":
    unittest.main()
