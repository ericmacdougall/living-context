"""SEC-A8, the digest field contract (EXPERIMENT 9.2; BUILD-PLAN Step 16), over the registered number of queries and a fixture that
includes one-sentence leaves and leaves whose top terms are dates, amounts and account codes written with digits.

On every digest: no pointer or digest entry carries a body, gist or fact field (`check_digest`); every pointer title equals the
code-built title of its leaf and holds no token with a digit; every reason and outline label is at most 12 words and shares no
5-gram with the leaf text, gists or facts of the call's input. Each broken variant must turn the check red for its stated reason:
a pointer that carries its leaf's gist; a model-written title copied from the leaf; a code-built title that keeps tokens containing
a digit; holder reasons that quote child facts (with the reason check switched off). This tests the field contract and the digit
rule only; it does not show that titles and reasons disclose nothing (DESIGN 20)."""
import random
import re
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from living_context.embed import stub as embed_stub
from living_context.ingest import title
from living_context.model.budget import Budget
from living_context.model.cache import DictStore
from living_context.model.port import ModelPort
from living_context.model.stub import StubBackend
from living_context.policy import ClassLattice
from living_context.ids import decode_pointer
from living_context.recall import digest as dg
from living_context.recall.fanout import Recall
from living_context.recall.view import MemView
from living_context.structure import build
from tests.unit.test_tree_build import fixture

LAT = ClassLattice(["base"], [])
_SPEC = " ".join((Path(__file__).resolve().parents[2] / "docs" / "EXPERIMENT.md").read_text().split())
N_QUERIES = int(re.search(r"runs the same (\d+) queries", _SPEC).group(1))
EXTRA = [
    "Invoice 4412345 for account 99812 totals 1250 dollars due 2026-03-03 payable March.",
    "Wire 77001 of 18,400 dollars to account 55-1209 settled in April.",
    "Refunds happen on Tuesday.",
    "Ticket 3318 closed.",
    "Reconcile quokka7 and zebra9 for fund trust4412 before April, quokka7 zebra9 trust4412 again.",
    "Lunch with Maria on 5/6 cost 42.50 and the card ending 4421 was used.",
]


def make_fixture():
    leaves = fixture(n=200, topics=20, seed=7)
    base = len(leaves)
    for i, t in enumerate(EXTRA):
        leaves.append(build.LeafRec(f"{base + i + 1:032x}", 1, t, title.build_title(t), embed_stub.embed(t), 20 + i))
    with tempfile.TemporaryDirectory() as d:
        tr = build.build_tree(leaves, ModelPort(StubBackend(), Budget(Path(d) / "b", 10**8), LAT, DictStore()), "tena", "base")
    return leaves, tr


class QuotingHolder(StubBackend):
    """A holder whose reasons quote the child's own gist and facts."""

    def _route(self, i):
        out = super()._route(i)
        by = {k["label"]: k for k in i["children"]}
        out["reasons"] = {lab: " ".join((by[lab].get("facts") or [by[lab].get("gist", "")])[0].split())
                          for lab in out["selected"]}
        return out


