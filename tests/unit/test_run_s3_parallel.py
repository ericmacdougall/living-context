"""S2/S3 under several workers (the registered model-port concurrency): the ledger and the results are byte-identical to one worker, and a
built S3 run with no question done may resume under a new commit only with a recorded amendment. In-memory tenant, fake CLI runner,
synthetic fixture; no Elasticsearch, no model, no dataset."""
import argparse
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from evalkit import dataset as ds, fixture, run_h1, run_s2
from tests.unit import test_run_s6h1 as t6
from tests.unit.test_parallel import SafeRunner, seeded_node_ids

FX = fixture.make("advantage", 10, mates=8)
HASHES = {sid: ds.turns_hash(t) for q in FX.questions for sid, t in q.sessions}


def loader(_a):
    return FX.questions, HASHES, FX.clusters


def args(run_dir, **kw):
    base = dict(stage="S3", dataset="", es_url="", es_ca="", es_pw_file="", tenant="s3test", run_dir=str(run_dir), n_questions=10,
                splits=["pilot"], model_kind="claude", binary="fake", model="m", approved_cap=100000, approval_ref="test", commit="c1",
                beta=None, resume=False, embed_dir="", threads=1, a0_fields="strong", max_retries=2, base_delay=0.0, shuffled_n=4,
                concurrency=1, concurrency_reason="", amendment_ref="")
    base.update(kw)
    return argparse.Namespace(**base)


class FirstRecallStop(SafeRunner):
    """Creates the stop file when the first non-build call arrives (the operator's watcher at the end of the S3 build)."""
    def __call__(self, prompt):
        if "TASK build_" not in prompt and self.stop_file is not None:
            Path(self.stop_file).write_text("")
        return super().__call__(prompt)


