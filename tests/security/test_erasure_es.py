"""BUILD-PLAN Step 14, first slice, on a real Elasticsearch (SEC-E closure, boundary and gate; AC-9).

Needs LC_TEST_ES_URL, LC_TEST_CA, LC_TEST_PW (the module FAILS without them). Each broken control must turn its check red:
closure without the upward walk (an upper node keeps the erased subject's summary), keys destroyed but nothing deleted,
commits not held at the write gate (a write lands between closure and boundary)."""
import base64
import os
import tempfile
import threading
import unittest
from pathlib import Path

from living_context.config import Constants
from living_context.erasure import scan
from living_context.erasure.executor import Executor, State
from living_context.gateway.es import EsClient
from living_context.gateway.fence import SendFence
from living_context.gateway.gate import WriteGate
from living_context.gateway.handler import EsGate
from living_context.gateway.journal import TombstoneJournal
from living_context.gateway.provision import provision_tenant
from living_context.ingest import title
from living_context.ingest import writer as wmod
from living_context.ingest.subjects import Guard, TokenSource
from living_context.ingest.writer import Writer
from living_context.keystore import Keystore
from living_context.keystore.store import KeystoreError
from living_context.model.budget import Budget
from living_context.model.cache import EsCacheStore
from living_context.model.port import CallContext, ModelPort, Task
from living_context.model.stub import StubBackend
from living_context.policy import ClassLattice
from living_context.structure import build
from living_context.structure.publish import Publisher

URL, CA, PW = (os.environ.get(k) for k in ("LC_TEST_ES_URL", "LC_TEST_CA", "LC_TEST_PW"))
T = "ersa"
LAT = ClassLattice(["base"], [])
SMALL = Constants(f_target=4, f_max=6, f_min=2)
MARK = "quillfeather"


def reset(admin):
    st, rows = admin.request("GET", "/_cat/indices/lc-*?format=json&h=index")
    for row in rows if st == 200 and isinstance(rows, list) else []:
        if row["index"].startswith(f"lc-{T}-") or row["index"] == "lc-sys-registry":
            admin.request("DELETE", "/" + row["index"])


class Scenario:
    """A tenant with 30 leaves (4 mention the erased person), a two-level tree, a cached model output and a quarantine item."""


