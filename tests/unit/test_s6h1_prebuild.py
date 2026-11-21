"""The early S6-H1 build (EXPERIMENT 14.5) on a SYNTHETIC dataset file: the pool contents are read with the questions, answers and gold
sealed at parse time (never held in any object); the prebuild ingests the whole pool and builds the tree with `parse_record`,
`load_questions` and `fresh_questions` made to raise; the questions stay unreleased until RELEASE.json; the stamped run attaches to
the prebuilt tenant and tree (no build call twice) and gives the same ledger as a run without the prebuild; and every binding (code,
fields, pool, registry, tenant state) refuses when it does not hold."""
import builtins
import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from evalkit import dataset as ds, run_s6h1 as rs
from tests.security.test_run_s2_es import Runner
from tests.unit import test_s6h1_release as rel
from tests.unit.test_parallel import seeded_node_ids
from tests.unit.test_run_s6h1 import POOL, RealEmbed, _TENANTS, args, factory, setup_dirs

AMEND = "DEV-14.5"


def raw(qs, extra=()):
    """Registered record shape with a marker in every sealed field."""
    out = []
    for q in qs:
        out.append({"question_id": q.qid, "question_type": q.qtype, "question": "SEALEDQ %s" % q.qid, "answer": "SEALEDA %s" % q.qid,
                    "answer_session_ids": list(q.gold_session_ids), "question_date": "SEALEDD",
                    "haystack_dates": ["SEALEDD"] * len(q.sessions), "haystack_session_ids": [sid for sid, _ in q.sessions],
                    "haystack_sessions": [[{"role": r, "content": c, "has_answer": True} for r, c in turns] for _, turns in q.sessions]})
    return out + list(extra)


def write(recs):
    p = Path(tempfile.mkdtemp()) / "data.json"
    p.write_text(json.dumps(recs))
    return str(p)


def sealed():
    boom = mock.Mock(side_effect=AssertionError("a question was parsed during the early build"))
    return mock.patch.multiple(ds, parse_record=boom, load_questions=boom, fresh_questions=boom, read_release=boom, session_hashes=boom,
                               content_clusters=boom)


def sealed_strings(qs):
    """What must never leave the early build: the markers of every sealed field, has_answer, every question id and session id."""
    return {"SEALED", "has_answer"} | {q.qid for q in qs} | {sid for q in qs for sid, _ in q.sessions}


class PoolContents(unittest.TestCase):
    def test_it_is_the_whole_pool_of_the_parsed_questions_with_a_repeated_id_inside_one_haystack(self):
        rep = {"question_id": "rep", "question_type": "x", "question": "SEALEDQ rep", "answer": "SEALEDA rep",
               "answer_session_ids": ["r1"], "haystack_session_ids": ["r1", "r2", "r1"],
               "haystack_sessions": [[{"role": "user", "content": "one"}], [{"role": "user", "content": "two"}],
                                     [{"role": "user", "content": "one, a second copy that differs"}]]}     # the first copy wins
        path = write(raw(rel.QS, [rep]))
        got = ds.pool_contents(path, expect_sha256=None)
        self.assertEqual(got, rs.whole_pool(ds.load_questions(path, expect_sha256=None)))
        self.assertEqual(len(got), POOL + 2)

    def test_nothing_but_the_haystack_arrays_and_role_and_content_survives_the_parser(self):
        path = write(raw(rel.QS))
        made = []
        real = ds._pool_pairs
        with mock.patch.object(ds, "_pool_pairs", lambda pairs: made.append(real(pairs)) or made[-1]):
            got = ds.pool_contents(path, expect_sha256=None)
        self.assertTrue(made)
        self.assertTrue(all(set(d) <= set(ds.POOL_KEYS) for d in made))
        self.assertNotIn("SEALED", repr(got))
        self.assertNotIn("has_answer", repr(got))

    def test_the_same_refusals_as_the_question_loader(self):
        with self.assertRaises(ds.DatasetError):
            ds.pool_contents(write(raw(rel.QS)), expect_sha256="0" * 64)
        bad = raw(rel.QS[:1])
        bad[0]["haystack_session_ids"].append("extra")
        with self.assertRaises(ds.DatasetError):
            ds.pool_contents(write(bad), expect_sha256=None)
        two = raw(rel.QS[:2])
        two[1]["haystack_session_ids"][0] = two[0]["haystack_session_ids"][0]       # one id, two contents (across records)
        two[1]["haystack_sessions"][0] = [{"role": "user", "content": "different"}]
        with self.assertRaises(ds.DatasetError):
            ds.pool_contents(write(two), expect_sha256=None)
        with self.assertRaises(ds.DatasetError):
            ds.load_questions(write(two), expect_sha256=None) and rs.whole_pool(ds.load_questions(write(two), expect_sha256=None))


