"""The S6-H1 runner: every start guard (approval and cap, release, freeze, S3 complete, one stamped run across run directories, real
embedder, stop file, call estimate) and the body end to end on a SYNTHETIC dataset with an in-memory tenant and a scripted fake model
runner (no Elasticsearch, no model, no real dataset): the whole pool is ingested, memory and budget refusals charge nothing, a stopped
run resumes to the same result without charging a call twice, and the results carry the registered schema."""
import argparse
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from evalkit import corpus, dataset as ds, run_s6h1 as rs, s6h1_freeze as fz, s2_stage
from evalkit.score import REQUIRED_FIELDS
from living_context.embed import e5, stub
from living_context.recall.view import MemView
from living_context.structure import build
from tests.security.test_run_s2_es import Runner
from tests.unit import test_s6h1_release as rel
from tests.unit.test_s6h1_budget import logs
from tests.unit.test_s6h1_freeze import s3_dir

MODEL, COMMIT, REF, APPROVAL = "claude-sonnet-5-5", "c0ffee1", "release-ref-1", "approval-ref-1"


class RealEmbed:
    is_real, model_id = True, e5.MODEL_ID
    def passage(self, t): return stub.embed(t)
    query = passage


def setup_dirs():
    base = Path(tempfile.mkdtemp())
    s3 = s3_dir()
    man = base / "freeze" / "S6-H1.json"
    fz.write_manifest(fz.make_manifest(s3_dir=s3, code_commit=COMMIT, model=MODEL), man)
    run = base / "run"
    run.mkdir()
    (run / ds.RELEASE_FILE).write_text(json.dumps({"stage": "S6-H1", "release_ref": REF, "source": "answer: owner",
                                                    "freeze_manifest_sha256": fz.manifest_sha256(man)}))
    proof(base / "mem.json")
    return base, s3, man, run


def proof(path, leaves=10 ** 6, peak=9000.0, code=None, dim=384, cluster_workers=None):
    rep = {"tool": "evalkit.s6h1_mem", "leaves": leaves, "peak_rss_mb": peak, "dim": dim, "code_sha256": code or fz.code_digest()}
    if cluster_workers is not None:
        rep["cluster_workers"] = cluster_workers
    Path(path).write_text(json.dumps(rep))
    return path


def args(base, s3, run, **kw):
    a = dict(stage="S6-H1", dataset="", es_url="", es_ca="", es_pw_file="", run_dir=str(run), s3_run_dir=str(s3),
             stamp_registry=str(base / "stamps.jsonl"), embed_dir="", binary="fake", model=MODEL, rates_from=[str(logs())],
             model_kind="claude", approved_cap=35574, approval_ref=APPROVAL, release_ref=REF, commit=COMMIT, a0_fields="strong",
             resume=False, threads=1, max_retries=2, base_delay=0.0, mem_proof=str(base / "mem.json"), concurrency=1,
             concurrency_reason="", cluster_workers=1, ingest_writers=1, embed_workers=1)
    a.update(kw)
    return argparse.Namespace(**a)


