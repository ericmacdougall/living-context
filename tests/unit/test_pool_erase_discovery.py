"""F11: a pool delete never reports success while an index of the pool may remain.

Index discovery that fails (HTTP 503 on the alias lookup) raises before the tombstone, so nothing is deleted and the pool stays in
state `deleting` for a retry; a drop that did not remove the index (the drop callable's result is not trusted) raises before the pool's
own records (`pp:`, `pm:`) are deleted; and a physical index the manifest names is dropped even when the alias no longer points at it.

Round 4 (review F11 remainder): `reads.aliases` took any HTTP 200 dict, so `(200, {})` read as "no index". A malformed answer to the
discovery lookup, or to the confirmation lookups after the drop, must refuse: only a 404 `index_not_found_exception` that names the exact
name asked is absence, and a 200 must name indices that are, or carry, that name. The same rule holds for the leaf scan's missing index
(`reads.scan(missing_ok=True)`): an unnamed 404 there would leave the pool's leaf keys out of the crypto-erasure.

Round 5 (the same class on the document deletes): `PoolEraser._del` took ANY 404 as "already deleted" and any 200 as deleted. A delete
of a closure document, or of the pool's own records, is done only on HTTP 200 `deleted` or a verified absence about exactly the index
(the hit's concrete `_index`) and id asked; every other answer refuses, the pool stays findable, and a retry with a sane answer completes.
"""
import contextlib
import unittest

from living_context.erasure.pool_scope import PoolEraseError, PoolEraser
from living_context.gateway import reads
from living_context.gateway.indices import physical_name, pool_index
from tests.unit.fake_es import ShardedFake, doc_answer, unconfirmed_deletes

T, P = "tena", "pzzzzzzzzzzzzzzz"
ALIAS = pool_index(T, "base", P, "leaf")
PHYS = physical_name(ALIAS, b"\x01\x02\x03\x04")


class Journal:
    def __init__(self):
        self.rows = []

    def append(self, rec):
        self.rows.append(rec)
        return len(self.rows)


class Keys:
    def __init__(self):
        self.destroyed = []

    def destroy(self, *, leaf_ids):
        self.destroyed += list(leaf_ids)
        return list(leaf_ids)


class Gate:
    @contextlib.contextmanager
    def exclusive(self):
        yield


class Fixture(unittest.TestCase):
    def setUp(self):
        self.es = ShardedFake()
        self.es.put(PHYS, "L1:1", {"lin_leaf_ids": ["L1"], "pool": P})
        self.es.aliases[PHYS] = {ALIAS}
        self.es.put(f"lc-{T}-base-ctl", f"pm:{P}", {"kind": "manifest", "pool": P, "indices": [ALIAS, PHYS]})
        self.es.put(f"lc-{T}-authz", f"pp:{P}", {"kind": "pool", "pool": P})
        self.es.put(f"lc-{T}-src", "s1", {"kind": "src", "pool": P, "leaf_id": "L1"})
        self.journal, self.keys, self.dropped = Journal(), Keys(), []

    def eraser(self, drop=None):
        def real_drop(name):
            self.dropped.append(name)
            self.es.docs.pop(name, None)
            self.es.aliases.pop(name, None)
        return PoolEraser(lambda: self.es, drop or real_drop, T, self.keys, self.journal, Gate(), purge_deadline_s=0.01,
                          purge_interval_s=0.001)


