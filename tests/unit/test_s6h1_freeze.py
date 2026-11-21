"""The S6-H1 freeze (EXPERIMENT 3.7): the manifest is written only from a COMPLETE S3, and `check` refuses S6-H1 when the manifest is
missing, names another commit, the code or any registered value differs from the freeze, the model or A0 fields differ, or S3's files
changed. No model, no dataset, no Elasticsearch."""
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from evalkit import claude_cli, s6h1_freeze as fz, stats

COMMIT, MODEL = "c0ffee1", "claude-sonnet-5-5"


def s3_dir(state="COMPLETE", stage="S3", beta=0.004, a0="strong"):
    d = Path(tempfile.mkdtemp())
    (d / "STATE.json").write_text(json.dumps({"state": state, "questions_done": 102}))
    (d / "STAMP.json").write_text(json.dumps({"stage": stage, "commit": "s3commit", "a0_fields": a0}))
    (d / "beta.json").write_text(json.dumps({"beta": beta, "n_questions": 94}))
    return d


def code_copy():
    d = Path(tempfile.mkdtemp())
    for sub in fz.DIGEST_DIRS:
        shutil.copytree(fz.ROOT / sub, d / sub, ignore=shutil.ignore_patterns("__pycache__"))
    for f in fz.DIGEST_FILES:
        if (fz.ROOT / f).exists():
            shutil.copy(fz.ROOT / f, d / f)
    return d


class Freeze(unittest.TestCase):
    def setUp(self):
        self.s3 = s3_dir()
        self.root = code_copy()
        self.path = Path(tempfile.mkdtemp()) / "S6-H1.json"
        fz.write_manifest(fz.make_manifest(s3_dir=self.s3, code_commit=COMMIT, model=MODEL, root=self.root), self.path)

    def check(self, **kw):
        args = dict(commit=COMMIT, s3_dir=self.s3, model=MODEL, a0_fields="strong", path=self.path, root=self.root)
        args.update(kw)
        return fz.check(**args)

    def test_the_frozen_state_passes(self):
        self.assertEqual(self.check(), [])

    def test_the_manifest_carries_beta_and_every_named_value(self):
        m = fz.load(self.path)
        self.assertEqual(m["values"]["beta"], 0.004)
        for k in ("a0_fields", "constants", "merge_rule", "K", "embedder", "prompts_sha256", "thresholds", "registered_fresh"):
            self.assertIn(k, m["values"])
        self.assertEqual(m["values"]["thresholds"]["shuffled_n"], 50)
        self.assertEqual(m["values"]["registered_fresh"]["primary"], 293)

    def test_no_manifest_is_refused(self):
        self.assertTrue(any("no freeze manifest" in w for w in self.check(path=self.path.with_name("absent.json"))))

    def test_another_commit_is_refused(self):
        self.assertTrue(any("not the commit under test" in w for w in self.check(commit="deadbee")))
        self.assertTrue(self.check(commit=""))

    def test_a_changed_source_file_is_refused_even_under_the_same_commit_name(self):
        p = self.root / "evalkit" / "arms.py"
        p.write_text(p.read_text() + "\n# one changed byte\n")
        self.assertTrue(any("differs from the frozen code" in w for w in self.check()))

    def test_the_freeze_directory_itself_is_outside_the_digest(self):
        (self.root / "evalkit" / "freeze").mkdir(exist_ok=True)
        (self.root / "evalkit" / "freeze" / "S6-H1.json").write_text("{}")
        self.assertEqual(fz.code_digest(self.root), fz.load(self.path)["code_sha256"])

    def test_a_changed_prompt_is_refused(self):
        with mock.patch.object(claude_cli, "SYSTEM_PROMPT", claude_cli.SYSTEM_PROMPT + " Be brief."):
            self.assertTrue(any("prompts_sha256" in w for w in self.check()))

    def test_a_changed_threshold_is_refused(self):
        with mock.patch.object(stats, "H1_MIN_EFFECT", 0.04):
            self.assertTrue(any("thresholds" in w for w in self.check()))

    def test_an_edited_manifest_value_is_refused(self):
        m = fz.load(self.path)
        m["values"]["merge_rule"]["k_rrf"] = 61
        self.path.write_text(json.dumps(m))
        self.assertTrue(any("merge_rule" in w for w in self.check()))

    def test_another_model_or_a0_field_set_is_refused(self):
        self.assertTrue(any("model" in w for w in self.check(model="claude-opus")))
        self.assertTrue(any("a0-fields" in w for w in self.check(a0_fields="text")))

    def test_s3_not_complete_is_refused_at_freeze_and_at_check(self):
        for bad in (s3_dir(state="STOPPED"), s3_dir(stage="S2")):
            with self.assertRaises(fz.FreezeError):
                fz.make_manifest(s3_dir=bad, code_commit=COMMIT, model=MODEL, root=self.root)
        (self.s3 / "STATE.json").write_text(json.dumps({"state": "PAUSED_THROTTLE"}))
        self.assertTrue(any("not COMPLETE" in w for w in self.check()))

    def test_a_changed_s3_beta_is_refused(self):
        (self.s3 / "beta.json").write_text(json.dumps({"beta": 0.008, "n_questions": 94}))
        why = self.check()
        self.assertTrue(any("beta" in w for w in why))

    def test_the_manifest_is_written_once(self):
        with self.assertRaises(fz.FreezeError):
            fz.write_manifest({"stage": "S6-H1"}, self.path)

    def test_an_amended_dev_exposed_list_restates_the_primary_count(self):
        m = fz.make_manifest(s3_dir=self.s3, code_commit=COMMIT, model=MODEL, root=self.root, dev_exposed_extra=["qa", "qb"])
        self.assertEqual(m["values"]["registered_fresh"]["primary"], 291)


if __name__ == "__main__":
    unittest.main()