class ErasureOnElasticsearch(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not (URL and CA and PW):
            raise RuntimeError("LC_TEST_ES_URL, LC_TEST_CA and LC_TEST_PW must point at a scratch cluster")
        cls.tmp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.tmp.name)
        cls.admin = EsClient(URL, CA, PW, scheme="basic", user="elastic")

    @classmethod
    def tearDownClass(cls):
        reset(cls.admin)
        cls.tmp.cleanup()

    def setUp(self):
        reset(self.admin)
        self.dir = Path(tempfile.mkdtemp(dir=self.root))
        r = provision_tenant(self.admin, T, ["base"], key_dir=self.dir / "keys", data_root=self.dir / "data", journal_size=65536)
        self.gate = EsGate(URL, CA, {T: r["keys"]})
        self.ks = Keystore(self.dir / "ks.db", T)
        self.addCleanup(self.ks.close)
        self.guard = Guard(TokenSource(self.ks))
        self.wgate, self.fence, self.state = WriteGate(), SendFence(), State()
        self.journal = TombstoneJournal(self.dir / "j" / "t.log", self.dir / "r" / "t.log")
        self.email = f"zed{id(self) % 1000}@example.com"
        self.subject = self.ks.subject_token(self.email)

    def writer(self, gated=True, pause=None):
        return Writer(lambda: self.gate.client(T, "ingest"), self.ks, T, self.guard, pause=pause,
                      gate=self.wgate if gated else None)

    def scenario(self):
        w = self.writer()
        recs, self.erased_ids = [], []
        for i in range(30):
            about = i % 8 == 0
            text = (f"Meeting notes number {i} about the {MARK} orchard and {self.email} visiting."
                    if about else f"Topic {i} discusses garden compost drainage and tomato staking in section {i}.")
            res = w.remember("tok", "base", text, key=f"k{i}")
            self.assertTrue(res.ok)
            toks = sorted({self.ks.subject_token(self.email)}) if about else []
            if about:
                self.erased_ids.append(res.leaf_id)
            from living_context.embed import stub
            recs.append(build.LeafRec(res.leaf_id, 1, text, title.build_title(text), stub.embed(text), 20 + i, tuple(toks), res.src_seq))
        with tempfile.TemporaryDirectory() as d:
            port = ModelPort(StubBackend(), Budget(Path(d) / "b", 10_000), LAT, EsCacheStore(lambda: self.gate.client(T, "log"), T))
            self.tree = build.build_tree(recs, port, T, "base", consts=SMALL)
        self.assertEqual(self.tree.levels, 2)
        Publisher(lambda: self.gate.client(T, "structure"), T, "base").publish(self.tree, watermark=30)
        self.admin.request("POST", f"/lc-{T}-*/_refresh")
        return recs

    def executor(self, **kw):
        er = lambda: self.gate.client(T, "erase")
        return Executor(er, lambda: self.gate.client(T, "structure"), T, self.ks, self.journal, self.wgate, self.fence,
                        self.state, purge_deadline_s=kw.pop("purge_deadline_s", 90), **kw)

    def guard_fn(self):
        return lambda: self.guard.erase_identifier(self.email)

    def node_docs(self):
        out = {}
        st, r = self.admin.request("POST", f"/lc-{T}-base-node/_search", {"size": 100, "query": {"match_all": {}}})
        for h in r["hits"]["hits"]:
            out[h["_id"]] = h["_source"]
        return out

    def residue(self):
        self.admin.request("POST", f"/lc-{T}-*/_refresh")
        return [h for h in scan.find(self.admin, T, subjects=[self.subject], leaf_ids=self.erased_ids)
                if h.kind not in ("tomb", "manifest", "node", "counter", "idem_scope")]

    # ---- the closure, the boundary and the purge -----------------------------------------------------------------
    def check_erasure(self, rc, nodes_before):
        self.assertEqual(self.residue(), [], "documents of the erased subject remain")
        self.assertEqual(rc.recount, 0)
        for lid in self.erased_ids:
            self.assertFalse(self.ks.has_key(lid, 1), "a data key survived")
        for ref, doc in self.node_docs().items():
            was = nodes_before[ref]
            if MARK in str(was["summary"]) + str(was["children"]) or (doc["level"] == 2 and was["summary"] and rc.rewritten):
                pass
        upper = [(r, d) for r, d in self.node_docs().items() if d["level"] == 2]
        self.assertTrue(all(d["summary_status"] == "erased_placeholder" for r, d in upper
                            if any(nodes_before[c["child_id"] + ":1"]["summary_status"] == "ok" and
                                   nodes_before[c["child_id"] + ":1"]["lin_subjects"] for c in nodes_before[r]["children"])),
                        "an upper node reached only through transitive lineage kept the erased subject's summary")

    def test_erasure_reaches_every_derived_artefact_and_meets_the_boundary(self):
        self.scenario()
        nodes_before = self.node_docs()
        self.assertTrue(any(MARK in str(d["summary"]) + str(d["children"]) for d in nodes_before.values()))
        rc = self.executor().erase_subject(self.subject, apply_guard=self.guard_fn())
        self.check_erasure(rc, nodes_before)
        self.assertLess(rc.boundary_s, 60.0)
        self.assertTrue(rc.purge_ok, rc.purge_s)
        print(f"erasure: boundary {rc.boundary_s:.2f} s, physical purge {max(rc.purge_s.values()):.1f} s, closure {rc.closure}")
        after = self.node_docs()
        for d in after.values():
            self.assertNotIn(MARK, str(d), "the erased subject's text survives in a node")
        self.assertEqual(self.state.seq, 1)
        self.assertTrue(set(self.erased_ids) <= self.state.erased_leaves)
        st, r = self.admin.request("POST", f"/lc-{T}-base-leaf/_search", {"query": {"match": {"text": MARK}}})
        self.assertEqual(r["hits"]["total"]["value"], 0)
        untouched = [r for r, d in after.items() if d["summary_status"] == "ok"]
        self.assertTrue(untouched, "unrelated nodes were rewritten too")
        self.assertEqual(self.writer().remember("tok", "base", f"again {self.email}").status, wmod.REFUSED)
        self.assertEqual(self.journal.last_seq, 1)

    def test_broken_closure_without_the_upward_walk_leaves_an_upper_node(self):
        self.scenario()
        nodes_before = self.node_docs()
        rc = self.executor(transitive=False, purge_deadline_s=2).erase_subject(self.subject, apply_guard=self.guard_fn())
        with self.assertRaises(AssertionError) as cm:
            self.check_erasure(rc, nodes_before)
        self.assertIn("transitive lineage", str(cm.exception))

    def test_broken_key_only_erasure_leaves_the_plaintext_projection(self):
        self.scenario()
        nodes_before = self.node_docs()
        rc = self.executor(destroy_only_keys=True).erase_subject(self.subject, apply_guard=self.guard_fn())
        with self.assertRaises(AssertionError) as cm:
            self.check_erasure(rc, nodes_before)
        self.assertIn("remain", str(cm.exception))

    def run_gate_case(self, gated):
        self.scenario()
        held, release = threading.Event(), threading.Event()

        def pause(step):
            if step == "closure":
                held.set()
                self.assertTrue(release.wait(30))

        ex = self.executor(pause=pause, purge_deadline_s=2)
        out = {}
        t = threading.Thread(target=lambda: out.__setitem__("rc", ex.erase_subject(self.subject, apply_guard=self.guard_fn())))
        t.start()
        self.assertTrue(held.wait(30))
        late = threading.Thread(target=lambda: out.__setitem__(
            "w", self.writer(gated=gated).remember("tok", "base", f"late news from {self.email} about {MARK}", key="late")))
        late.start()
        late.join(1.5)  # gated: the commit waits for the executor; ungated: it lands between closure and boundary
        release.set()
        t.join(60)
        late.join(30)
        self.assertEqual(out["w"].status, wmod.REFUSED, "a commit landed between the closure and the boundary")
        self.assertEqual(self.residue(), [], "a write that raced the closure survived the erasure")

    def test_commits_wait_at_the_write_gate_and_meet_the_tombstone(self):
        self.run_gate_case(gated=True)

    def test_broken_commits_not_held_at_the_gate_survive_the_erasure(self):
        with self.assertRaises(AssertionError) as cm:
            self.run_gate_case(gated=False)
        self.assertIn("landed between the closure and the boundary", str(cm.exception))


if __name__ == "__main__":
    unittest.main()
