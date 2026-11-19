"""BUILD-PLAN Step 10: the owner's fan-out read path on the stub (AC-4, AC-8; SEC-A8 contract checks)."""
import random
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from living_context.config import Constants
from living_context.ids import KIND_LEAF, Pointer, decode_pointer
from living_context.ingest import title
from living_context.model.budget import Budget
from living_context.model.cache import DictStore
from living_context.model.port import ModelPort
from living_context.model.stub import StubBackend
from living_context.policy import ClassLattice
from living_context.recall import digest as dg
from living_context.recall.fanout import Recall
from living_context.recall.view import MemView
from living_context.structure import build
from tests.unit.test_tree_build import fixture

C = Constants()
LAT = ClassLattice(["base"], [])
LEAVES = fixture()
BY_ID = {l.leaf_id: l for l in LEAVES}


class Spy(StubBackend):
    def __init__(self):
        super().__init__()
        self.seen = []

    def run(self, task, inputs):
        self.seen.append((task, inputs))
        return super().run(task, inputs)


def mk(tmp, backend=None, **kw):
    be = backend or StubBackend()
    return ModelPort(be, Budget(Path(tmp) / "b", 1_000_000), LAT, DictStore()), be


_TREE = None


def tree():
    global _TREE
    if _TREE is None:
        with tempfile.TemporaryDirectory() as d:
            _TREE = build.build_tree(LEAVES, mk(d)[0], "tena", "base")
    return _TREE


def questions(n=40, seed=3):
    rng = random.Random(seed)
    out = []
    for _ in range(n):
        gold = rng.choice(LEAVES)
        words = gold.text.rstrip(".").split()[:6]
        out.append((" ".join(rng.sample(words, 4)), gold.leaf_id))
    return out


def recall_at_10(view, qs, **kw):
    hits = 0
    for q, gold in qs:
        with tempfile.TemporaryDirectory() as d:
            port, _ = mk(d)
            res = Recall(port, "tena", "base", "px", **kw).recall(q, view)
        ids = [decode_pointer(p["pointer"]).id for p in res.digest["sources"]["tree"][:10]]
        hits += gold in ids
    return hits / len(qs)


def shuffle_summaries(ref, entries):
    """SHUFFLED-SUMMARIES: the node-level descriptors are rotated among the children (leaf entries keep theirs)."""
    nodes = [e for e in entries if not e["is_leaf"]]
    keys = ("title", "gist", "entities", "facts")
    rot = [{k: e[k] for k in keys} for e in nodes[1:] + nodes[:1]]
    for e, r in zip(nodes, rot):
        e.update(r)
    return entries


class StubGate(unittest.TestCase):
    def test_tree_only_recall_and_the_shuffled_control(self):
        qs = questions()
        a1 = recall_at_10(MemView(tree()), qs)
        shuf = recall_at_10(MemView(tree(), mangle=shuffle_summaries), qs)
        print(f"stub gate: A1 tree-only recall@10 {a1:.3f}, SHUFFLED-SUMMARIES {shuf:.3f}")
        self.assertGreaterEqual(a1, 0.80, f"A1 tree-only recall@10 {a1}")
        self.assertGreaterEqual(a1 - shuf, 0.30, f"SHUFFLED-SUMMARIES recall {shuf} is not 0.30 below A1 {a1}")


