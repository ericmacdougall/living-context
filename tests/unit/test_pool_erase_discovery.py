"""F11: a pool delete never reports success while an index of the pool may remain.

Index discovery that fails (HTTP 503 on the alias lookup) raises before the tombstone, so nothing is deleted and the pool stays in
state `deleting` for a retry; a drop that did not remove the index (the drop callable's result is not trusted) raises before the pool's
own records (`pp:`, `pm:`) are deleted; and a physical index the manifest names is dropped even when the alias no longer points at it.
"""
import contextlib
import unittest

from living_context.erasure.pool_scope import PoolEraseError, PoolEraser
from living_context.gateway.indices import physical_name, pool_index
from tests.unit.fake_es import ShardedFake

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


class Discovery(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
