"""BUILD-PLAN Step 20, data path: the field whitelist, the id guard, the registered split and the release refusal.

The real-file check (registered counts 363 / 102 / 19 / 16 and the 51-question exclusion) runs when LC_EVAL_DATASET names
the dataset file; without it only the synthetic checks run."""
import os
import unittest

from evalkit import dataset as ds

MARK = "ZZMARKER"


def rec(qid, qtype="knowledge-update", gold=(), sessions=None, extra=True):
    sessions = sessions or {f"{qid}_h{i}": [{"role": "user", "content": f"hello {qid} {i}", "has_answer": False},
                                            {"role": "assistant", "content": f"reply {i}"}] for i in range(3)}
    r = {"question_id": qid, "question_type": qtype, "question": f"what about {qid}?", "answer": 7,
         "answer_session_ids": list(gold) or [next(iter(sessions))], "haystack_session_ids": list(sessions),
         "haystack_sessions": list(sessions.values())}
    if extra:
        r.update({"question_date": MARK, "haystack_dates": [MARK] * len(sessions)})
    return r


def synthetic(n=60):
    out = []
    for i in range(n):
        qt = ("knowledge-update", "multi-session", "temporal-reasoning")[i % 3]
        sess = {f"q{i}_s{j}": [{"role": "user", "content": f"topic {i} {j} words"}, {"role": "assistant", "content": "ok"}] for j in range(3)}
        out.append(ds.parse_record(rec(f"q{i:03d}" + ("_abs" if i % 17 == 16 else ""), qt, [f"q{i}_s0"], sess)))
    return out


class Whitelist(unittest.TestCase):
    def test_only_role_and_content_survive_and_no_marker_field_is_reachable(self):
        q = ds.parse_record(rec("q1", gold=["q1_h0"]))
        blob = repr(q)
        self.assertNotIn(MARK, blob)
        self.assertNotIn("has_answer", blob)
        for _, turns in q.sessions:
            for t in turns:
                self.assertEqual(len(t), 2)

    def test_broken_adapter_copying_has_answer_would_be_caught_by_the_scan(self):
        # the control: a turn carrying the flag renders it, and the assertion above would fire on it
        self.assertIn("has_answer", repr({"role": "user", "content": "x", "has_answer": True}))

    def test_repeated_session_id_keeps_the_first_copy(self):
        r = rec("q2")
        r["haystack_session_ids"] = ["a", "a"]
        r["haystack_sessions"] = [[{"role": "user", "content": "first"}], [{"role": "user", "content": "second"}]]
        r["answer_session_ids"] = ["a"]
        self.assertEqual(ds.parse_record(r).sessions, (("a", (("user", "first"),)),))

    def test_id_guard_refuses_a_gold_id_and_opaque_ids_do_not_contain_one(self):
        with self.assertRaises(ds.DatasetError):
            ds.assert_model_safe("see " + "answer" + "_4be1b6b4_2 here")
        ds.assert_model_safe("answer_session_ids is a field name")
        ref = ds.opaque_ref("ab" * 32)
        self.assertRegex(ref, r"^s[0-9a-f]{12}$")
        ds.assert_model_safe(ref)
        self.assertEqual(ref, ds.opaque_ref("ab" * 32))
        self.assertNotEqual(ref, ds.opaque_ref("ab" * 32, salt="other"))


class Split(unittest.TestCase):
    def test_split_is_deterministic_independent_of_order_and_never_all_pilot(self):
        qs = synthetic()
        a = ds.full_split_assignment(qs)
        b = ds.full_split_assignment(list(reversed(qs)))
        self.assertEqual(a, b)
        self.assertTrue({"pilot", "test"} <= set(a.values()))
        self.assertLess(sum(v == "pilot" for v in a.values()), len(qs) // 2)

    def test_dev2_takes_sorted_knowledge_update_non_abstention_singletons_from_test(self):
        qs = synthetic(90)
        sp = ds.split_assignment(qs)
        dev2 = sorted(q for q, v in sp.items() if v == "dev2")
        self.assertEqual(len(dev2), ds.DEV2_SIZE)
        by = {q.qid: q for q in qs}
        self.assertTrue(all(by[q].qtype == "knowledge-update" and not by[q].is_abstention for q in dev2))

    def test_shared_gold_content_moves_a_test_question_to_held_back(self):
        qs = synthetic(60)
        sp0 = ds.split_assignment(qs)
        pilot = next(q for q in qs if sp0[q.qid] == "pilot")
        test = next(q for q in qs if sp0[q.qid] == "test" and q.qtype == "multi-session")
        # the test question's gold session has the same content as the pilot question's gold session
        gold_sid = test.gold_session_ids[0]
        twin = tuple((gold_sid, pilot.sessions[0][1]) if sid == gold_sid else (sid, t) for sid, t in test.sessions)
        qs2 = [ds.Question(q.qid, q.qtype, q.question, q.answer, q.gold_session_ids, twin) if q.qid == test.qid else q for q in qs]
        # the pilot question's own gold session id is session 0; make the shared content the pilot's gold
        self.assertEqual(ds.full_split_assignment(qs2)[test.qid], "held_back")

    def test_the_fresh_set_is_refused(self):
        qs = synthetic()
        with self.assertRaises(ds.Unreleased):
            ds.development_questions(qs, ("test",))
        with self.assertRaises(ds.Unreleased):
            ds.confirmatory_questions()
        got = ds.development_questions(qs, ("pilot",))
        self.assertTrue(got)
        sp = ds.full_split_assignment(qs)
        self.assertTrue(all(sp[q.qid] == "pilot" for q in got))

    def test_a_wrong_dataset_hash_is_refused(self):
        import tempfile
        with tempfile.NamedTemporaryFile("w", suffix=".json") as f:
            f.write("[]")
            f.flush()
            with self.assertRaises(ds.DatasetError):
                ds.load_questions(f.name)
            self.assertEqual(ds.load_questions(f.name, expect_sha256=None), [])


@unittest.skipUnless(os.environ.get("LC_EVAL_DATASET"), "LC_EVAL_DATASET not set")
class RegisteredCounts(unittest.TestCase):
    def test_the_registered_split_is_reproduced_on_the_real_file(self):
        qs = ds.load_questions(os.environ["LC_EVAL_DATASET"])
        rep = ds.split_report(qs)
        self.assertEqual(rep["n_questions"], 500)
        self.assertEqual(rep["counts"], ds.REGISTERED_COUNTS)
        self.assertEqual(rep["sensitivity_excluded"], ds.REGISTERED_SENSITIVITY_EXCLUDED)
        self.assertEqual(rep["distinct_contents"], 18464)


if __name__ == "__main__":
    unittest.main()