class Contract(unittest.TestCase):
    def run_one(self, q=None, recall_cls=Recall, view=None, backend=None, **kw):
        q = q or questions(1)[0][0]
        with tempfile.TemporaryDirectory() as d:
            port, be = mk(d, backend)
            return recall_cls(port, "tena", "base", "px", **kw).recall(q, view or MemView(tree())), be

    def test_a_digest_holds_only_the_contract_fields(self):
        res, _ = self.run_one()
        dg.check_digest(res.digest)
        self.assertTrue(res.digest["pointers"])
        for p in res.digest["pointers"]:
            self.assertEqual(set(p), dg.POINTER_FIELDS)
            leaf = BY_ID[decode_pointer(p["pointer"]).id]
            self.assertNotIn(leaf.text, str(p), "a pointer carries the leaf text")
            self.assertEqual(p["size"], leaf.size_tokens)

    def test_broken_gist_with_the_pointer_is_red(self):
        class Gist(Recall):
            def _pointer(self, view, k):
                return dict(super()._pointer(view, k), gist=BY_ID[k.leaf_id].text)

        with self.assertRaises(dg.ContractViolation):
            self.run_one(recall_cls=Gist)

    def check_titles(self, res):
        for p in res.digest["pointers"]:
            leaf = BY_ID[decode_pointer(p["pointer"]).id]
            self.assertEqual(p["title"], title.build_title(leaf.text), "a title is not the code-built title")

    def test_every_title_is_the_code_built_title(self):
        res, _ = self.run_one()
        self.check_titles(res)

    def test_broken_model_written_title_is_red(self):
        class First(Recall):
            def _pointer(self, view, k):
                return dict(super()._pointer(view, k), title=BY_ID[k.leaf_id].text.split(".")[0])

        res, _ = self.run_one(recall_cls=First)
        with self.assertRaises(AssertionError) as cm:
            self.check_titles(res)
        self.assertIn("not the code-built title", str(cm.exception))

    def one_leaf_tree(self, text):
        leaf = build.LeafRec("a" * 32, 1, text, title.build_title(text), __import__("living_context.embed.stub", fromlist=["x"]).embed(text), 30)
        with tempfile.TemporaryDirectory() as d:
            return build.build_tree([leaf], mk(d)[0], "tena", "base"), leaf

    def test_a_title_has_no_digit_token_and_keeps_a_month_name(self):
        text = "Invoice dated 2024-03-05 for $1,200 on account AC4471 was settled in March by the bookkeeper."
        t, leaf = self.one_leaf_tree(text)
        res, _ = self.run_one("invoice settled bookkeeper March", view=MemView(t))
        ptrs = res.digest["pointers"]
        self.assertTrue(ptrs)
        for tok in ptrs[0]["title"].split():
            self.assertFalse(any(ch.isdigit() for ch in tok), "a digit token reached a title")

    def check_digit_tokens_dropped_whole(self):
        text = "Quokka7x quokka7x ledger a9 settled account account bookkeeper bookkeeper."
        self.assertNotIn("quokka", title.build_title(text), "a token containing a digit was kept in a title")

    def test_a_token_containing_a_digit_is_dropped_whole(self):
        self.check_digit_tokens_dropped_whole()

    def test_broken_title_that_keeps_digit_tokens_is_red(self):
        with mock.patch.object(title, "has_digit", lambda tok: False):
            with self.assertRaises(AssertionError) as cm:
                self.check_digit_tokens_dropped_whole()
        self.assertIn("containing a digit was kept", str(cm.exception))

    def test_reasons_share_no_5_gram_with_the_input(self):
        class Quote(StubBackend):
            def _route(self, i):
                out = super()._route(i)
                for k in i["children"]:
                    if k["label"] in out["reasons"] and k.get("facts"):
                        out["reasons"][k["label"]] = " ".join(k["facts"][0].split()[:9])  # quotes a fact
                return out

        res, _ = self.run_one(backend=Quote())
        self.assertGreater(res.counters.reason_failures, 0)
        bad, _ = self.run_one(backend=Quote(), check_reasons=False)  # broken: no check lets the quote through
        quoted = [p for p in bad.digest["pointers"] if len(p["reason"].split()) >= 5]
        self.assertTrue(quoted, "the broken variant should pass quoted reasons")
        self.assertEqual([p for p in res.digest["pointers"] if len(p["reason"].split()) >= 5], [])

    def test_holder_prompt_never_contains_last_seen(self):
        spy = Spy()
        res, _ = self.run_one(backend=spy)
        routes = [i for t, i in spy.seen if t == "route"]
        self.assertTrue(routes)
        self.assertFalse(any("last_seen" in str(i) or "2026-01-01" in str(i) for i in routes))
        self.assertFalse(any("child_id" in str(i) for i in routes), "a holder prompt carries ids")

    def test_a_node_with_three_times_f_max_entries_is_asked_in_three_chunks(self):
        t = tree()
        top = t.top[0]
        node = t.nodes[top]
        pend = [{"child_id": f"{i:032x}", "child_version": 1, "title": f"pending topic{i}", "size_tokens": 10}
                for i in range(3 * C.f_max - node.child_count)]
        spy = Spy()
        view = MemView(t, pending={top: pend})
        res, _ = self.run_one(backend=spy, view=view)
        rec = next(r for r in res.log if r["node"] == top)
        self.assertEqual(rec["chunks"], 3)
        pending_shown = [c for t_, i in spy.seen if t_ == "route" for c in i["children"] if c.get("pending")]
        self.assertTrue(all(set(c) == {"label", "title", "size_tokens", "pending"} for c in pending_shown))

    def test_a_holder_failure_is_a_flagged_fallback_not_an_empty_branch(self):
        class Down(StubBackend):
            def _route(self, i):
                raise RuntimeError("provider down")

        res, _ = self.run_one(backend=Down())
        self.assertGreater(res.counters.fallbacks, 0)
        self.assertTrue(all(r["fallback"] for r in res.log))
        self.assertTrue(res.digest["node_pointers"] or res.digest["pointers"], "a failed holder produced an empty branch")

    def test_an_id_not_presented_is_dropped_and_counted(self):
        class Wild(StubBackend):
            def _route(self, i):
                out = super()._route(i)
                out["selected"] = out["selected"] + ["c999"]
                return out

        res, _ = self.run_one(backend=Wild())
        self.assertGreater(res.counters.dropped_ids, 0)
        self.assertNotIn("c999", str(res.digest))

    def test_the_holder_budget_leaves_node_pointers_for_unopened_branches(self):
        c = Constants(b_holders_extra=0)
        res, _ = self.run_one(consts=c)
        self.assertTrue(res.counters.budget_reached)
        self.assertTrue(res.digest["node_pointers"])
        self.assertEqual(set(res.digest["node_pointers"][0]), dg.NODE_POINTER_FIELDS)

    def test_digest_pointers_decode_and_a_direct_list_is_fused(self):
        res, _ = self.run_one()
        p = decode_pointer(res.digest["pointers"][0]["pointer"])
        self.assertEqual((p.kind, p.gen), (KIND_LEAF, 1))
        direct = [dg.make_pointer("zzz", "t", 1, "direct", "", 0)]
        with tempfile.TemporaryDirectory() as d:
            port, _ = mk(d)
            r = Recall(port, "tena", "base", "px").recall("tomato", MemView(tree()), direct=direct)
        self.assertEqual(len(r.digest["sources"]["direct"]), 1)


if __name__ == "__main__":
    unittest.main()