class Guards(unittest.TestCase):
    def setUp(self):
        self.base, self.s3, self.man, self.run = setup_dirs()

    def why(self, free=64000.0, **kw):
        return rs.check_prereqs(args(self.base, self.s3, self.run, **kw), embed=RealEmbed(), manifest_path=self.man,
                                mem_available=lambda: free)

    def has(self, text, **kw):
        w = self.why(**kw)
        self.assertTrue(any(text in x for x in w), w)

    def test_everything_in_place_passes(self):
        self.assertEqual(self.why(), [])

    def test_a_cap_above_the_approved_one_or_none_is_refused(self):
        self.has("at most 35574", approved_cap=35575)
        self.has("at most 35574", approved_cap=0)

    def test_missing_references_are_refused(self):
        self.has("approval", approval_ref="")
        self.has("release", release_ref="")
        self.has("commit", commit="")
        self.has("stamp registry", stamp_registry="")

    def test_no_release_file_or_another_reference_is_refused(self):
        (self.run / ds.RELEASE_FILE).unlink()
        self.has("no release file")
        (self.run / ds.RELEASE_FILE).write_text(json.dumps({"stage": "S6-H1", "release_ref": "other", "source": "x",
                                                             "freeze_manifest_sha256": fz.manifest_sha256(self.man)}))
        self.has("release reference")

    def test_a_release_written_before_the_freeze_is_refused(self):
        (self.run / ds.RELEASE_FILE).write_text(json.dumps({"stage": "S6-H1", "release_ref": REF, "source": "x",
                                                             "freeze_manifest_sha256": "0" * 64}))
        self.has("the release follows the freeze")

    def test_no_freeze_or_another_commit_or_s3_incomplete_is_refused(self):
        self.has("not the commit under test", commit="deadbee")
        (self.s3 / "STATE.json").write_text(json.dumps({"state": "STOPPED"}))
        self.has("not COMPLETE")
        self.assertTrue(any("no freeze manifest" in x for x in rs.check_prereqs(args(self.base, self.s3, self.run), embed=RealEmbed(),
                                                                                  manifest_path=self.base / "absent.json")))

    def test_the_stub_embedder_or_the_stub_model_is_refused(self):
        w = rs.check_prereqs(args(self.base, self.s3, self.run), embed=None, manifest_path=self.man)
        self.assertTrue(any("embedder" in x for x in w))
        self.has("real model port", model_kind="stub")

    def test_a_second_stamped_run_in_another_run_directory_is_refused(self):
        other = self.base / "other"
        other.mkdir()
        with open(self.base / "stamps.jsonl", "a") as f:
            f.write(json.dumps({"stage": "S6-H1", "run_dir": os.path.realpath(other)}) + "\n")
        self.has("another run directory")

    def test_a_stamp_in_the_run_directory_needs_resume_of_the_same_run(self):
        stamp = {"stage": "S6-H1", "commit": COMMIT, "cap": 35574, "approval_ref": APPROVAL, "release_ref": REF,
                 "freeze_sha256": fz.manifest_sha256(self.man), "a0_fields": "strong", "model": MODEL}
        (self.run / "STAMP.json").write_text(json.dumps(stamp))
        with open(self.base / "stamps.jsonl", "a") as f:
            f.write(json.dumps({"stage": "S6-H1", "run_dir": os.path.realpath(self.run)}) + "\n")
        self.has("already exists in the run directory")
        self.assertEqual(self.why(resume=True), [])
        self.has("cap differs", resume=True, approved_cap=30000)

    def test_resume_without_a_stamp_is_refused(self):
        self.has("--resume needs the stamp", resume=True)

    def test_a_stop_file_is_refused(self):
        (self.run / "STOP").write_text("")
        self.has("stop file")

    def test_a_missing_stale_or_too_small_memory_proof_is_refused(self):
        self.has("no memory proof", mem_proof=str(self.base / "absent.json"))
        proof(self.base / "m2.json", code="0" * 64)
        self.has("other code", mem_proof=str(self.base / "m2.json"))
        proof(self.base / "m3.json", leaves=rs.EXPECTED_LEAVES - 1)
        self.has("fewer than the expected", mem_proof=str(self.base / "m3.json"))
        proof(self.base / "m4.json", dim=768)
        self.has("384 dimensions", mem_proof=str(self.base / "m4.json"))

    def test_a_host_without_the_proven_peak_free_is_refused(self):
        self.has("less than the proven build peak", free=9000.0)
        self.assertEqual(self.why(free=9000.0 + rs.CLI_CHILD_MB + rs.MEM_MARGIN_MB), [])
        # each concurrent CLI process needs its own memory on top of the proven build peak
        self.has("4 CLI processes", free=9000.0 + rs.CLI_CHILD_MB + rs.MEM_MARGIN_MB, concurrency=4, concurrency_reason="probe")
        self.assertEqual(self.why(free=9000.0 + 4 * rs.CLI_CHILD_MB + rs.MEM_MARGIN_MB, concurrency=4, concurrency_reason="probe"), [])

    def test_more_clusterer_workers_than_the_memory_proof_used_are_refused(self):
        self.has("clusterer with 1 workers, fewer than --cluster-workers 4", cluster_workers=4)    # a proof without the field: one
        proof(self.base / "m5.json", cluster_workers=16)
        self.assertEqual(self.why(mem_proof=str(self.base / "m5.json"), cluster_workers=16), [])
        self.assertEqual(self.why(mem_proof=str(self.base / "m5.json"), cluster_workers=8), [])
        self.has("with 16 workers, fewer than --cluster-workers 17", mem_proof=str(self.base / "m5.json"), cluster_workers=17)

    def test_embedding_processes_need_their_own_memory(self):
        base_free = 9000.0 + rs.CLI_CHILD_MB + rs.MEM_MARGIN_MB
        self.assertEqual(self.why(free=base_free, embed_workers=1), [])
        self.has("6 embedding processes", free=base_free, embed_workers=6)
        self.assertEqual(self.why(free=9000.0 + 6 * rs.E5_WORKER_MB + rs.MEM_MARGIN_MB, embed_workers=6), [])

    def test_concurrency_above_the_registered_value_needs_a_recorded_reason(self):
        self.has("above the registered 2", free=64000.0, concurrency=4)
        self.assertEqual(self.why(concurrency=2), [])
        self.assertEqual(self.why(concurrency=4, concurrency_reason="probe of 2026-11-21, no 429 at 8"), [])
        self.has("at least 1", concurrency=0)

    def test_an_estimate_over_the_cap_is_refused(self):
        self.has("exceeds the approved cap", approved_cap=20000)
        self.has("measured rates", rates_from=[str(self.base / "absent")])


