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
from living_context.ids import decode_pointer
from living_context.recall.fanout import Recall
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
        self.admin.request("POST", f"/lc-{T}-ctl/_delete_by_query?refresh=true&conflicts=proceed", {"query": {"match_all": {}}})
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

    # ---- pending members (DESIGN 8.1, 13.2) and the pinned view through the recall path ----------------------------
    def put_member(self, parent, n, src_seq, title="zymurgy", gen_to=()):
        doc = {"tenant": T, "cls": "base", "kind": "member", "lin_leaves": [], "lin_leaf_ids": [f"{n:032x}"], "lin_nodes": [parent],
               "lin_subjects": [], "lin_queries": [], "derivation": "placement", "erasure_rewrite": 0, "created_at": "2026-01-01T00:00:00Z",
               "schema_v": 1, "parent_id": parent, "child_id": f"{n:032x}:1", "score": 1.0, "shaped_by": "placement",
               "src_seq": src_seq, "gen_to": list(gen_to), "title": title, "size_tokens": 7}
        st, r = self.admin.request("PUT", f"/lc-{T}-base-member/_doc/pm{n}?refresh=true", doc)
        self.assertIn(st, (200, 201), r)

    def set_p(self, p):
        doc = {"tenant": T, "cls": "tenantwide", "kind": "counter", "lin_leaves": [], "lin_leaf_ids": [], "lin_nodes": [], "lin_subjects": [],
               "lin_queries": [], "derivation": "build", "erasure_rewrite": 0, "created_at": "2026-01-01T00:00:00Z", "schema_v": 1, "P": p}
        st, r = self.admin.request("PUT", f"/lc-{T}-ctl/_doc/wp?refresh=true", doc)
        self.assertIn(st, (200, 201), r)

    def pending_scenario(self, **kw):
        p = self.pub()
        p.publish(self.t1, watermark=120)
        top = self.t1.top[0]
        self.put_member(top, 1, 121)
        self.put_member(top, 2, 122)
        self.put_member(top, 3, 119)           # covered by the build already (at most W): not pending
        self.put_member(top, 4, 121, gen_to=[1])  # retired in the pinned ancestry
        self.set_p(122)
        v = self.view(**kw)
        self.put_member(top, 5, 123)           # written after the pin; above the pinned P
        self.set_p(125)
        return v, top

    def test_pending_members_are_those_above_w_and_at_most_the_pinned_p_for_the_whole_task(self):
        v, top = self.pending_scenario()
        got = sorted(e["child_id"] for e in v.entries(top) if e["pending"])
        self.assertEqual(got, [f"{1:032x}", f"{2:032x}"], "pending members above the pinned P, retired or covered ones are shown")
        e = next(e for e in v.entries(top) if e["pending"])
        self.assertEqual((e["title"], e["size_tokens"], e["gist"]), ("zymurgy", 7, ""))
        self.assertEqual(len(self.view().entries(top)) - len(v.entries(top)), 1)  # a task pinned after P moved sees member 5 too

    def test_broken_ignoring_the_pinned_p_shows_a_member_published_after_the_pin(self):
        v, top = self.pending_scenario(broken_ignore_p=True)
        with self.assertRaises(AssertionError) as cm:
            got = sorted(e["child_id"] for e in v.entries(top) if e["pending"])
            self.assertEqual(got, [f"{1:032x}", f"{2:032x}"], "pending members above the pinned P, retired or covered ones are shown")
        self.assertIn("above the pinned P", str(cm.exception))

    def test_the_pinned_view_drives_recall_end_to_end_and_a_pending_leaf_is_found_with_its_code_built_title(self):
        v, top = self.pending_scenario()
        with tempfile.TemporaryDirectory() as d:
            port = ModelPort(StubBackend(), Budget(Path(d) / "b", 100_000), LAT, DictStore())
            res = Recall(port, T, "base", "px").recall("zymurgy", v)
        ids = {decode_pointer(p["pointer"]).id for p in res.digest["pointers"]}
        self.assertIn(f"{1:032x}", ids)
        self.assertNotIn(f"{5:032x}", ids, "a member above the pinned P reached the digest")
        self.assertEqual(res.digest["pinned"]["gen"], v.gen)
        titles = {p["title"] for p in res.digest["pointers"] if decode_pointer(p["pointer"]).id == f"{1:032x}"}
        self.assertEqual(titles, {"zymurgy"})
        # a flip during the task changes nothing the recall saw
        p = self.pub()
        p.publish(self.t2, watermark=130, expect_head=p.head())
        with tempfile.TemporaryDirectory() as d:
            port = ModelPort(StubBackend(), Budget(Path(d) / "b", 100_000), LAT, DictStore())
            res2 = Recall(port, T, "base", "px").recall("zymurgy", v)
        self.assertEqual(res2.digest["pointers"], res.digest["pointers"])

    def test_an_empty_family_pins_generation_zero(self):
        v = self.view()
        self.assertEqual((v.gen, v.top()), (0, []))


if __name__ == "__main__":
    unittest.main()