class A8(unittest.TestCase):
    def check_a8(self, leaves, tr, *, recall_cls=Recall, backend=None, **kw):
        by_id = {l.leaf_id: l for l in leaves}
        sources = [l.text for l in leaves]
        for n in tr.nodes.values():
            for ch in n.children:
                sources += [ch.get("gist", "")] + list(ch.get("facts", []))
        rng = random.Random(5)
        words = [w for l in leaves for w in l.text.rstrip(".").split()]
        queries = [" ".join(rng.sample(rng.choice(leaves).text.rstrip(".").split(), min(3, 3)) + [rng.choice(words)])
                   for _ in range(N_QUERIES)]
        queries[:len(EXTRA)] = [" ".join(t.rstrip(".").replace(",", "").split()) for t in EXTRA]   # every digit leaf is asked for
        executed = checked_titles = 0
        with tempfile.TemporaryDirectory() as d:
            port = ModelPort(backend or StubBackend(), Budget(Path(d) / "b", 10**9), LAT, DictStore())
            for q in queries:
                executed += 1
                dig = recall_cls(port, "tena", "base", "px", **kw).recall(q, MemView(tr)).digest
                dg.check_digest(dig)
                for p in dig["pointers"]:
                    leaf = by_id[decode_pointer(p["pointer"]).id]
                    self.assertEqual(p["title"], title.build_title(leaf.text), "A8: a title is not the code-built title of its leaf")
                    allowed = {w for tok in leaf.text.lower().split() if not any(c.isdigit() for c in tok)
                               for w in re.findall(r"[^\W\d_]+(?:['\u2019-][^\W\d_]+)*", tok)}
                    self.assertFalse(any(any(c.isdigit() for c in w) for w in p["title"].split()) or not set(p["title"].split()) <= allowed,
                                     "A8: a title holds a token with a digit, or a word taken from one")
                    checked_titles += 1
                    self.assertLessEqual(len(dg.words(p["reason"])), 12, "A8: a reason is over 12 words")
                    self.assertFalse(dg.shares_ngram(p["reason"], sources), "A8: a reason shares a 5-gram with its input")
                for o in dig["outline"]:
                    self.assertLessEqual(len(dg.words(o["label"])), 12, "A8: an outline label is over 12 words")
                    self.assertFalse(dg.shares_ngram(o["label"], sources), "A8: an outline label shares a 5-gram with its input")
        self.assertEqual(executed, N_QUERIES, "the registered number of queries was not run")
        self.assertGreater(checked_titles, N_QUERIES, "too few pointers were checked")

    def test_a8_every_digest_keeps_the_field_contract_and_the_digit_rule(self):
        leaves, tr = make_fixture()
        self.assertTrue(any(re.search(r"\d", l.text) for l in leaves) and any(len(l.text.split()) <= 4 for l in leaves))
        self.check_a8(leaves, tr)

    def test_a8_the_contract_also_holds_under_the_no_content_title_mode(self):
        """AC-16: the documented alternative (an empty title for every text) is selectable and the same digest tests pass under it."""
        title.set_mode(title.MODE_NO_CONTENT)
        try:
            leaves, tr = make_fixture()
            self.assertTrue(all(l.title == "" for l in leaves))
            self.check_a8(leaves, tr)
        finally:
            title.set_mode(title.MODE_TFIDF)

    def test_a8_broken_pointer_carrying_its_leafs_gist_is_red(self):
        leaves, tr = make_fixture()
        by_id = {l.leaf_id: l for l in leaves}

        class Gist(Recall):
            def _pointer(self, view, k):
                return dict(super()._pointer(view, k), gist=by_id[k.leaf_id].text)

        with self.assertRaises(dg.ContractViolation):
            self.check_a8(leaves, tr, recall_cls=Gist)

    def test_a8_broken_model_written_title_copied_from_the_leaf_is_red(self):
        leaves, tr = make_fixture()
        by_id = {l.leaf_id: l for l in leaves}

        class First(Recall):
            def _pointer(self, view, k):
                return dict(super()._pointer(view, k), title=by_id[k.leaf_id].text.split(".")[0])

        with self.assertRaises(AssertionError) as cm:
            self.check_a8(leaves, tr, recall_cls=First)
        self.assertIn("not the code-built title", str(cm.exception))

    def test_a8_broken_code_built_title_that_keeps_digit_tokens_is_red(self):
        with mock.patch.object(title, "has_digit", lambda tok: False):
            leaves, tr = make_fixture()
            with self.assertRaises(AssertionError) as cm:
                self.check_a8(leaves, tr)
        self.assertIn("token with a digit", str(cm.exception))

    def test_a8_broken_holder_reasons_that_quote_child_facts_are_red(self):
        leaves, tr = make_fixture()
        self.check_a8(leaves, tr, backend=QuotingHolder())   # the 5-gram filter replaces the quoting reason: the digest stays clean
        with self.assertRaises(AssertionError) as cm:
            self.check_a8(leaves, tr, backend=QuotingHolder(), check_reasons=False)
        self.assertRegex(str(cm.exception), "5-gram|over 12 words")


if __name__ == "__main__":
    unittest.main()
