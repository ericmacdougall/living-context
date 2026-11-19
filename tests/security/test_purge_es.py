"""BUILD-PLAN Step 14, purge (DESIGN 14.5; probes P9, P10): the purge repeats the expunge pass until the deleted-document
count is 0. Needs LC_TEST_ES_URL, LC_TEST_CA, LC_TEST_PW (the module FAILS without them). The broken variant (one pass
right after the delete) must leave a non-zero count."""
import os
import unittest
import uuid

from living_context.erasure import purge
from living_context.gateway.es import EsClient

URL, CA, PW = (os.environ.get(k) for k in ("LC_TEST_ES_URL", "LC_TEST_CA", "LC_TEST_PW"))


class Purge(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not (URL and CA and PW):
            raise RuntimeError("LC_TEST_ES_URL, LC_TEST_CA and LC_TEST_PW must point at a scratch cluster")
        cls.es = EsClient(URL, CA, PW, scheme="basic", user="elastic")

    def make(self):
        ix = "lc-prgx-base-leaf-" + uuid.uuid4().hex[:6]
        self.es.request("PUT", f"/{ix}", {"settings": {"number_of_shards": 1, "number_of_replicas": 0}})
        lines = []
        for i in range(600):
            lines += [{"index": {"_index": ix, "_id": str(i)}}, {"k": "x" * 20}]
        self.es.request("POST", "/_bulk?refresh=true", lines, ndjson=True)
        lines = []
        for i in range(300):
            lines += [{"delete": {"_index": ix, "_id": str(i)}}]
        self.es.request("POST", "/_bulk?refresh=true", lines, ndjson=True)
        self.addCleanup(lambda: self.es.request("DELETE", f"/{ix}"))
        return ix

    def test_the_purge_loop_reaches_zero_and_reports_the_measured_time(self):
        ix = self.make()
        res = purge.purge_index(self.es, ix, deadline_s=150, interval_s=3)
        self.assertTrue(res.ok, f"deleted documents left: {res.deleted_left}")
        self.assertEqual(purge.deleted_count(self.es, ix), 0)
        self.assertGreater(res.passes, 1)
        print(f"physical purge: deleted count reached 0 after {res.seconds:.1f} s in {res.passes} passes")

    def test_broken_single_pass_right_after_the_delete_leaves_deleted_documents(self):
        ix = self.make()
        res = purge.purge_index(self.es, ix, single_pass=True)
        self.assertFalse(res.ok, "the single-pass variant should not reach zero")
        self.assertGreater(res.deleted_left, 0)

    def test_a_deadline_that_passes_reports_failure_for_the_copy_and_swap_fallback(self):
        ix = self.make()
        res = purge.purge_index(self.es, ix, deadline_s=2, interval_s=1)
        self.assertFalse(res.ok)
        self.assertGreater(res.deleted_left, 0)


if __name__ == "__main__":
    unittest.main()
