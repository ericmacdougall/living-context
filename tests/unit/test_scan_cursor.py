"""F12 and its siblings: every paginated authoritative read finds every document when a page boundary falls between equal sort values
in different indices, and refuses a partial page.

Each case plants one matching document as the FIRST document of three different indices (so a `_doc` sort gives all three the value
[0]) and scans with a page size of 1. A `_doc` cursor returns the first and skips the other two; a PIT with `_shard_doc` returns all
three. The fake (tests/unit/fake_es.py `ShardedFake`) models exactly these two cursors; the ES suite repeats the pool-delete case on
the real cluster (tests/security/test_pools_rescue_es.py).
"""
import unittest
from unittest import mock

from living_context.erasure import pool_scope, scan
from living_context.erasure.pool_scope import PoolEraseError, PoolEraser
from living_context.gateway import reads
from living_context.gateway.indices import physical_name, pool_index
from tests.unit.fake_es import ShardedFake

T = "tena"
P = "pzzzzzzzzzzzzzzz"   # pool ids are 16 characters


def closure_doc(kind, pool=P, **kw):
    return {"tenant": T, "kind": kind, "pool": pool, "lin_subjects": ["s1"], "lin_leaf_ids": ["L1"], **kw}


def eraser(es):
    return PoolEraser(lambda: es, lambda name: None, T, keystore=None, journal=None, gate=None)


class ScanCursor(unittest.TestCase):
    def setUp(self):
        self.es = ShardedFake()
        for i, idx in enumerate((f"lc-{T}-base-ctl", f"lc-{T}-src", f"lc-{T}-authz")):
            self.es.put(idx, f"q{i}", closure_doc("quarantine", qid=f"k{i}"))
            self.es.put(idx, f"other{i}", {"tenant": T, "kind": "event", "pool": "pooooooooooother"})

    def test_pool_delete_inventory_finds_every_document_across_indices_at_page_one(self):
        with mock.patch.object(pool_scope, "PAGE", 1):
            got = eraser(self.es)._docs(P)
        self.assertEqual(sorted((i, d, key) for i, d, _, key in got),
                         [(f"lc-{T}-authz", "q2", "k2"), (f"lc-{T}-base-ctl", "q0", "k0"), (f"lc-{T}-src", "q1", "k1")])

    def test_pool_leaf_ids_scan_finds_every_leaf_across_physical_indices(self):
        alias = pool_index(T, "base", P, "leaf")
        for n in range(2):
            idx = physical_name(alias, bytes([n]))
            self.es.put(idx, f"l{n}", {"lin_leaf_ids": [f"leaf{n}"]})
            self.es.aliases[idx] = {alias}
        with mock.patch.object(pool_scope, "PAGE", 1):
            self.assertEqual(eraser(self.es)._leaf_ids("base", P), {"leaf0", "leaf1"})

    def test_lineage_scan_finds_every_document_across_indices_at_page_one(self):
        with mock.patch.object(scan, "PAGE", 1):
            got = scan.find(self.es, T, subjects=["s1"])
        self.assertEqual(sorted(h.id for h in got), ["q0", "q1", "q2"])

    def test_a_partial_page_is_refused_by_every_scan(self):
        for name, run, err in (("pool delete", lambda: eraser(self.es)._docs(P), PoolEraseError),
                               ("lineage", lambda: scan.find(self.es, T, subjects=["s1"]), scan.ScanError)):
            for k in (1, 2, 3):   # the k-th answer (the refresh, the PIT open or the first page; before the fix the second page) is partial
                with self.subTest(name=name, k=k):
                    self.es.partial = 0
                    calls = {"n": 0}
                    real = ShardedFake.request

                    def req(es, method, path, body=None, _k=k, **kw):
                        calls["n"] += 1
                        if calls["n"] == _k:
                            es.partial = 1
                        return real(es, method, path, body, **kw)

                    with mock.patch.object(ShardedFake, "request", req), mock.patch.object(pool_scope, "PAGE", 1), \
                            mock.patch.object(scan, "PAGE", 1):
                        with self.assertRaises(err):
                            run()

    def test_the_reads_scan_closes_its_point_in_time(self):
        reads.scan(self.es, f"lc-{T}-*", {"query": {"term": {"pool": P}}}, page=1)
        self.assertEqual(self.es.pits, {})
        self.assertEqual(len(self.es.closed_pits), 1)


if __name__ == "__main__":
    unittest.main()
