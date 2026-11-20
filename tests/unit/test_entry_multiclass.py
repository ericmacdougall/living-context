"""BUILD-PLAN Step 16, the reader entry for a reader of several classes (DESIGN 8.1, 17): one digest per class, each exactly the digest
the reader of that class alone gets, merged into one. Needs no Elasticsearch (the direct search is replaced by a recorder; the tree
view is the in-memory one). Broken variant: an entry that reads every class of the lattice instead of the read set."""
import tempfile
import unittest
from pathlib import Path

from living_context.embed import stub as embed_stub
from living_context.ids import decode_pointer
from living_context.ingest import title
from living_context.model.budget import Budget
from living_context.model.cache import DictStore
from living_context.model.port import ModelPort
from living_context.model.stub import StubBackend
from living_context.policy import ClassLattice, Principal, Role
from living_context.recall.direct import DirectSearch, Hit
from living_context.recall.entry import recall_digest
from living_context.recall.fanout import Recall
from living_context.recall.view import MemView
from living_context.structure import build
from tests.unit.test_tree_build import fixture

LAT = ClassLattice(["base", "restricted"], [("restricted", "base")])
T = "tena"


def leaves_of(cls, seed):
    out = fixture(n=40, topics=4, seed=seed)
    off = 0 if cls == "base" else 5000
    return [build.LeafRec(f"{int(l.leaf_id, 16) + off:032x}", 1, l.text, l.title, l.embedding, l.size_tokens) for l in out]


LEAVES = {"base": leaves_of("base", 7), "restricted": leaves_of("restricted", 99)}


class Recorder(DirectSearch):
    def __init__(self):
        super().__init__(lambda c: None, T)
        self.calls = []

    def search(self, p, lattice, text, *, size=10, classes=None):
        self.calls += list(classes or [])
        return [Hit(c, f"{l.leaf_id}:1", l.title, l.size_tokens, 1.0) for c in (classes or []) for l in LEAVES[c][:3]]


class MultiClass(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with tempfile.TemporaryDirectory() as d:
            port = ModelPort(StubBackend(), Budget(Path(d) / "b", 10**8), LAT, DictStore())
            cls.trees = {c: build.build_tree(LEAVES[c], port, T, c) for c in LEAVES}

    def run_entry(self, principal, **kw):
        rec = Recorder()
        with tempfile.TemporaryDirectory() as d:
            port = ModelPort(StubBackend(), Budget(Path(d) / "b", 10**8), LAT, DictStore())
            dig = recall_digest(principal, LAT, "topic w0x1ab shared", direct=rec, view_for=lambda c: MemView(self.trees[c]),
                                recall_for=lambda c: Recall(port, T, c, "px"), **kw)
        return dig, rec

    @staticmethod
    def reader(*classes):
        return Principal(T, "p-" + "-".join(classes), frozenset([Role.READER]), frozenset(classes), frozenset())

    def classes_of(self, dig):
        ids = {l.leaf_id: c for c, ls in LEAVES.items() for l in ls}
        return {ids[decode_pointer(p["pointer"]).id] for p in dig["pointers"]}

    def check_read_set(self, **kw):
        both, rec_both = self.run_entry(self.reader("base", "restricted"))
        self.assertEqual(self.classes_of(both), {"base", "restricted"}, "a reader of two classes got one class only")
        self.assertEqual(sorted(set(rec_both.calls)), ["base", "restricted"])
        self.assertEqual(set(both["pinned"]["gens"]), {"base", "restricted"})
        only_base, rec = self.run_entry(self.reader("base"), **kw)
        self.assertEqual(set(rec.calls) - {"base"}, set(), "a class outside the read set was read")
        self.assertEqual(self.classes_of(only_base), {"base"})
        return both, only_base

    def test_a_reader_of_several_classes_gets_one_merged_digest_and_a_single_class_reader_is_unchanged(self):
        both, only_base = self.check_read_set()
        # the base part of the merged digest is what a reader of base alone gets (a restricted-only read set is not down-closed:
        # a reader of restricted reads base too, so there is no reader of the restricted class alone)
        base_ids = {l.leaf_id for l in LEAVES["base"]}
        merged_base = {p["pointer"] for p in both["pointers"] if decode_pointer(p["pointer"]).id in base_ids}
        self.assertEqual(merged_base, {p["pointer"] for p in only_base["pointers"]}, "the base part of the merged digest differs")
        again, _ = self.run_entry(self.reader("restricted", "base"))
        self.assertEqual(again, both, "the merged digest depends on the order of the read set")
        self.assertEqual(both["sizes"]["count"], len(both["pointers"]))

    def test_broken_entry_that_reads_every_class_of_the_lattice_is_red(self):
        with self.assertRaises(AssertionError) as cm:
            self.check_read_set(broken_all_classes=True)
        self.assertIn("outside the read set", str(cm.exception))

    def test_a_reader_with_no_readable_class_is_refused(self):
        from living_context.recall.entry import EntryError
        with self.assertRaises(EntryError):
            self.run_entry(Principal(T, "p-none", frozenset([Role.READER]), frozenset(), frozenset()))


if __name__ == "__main__":
    unittest.main()