@mock.patch.object(rs, "EXPECTED_LEAVES", 1)
class Prebuild(unittest.TestCase):
    def setUp(self):
        self.base, self.s3, self.man, self.run = setup_dirs()
        self.release = (self.run / ds.RELEASE_FILE).read_text()
        (self.run / ds.RELEASE_FILE).unlink()                     # the early build comes before the release
        self.path = write(raw(rel.QS))

    def pa(self, run=None, **kw):
        return args(self.base, self.s3, run or self.run, **dict(dict(prebuild=True, amendment_ref=AMEND, release_ref=""), **kw))

    def sa(self, run=None, **kw):
        return args(self.base, self.s3, run or self.run, **dict(dict(amendment_ref=""), **kw))

    def why(self, a, man=None):
        return rs.check_prereqs(a, embed=RealEmbed(), manifest_path=man or self.man, mem_available=lambda: 64000.0)

    def prebuild(self, run=None, runner=None, **kw):
        run = run or self.run
        real = ds.pool_contents
        with sealed(), mock.patch.object(ds, "pool_contents", lambda path: real(path, expect_sha256=None)):    # the runner's own read
            return rs.run(self.pa(run, dataset=self.path, **kw), embedder=RealEmbed(), runner=runner or Runner(),
                          tenant_factory=factory(run), registered_pool=POOL, manifest_path=self.base / "absent.json")

    def stamped(self, run=None, man=None, **kw):
        run = run or self.run
        return rs.run(self.sa(run, **kw), embedder=RealEmbed(), runner=Runner(), loader=lambda _a: rel.QS, tenant_factory=factory(run),
                      registered_pool=POOL, registered_fresh=rel.REG, manifest_path=man or self.man)

    def calls(self, run=None):
        return [json.loads(l) for l in ((run or self.run) / "calls.jsonl").read_text().splitlines()]

    def test_prebuild_then_release_then_the_stamped_run_attaches_and_gives_the_same_ledger(self):
        base0, s30, man0, run0 = setup_dirs()
        with seeded_node_ids():
            self.assertEqual(rs.run(args(base0, s30, run0), embedder=RealEmbed(), runner=Runner(), loader=lambda _a: rel.QS,
                                    tenant_factory=factory(run0), registered_pool=POOL, registered_fresh=rel.REG, manifest_path=man0), 0)
        with seeded_node_ids():
            self.assertEqual(self.why(self.pa(), man=self.base / "absent.json"), [])        # no freeze, no release needed
            self.assertEqual(self.prebuild(), 0)
            st = json.loads((self.run / "STATE.json").read_text())
            self.assertEqual(st["state"], "PREBUILT")
            self.assertNotIn("questions_done", st)
            self.assertFalse((self.run / "done.jsonl").exists())
            self.assertFalse((self.run / "STAMP.json").exists())
            pre = json.loads((self.run / rs.PREBUILD_FILE).read_text())
            self.assertEqual((pre["commit"], pre["amendment_ref"], pre["pool_contents"]), (args(self.base, self.s3, self.run).commit,
                                                                                         AMEND, POOL))
            self.assertEqual([r["stage"] for r in rs._registry_rows(self.base / "stamps.jsonl")], [rs.PREBUILD_STAGE])
            n_build = len(self.calls())
            self.assertTrue(n_build > 0 and all(c["task"].startswith("build") for c in self.calls()))
            self.assertNotIn("SEALED", (self.run / "calls.jsonl").read_text())
            self.assertEqual(self.prebuild(), 0)                                               # a rerun charges nothing more
            self.assertEqual(len(self.calls()), n_build)
            # the questions stay sealed until RELEASE.json
            self.assertTrue(any("no release file" in w for w in self.why(self.sa())))
            self.assertEqual(self.stamped(), 2)
            self.assertIn("Unreleased", json.loads((self.run / "STATE.json").read_text())["error"])
            self.assertEqual(len(self.calls()), n_build)
            (self.run / ds.RELEASE_FILE).write_text(self.release)
            self.assertEqual(self.why(self.sa()), [])
            self.assertEqual(self.stamped(), 0)
        self.assertEqual(json.loads((self.run / "STATE.json").read_text())["state"], "COMPLETE")
        self.assertEqual(sum(c["task"].startswith("build") for c in self.calls()), n_build)          # the tree was not built again
        self.assertEqual(json.loads((self.run / "STAMP.json").read_text())["prebuild"]["pool_sha256"], pre["pool_sha256"])
        self.assertEqual((self.run / "done.jsonl").read_bytes(), (run0 / "done.jsonl").read_bytes())
        self.assertEqual(len(self.calls()), len(self.calls(run0)))

    def test_no_prebuild_output_names_a_question_an_answer_a_session_or_has_answer(self):
        """The release gate of the early build: the dataset is opened once, inside pool_contents, and no file of the run directory,
        no registry row and nothing printed holds a sealed marker, has_answer, a question id or a session id."""
        target, inside, opened = os.path.realpath(self.path), [False], []
        real_pc, real_open, real_io_open = ds.pool_contents, builtins.open, io.open

        def pc(path):
            inside[0] = True
            try:
                return real_pc(path, expect_sha256=None)
            finally:
                inside[0] = False

        def spy(opener):
            def o(file, *a, **k):
                if isinstance(file, (str, bytes, os.PathLike)) and os.path.realpath(os.fsdecode(file)) == target:
                    opened.append(inside[0])
                return opener(file, *a, **k)
            return o
        out = io.StringIO()
        with sealed(), mock.patch.object(ds, "pool_contents", pc), mock.patch("builtins.open", spy(real_open)), \
                mock.patch("io.open", spy(real_io_open)), contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            rc = rs.run(self.pa(dataset=self.path), embedder=RealEmbed(), runner=Runner(), tenant_factory=factory(self.run),
                        registered_pool=POOL, manifest_path=self.base / "absent.json")
        self.assertEqual(rc, 0)
        self.assertEqual(opened, [True])                                    # one read of the dataset, through the sealed parser
        texts = {"stdout": out.getvalue(), "registry": (self.base / "stamps.jsonl").read_text()}
        files = [p for p in self.run.rglob("*") if p.is_file()]
        self.assertTrue({"PREBUILD.json", "STATE.json", "calls.jsonl", "journal.jsonl", "tree.json"} <= {p.name for p in files})
        texts.update({str(p): p.read_bytes().decode("utf-8", "replace") for p in files})
        bad = sealed_strings(rel.QS)
        for name, text in texts.items():
            hit = sorted(x for x in bad if x in text)
            self.assertEqual(hit, [], name)

    def test_a_prebuild_stopped_in_its_build_goes_on_at_another_concurrency_and_the_stamped_run_gives_the_same_ledger(self):
        base0, s30, man0, run0 = setup_dirs()
        with seeded_node_ids():
            self.assertEqual(rs.run(args(base0, s30, run0), embedder=RealEmbed(), runner=Runner(), loader=lambda _a: rel.QS,
                                    tenant_factory=factory(run0), registered_pool=POOL, registered_fresh=rel.REG, manifest_path=man0), 0)
        n_build0 = sum(c["task"].startswith("build") for c in self.calls(run0))
        with seeded_node_ids():
            self.assertEqual(self.prebuild(runner=Runner(stop_at=n_build0 // 2, stop_file=self.run / "STOP")), 3)
            self.assertEqual(json.loads((self.run / "STATE.json").read_text())["state"], "STOPPED")
            self.assertTrue((self.run / rs.PREBUILD_FILE).exists())
            self.assertFalse((self.run / "BUILT.json").exists())
            (self.run / "STOP").unlink()
            self.assertEqual(self.why(self.pa(concurrency=3, concurrency_reason="test"), man=self.base / "absent.json"), [])
            self.assertEqual(self.prebuild(concurrency=3, concurrency_reason="test"), 0)        # the journal replays the first part
            self.assertEqual(json.loads((self.run / "STATE.json").read_text())["state"], "PREBUILT")
            (self.run / ds.RELEASE_FILE).write_text(self.release)
            self.assertEqual(self.stamped(), 0)
        self.assertEqual(sum(c["task"].startswith("build") for c in self.calls()), n_build0)       # no build call charged twice
        self.assertEqual((self.run / "done.jsonl").read_bytes(), (run0 / "done.jsonl").read_bytes())
        self.assertEqual(len(self.calls()), len(self.calls(run0)))
        self.assertEqual([json.loads(l)["mode"] for l in (self.run / "starts.jsonl").read_text().splitlines()],
                         ["prebuild", "prebuild", "stamped"])
        self.assertEqual([r["stage"] for r in rs._registry_rows(self.base / "stamps.jsonl")], [rs.PREBUILD_STAGE, rs.STAGE])

    def test_the_stamped_run_refuses_another_code_fields_model_or_pool(self):
        self.assertEqual(self.prebuild(), 0)
        (self.run / ds.RELEASE_FILE).write_text(self.release)
        p = self.run / rs.PREBUILD_FILE
        good = p.read_text()
        for k, v in (("code_sha256", "0" * 64), ("commit", "deadbee"), ("a0_fields", "text"), ("model", "other"), ("threads", 6)):
            p.write_text(json.dumps(dict(json.loads(good), **{k: v})))
            self.assertTrue(any("early build's %s" % k in w for w in self.why(self.sa())), k)
        p.write_text(json.dumps(dict(json.loads(good), pool_sha256="0" * 64)))
        n = len(self.calls())
        self.assertEqual(self.stamped(), 2)
        self.assertIn("differs from the one the early build ingested", json.loads((self.run / "STATE.json").read_text())["error"])
        self.assertEqual(len(self.calls()), n)

    def test_a_prebuilt_tenant_is_never_reset(self):
        self.assertEqual(self.prebuild(), 0)
        (self.run / ds.RELEASE_FILE).write_text(self.release)
        (self.run / "tenant_state.json").unlink()
        t = _TENANTS[str(self.run)]
        self.assertEqual(self.stamped(), 2)
        self.assertIn("refusing to reset", json.loads((self.run / "STATE.json").read_text())["error"])
        self.assertIs(_TENANTS[str(self.run)], t)                                              # no fresh tenant was made
        self.assertEqual(self.prebuild(), 2)
        self.assertIs(_TENANTS[str(self.run)], t)

    def test_one_run_directory_across_the_early_build_and_the_stamped_run(self):
        self.assertEqual(self.prebuild(), 0)
        other = self.base / "run2"
        other.mkdir()
        self.assertTrue(any("another run directory" in w for w in self.why(self.pa(other))))
        (other / ds.RELEASE_FILE).write_text(self.release)
        self.assertTrue(any("another run directory" in w for w in self.why(self.sa(other))))
        # an early build after a stamped run, or over charged calls without PREBUILD.json, is refused
        base, s3, man, run = setup_dirs()
        a = args(base, s3, run)
        self.assertEqual(rs.run(a, embedder=RealEmbed(), runner=Runner(), loader=lambda _a: rel.QS, tenant_factory=factory(run),
                                registered_pool=POOL, registered_fresh=rel.REG, manifest_path=man), 0)
        w = rs.check_prereqs(args(base, s3, run, prebuild=True, amendment_ref=AMEND), embed=RealEmbed(), manifest_path=man,
                             mem_available=lambda: 64000.0)
        self.assertTrue(any("stamped S6-H1 run exists" in x for x in w), w)
        self.assertTrue(any("charged calls without" in x for x in w), w)

    def test_the_early_build_needs_its_amendment_and_continues_only_unchanged(self):
        self.assertTrue(any("amendment" in w for w in self.why(self.pa(amendment_ref=""))))
        self.assertEqual(self.prebuild(), 0)
        self.assertTrue(any("same approval_ref" in w for w in self.why(self.pa(approval_ref="another"))))
        self.assertEqual(self.why(self.pa(), man=self.base / "absent.json"), [])


if __name__ == "__main__":
    unittest.main()
