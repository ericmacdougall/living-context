"""BUILD-PLAN Step 9 on a real Elasticsearch: publication of a built tree (AC-4, AC-5).

Needs LC_TEST_ES_URL, LC_TEST_CA, LC_TEST_PW (the module FAILS without them). Broken variants: a head flip before the
refresh barrier (a task that pins the head cannot find the nodes), and a flip without compare-and-set (two builders both win)."""
import os
import tempfile
import threading
import unittest
from pathlib import Path

from living_context.gateway.es import EsClient
from living_context.gateway.handler import EsGate
from living_context.gateway.provision import provision_tenant
from living_context.model.budget import Budget
from living_context.model.cache import DictStore
from living_context.model.port import ModelPort
from living_context.model.stub import StubBackend
from living_context.policy import ClassLattice
from living_context.structure import build
from living_context.structure.publish import Publisher
from tests.unit.test_tree_build import fixture

URL, CA, PW = (os.environ.get(k) for k in ("LC_TEST_ES_URL", "LC_TEST_CA", "LC_TEST_PW"))
T = "trpb"
LAT = ClassLattice(["base"], [])


def reset(admin):
    st, rows = admin.request("GET", "/_cat/indices/lc-*?format=json&h=index")
    for row in rows if st == 200 and isinstance(rows, list) else []:
        if row["index"].startswith(f"lc-{T}-") or row["index"] == "lc-sys-registry":
            admin.request("DELETE", "/" + row["index"])


class PublishOnElasticsearch(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not (URL and CA and PW):
            raise RuntimeError("LC_TEST_ES_URL, LC_TEST_CA and LC_TEST_PW must point at a scratch cluster")
        cls.tmp = tempfile.TemporaryDirectory()
        root = Path(cls.tmp.name)
        cls.admin = EsClient(URL, CA, PW, scheme="basic", user="elastic")
        reset(cls.admin)
        r = provision_tenant(cls.admin, T, ["base"], key_dir=root / "keys", data_root=root / "data", journal_size=65536)
        cls.gate = EsGate(URL, CA, {T: r["keys"]})
        for kind in ("node", "member", "ctl"):  # a long refresh interval: only an explicit refresh makes a write searchable
            cls.admin.request("PUT", f"/lc-{T}-base-{kind}/_settings", {"index.refresh_interval": "300s"})
        with tempfile.TemporaryDirectory() as d:
            port = ModelPort(StubBackend(), Budget(Path(d) / "b", 1000), LAT, DictStore())
            cls.tree = build.build_tree(fixture(n=120, topics=10), port, T, "base")

    @classmethod
    def tearDownClass(cls):
        reset(cls.admin)
        cls.tmp.cleanup()

    def pub(self, **kw):
        return Publisher(lambda: self.gate.client(T, "structure"), T, "base", **kw)

    def clear(self):
        for kind in ("node", "member", "ctl"):
            self.admin.request("POST", f"/lc-{T}-base-{kind}/_refresh")
            self.admin.request("POST", f"/lc-{T}-base-{kind}/_delete_by_query?refresh=true&conflicts=proceed", {"query": {"match_all": {}}})

    def visible_nodes(self):
        st, r = self.admin.request("POST", f"/lc-{T}-base-node/_count", {"query": {"match_all": {}}})
        return r["count"]

    def test_publish_writes_nodes_members_manifest_and_head(self):
        self.clear()
        p = self.pub()
        gen = p.publish(self.tree, watermark=120)
        self.assertEqual(gen, 1)
        h = p.head()
        self.assertEqual((h["gen"], h["ancestry"]), (1, [1]))
        mf = self.admin.request("GET", f"/lc-{T}-base-ctl/_doc/mf:topic:1")[1]["_source"]
        self.assertEqual((mf["status"], mf["W"], mf["top"]), ("live", 120, self.tree.top))
        self.admin.request("POST", f"/lc-{T}-base-*/_refresh")
        self.assertEqual(self.visible_nodes(), len(self.tree.nodes))
        st, r = self.admin.request("POST", f"/lc-{T}-base-member/_count", {"query": {"term": {"gen_from": 1}}})
        self.assertEqual(r["count"], 120)
        # the second generation adopts the first's ancestry
        gen2 = p.publish(self.tree, watermark=130, expect_head=h)
        self.assertEqual((gen2, p.head()["ancestry"]), (2, [1, 2]))

    def check_barrier(self, publisher):
        self.clear()
        seen = []
        publisher._pause = lambda step: seen.append(self.visible_nodes()) if step == "refreshed" else None
        publisher.publish(self.tree)
        self.assertEqual(seen, [len(self.tree.nodes)], "the head flipped before the nodes were searchable")

    def test_head_flips_only_after_the_refresh_barrier(self):
        self.check_barrier(self.pub())

    def test_broken_flip_on_acknowledgement_is_red(self):
        with self.assertRaises(AssertionError) as cm:
            self.check_barrier(self.pub(refresh_before_flip=False))
        self.assertIn("before the nodes were searchable", str(cm.exception))

    def check_two_builders(self, mk):
        self.clear()
        a, b = mk(), mk()
        self.assertEqual(a.publish(self.tree), 1)
        head_then = a.head()
        # both builders read the head (gen 1) and both try to flip it
        g_a = a.publish(self.tree, expect_head=head_then)
        g_b = b.publish(self.tree, expect_head=head_then)
        self.assertEqual(g_a, 2)
        self.assertIsNone(g_b, "two builders both flipped the same head")
        self.admin.request("POST", f"/lc-{T}-base-*/_refresh")
        st, r = self.admin.request("POST", f"/lc-{T}-base-member/_count", {"query": {"term": {"gen_from": 3}}})
        self.assertEqual(r["count"], 0, "the losing generation left members behind")

    def test_lost_compare_and_set_discards_the_losers_documents(self):
        self.check_two_builders(self.pub)

    def test_broken_flip_without_compare_and_set_is_red(self):
        with self.assertRaises(AssertionError) as cm:
            self.check_two_builders(lambda: self.pub(use_cas=False))
        self.assertIn("both flipped", str(cm.exception))


if __name__ == "__main__":
    unittest.main()
