"""Round 5 (review: the F11 class on single-document answers): a malformed or foreign answer never certifies an erasure, a removal or
an absence.

At f16ea94 the subject-erasure delete (`erasure/scan.delete`) took ANY 404 as "already deleted" and any 200 as deleted; the node rewrite
of an erasure (`Executor._rewrite_node`) read any non-200 as "nothing to rewrite" and any 200 or 201 as "rewritten", and the residue
recount does not count rewrite kinds, so the erased content stayed in the node unreported; and the start-up counted an expired token
record as deleted whatever the answer. Every one now goes through `reads.delete_doc`, `reads.read_doc` or `reads.put_doc`: done only on
a well-formed answer about exactly the index and id sent (an erasure asks by the hit's concrete `_index`), absent only on a verified
absence, and anything else raises so the step stays retryable.
"""
import json
import unittest

from living_context.erasure import scan
from living_context.erasure.executor import Executor
from living_context.gateway import pool_boot
from living_context.gateway.indices import physical_name
from living_context.policy import pools as pl
from tests.unit.fake_es import DocStore, FakeAuthz, ShardedFake, behind, doc_answer, unconfirmed_deletes

T = "tena"
SRC = f"lc-{T}-src"
NODE = physical_name(f"lc-{T}-base-node", b"\x01\x02\x03\x04")   # the concrete `_index` a lineage scan hit names
UNAVAILABLE = (503, {"error": {"type": "unavailable_shards_exception"}, "status": 503})
CONFLICT = (409, {"error": {"type": "version_conflict_engine_exception"}, "status": 409})


class SubjectErasureDelete(unittest.TestCase):
    def setUp(self):
        self.es = ShardedFake()
        self.es.put(SRC, "s1", {"kind": "src", "lin_subjects": ["subj"]})

    def test_a_delete_answer_that_confirms_nothing_raises(self):
        for name, answer in unconfirmed_deletes(SRC, "s1").items():
            with self.subTest(name):
                self.es.doc_answers[("DELETE", SRC, "s1")] = answer
                with self.assertRaises(scan.ScanError):
                    scan.delete(self.es, [scan.Hit(SRC, "s1", "src")])
                self.assertIn("s1", self.es.docs[SRC])

    def test_a_delete_counts_one_and_a_verified_absence_zero(self):
        self.assertEqual(scan.delete(self.es, [scan.Hit(SRC, "s1", "src")]), 1)
        self.assertEqual(scan.delete(self.es, [scan.Hit(SRC, "s1", "src")]), 0)             # 404 not_found about exactly this document
        self.assertEqual(scan.delete(self.es, [scan.Hit(f"lc-{T}-gone", "x", "src")]), 0)   # 404 index_not_found naming the index


NODE_SRC = {"kind": "node", "children": [{"child_id": "L1", "gist": "erased gist"}, {"child_id": "L2", "gist": "kept gist"}],
            "summary": "erased summary", "facts": ["erased fact"], "lin_leaf_ids": ["L1", "L2"], "lin_nodes": [], "lin_subjects": ["subj"]}


def rewrite(es):
    ex = object.__new__(Executor)   # only the structure client is needed by the node rewrite
    ex._st = lambda: es
    return ex._rewrite_node(scan.Hit(NODE, "n1", "node"), {"L1"}, "subj")