# ---- the body ---------------------------------------------------------------------------------------------------------------------
_TENANTS = {}


class MemTenant:
    """In-memory stand-in for EvalTenant: the same calls, the real builder and fan-out, MemDirect for the flat list."""
    def __init__(self, root):
        self.root, self.tree, self.mc, self.ingested = root, None, None, 0

    def ingest(self, pool):
        self.mc = corpus.MemCorpus(pool)
        self.ingested = len(pool)
        return len(self.mc.leaves)

    def ingest_concurrent(self, pool, *, writers, embed_many):
        """The concurrent path's contract as the runner uses it: every leaf text is embedded up front, then the same leaves."""
        self.writers = writers
        texts = [t for _h, turns in pool for t in corpus.session_leaf_texts(turns)]
        self.vectors = embed_many(texts)
        if set(self.vectors) != set(texts):
            raise RuntimeError("missing vectors")
        return self.ingest(pool)

    def save_state(self, p):
        Path(p).write_text(json.dumps({"leaves": len(self.mc.leaves)}))

    def load_state(self, p):
        return json.loads(Path(p).read_text())["leaves"]

    def leaf_count_in_es(self):
        return len(self.mc.leaves)

    def load_leaves(self, n):
        return n

    def build_and_publish(self, port, **kw):
        self.tree = build.build_tree(self.mc.leaves, port, rs.TENANT, "base", **kw)
        return self.tree

    def view(self):
        return MemView(self.tree)

    @property
    def world(self):
        return self.mc.world

    def direct(self):
        return corpus.MemDirect(self.mc.leaves)

    def close(self):
        pass


def factory(run):
    def make(attach):
        if attach:
            return _TENANTS[str(run)]
        t = _TENANTS[str(run)] = MemTenant(run)
        return t
    return make


POOL = len({ds.turns_hash(t) for q in rel.QS for _, t in q.sessions})     # counted independently of whole_pool