class Discovery(Fixture):
    def test_failed_alias_discovery_refuses_before_the_tombstone(self):
        self.es.alias_status = 503
        with self.assertRaises(PoolEraseError):
            self.eraser().delete("base", P)
        self.assertEqual((self.journal.rows, self.keys.destroyed, self.dropped), ([], [], []))
        self.assertIn(f"pm:{P}", self.es.docs[f"lc-{T}-base-ctl"])
        self.assertIn("s1", self.es.docs[f"lc-{T}-src"])

    def test_a_drop_that_left_the_index_keeps_the_pool_records(self):
        with self.assertRaises(PoolEraseError):
            self.eraser(drop=lambda name: 503).delete("base", P)   # the drop answers an error status and removes nothing
        self.assertIn(PHYS, self.es.docs)
        self.assertIn(f"pm:{P}", self.es.docs[f"lc-{T}-base-ctl"])   # what a retry needs to find the pool and its indices
        self.assertIn(f"pp:{P}", self.es.docs[f"lc-{T}-authz"])

    def test_an_index_named_only_by_the_manifest_is_dropped(self):
        self.es.aliases[PHYS] = set()   # the alias was removed (an interrupted earlier attempt), the physical index remains
        rc = self.eraser().delete("base", P)
        self.assertEqual(self.dropped, [PHYS])
        self.assertEqual(rc.indices_dropped, [PHYS])
        self.assertNotIn(PHYS, self.es.docs)

    def test_the_nominal_delete_drops_and_confirms(self):
        rc = self.eraser().delete("base", P)
        self.assertEqual(rc.indices_dropped, [PHYS])
        self.assertNotIn(f"pm:{P}", self.es.docs[f"lc-{T}-base-ctl"])
        self.assertEqual(sorted(self.keys.destroyed), ["L1"])


NOT_FOUND = "index_not_found_exception"
# Answers that read as "no index" at dbc528f (an empty 200, a 404 that does not name the name asked) and answers that are malformed
# in other ways (a 200 about another index, entries without an `aliases` object). Every one must refuse.
MALFORMED = {
    "200 empty object": (200, {}),
    "404 naming another index": (404, {"error": {"type": NOT_FOUND, "index": "lc-other", "resource.id": "lc-other"}, "status": 404}),
    "404 naming no index": (404, {"error": {"type": NOT_FOUND}, "status": 404}),
    "200 about an index without the alias": (200, {"lc-other": {"aliases": {}}}),
    "200 entry without aliases": (200, {PHYS: {}}),
    "200 aliases not an object": (200, {PHYS: {"aliases": []}}),
    "200 entry not an object": (200, {PHYS: "x"}),
    "200 list": (200, []),
    "404 error as a string": (404, {"error": "alias missing", "status": 404}),
}


class MalformedAnswers(Fixture):
    def fresh(self, manifest_names_phys=True):
        self.setUp()
        if not manifest_names_phys:   # the alias is then the only way to find the physical index
            self.es.put(f"lc-{T}-base-ctl", f"pm:{P}", {"kind": "manifest", "pool": P, "indices": [ALIAS]})

    def assert_kept(self):
        self.assertIn(PHYS, self.es.docs)
        self.assertIn(f"pm:{P}", self.es.docs[f"lc-{T}-base-ctl"])
        self.assertIn(f"pp:{P}", self.es.docs[f"lc-{T}-authz"])

    def test_a_malformed_discovery_answer_refuses_before_the_tombstone(self):
        for name, answer in MALFORMED.items():
            with self.subTest(name):
                self.fresh(manifest_names_phys=False)
                self.es.alias_answers[ALIAS] = answer
                with self.assertRaises(PoolEraseError):
                    self.eraser().delete("base", P)
                self.assertEqual((self.journal.rows, self.keys.destroyed, self.dropped), ([], [], []))   # nothing done, nothing dropped
                self.assert_kept()
                self.assertIn("s1", self.es.docs[f"lc-{T}-src"])

    def test_a_malformed_confirmation_after_the_drop_keeps_the_pool_records(self):
        for name, answer in MALFORMED.items():
            with self.subTest(name):
                self.fresh()

                def drop(index, answer=answer):   # the drop removes nothing, and every lookup after it answers malformed
                    self.dropped.append(index)
                    self.es.alias_answers.update({ALIAS: answer, PHYS: answer})
                with self.assertRaises(PoolEraseError):
                    self.eraser(drop=drop).delete("base", P)
                self.assertEqual(self.dropped, [PHYS])
                self.assert_kept()   # what a retry needs to find the pool and its indices

    def test_an_unnamed_missing_index_on_the_leaf_scan_refuses_before_any_key_is_destroyed(self):
        for name in ("404 naming another index", "404 naming no index", "404 error as a string"):
            with self.subTest(name):
                self.fresh()
                self.es.pit_answers[ALIAS] = MALFORMED[name]
                with self.assertRaises(PoolEraseError):
                    self.eraser().delete("base", P)
                self.assertEqual((self.journal.rows, self.keys.destroyed, self.dropped), ([], [], []))
                self.assert_kept()

    def test_the_well_formed_answers_still_read_as_they_did(self):
        self.fresh()
        self.assertEqual(reads.aliases(self.es, ALIAS), [PHYS])               # an alias: the indices that carry it
        self.assertEqual(reads.aliases(self.es, PHYS), [PHYS])                # an index: itself
        self.es.aliases[PHYS] = set()
        self.assertEqual(reads.aliases(self.es, PHYS), [PHYS])                # an index without any alias is still there
        self.assertEqual(reads.aliases(self.es, ALIAS), [])                   # a verified absence of the alias
        self.assertEqual(reads.aliases(self.es, f"lc-{T}-nothing"), [])
        for pattern in ("lc-*", "a,b", "-lc-x", "_all", ""):
            with self.subTest(pattern), self.assertRaises(ValueError):
                reads.aliases(self.es, pattern)


