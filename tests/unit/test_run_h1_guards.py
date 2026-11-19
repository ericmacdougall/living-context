"""The H1 runner starts nothing it should not: S2 needs the owner's approval and cap, a real embedding port, a named commit, a clean
run directory and pilot questions only. No Elasticsearch and no model are touched here."""
import argparse
import tempfile
import unittest
from pathlib import Path

from evalkit import run_h1
from living_context.embed import e5, stub as embed_stub


def args(**kw):
    base = dict(stage="S2", splits=["pilot"], approved_cap=6000, approval_ref="owner approval of S2", embed=None, commit="abc1234",
                model_kind="claude", n_questions=30, run_dir=tempfile.mkdtemp(), resume=False)
    base.update(kw)
    return argparse.Namespace(**base)


class RealEmbed:               # stands in for the pinned real embedder (the guard checks the pin, not the model)
    is_real, model_id = True, e5.MODEL_ID
    def passage(self, t): return [0.0]
    query = passage


real_embed = RealEmbed()


class Guards(unittest.TestCase):
    def test_s2_with_everything_but_a_real_embedding_port_is_refused_for_that_reason(self):
        why = run_h1.check_prereqs(args())
        self.assertEqual(len(why), 1)
        self.assertIn("pinned real embedder", why[0])

    def test_s2_with_every_prerequisite_passes_the_guards(self):
        self.assertEqual(run_h1.check_prereqs(args(), embed=real_embed), [])

    def test_the_stub_embedder_cannot_be_selected_in_a_real_stage(self):
        for emb in (embed_stub, embed_stub.embed, None, object()):
            why = run_h1.check_prereqs(args(), embed=emb)
            self.assertTrue(any("pinned real embedder" in w for w in why), emb)

    def test_resume_must_continue_the_same_run(self):
        import json
        d = tempfile.mkdtemp()
        Path(d, "STAMP.json").write_text(json.dumps({"commit": "abc1234", "cap": 6000, "n_questions": 30, "a0_fields": "strong"}))
        self.assertEqual(run_h1.check_prereqs(args(run_dir=d, resume=True), embed=real_embed), [])
        for kw, word in ((dict(commit="zzz"), "commit"), (dict(approved_cap=9000), "cap"), (dict(a0_fields="text"), "a0_fields")):
            why = " ".join(run_h1.check_prereqs(args(run_dir=d, resume=True, **kw), embed=real_embed))
            self.assertIn("differs from the stamp", why)
            self.assertIn(word, why)

    def test_a_stop_file_blocks_start_and_resume(self):
        d = tempfile.mkdtemp()
        Path(d, "STOP").write_text("")
        self.assertTrue(any("stop file" in w for w in run_h1.check_prereqs(args(run_dir=d), embed=real_embed)))

    def test_each_missing_prerequisite_is_named(self):
        for kw, word in ((dict(approved_cap=0), "call cap"), (dict(approval_ref=""), "approval"), (dict(commit=""), "commit"),
                         (dict(model_kind="stub"), "real model port"), (dict(n_questions=31), "30 pilot"), (dict(n_questions=12), "30 pilot"),
                         (dict(splits=["test"]), "pilot split"), (dict(splits=["pilot", "held_back"]), "pilot split")):
            why = " ".join(run_h1.check_prereqs(args(**kw), embed=real_embed))
            self.assertIn(word, why, kw)

    def test_a_second_stamped_run_is_refused_and_resume_is_allowed(self):
        d = tempfile.mkdtemp()
        Path(d, "STAMP.json").write_text('{"commit": "abc1234", "cap": 6000, "n_questions": 30, "a0_fields": "strong"}')
        self.assertTrue(any("stamped run" in w for w in run_h1.check_prereqs(args(run_dir=d), embed=real_embed)))
        self.assertEqual(run_h1.check_prereqs(args(run_dir=d, resume=True), embed=real_embed), [])

    def test_s3_is_registered_on_102_questions_with_shuffled_on_50_and_needs_the_same_approvals(self):
        ok = dict(stage="S3", n_questions=102, shuffled_n=50, approved_cap=17252)
        self.assertEqual(run_h1.check_prereqs(args(**ok), embed=real_embed), [])
        for kw, word in ((dict(n_questions=30), "102 pilot"), (dict(shuffled_n=0), "50 questions"), (dict(shuffled_n=30), "50 questions"),
                         (dict(approved_cap=0), "call cap"), (dict(approval_ref=""), "approval"), (dict(model_kind="stub"), "real model port")):
            self.assertIn(word, " ".join(run_h1.check_prereqs(args(**{**ok, **kw}), embed=real_embed)), kw)
        self.assertTrue(any("pinned real embedder" in w for w in run_h1.check_prereqs(args(**ok), embed=embed_stub.embed)))
        self.assertTrue(any("on all" in w for w in run_h1.check_prereqs(args(shuffled_n=50), embed=real_embed)))   # S2 runs SHUFFLED on all

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