class NodeRewrite(unittest.TestCase):
    def setUp(self):
        self.es = DocStore()
        self.es.request("PUT", f"/{NODE}/_doc/n1", NODE_SRC)

    def stored(self):
        return self.es.docs[(NODE, "n1")][0]

    def test_the_rewrite_lands_and_strips_the_erased_leaf(self):   # the control
        self.assertEqual(rewrite(self.es), 1)
        self.assertEqual((self.stored()["summary"], [c["child_id"] for c in self.stored()["children"]]), ("", ["L2"]))

    def test_a_verified_absence_is_nothing_to_rewrite(self):
        del self.es.docs[(NODE, "n1")]
        self.assertEqual(rewrite(self.es), 0)

    def test_a_read_answer_that_confirms_nothing_raises_and_writes_nothing(self):
        for name, answer in {
            "404 with an empty body": (404, {}),
            "404 found false for another _id": (404, {"_index": NODE, "_id": "n2", "found": False}),
            "404 found false for another index": (404, {"_index": f"lc-{T}-base-other", "_id": "n1", "found": False}),
            "404 found false for the alias form of the name": (404, {"_index": f"lc-{T}-base-node", "_id": "n1", "found": False}),
            "200 found for another _id": doc_answer("GET", NODE, "n2", True, dict(NODE_SRC, summary="another node")),
            "200 without found": (200, {"_index": NODE, "_id": "n1", "_source": dict(NODE_SRC), "_seq_no": 1, "_primary_term": 1}),
            "503": UNAVAILABLE,
        }.items():
            with self.subTest(name):
                self.setUp()
                self.es.doc_answers[("GET", NODE, "n1")] = [answer]
                with self.assertRaises(scan.ScanError):
                    rewrite(self.es)
                self.assertEqual(self.stored()["summary"], "erased summary")

    def test_a_write_answer_that_confirms_nothing_raises(self):
        for name, answer in {
            "200 with an empty body": (200, {}),
            "200 updated for another _id": (200, {"_index": NODE, "_id": "n2", "result": "updated"}),
            "200 updated for another index": (200, {"_index": f"lc-{T}-base-other", "_id": "n1", "result": "updated"}),
            "200 that says noop": (200, {"_index": NODE, "_id": "n1", "result": "noop"}),
            "503": UNAVAILABLE,
        }.items():
            with self.subTest(name):
                self.setUp()
                self.es.doc_answers[("PUT", NODE, "n1")] = [answer]   # answered without writing
                with self.assertRaises(scan.ScanError):
                    rewrite(self.es)
                self.assertEqual(self.stored()["summary"], "erased summary")

    def test_a_lost_compare_and_set_is_re_read_and_retried_but_never_reads_as_done(self):
        self.es.doc_answers[("PUT", NODE, "n1")] = [CONFLICT]   # a concurrent write to the node, once
        self.assertEqual(rewrite(self.es), 1)
        self.assertEqual(self.stored()["summary"], "")
        self.setUp()
        self.es.doc_answers[("PUT", NODE, "n1")] = [CONFLICT] * 10   # it never lands
        with self.assertRaises(scan.ScanError):
            rewrite(self.es)
        self.assertEqual(self.stored()["summary"], "erased summary")


class BootExpiredTokens(unittest.TestCase):
    AUTHZ, DID = f"lc-{T}-authz", "tk:h1"

    def setUp(self):
        self.fa = FakeAuthz()
        self.fa.docs[self.DID] = {"kind": "token", "hash": "h1", "rec_json": json.dumps({"exp": 10.0})}

    def load(self):
        return pool_boot.load(lambda: self.fa, T, pl.PoolDirectory(), None, None, now=100.0)["expired_deleted"]

    def test_an_expired_token_record_is_deleted_and_counted(self):   # the control
        self.assertEqual(self.load(), 1)
        self.assertNotIn(self.DID, self.fa.docs)

    def test_only_a_confirmed_delete_is_counted(self):
        for name, answer in unconfirmed_deletes(self.AUTHZ, self.DID, strict=False).items():
            with self.subTest(name):
                self.setUp()
                self.fa.doc_answers[("DELETE", self.DID)] = answer
                self.assertEqual(self.load(), 0)   # best effort: not counted, the record waits for the next start
                self.assertIn(self.DID, self.fa.docs)

    def test_a_verified_absence_through_the_alias_is_not_counted(self):
        self.fa.doc_answers[("DELETE", self.DID)] = doc_answer("DELETE", behind(self.AUTHZ), self.DID, False)
        self.assertEqual(self.load(), 0)


if __name__ == "__main__":
    unittest.main()