@mock.patch.object(rs, "EXPECTED_LEAVES", 1)          # the synthetic pool is tiny; proofs then cover it
class Body(unittest.TestCase):
    def test_the_pool_count_is_the_distinct_contents(self):
        self.assertEqual(len(rs.whole_pool(rel.QS)), POOL)
        self.assertLess(POOL, sum(len(q.sessions) for q in rel.QS))         # the synthetic set repeats contents, so dedup matters

    def go(self, base, s3, run, man, runner=None, proven=None, **kw):
        if proven is not None:
            proof(base / "mem.json", leaves=proven)
        a = args(base, s3, run, **kw)
        return rs.run(a, embedder=RealEmbed(), runner=runner or Runner(), loader=lambda _a: rel.QS, tenant_factory=factory(run),
                      registered_pool=POOL, registered_fresh=rel.REG, manifest_path=man)

    def test_a_complete_run_decides_h1_over_the_whole_pool(self):
        base, s3, man, run = setup_dirs()
        self.assertEqual(self.go(base, s3, run, man), 0)
        state = json.loads((run / "STATE.json").read_text())
        self.assertEqual(state["state"], "COMPLETE")
        self.assertEqual(state["questions_done"], rel.REG["fresh"])
        self.assertEqual(_TENANTS[str(run)].ingested, POOL)                     # the whole pool, not the fresh haystacks only
        self.assertGreater(POOL, len({ds.turns_hash(t) for q in ds.fresh_questions(rel.QS, stage="S6-H1", run_dir=run, release_ref=REF,
                                       freeze_sha256=fz.manifest_sha256(man), registered=rel.REG).questions for _, t in q.sessions}))
        dec = json.loads((run / "results" / "h1_decision.json").read_text())
        self.assertIn(dec["word"], ("HOLDS", "HOLDS, BELOW MINIMUM", "NOT ESTABLISHED", "DID NOT HOLD", "HARM"))
        self.assertEqual(dec["n_primary"], rel.REG["primary"])
        for arm in ("A0", "A0_plus_LEN", "A1", "A1-tree-only", "SHUFFLED"):
            doc = json.loads((run / "results" / ("arm_%s.json" % arm)).read_text())
            self.assertTrue(all(doc.get(f) not in (None, "") for f in REQUIRED_FIELDS), arm)
        calls = (run / "calls.jsonl").read_text().splitlines()
        self.assertEqual(int((run / "counter").read_text().split()[0]) if (run / "counter").exists() else len(calls), len(calls))
        body = (run / "done.jsonl").read_text() + (run / "results" / "h1_decision.json").read_text()
        for q in rel.QS:
            self.assertNotIn(q.question, body)
        ds.assert_model_safe(body)
        self.assertEqual(len(rs._registry_rows(base / "stamps.jsonl")), 1)

    def test_a_concurrent_ingest_embeds_up_front_in_worker_processes_and_gives_the_same_result(self):
        from tests.unit.test_ingest_concurrent import make_embed
        from tests.unit.test_parallel import seeded_node_ids
        base, s3, man, run = setup_dirs()
        with seeded_node_ids():                                   # node ids enter recall tie-breaks; seed them for the comparison
            self.assertEqual(self.go(base, s3, run, man), 0)
        base2, s32, man2, run2 = setup_dirs()
        a = args(base2, s32, run2, ingest_writers=3, embed_workers=2)
        with seeded_node_ids():
            rc = rs.run(a, embedder=RealEmbed(), runner=Runner(), loader=lambda _a: rel.QS, tenant_factory=factory(run2),
                        registered_pool=POOL, registered_fresh=rel.REG, manifest_path=man2, embed_factory=make_embed)
        self.assertEqual(rc, 0)
        t = _TENANTS[str(run2)]
        self.assertEqual(t.writers, 3)
        self.assertEqual(t.vectors, {x: make_embed().passage(x) for x in t.vectors})        # embedded in the worker processes
        self.assertEqual((run2 / "done.jsonl").read_bytes(), (run / "done.jsonl").read_bytes())
        row = json.loads((run2 / "starts.jsonl").read_text().splitlines()[-1])
        self.assertEqual((row["ingest_writers"], row["embed_workers"]), (3, 2))

    def test_more_leaves_than_the_proven_size_is_refused_before_any_call(self):
        base, s3, man, run = setup_dirs()
        self.assertEqual(self.go(base, s3, run, man, proven=10), 2)
        self.assertEqual(json.loads((run / "STATE.json").read_text())["state"], "REFUSED")
        self.assertFalse((run / "calls.jsonl").exists())
        self.assertUnspent(base, run)

    def test_an_estimate_over_the_cap_at_the_exact_leaf_count_is_refused_before_any_call(self):
        base, s3, man, run = setup_dirs()
        self.assertEqual(self.go(base, s3, run, man, approved_cap=1000), 2)
        st = json.loads((run / "STATE.json").read_text())
        self.assertEqual(st["state"], "REFUSED")
        self.assertIn("exceeds the approved cap", st["error"])
        self.assertFalse((run / "calls.jsonl").exists())
        self.assertUnspent(base, run)
        self.assertEqual(rs.check_prereqs(args(base, s3, run), embed=RealEmbed(), manifest_path=man, mem_available=lambda: 64000.0), [])

    def test_a_stopped_run_resumes_to_the_same_result_without_charging_twice(self):
        base, s3, man, run = setup_dirs()
        self.assertEqual(self.go(base, s3, run, man), 0)
        ref = json.loads((run / "results" / "h1_decision.json").read_text())
        n_ref = len((run / "calls.jsonl").read_text().splitlines())
        base2, s32, man2, run2 = setup_dirs()
        self.assertEqual(self.go(base2, s32, run2, man2, runner=Runner(stop_at=n_ref // 2, stop_file=run2 / "STOP")), 3)
        (run2 / "STOP").unlink()
        self.assertEqual(rs.check_prereqs(args(base2, s32, run2, resume=True), embed=RealEmbed(), manifest_path=man2,
                                          mem_available=lambda: 64000.0), [])
        self.assertEqual(self.go(base2, s32, run2, man2, resume=True), 0)
        got = json.loads((run2 / "results" / "h1_decision.json").read_text())
        for k in ("word", "a_A1_minus_A0", "b_A1_minus_A0_plus_LEN", "c_tree_only_share", "shuffled_control"):
            self.assertEqual(got[k], ref[k], k)
        self.assertEqual(len((run2 / "calls.jsonl").read_text().splitlines()), n_ref)

    def test_a_pool_that_differs_from_the_registered_count_is_refused(self):
        base, s3, man, run = setup_dirs()
        a = args(base, s3, run)
        rc = rs.run(a, embedder=RealEmbed(), runner=Runner(), loader=lambda _a: rel.QS, tenant_factory=factory(run),
                    registered_pool=POOL + 1, registered_fresh=rel.REG, manifest_path=man)
        self.assertEqual(rc, 2)
        self.assertFalse((run / "calls.jsonl").exists())
        self.assertUnspent(base, run)

    def test_fresh_counts_that_differ_are_refused_without_spending_the_stamp(self):
        base, s3, man, run = setup_dirs()
        rc = rs.run(args(base, s3, run), embedder=RealEmbed(), runner=Runner(), loader=lambda _a: rel.QS, tenant_factory=factory(run),
                    registered_pool=POOL, registered_fresh=dict(rel.REG, primary=rel.REG["primary"] + 1), manifest_path=man)
        self.assertEqual(rc, 2)
        self.assertIn("DatasetError", json.loads((run / "STATE.json").read_text())["error"])
        self.assertUnspent(base, run)

    def assertUnspent(self, base, run):
        self.assertFalse((run / "STAMP.json").exists())
        self.assertEqual(rs._registry_rows(base / "stamps.jsonl"), [])


if __name__ == "__main__":
    unittest.main()


class Reset(unittest.TestCase):
    """S6-H1 shares the Elasticsearch with S2/S3 tenants: its reset deletes its own indices and its own registry row only."""
    class Admin:
        def __init__(self):
            self.calls = []

        def request(self, method, path, body=None):
            self.calls.append((method, path))
            if method == "GET":
                return 200, [{"index": "lc-s6h1-base-leaf"}, {"index": "lc-s3h1-base-leaf"}, {"index": "lc-sys-registry"}]
            return 200, {}

    def test_registry_doc_only_keeps_other_tenants(self):
        from evalkit.eval_tenant import reset_tenant
        a = self.Admin()
        reset_tenant(a, "s6h1", registry_doc_only=True)
        deleted = [p for m, p in a.calls if m == "DELETE"]
        self.assertIn("/lc-s6h1-base-leaf", deleted)
        self.assertNotIn("/lc-s3h1-base-leaf", deleted)
        self.assertNotIn("/lc-sys-registry", deleted)
        self.assertTrue(any(p.startswith("/lc-sys-registry/_doc/s6h1") for p in deleted))
        b = self.Admin()
        reset_tenant(b, "s2test")                                             # the S2/S3 default is unchanged
        self.assertIn("/lc-sys-registry", [p for m, p in b.calls if m == "DELETE"])

    def test_the_runner_asks_for_the_narrow_reset(self):
        import inspect
        self.assertIn("registry_doc_only=True", inspect.getsource(rs._default_tenant))
