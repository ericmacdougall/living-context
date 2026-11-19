"""The model-output cache on Elasticsearch (DESIGN 5.8; BUILD-PLAN Step 8, fix before Step 9; AC-7, AC-16).

Needs LC_TEST_ES_URL, LC_TEST_CA and LC_TEST_PW (the module FAILS without them, it never skips).
Outputs live in the class's cache index with the lineage of the call, so the erasure closure reaches them; a
broken variant that keeps outputs outside the indices is caught by the E1-style grep of the data root.
"""
import os
import tempfile
import unittest
from pathlib import Path

from living_context.erasure import scan
from living_context.gateway.es import EsClient
from living_context.gateway.handler import EsGate
from living_context.gateway.provision import provision_tenant
from living_context.model.budget import Budget
from living_context.model.cache import DictStore, EsCacheStore
from living_context.model.port import CallContext, ModelPort, PortRefused, Task
from living_context.model.stub import StubBackend
from living_context.policy import ClassLattice

URL, CA, PW = (os.environ.get(k) for k in ("LC_TEST_ES_URL", "LC_TEST_CA", "LC_TEST_PW"))
T = "cchb"
LAT = ClassLattice(["base", "restricted"], [("restricted", "base")])
MARK = "zyxwvmarkerzz"
LEAVES = [{"text": f"The {MARK} orchard needs pruning in winter. Pruning keeps the {MARK} healthy.", "title": "orchard"}]


def reset(admin):
    st, rows = admin.request("GET", "/_cat/indices/lc-*?format=json&h=index")
    for row in rows if st == 200 and isinstance(rows, list) else []:
        if row["index"].startswith(f"lc-{T}-") or row["index"] == "lc-sys-registry":
            admin.request("DELETE", "/" + row["index"])


class FileStore(DictStore):
    """Broken variant: model outputs kept in a file outside the indices (and so outside the closure)."""
    requires_lineage = False

    def __init__(self, root):
        super().__init__()
        self.root = Path(root)

    def put(self, cls, key, value, lineage):
        (self.root / f"{key}.json").write_text(repr(value))
        super().put(cls, key, value, lineage)


class CacheOnElasticsearch(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not (URL and CA and PW):
            raise RuntimeError("LC_TEST_ES_URL, LC_TEST_CA and LC_TEST_PW must point at a scratch cluster")
        cls.tmp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.tmp.name)
        cls.admin = EsClient(URL, CA, PW, scheme="basic", user="elastic")
        reset(cls.admin)
        r = provision_tenant(cls.admin, T, ["base", "restricted"], key_dir=cls.root / "keys",
                             data_root=cls.root / "data", journal_size=65536)
        cls.gate = EsGate(URL, CA, {T: r["keys"]})

    @classmethod
    def tearDownClass(cls):
        reset(cls.admin)
        cls.tmp.cleanup()

    def port(self, name, store=None):
        store = store or EsCacheStore(lambda: self.gate.client(T, "log"), T)
        return ModelPort(StubBackend(), Budget(self.root / f"{name}.budget", 100), LAT, store), store

    def ctx(self, cls="base", subject="sub1", leaf="leaf1"):
        return CallContext(T, cls, "px", (cls,), lin_leaf_ids=(leaf,), lin_subjects=(subject,))

    def test_round_trip_class_partition_and_lineage(self):
        port, _ = self.port("b1")
        r1 = port.call(Task.BUILD_L1, {"leaves": LEAVES}, ctx=self.ctx(subject="sub-rt", leaf="leaf-rt"))
        r2 = port.call(Task.BUILD_L1, {"leaves": LEAVES}, ctx=self.ctx(subject="sub-rt", leaf="leaf-rt"))
        self.assertTrue(r1.ok and not r1.cached and r2.cached and r2.output == r1.output)
        self.assertIn(MARK, str(r1.output))
        self.admin.request("POST", f"/lc-{T}-*/_refresh")
        st, r = self.admin.request("GET", f"/lc-{T}-base-cache/_search")
        self.assertEqual(st, 200)
        src = [h["_source"] for h in r["hits"]["hits"] if "sub-rt" in h["_source"]["lin_subjects"]]
        self.assertEqual(len(src), 1)
        self.assertEqual((src[0]["cls"], src[0]["lin_leaf_ids"]), ("base", ["leaf-rt"]))
        # the same call from a restricted channel is another class: a miss, stored in the restricted cache
        r3 = port.call(Task.BUILD_L1, {"leaves": LEAVES}, ctx=self.ctx(cls="restricted", subject="sub-rt", leaf="leaf-rt"))
        self.assertFalse(r3.cached)
        self.admin.request("POST", f"/lc-{T}-*/_refresh")
        st, r = self.admin.request("GET", f"/lc-{T}-restricted-cache/_count")
        self.assertGreaterEqual(r["count"], 1)

    def test_a_call_without_lineage_is_refused_before_it_spends_budget(self):
        port, _ = self.port("b2")
        with self.assertRaises(PortRefused):
            port.call(Task.BUILD_L1, {"leaves": LEAVES}, ctx=CallContext(T, "base", "px", ("base",)))
        self.assertEqual(port._budget.count(), 0)

    def erase_and_look_for_outputs(self, store_factory, label):
        """Run a call whose output holds MARK, erase its subject through the lineage closure, then look for MARK in
        the indices (the lineage scan) and in every file under the data root (the E1 grep)."""
        data = self.root / label
        data.mkdir()
        port, _ = self.port(label, store_factory(data))
        subj = f"sub-{label}"
        out = port.call(Task.BUILD_L1, {"leaves": LEAVES}, ctx=self.ctx(subject=subj, leaf=f"leaf-{label}")).output
        self.assertIn(MARK, str(out))
        found = scan.find(self.admin, T, subjects=[subj])
        scan.delete(self.admin, found)
        self.admin.request("POST", f"/lc-{T}-*/_refresh")
        self.assertEqual(scan.find(self.admin, T, subjects=[subj]), [])
        left = [p.name for p in data.iterdir() if MARK.encode() in p.read_bytes()]
        self.assertEqual(left, [], "model output outside the erasure closure")
        return len(found)

    def test_erasure_closure_reaches_cached_model_outputs(self):
        self.assertEqual(self.erase_and_look_for_outputs(lambda d: None, "es"), 1)

    def test_broken_output_store_outside_the_indices_is_caught(self):
        with self.assertRaises(AssertionError) as cm:
            self.erase_and_look_for_outputs(lambda d: FileStore(d), "file")
        self.assertIn("outside the erasure closure", str(cm.exception))


if __name__ == "__main__":
    unittest.main()
