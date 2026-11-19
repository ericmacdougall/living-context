"""BUILD-PLAN Step 10 on a real Elasticsearch: the pinned view reads the node index at the generation pinned when the task
starts (SEC-V1 for the read path). Needs LC_TEST_ES_URL, LC_TEST_CA, LC_TEST_PW. Broken variant: the view re-reads the head
on every call, so a flip during the task changes what the task sees (red)."""
import os
import tempfile
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
from living_context.recall.view import EsView, MemView
from living_context.structure import build
from living_context.structure.publish import Publisher
from tests.unit.test_tree_build import fixture

URL, CA, PW = (os.environ.get(k) for k in ("LC_TEST_ES_URL", "LC_TEST_CA", "LC_TEST_PW"))
T = "vwes"
LAT = ClassLattice(["base"], [])


def reset(admin):
    st, rows = admin.request("GET", "/_cat/indices/lc-*?format=json&h=index")
    for row in rows if st == 200 and isinstance(rows, list) else []:
        if row["index"].startswith(f"lc-{T}-") or row["index"] == "lc-sys-registry":
            admin.request("DELETE", "/" + row["index"])


class ViewOnElasticsearch(unittest.TestCase):
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
        with tempfile.TemporaryDirectory() as d:
            port = ModelPort(StubBackend(), Budget(Path(d) / "b", 2000), LAT, DictStore())
            cls.t1 = build.build_tree(fixture(n=120, topics=10), port, T, "base")
            cls.t2 = build.build_tree(fixture(n=60, topics=6), port, T, "base")

    @classmethod
    def tearDownClass(cls):
        reset(cls.admin)
        cls.tmp.cleanup()

    def pub(self):
        return Publisher(lambda: self.gate.client(T, "structure"), T, "base")

    def view(self, **kw):
        return EsView(self.gate.client(T, "read-base"), T, "base", "topic", **kw)

    def setUp(self):
        for kind in ("node", "member", "ctl"):
            self.admin.request("POST", f"/lc-{T}-base-{kind}/_refresh")  # a delete-by-query only sees refreshed documents
            self.admin.request("POST", f"/lc-{T}-base-{kind}/_delete_by_query?refresh=true&conflicts=proceed", {"query": {"match_all": {}}})

    def flip_during_task(self, **kw):
        p = self.pub()
        p.publish(self.t1, watermark=120)
        v = self.view(**kw)
        before = v.top()
        p.publish(self.t2, watermark=130, expect_head=p.head())
        self.assertEqual(v.top(), before, "a flip during the task changed what the task sees")
        return v

    def test_the_view_matches_the_built_tree_and_stays_on_its_pinned_generation(self):
        v = self.flip_during_task()
        self.assertEqual(v.gen, 1)
        mem = MemView(self.t1)
        for ref in v.top():
            got = [{k: e[k] for k in ("child_id", "title", "gist", "is_leaf")} for e in v.entries(ref)]
            want = [{k: e[k] for k in ("child_id", "title", "gist", "is_leaf")} for e in mem.entries(ref)]
            self.assertEqual(got, want)
        self.assertEqual(self.view().gen, 2)  # a task that pins after the flip sees the new generation
        self.assertEqual(self.view().top(), self.t2.top)

    def test_broken_re_reading_the_head_per_call_is_red(self):
        with self.assertRaises(AssertionError) as cm:
            self.flip_during_task(repin_each_call=True)
        self.assertIn("flip during the task", str(cm.exception))

    def test_an_empty_family_pins_generation_zero(self):
        v = self.view()
        self.assertEqual((v.gen, v.top()), (0, []))


if __name__ == "__main__":
    unittest.main()
