"""The fresh-set release gate of S6-H1 (EXPERIMENT 3.2, X1), on a SYNTHETIC dataset of the registered record shape (never the real file):
the fresh set is returned only for stage S6-H1 and only with a release file that names the same release and the same freeze manifest;
every other stage still gets `Unreleased`; the sensitivity exclusion, the DEV-EXPOSED stratum and the abstention stratum are applied as
registered; the counts must equal the registered ones."""
import json
import tempfile
import unittest
from pathlib import Path

from evalkit import dataset as ds

REF, FREEZE = "release-ref-1", "f" * 64


def _rec(qid, qtype, sessions, gold):
    return {"question_id": qid, "question_type": qtype, "question": "synthetic question %s" % qid, "answer": "a",
            "answer_session_ids": list(gold), "haystack_session_ids": list(sessions), "haystack_sessions": list(sessions.values()),
            "question_date": "x", "haystack_dates": ["x"] * len(sessions)}


def build():
    """60 synthetic questions; then three plants whose effect is known by construction:
    EXCL, a test question whose gold content is also inside a pilot haystack (sensitivity-excluded);
    EXPO, a test question whose gold content is inside a held_back haystack but no pilot or dev2 one (DEV-EXPOSED);
    a held_back question made by sharing gold content with a pilot question (the registered rule)."""
    recs = []
    for i in range(60):
        qt = ("knowledge-update", "multi-session", "temporal-reasoning")[i % 3]
        sess = {"q%d_s%d" % (i, j): [{"role": "user", "content": "topic %d %d words" % (i, j)}, {"role": "assistant", "content": "ok"}]
                for j in range(3)}
        recs.append(_rec("q%03d" % i + ("_abs" if i % 17 == 16 else ""), qt, sess, ["q%d_s0" % i]))
    qs = [ds.parse_record(r) for r in recs]
    sp = ds.full_split_assignment(qs)
    by = {q.qid: q for q in qs}
    pilot = [q for q in qs if sp[q.qid] == "pilot"]
    test = [q for q in qs if sp[q.qid] == "test" and not q.is_abstention]
    hb_src, excl, expo = test[0], test[1], test[2]
    # held_back: hb_src's gold content becomes pilot[0]'s gold content
    def with_session(q, sid, turns):
        return ds.Question(q.qid, q.qtype, q.question, q.answer, q.gold_session_ids,
                           tuple((s, turns if s == sid else t) for s, t in q.sessions))
    by[hb_src.qid] = with_session(hb_src, hb_src.gold_session_ids[0], pilot[0].sessions[0][1])
    # EXCL: gold content equal to a NON-gold haystack session of a pilot question
    by[excl.qid] = with_session(excl, excl.gold_session_ids[0], pilot[1].sessions[2][1])
    # EXPO: gold content equal to a non-gold haystack session of the held_back question
    by[expo.qid] = with_session(expo, expo.gold_session_ids[0], hb_src.sessions[2][1])
    out = [by[q.qid] for q in qs]
    return out, hb_src.qid, excl.qid, expo.qid


QS, HB, EXCL, EXPO = build()


def expected():
    sp = ds.full_split_assignment(QS)
    test = [q for q in QS if sp[q.qid] == "test"]
    fresh = [q for q in test if q.qid != EXCL]
    ab = [q for q in fresh if q.is_abstention]
    return {"test": len(test), "excluded": 1, "fresh": len(fresh), "non_abstention": len(fresh) - len(ab), "abstention": len(ab),
            "dev_exposed": 1, "primary": len(fresh) - len(ab) - 1}


REG = expected()


def run_dir(stage="S6-H1", ref=REF, freeze=FREEZE, **extra):
    d = Path(tempfile.mkdtemp())
    rec = {"stage": stage, "release_ref": ref, "source": "answer: the owner's release", "freeze_manifest_sha256": freeze}
    rec.update(extra)
    (d / ds.RELEASE_FILE).write_text(json.dumps(rec))
    return d


class Plants(unittest.TestCase):
    def test_the_plants_land_where_the_registered_rules_put_them(self):
        sp = ds.full_split_assignment(QS)
        self.assertEqual(sp[HB], "held_back")
        self.assertEqual(sp[EXCL], "test")
        self.assertEqual(sp[EXPO], "test")
        self.assertIn(EXCL, ds.sensitivity_excluded(QS, sp, ds.session_hashes(QS)))