SRC, CTL, AUTHZ = f"lc-{T}-src", f"lc-{T}-base-ctl", f"lc-{T}-authz"


class DeleteAnswers(Fixture):
    def assert_findable(self):   # what a retry needs: the pool's own records, so a restarted gateway finds the pool in `deleting`
        self.assertIn(f"pm:{P}", self.es.docs[CTL])

    def test_a_closure_delete_answer_that_confirms_nothing_refuses_and_a_retry_completes(self):
        for name, answer in unconfirmed_deletes(SRC, "s1").items():
            with self.subTest(name):
                self.setUp()
                self.es.doc_answers[("DELETE", SRC, "s1")] = answer
                with self.assertRaises(PoolEraseError):
                    self.eraser().delete("base", P)
                self.assertIn("s1", self.es.docs[SRC])           # the document the answer did not confirm is still there ...
                self.assertEqual(self.dropped, [])               # ... and the delete stopped before the drop and the pool's records
                self.assertIn(PHYS, self.es.docs)
                self.assertIn(f"pp:{P}", self.es.docs[AUTHZ])
                self.assert_findable()
                del self.es.doc_answers[("DELETE", SRC, "s1")]
                rc = self.eraser().delete("base", P)              # the retry, with a well-formed answer, completes
                self.assertNotIn("s1", self.es.docs[SRC])
                self.assertEqual(rc.indices_dropped, [PHYS])
                self.assertNotIn(f"pm:{P}", self.es.docs[CTL])

    def test_a_pool_record_delete_answer_that_confirms_nothing_keeps_the_pool_findable(self):
        for name, answer in unconfirmed_deletes(CTL, f"pm:{P}").items():
            with self.subTest(name):
                self.setUp()
                self.es.doc_answers[("DELETE", CTL, f"pm:{P}")] = answer
                with self.assertRaises(PoolEraseError):
                    self.eraser().delete("base", P)
                self.assert_findable()
                del self.es.doc_answers[("DELETE", CTL, f"pm:{P}")]
                self.eraser().delete("base", P)
                self.assertNotIn(f"pm:{P}", self.es.docs[CTL])

    def test_a_verified_absence_is_done_and_is_not_counted(self):
        for name, answer in (("404 not_found about the document", doc_answer("DELETE", SRC, "s1", False)),
                             ("404 index_not_found naming the index", ShardedFake._missing(SRC))):
            with self.subTest(name):
                self.setUp()
                self.es.doc_answers[("DELETE", SRC, "s1")] = answer   # a concurrent delete got there first
                rc = self.eraser().delete("base", P)
                self.assertEqual((rc.docs_deleted, rc.indices_dropped), (2, [PHYS]))   # pp: and pm: deleted here; s1 not counted

    def test_the_nominal_delete_counts_every_document_it_deleted(self):
        rc = self.eraser().delete("base", P)
        self.assertEqual(rc.docs_deleted, 3)
        self.assertEqual({n: sorted(d) for n, d in self.es.docs.items()}, {AUTHZ: [], CTL: [], SRC: []})


if __name__ == "__main__":
    unittest.main()
