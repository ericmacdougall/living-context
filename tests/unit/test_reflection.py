"""BUILD-PLAN Step 17 on the stub (no Elasticsearch): reflection and the separate judge.

  * a planted relevant leaf that the tree did not offer is labelled relevant and `tree_missed` even when the direct search offered it
    (broken: "never offered by any source" records nothing);
  * pool (c) finds a leaf nobody offered, and the NO-AUDIT switch removes it;
  * the judge input holds the query and the leaf text only (broken: a source tag in the prompt);
  * a pair the judge could not label is unknown, never a note;
  * notes are commits: written in the join class with lineage including the query's subject tokens, and refused when the leaf or a
    subject was erased at a published tombstone, E4 (broken: the commit never consults the tombstones)."""
import tempfile
import unittest
from pathlib import Path

from living_context.feedback import judge as judgemod
from living_context.feedback.reflection import NoteWriter, Pools, reflect
from living_context.model.budget import Budget
from living_context.model.cache import DictStore
from living_context.model.port import ModelPort
from living_context.model.stub import StubBackend
from living_context.policy import ClassLattice

LAT = ClassLattice(["base", "restricted"], [("restricted", "base")])
T = "tena"
QUERY = "orchard keeper lantern"
TEXTS = {"P": "orchard keeper lantern tonight", "X1": "zebra quokka marathon", "X2": "volcano ferry timetable", "D": "orchard keeper lantern now"}


class Spy(StubBackend):
    def __init__(self, fail=False):
        super().__init__()
        self.inputs, self.fail = [], fail

    def run(self, task, inputs):
        if task == "judge_relevance":
            self.inputs.append(dict(inputs))
            if self.fail:
                raise RuntimeError("down")
        return super().run(task, inputs)


def judge_with(backend, cls=judgemod.Judge):
    d = tempfile.mkdtemp(prefix="lcrefl-")
    return cls(ModelPort(backend, Budget(Path(d) / "b", 10**6), LAT, DictStore()), T, "base", "px")


class FakeEs:
    def __init__(self):
        self.puts = []

    def request(self, method, path, body=None, **kw):
        self.puts.append((method, path, body))
        return 201, {}


class Reflection(unittest.TestCase):
    def check_tree_missed(self, **kw):
        notes = reflect(judge_with(Spy()), QUERY, Pools(tree=frozenset({"X1"}), direct=frozenset({"P", "X2"})), TEXTS.get, **kw)
        self.assertEqual([(n.leaf_id, n.tree_missed, n.any_missed, n.pool) for n in notes], [("P", True, False, "direct")],
                         "the planted relevant leaf was not recorded as tree_missed although the direct search offered it")

    def test_a_relevant_leaf_the_tree_did_not_offer_is_tree_missed_even_when_the_direct_search_offered_it(self):
        self.check_tree_missed()

    def test_broken_never_offered_by_any_source_records_nothing(self):
        with self.assertRaises(AssertionError) as cm:
            self.check_tree_missed(broken_never_offered_by_any_source=True)
        self.assertIn("planted relevant leaf was not recorded", str(cm.exception))

    def test_the_deeper_pool_finds_what_nobody_offered_and_the_switch_removes_it(self):
        pools = Pools(tree=frozenset({"X1"}), deep=frozenset({"D"}))
        found = reflect(judge_with(Spy()), QUERY, pools, TEXTS.get)
        self.assertEqual([(n.leaf_id, n.tree_missed, n.any_missed, n.pool) for n in found], [("D", True, True, "deep")])
        self.assertEqual(reflect(judge_with(Spy()), QUERY, pools, TEXTS.get, use_deep=False), [], "pool (c) was not switched off")

    def check_judge_input(self, cls=judgemod.Judge):
        be = Spy()
        reflect(judge_with(be, cls), QUERY, Pools(tree=frozenset({"X1"}), direct=frozenset({"P"})), TEXTS.get)
        self.assertTrue(be.inputs)
        for i in be.inputs:
            self.assertEqual(set(i), judgemod.INPUT_FIELDS, "the judge input carries more than the query and the leaf text")

    def test_the_judge_input_carries_no_source_tag(self):
        self.check_judge_input()

    def test_broken_judge_input_with_a_source_tag_is_red(self):
        class SourceAware(judgemod.Judge):
            @staticmethod
            def input_for(query, text):
                return {"query": query, "text": text, "source": "tree"}

        with self.assertRaises(AssertionError) as cm:
            self.check_judge_input(SourceAware)
        self.assertIn("more than the query and the leaf text", str(cm.exception))

    def test_a_pair_the_judge_could_not_label_is_unknown_not_a_note(self):
        j = judge_with(Spy(fail=True))
        self.assertIsNone(j.label(QUERY, "P", TEXTS["P"]))
        self.assertEqual(reflect(j, QUERY, Pools(direct=frozenset({"P"})), TEXTS.get), [])

    def write(self, erased, **kw):
        es = FakeEs()
        w = NoteWriter(lambda: es, T, LAT, erased_leaf=lambda l: l in erased, erased_subject=lambda t: t in erased, **kw)
        notes = reflect(judge_with(Spy()), QUERY, Pools(tree=frozenset({"X1"}), direct=frozenset({"P", "D"})), TEXTS.get)
        n = w.write(notes, search_id="s1", query=QUERY, reader_classes=["base"], item_cls_of=lambda l: "restricted" if l == "D" else "base",
                    query_subjects=["tok-asker"])
        return es, w, n

    def test_notes_are_written_in_the_join_class_with_lineage(self):
        es, w, n = self.write(set())
        self.assertEqual(n, 2)
        by = {p.split("/_doc/")[1].split("?")[0]: (p, b) for _, p, b in es.puts}
        p_path, p_doc = by["s1:P"]
        d_path, d_doc = by["s1:D"]
        self.assertTrue(p_path.startswith(f"/lc-{T}-base-note/"), p_path)
        self.assertTrue(d_path.startswith(f"/lc-{T}-restricted-note/"), "a note about a restricted leaf must sit in the join class")
        for doc in (p_doc, d_doc):
            self.assertEqual((doc["lin_subjects"], doc["lin_queries"]), (["tok-asker"], ["s1"]))
        self.assertEqual(p_doc["lin_leaf_ids"], ["P"])

    def check_erased(self, **kw):
        es, w, n = self.write({"P"}, **kw)
        self.assertEqual(n, 1, "a note about an erased leaf was committed")
        self.assertEqual(w.refused_erased, 1)
        es2, w2, n2 = self.write({"tok-asker"}, **kw)
        self.assertEqual(n2, 0, "a note carrying an erased subject was committed")

    def test_a_commit_naming_an_erased_leaf_or_subject_is_refused(self):
        self.check_erased()

    def test_broken_commit_that_never_consults_the_tombstones_is_red(self):
        with self.assertRaises(AssertionError) as cm:
            self.check_erased(check_tombstones=False)
        self.assertIn("was committed", str(cm.exception))


if __name__ == "__main__":
    unittest.main()