class Gate(unittest.TestCase):
    def fresh(self, d, stage="S6-H1", ref=REF, freeze=FREEZE, **kw):
        return ds.fresh_questions(QS, stage=stage, run_dir=d, release_ref=ref, freeze_sha256=freeze, registered=REG, **kw)

    def test_s6h1_with_a_matching_release_file_gets_the_fresh_set(self):
        fs = self.fresh(run_dir())
        self.assertEqual(fs.counts(), {k: REG[k] for k in fs.counts()})
        sp = ds.full_split_assignment(QS)
        self.assertTrue(all(sp[q.qid] == "test" for q in fs.questions))

    def test_every_other_stage_is_refused_even_with_a_valid_release_file(self):
        d = run_dir()
        for stage in ("S2", "S3", "S6-H2", "S7a", "S7b", "DEV-STUB", "s6-h1", ""):
            with self.assertRaises(ds.Unreleased, msg=stage):
                self.fresh(d, stage=stage)

    def test_a_release_file_for_another_stage_is_refused(self):
        with self.assertRaises(ds.Unreleased):
            self.fresh(run_dir(stage="S6-H2"))

    def test_no_release_file_is_refused(self):
        with self.assertRaises(ds.Unreleased):
            self.fresh(Path(tempfile.mkdtemp()))

    def test_a_different_or_empty_release_reference_is_refused(self):
        with self.assertRaises(ds.Unreleased):
            self.fresh(run_dir(), ref="another-ref")
        with self.assertRaises(ds.Unreleased):
            self.fresh(run_dir(ref=""), ref="")

    def test_a_release_naming_another_freeze_manifest_is_refused(self):
        with self.assertRaises(ds.Unreleased):
            self.fresh(run_dir(), freeze="0" * 64)
        with self.assertRaises(ds.Unreleased):
            self.fresh(run_dir(freeze=""), freeze="")

    def test_a_release_file_missing_its_source_is_refused(self):
        with self.assertRaises(ds.Unreleased):
            self.fresh(run_dir(source=""))

    def test_the_sensitivity_exclusion_is_applied(self):
        fs = self.fresh(run_dir())
        self.assertNotIn(EXCL, {q.qid for q in fs.questions})
        self.assertEqual(fs.excluded_n, 1)

    def test_dev_exposed_questions_leave_the_primary_set_and_stay_in_the_sensitivity_set(self):
        fs = self.fresh(run_dir())
        self.assertEqual(fs.dev_exposed, (EXPO,))
        self.assertNotIn(EXPO, fs.primary)
        self.assertIn(EXPO, fs.sensitivity)

    def test_abstention_questions_are_their_own_stratum_and_never_primary(self):
        fs = self.fresh(run_dir())
        self.assertTrue(fs.abstention)
        self.assertTrue(all(q.endswith("_abs") for q in fs.abstention))
        self.assertFalse(set(fs.abstention) & set(fs.primary))
        self.assertFalse(set(fs.abstention) & set(fs.sensitivity))
        self.assertTrue(set(fs.abstention) <= {q.qid for q in fs.questions})

    def test_an_amended_dev_exposed_question_leaves_the_primary_set(self):
        fs0 = self.fresh(run_dir())
        extra = fs0.primary[0]
        reg = dict(REG, primary=REG["primary"] - 1)
        fs = ds.fresh_questions(QS, stage="S6-H1", run_dir=run_dir(), release_ref=REF, freeze_sha256=FREEZE, registered=reg,
                                extra_dev_exposed=[extra])
        self.assertIn(extra, fs.dev_exposed)
        self.assertNotIn(extra, fs.primary)

    def test_counts_that_differ_from_the_registered_ones_are_refused(self):
        with self.assertRaises(ds.DatasetError):
            ds.fresh_questions(QS, stage="S6-H1", run_dir=run_dir(), release_ref=REF, freeze_sha256=FREEZE,
                               registered=dict(REG, fresh=REG["fresh"] + 1))

    def test_the_real_registered_counts_are_the_ones_of_the_plan(self):
        r = ds.REGISTERED_FRESH
        self.assertEqual((r["test"], r["excluded"], r["fresh"], r["non_abstention"], r["abstention"], r["dev_exposed"], r["primary"]),
                         (363, 51, 312, 299, 13, 6, 293))
        self.assertEqual(r["fresh"], r["test"] - r["excluded"])
        self.assertEqual(r["primary"], r["non_abstention"] - r["dev_exposed"])

    def test_development_loading_still_refuses_the_test_split(self):
        with self.assertRaises(ds.Unreleased):
            ds.development_questions(QS, ("test",))
        with self.assertRaises(ds.Unreleased):
            ds.confirmatory_questions()


if __name__ == "__main__":
    unittest.main()
