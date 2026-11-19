"""Shared fixture of the Step 13 tests: a tenant on a real Elasticsearch, a world of topic leaves written by the real writer,
placement, the write-path watermark, the batch rebuilder (stub model only). Not a test module."""
import os
import tempfile
import unittest
from pathlib import Path

from living_context.config import Constants
from living_context.gateway.es import EsClient
from living_context.gateway.handler import EsGate
from living_context.gateway.indices import class_index
from living_context.gateway.provision import provision_tenant
from living_context.ingest import segment, title
from living_context.ingest.subjects import Guard, TokenSource
from living_context.ingest.writer import Writer
from living_context.keystore import Keystore
from living_context.model.budget import Budget
from living_context.model.cache import DictStore
from living_context.model.port import ModelPort
from living_context.model.stub import StubBackend
from living_context.policy import ClassLattice
from living_context.structure.placement import Placer
from living_context.structure.rebuild import Rebuilder
from living_context.structure.watermark import WritePath

URL, CA, PW = (os.environ.get(k) for k in ("LC_TEST_ES_URL", "LC_TEST_CA", "LC_TEST_PW"))
LAT = ClassLattice(["base"], [])
C = Constants(f_target=4, f_max=6, f_min=2, expand_hops=1)
LETTERS = "bcdfghjklmnprstvwz"
_PORTS = 0


def word(topic: int, k: int) -> str:
    return "".join(LETTERS[(topic * 7 + k * 3 + j * 5) % len(LETTERS)] + "aeiou"[(topic + k + j) % 5] for j in range(3)) + \
        "xyqzw"[topic % 5] + LETTERS[(topic // 5 + k) % len(LETTERS)]


def leaf_text(topic: int, n: int) -> str:
    return " ".join(word(topic, (n + i) % 8) for i in range(5)) + f" topic{'abcdefghijklmnopqrstuvwxyz'[topic % 26]} note."


class FailBackend(StubBackend):
    """The stub, but a build call whose leaves carry the marker fails when `fail` is set (a timeout or refusal)."""
    fail = False

    def run(self, task, inputs):
        if self.fail and task == "build_l1" and any("zzfailzz" in l["text"] for l in inputs["leaves"]):
            raise RuntimeError("injected build failure")
        return super().run(task, inputs)


class VersionsBase(unittest.TestCase):
    T = "vers"

    @classmethod
    def reset(cls):
        st, rows = cls.admin.request("GET", "/_cat/indices/lc-*?format=json&h=index")
        for row in rows if st == 200 and isinstance(rows, list) else []:
            if row["index"].startswith(f"lc-{cls.T}-") or row["index"] == "lc-sys-registry":
                cls.admin.request("DELETE", "/" + row["index"])

    @classmethod
    def setUpClass(cls):
        if not (URL and CA and PW):
            raise RuntimeError("LC_TEST_ES_URL, LC_TEST_CA and LC_TEST_PW must point at a scratch cluster")
        cls.tmp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.tmp.name)
        cls.admin = EsClient(URL, CA, PW, scheme="basic", user="elastic")
        cls.reset()
        r = provision_tenant(cls.admin, cls.T, ["base"], key_dir=cls.root / "keys", data_root=cls.root / "data", journal_size=65536)
        cls.gate = EsGate(URL, CA, {cls.T: r["keys"]})
        cls.ks = Keystore(cls.root / "ks.db", cls.T)
        cls.guard = Guard(TokenSource(cls.ks))

    @classmethod
    def tearDownClass(cls):
        cls.ks.close()
        cls.reset()
        cls.tmp.cleanup()

    # ---- a fresh world per test: empty class indices, a new watermark, a new writer -------------------------------------------
    def structure(self):
        return lambda: self.gate.client(self.T, "structure")

    def ingest(self):
        return lambda: self.gate.client(self.T, "ingest")

    def wipe(self):
        self.admin.request("POST", f"/lc-{self.T}-*/_refresh")   # a delete-by-query sees only refreshed documents
        for kind in ("leaf", "node", "member", "edge", "ctl"):
            self.admin.request("POST", f"/lc-{self.T}-base-{kind}/_delete_by_query?refresh=true&conflicts=proceed", {"query": {"match_all": {}}})
        self.admin.request("POST", f"/lc-{self.T}-ctl/_delete_by_query?refresh=true&conflicts=proceed", {"query": {"match_all": {}}})
        self.admin.request("POST", f"/lc-{self.T}-src/_delete_by_query?refresh=true&conflicts=proceed", {"query": {"match_all": {}}})

    def port(self, backend=None):
        global _PORTS
        _PORTS += 1
        return ModelPort(backend or StubBackend(), Budget(self.root / f"b{_PORTS}", 10_000_000), LAT, DictStore())

    def world(self, consts=C, backend=None, **wp_kw):
        self.wipe()
        self.consts = consts
        self.wp = WritePath(self.structure(), self.T, consts=consts, **wp_kw)
        self.writer = Writer(self.ingest(), self.ks, self.T, self.guard, write_path=self.wp, consts=consts, gate=getattr(self, "wgate", None))
        self.placer = Placer(self.structure(), self.T, "base", self.wp, consts=consts)
        self.backend = backend or StubBackend()
        self.new = []
        self.count = 0
        return self

    def rebuilder(self, **kw):
        return Rebuilder(self.structure(), self.ingest(), self.port(self.backend), self.T, "base", self.wp, consts=self.consts,
                         owner=kw.pop("owner", "b1"), **kw)

    def add(self, topic: int, count: int, extra: str = "", place: bool = False):
        out = []
        for i in range(count):
            text = leaf_text(topic, self.count + i) + extra
            res = self.writer.remember("tok", "base", text)
            assert res.ok, res.status
            out.append((res, text))
        self.new += out
        self.count += count
        if place:
            self.settle(out)
        return [r for r, _ in out]

    def settle(self, items=None):
        """Place the given (default: the not yet placed) leaves, finish their jobs and advance P (the refresh barrier)."""
        items = items if items is not None else self.new
        for res, text in items:
            job = self.wp.begin(res.src_seq)
            self.placer.place(job, res.leaf_id, res.lv, res.src_seq, text, title.build_title(text), segment.tokens_of(text, 2.4))
            self.wp.finish(job)
        self.new = [x for x in self.new if x not in items]
        return self.wp.advance()

    def bootstrap(self, topics=6, per=4):
        for t in range(topics):
            self.add(t, per)
        for res, _ in self.new:
            self.wp.finish(self.wp.begin(res.src_seq))
        self.new = []
        self.wp.advance()
        r = self.rebuilder().bootstrap()
        assert r.status == "published", r
        return r

    def doc(self, kind, doc_id):
        st, r = self.admin.request("GET", f"/{class_index(self.T, 'base', kind)}/_doc/{doc_id}")
        return r["_source"] if st == 200 else None

    def head(self):
        return self.doc("ctl", "hd:topic")

    def leaves_under(self, ref):
        """The leaf ids under a node version, read from the immutable node documents."""
        d = self.doc("node", ref)
        if d is None:
            return None
        out = set()
        for ch in d["children"]:
            if ch["is_leaf"]:
                out.add(ch["child_id"])
            else:
                sub = self.leaves_under(f"{ch['child_id']}:{ch['child_version']}")
                if sub is None:
                    return None
                out |= sub
        return out