@mock.patch.dict(run_h1.REGISTERED_N, {"S3": 10})          # the synthetic S3 is 10 questions with SHUFFLED on 4
@mock.patch.dict(run_h1.SHUFFLED_N, {"S3": 4})
class S3Parallel(unittest.TestCase):
    def go(self, d, runner, **kw):
        ids = seeded_node_ids()
        ids.start()
        try:
            return run_s2.run(args(d, **kw), run_h1.pick_questions, embedder=t6.RealEmbed(), runner=runner, loader=loader,
                              tenant_factory=t6.factory(d))
        finally:
            ids.stop()

    def outputs(self, d):
        res = Path(d) / "results"
        return (Path(d) / "done.jsonl").read_bytes(), {p.name: p.read_bytes() for p in sorted(res.glob("*.json")) if p.name != "summaries.json"}

    def test_one_and_four_workers_give_the_same_ledger_and_results(self):
        d1, d4 = Path(tempfile.mkdtemp()), Path(tempfile.mkdtemp())
        self.assertEqual(self.go(d1, SafeRunner()), 0)
        r4 = SafeRunner()
        self.assertEqual(self.go(d4, r4, concurrency=4, concurrency_reason="test"), 0)
        self.assertGreater(r4.max_inflight, 1)
        self.assertEqual(self.outputs(d4), self.outputs(d1))
        self.assertEqual(len((d4 / "calls.jsonl").read_text().splitlines()), len((d1 / "calls.jsonl").read_text().splitlines()))
        self.assertEqual(json.loads((d4 / "starts.jsonl").read_text().splitlines()[-1])["concurrency"], 4)

    def test_a_serial_run_stopped_after_its_build_resumes_under_an_amendment_at_four_workers(self):
        ref = Path(tempfile.mkdtemp())
        self.assertEqual(self.go(ref, SafeRunner()), 0)
        d = Path(tempfile.mkdtemp())
        self.assertEqual(self.go(d, FirstRecallStop(stop_file=d / "STOP")), 3)          # stopped at the first recall call
        self.assertTrue((d / "BUILT.json").exists())
        self.assertFalse((d / "done.jsonl").exists() and (d / "done.jsonl").read_text().strip())
        recall_before = sum(json.loads(l)["task"] not in ("build_l1", "build_upper") for l in (d / "calls.jsonl").read_text().splitlines())
        self.assertGreaterEqual(recall_before, 1)
        (d / "STOP").unlink()
        new = dict(resume=True, commit="c2", concurrency=4, concurrency_reason="test")
        # the guard: a new commit on a charged run needs the amendment; with it, only between the build and the first question
        why = " ".join(run_h1.check_prereqs(args(d, **new), embed=t6.RealEmbed()))
        self.assertIn("commit differs from the stamp", why)
        self.assertEqual(run_h1.check_prereqs(args(d, amendment_ref="DEV-14.4", **new), embed=t6.RealEmbed()), [])
        self.assertEqual(self.go(d, SafeRunner(), amendment_ref="DEV-14.4", **new), 0)
        self.assertEqual(self.outputs(d)[0], self.outputs(ref)[0])                        # the ledger, byte for byte
        strip = lambda b: {k: v for k, v in json.loads(b).items() if k not in ("commit", "call_counter")}
        res, res_ref = self.outputs(d)[1], self.outputs(ref)[1]
        self.assertEqual(sorted(res), sorted(res_ref))
        for k in res:                       # the results differ only in the commit and the call counter (the re-asked calls)
            self.assertEqual(strip(res[k]), strip(res_ref[k]), k)
        stamp = json.loads((d / "STAMP.json").read_text())
        self.assertEqual((stamp["commit"], stamp["earlier_commits"]), ("c2", ["c1"]))
        self.assertEqual(stamp["amendments"][0]["ref"], "DEV-14.4")
        rows = (d / "calls.jsonl").read_text().splitlines()
        self.assertEqual(int((d / "counter").read_text()), len(rows))                   # every charge is one logged call
        n_ref = len((ref / "calls.jsonl").read_text().splitlines())
        # the recall calls charged before the stop were journaled without a question scope and are asked again under it: exactly
        # those calls are charged twice (disclosed in the amendment); every build call was charged once
        self.assertEqual(len(rows) - n_ref, recall_before)

    def test_the_amendment_does_not_open_an_unbuilt_or_started_run(self):
        d = Path(tempfile.mkdtemp())
        Path(d, "STAMP.json").write_text(json.dumps({"commit": "c1", "cap": 100000, "n_questions": 10, "a0_fields": "strong", "shuffled_n": 4}))
        Path(d, "counter").write_text("5")
        Path(d, "journal.jsonl").write_text("")
        kw = dict(resume=True, commit="c2", amendment_ref="DEV-14.4")
        self.assertIn("after its build is complete", " ".join(run_h1.check_prereqs(args(d, **kw), embed=t6.RealEmbed())))
        Path(d, "BUILT.json").write_text("{}")
        self.assertEqual(run_h1.check_prereqs(args(d, **kw), embed=t6.RealEmbed()), [])
        Path(d, "done.jsonl").write_text('{"qref": "q1"}\n')
        self.assertIn("before its first question", " ".join(run_h1.check_prereqs(args(d, **kw), embed=t6.RealEmbed())))

    def test_an_unfinished_unscoped_build_resumes_at_one_worker_only(self):
        d = Path(tempfile.mkdtemp())
        Path(d, "STAMP.json").write_text(json.dumps({"commit": "c1", "cap": 100000, "n_questions": 10, "a0_fields": "strong", "shuffled_n": 4}))
        Path(d, "journal.jsonl").write_text("")
        kw = dict(resume=True, concurrency=4, concurrency_reason="test")
        self.assertIn("resumes at --concurrency 1", " ".join(run_h1.check_prereqs(args(d, **kw), embed=t6.RealEmbed())))
        self.assertEqual(run_h1.check_prereqs(args(d, resume=True), embed=t6.RealEmbed()), [])


if __name__ == "__main__":
    unittest.main()
