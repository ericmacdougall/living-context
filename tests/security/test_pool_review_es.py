"""Fixes for the independent review of the pools foundation (findings M1 and following), each shown red first on the unfixed code and
then green. Needs LC_TEST_ES_URL, LC_TEST_CA and LC_TEST_PW; without them the module FAILS (it never skips)."""
import unittest

from living_context.gateway.indices import class_index, pool_index
from tests.security.test_pools_es import Base, T


class Repair(Base):
    def test_m1_repair_reprojects_a_pool_leaf_into_its_own_index_only(self):
        a, b = self.pool("alpha"), self.pool("beta")
        tok = self.mint("alice", {a: {"query", "save"}, b: {"query", "save"}})
        ptrs = {}
        for pid in (a, b):
            r = self.tools.save(tok, {"pool": pid, "text": f"repair canary {pid[:4]} secret", "key": f"k{pid}"})
            self.assertTrue(r["ok"], r)
            ptrs[pid] = r
        self.refresh()
        # a crash between source and projection: the pool leaf document of A is gone, its source stays
        st, hits = self.admin.request("POST", f"/{pool_index(T, 'base', a, 'leaf')}/_search", {"query": {"match_all": {}}, "_source": False})
        for h in hits["hits"]["hits"]:
            self.admin.request("DELETE", f"/{pool_index(T, 'base', a, 'leaf')}/_doc/{h['_id']}?refresh=true")
        out = self.writer.repair(["base"])
        self.refresh()
        # nothing of any pool is in a class-level index or in the other pool's index
        for idx in (class_index(T, "base", "leaf"), class_index(T, "base", "ctl"), pool_index(T, "base", b, "leaf")):
            st, r = self.admin.request("POST", f"/{idx}/_search", {"size": 50, "query": {"match_all": {}}})
            blob = str(r)
            self.assertNotIn("repair canary " + a[:4], blob, idx)
            if idx != pool_index(T, "base", b, "leaf"):
                self.assertNotIn("repair canary " + b[:4], blob, idx)
        st, r = self.admin.request("POST", f"/{class_index(T, 'base', 'ctl')}/_search", {"size": 50, "query": {"term": {"kind": "dirty"}}})
        self.assertEqual(r["hits"]["total"]["value"], 0)                      # no placement job for a pool leaf
        st, r = self.admin.request("POST", f"/{pool_index(T, 'base', a, 'leaf')}/_search", {"size": 5, "query": {"match_all": {}}})
        self.assertEqual(r["hits"]["total"]["value"], 1)                      # the repair put it back where it belongs
        self.assertEqual(r["hits"]["hits"][0]["_source"]["pool"], a)
        self.assertGreaterEqual(out["reprojected"], 1)
        f = self.tools.fetch(tok, {"pointer": ptrs[a]["pointer"]})
        self.assertEqual(f["status"], "ok")


if __name__ == "__main__":
    unittest.main()
