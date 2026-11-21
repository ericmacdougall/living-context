"""Pools rescue (goodqa#16441, F12) on the real cluster: the pool delete inventory and the lineage scan find every document when one
matching document is the FIRST document of each of three fresh indices and the page size is 1. A `_doc` cursor (71b23a5) gives the
three the same sort value and returns one; a point in time with `_shard_doc` returns all three. Each scan closes its point in time.

Round 4 (F11 remainder): the exact-name lookups the pool delete decides on (`reads.aliases`, `reads.scan(missing_ok=True)`) read the
answers this cluster really gives, through the tenant's `erase` credential: an alias names the physical index behind it, an index without
an alias names itself, and a missing name is a 404 `index_not_found_exception` that names it (the one shape that reads as absence; the
unit fake in tests/unit/fake_es.py answers the same shape). After the physical index is dropped, both names read as absent.

The fresh indices are created under the tenant's prefix by the cluster administrator (one shard, no replica) and deleted after.
Needs LC_TEST_ES_URL, LC_TEST_CA and LC_TEST_PW."""
from unittest import mock

from living_context.erasure import pool_scope, scan
from living_context.erasure.pool_scope import PoolEraser
from living_context.gateway import reads
from living_context.gateway.indices import physical_name, pool_index
from tests.security.test_pools_es import Base, T

P = "prescue000000001"   # a pool id (16 characters) no other test uses
SUBJECT = "s-rescue-f12"


def open_pits(admin) -> int:
    st, r = admin.request("GET", "/_nodes/stats/indices/search")
    assert st == 200, r
    return sum(int(n["indices"]["search"].get("point_in_time_current", 0)) for n in r["nodes"].values())


class RescueReadsES(Base):
    IDX = [f"lc-{T}-rescue{n}" for n in range(3)]

    def setUp(self):
        super().setUp()
        for i, idx in enumerate(self.IDX):
            st, r = self.admin.request("PUT", f"/{idx}", {   # keyword strings, as the product mappings have them (term queries)
                "settings": {"number_of_shards": 1, "number_of_replicas": 0},
                "mappings": {"dynamic_templates": [{"strings": {"match_mapping_type": "string", "mapping": {"type": "keyword"}}}]}})
            self.assertEqual(st, 200, r)
            self.addCleanup(self.admin.request, "DELETE", f"/{idx}")
            st, r = self.admin.request("PUT", f"/{idx}/_doc/q{i}?refresh=true", {
                "tenant": T, "kind": "quarantine", "pool": P, "lin_subjects": [SUBJECT], "lin_leaf_ids": ["L1"], "qid": f"k{i}"})
            self.assertIn(st, (200, 201), r)

    def eraser(self):
        return PoolEraser(lambda: self.gate.client(T, "erase"), lambda name: None, T, keystore=None, journal=None, gate=None)

    def test_f12_pool_delete_inventory_finds_the_first_document_of_every_index_at_page_one(self):
        before = open_pits(self.admin)
        with mock.patch.object(pool_scope, "PAGE", 1):
            got = self.eraser()._docs(P)
        self.assertEqual(sorted((i, d) for i, d, _, _ in got), sorted((idx, f"q{i}") for i, idx in enumerate(self.IDX)))
        self.assertLessEqual(open_pits(self.admin), before)

    def test_f12_lineage_scan_finds_the_first_document_of_every_index_at_page_one(self):
        before = open_pits(self.admin)
        with mock.patch.object(scan, "PAGE", 1):
            got = scan.find(self.gate.client(T, "erase"), T, subjects=[SUBJECT])
        self.assertTrue({f"q{i}" for i in range(3)} <= {h.id for h in got}, [h.id for h in got])
        self.assertLessEqual(open_pits(self.admin), before)


class AliasShapesES(Base):
    """The real answers of the exact-name lookups (F11 remainder)."""
    P2 = "prescue000000002"

    def setUp(self):
        super().setUp()
        self.alias = pool_index(T, "base", self.P2, "leaf")
        self.phys = physical_name(self.alias)
        self.bare = f"lc-{T}-rescuebare"
        self.missing = f"lc-{T}-rescuemissing"
        for name, body in ((self.phys, {"aliases": {self.alias: {}}}), (self.bare, {})):
            st, r = self.admin.request("PUT", f"/{name}", {"settings": {"number_of_shards": 1, "number_of_replicas": 0}, **body})
            self.assertEqual(st, 200, r)
            self.addCleanup(self.admin.request, "DELETE", f"/{name}")
        self.er = self.gate.client(T, "erase")

    def test_f11_the_exact_name_lookups_read_the_real_answers(self):
        self.assertEqual(reads.aliases(self.er, self.alias), [self.phys])
        self.assertEqual(reads.aliases(self.er, self.phys), [self.phys])
        self.assertEqual(reads.aliases(self.er, self.bare), [self.bare])     # an index without an alias is still there
        st, r = self.er.request("GET", f"/{self.missing}/_alias")
        err = r.get("error") if isinstance(r, dict) else None
        self.assertEqual((st, isinstance(err, dict) and err.get("type")), (404, "index_not_found_exception"), r)
        self.assertIn(self.missing, (err.get("index"), err.get("resource.id")), r)   # the absence names the name asked
        self.assertEqual(reads.aliases(self.er, self.missing), [])
        self.assertEqual(reads.scan(self.er, self.missing, {"query": {"match_all": {}}}, missing_ok=True), [])
        st, r = self.admin.request("DELETE", f"/{self.phys}")
        self.assertEqual(st, 200, r)
        self.assertEqual(reads.aliases(self.er, self.alias), [])             # the alias went with its only index
        self.assertEqual(reads.aliases(self.er, self.phys), [])
        self.assertEqual(reads.scan(self.er, self.alias, {"query": {"match_all": {}}}, missing_ok=True), [])
