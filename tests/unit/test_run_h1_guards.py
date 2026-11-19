"""The H1 runner starts nothing it should not: S2 needs the owner's approval and cap, a real embedding port, a named commit, a clean
run directory and pilot questions only. No Elasticsearch and no model are touched here."""
import argparse
import tempfile
import unittest
from pathlib import Path

from evalkit import run_h1
from living_context.embed import stub as embed_stub


def args(**kw):
    base = dict(stage="S2", splits=["pilot"], approved_cap=6000, approval_ref="owner approval of S2", embed=None, commit="abc1234",
                model_kind="claude", n_questions=30, run_dir=tempfile.mkdtemp(), resume=False)
    base.update(kw)
    return argparse.Namespace(**base)


def real_embed(text):          # stands in for a real embedding port; any callable that is not the stub
    return [0.0]


class Guards(unittest.TestCase):
    def test_s2_with_everything_but_a_real_embedding_port_is_refused_for_that_reason(self):
        why = run_h1.check_prereqs(args())
        self.assertEqual(len(why), 1)
        self.assertIn("real embedding port", why[0])

    def test_s2_with_every_prerequisite_passes_the_guards(self):
        self.assertEqual(run_h1.check_prereqs(args(), embed=real_embed), [])

    def test_each_missing_prerequisite_is_named(self):
        for kw, word in ((dict(approved_cap=0), "call cap"), (dict(approval_ref=""), "approval"), (dict(commit=""), "commit"),
                         (dict(model_kind="stub"), "real model port"), (dict(n_questions=31), "30 pilot"),
                         (dict(splits=["test"]), "pilot split"), (dict(splits=["pilot", "held_back"]), "pilot split")):
            why = " ".join(run_h1.check_prereqs(args(**kw), embed=real_embed))
            self.assertIn(word, why, kw)

    def test_a_second_stamped_run_is_refused_and_resume_is_allowed(self):
        d = tempfile.mkdtemp()
        Path(d, "STAMP.json").write_text("{}")
        self.assertTrue(any("stamped run" in w for w in run_h1.check_prereqs(args(run_dir=d), embed=real_embed)))
        self.assertEqual(run_h1.check_prereqs(args(run_dir=d, resume=True), embed=real_embed), [])

    def test_the_dev_stub_smoke_needs_no_approval_and_no_embedding_port(self):
        self.assertEqual(run_h1.check_prereqs(args(stage="DEV-STUB", approved_cap=0, approval_ref="", model_kind="stub", n_questions=3)), [])

    def test_the_question_pick_is_seeded_and_skips_abstention(self):
        from evalkit import dataset as ds
        mk = lambda qid, gold: ds.Question(qid, "t", "q", "a", tuple(gold), (("s", (("user", "x"),)),))
        qs = [mk("a1", ["s"]), mk("b2_abs", []), mk("c3", ["s"]), mk("d4", ["s"])]
        got = [q.qid for q in run_h1.pick_questions(qs, 5)]
        self.assertEqual(sorted(got), ["a1", "c3", "d4"])
        self.assertEqual(got, [q.qid for q in run_h1.pick_questions(list(reversed(qs)), 5)])


if __name__ == "__main__":
    unittest.main()
