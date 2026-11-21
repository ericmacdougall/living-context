"""LoCoMo adapter (docs/LOCOMO-PLAN.md): evidence parsing, the turn to leaf gold map, the seeded subset and the metrics, offline on a
synthetic fixture in LoCoMo's schema (no dataset text is committed). A real-file check runs when LOCOMO_PATH names locomo10.json."""
import json
import os
import unittest

from evalkit import corpus, locomo as lo


def turn(m, who, text, cap=None):
    t = {"speaker": who, "dia_id": "x", "text": text}
    if cap:
        t["blip_caption"] = cap
    return t


def sessions(spec):
    """spec: {n: [(who, text, caption)]}; dia ids are D<n>:<1-based position>."""
    conv = {"speaker_a": "Ann", "speaker_b": "Bob"}
    for n, items in spec.items():
        rows = []
        for j, (who, text, cap) in enumerate(items, 1):
            t = turn(n, who, text, cap)
            t["dia_id"] = "D%d:%d" % (n, j)
            rows.append(t)
        conv["session_%d" % n] = rows
        conv["session_%d_date_time" % n] = "1:00 pm on %d May, 2023" % n
    return conv


LONG = [("Ann" if i % 2 == 0 else "Bob", "line %d " % i + "word " * 20, None) for i in range(12)]    # long enough to split at a tiny cap


def fixture():
    c0 = {"sample_id": "conv-t0", "conversation": sessions({1: [("Ann", "I adopted a dog called Rex", None),
                                                               ("Bob", "nice, what breed", None),
                                                               ("Ann", "a husky", "a photo of a husky on snow")],
                                                          2: LONG}),
          "qa": [{"question": "q0", "answer": "a", "evidence": ["D1:1"], "category": 1},
                 {"question": "q1", "answer": "a", "evidence": ["D1:3", "D2:12"], "category": 2},
                 {"question": "q2", "answer": "a", "evidence": ["D2:2; D1:1"], "category": 4},          # two ids in one string
                 {"question": "q3", "answer": "a", "evidence": ["D:2:5"], "category": 4},               # stray colon
                 {"question": "q4", "answer": "a", "evidence": ["D2:05"], "category": 4},               # zero padded
                 {"question": "q5", "answer": "a", "evidence": ["D9:1", "D1:2"], "category": 1},        # D9 does not exist: dropped
                 {"question": "q6", "answer": "a", "evidence": ["D"], "category": 3},                   # nothing resolves: excluded
                 {"question": "q7", "answer": "a", "evidence": [], "category": 3},                      # no evidence: excluded
                 {"question": "q8", "answer": "a", "category": 5, "adversarial_answer": "z"}]}          # category 5: not read
    c1 = {"sample_id": "conv-t1", "conversation": sessions({1: [("Cy", "I paint", None), ("Di", "oh really", None)]}),
          "qa": [{"question": "r0", "answer": "a", "evidence": ["D1:1"], "category": 1}]}
    return [c0, c1]


def load_fixture(tmp):
    p = os.path.join(tmp, "f.json")
    with open(p, "w") as fh:
        json.dump(fixture(), fh)
    return lo.load(p, expect_sha256=None)


class T(unittest.TestCase):
    def setUp(self):
        import tempfile
        self._d = tempfile.TemporaryDirectory()
        self.convs = load_fixture(self._d.name)
        self.c0 = self.convs[0]

    def tearDown(self):
        self._d.cleanup()

    # ---- evidence parsing -------------------------------------------------------------------------------------------------
    def test_parse_evidence_shapes(self):
        self.assertEqual(lo.parse_evidence(["D8:6; D9:17"]), ["D8:6", "D9:17"])
        self.assertEqual(lo.parse_evidence(["D:11:26"]), ["D11:26"])
        self.assertEqual(lo.parse_evidence(["D30:05"]), ["D30:5"])
        self.assertEqual(lo.parse_evidence(["D"]), [])
        self.assertEqual(lo.parse_evidence(["D9:1 D4:4 D4:6", "D4:4"]), ["D9:1", "D4:4", "D4:6"])      # repeats removed

    def test_eligibility_and_counts(self):
        qs = {q.qid: q for q in self.c0.questions}
        self.assertEqual(sorted(qs), ["conv-t0#0", "conv-t0#1", "conv-t0#2", "conv-t0#3", "conv-t0#4", "conv-t0#5"])
        self.assertEqual(self.c0.n_excluded, 2)                            # q6 and q7; the category 5 item is not counted at all
        self.assertEqual(qs["conv-t0#2"].gold_turns, ("D2:2", "D1:1"))
        self.assertEqual(qs["conv-t0#3"].gold_turns, ("D2:5",))
        self.assertEqual(qs["conv-t0#4"].gold_turns, ("D2:5",))
        self.assertEqual(qs["conv-t0#5"].gold_turns, ("D1:2",))
        self.assertEqual(qs["conv-t0#5"].n_unresolved, 1)
        self.assertTrue(all(q.category in (1, 2, 3, 4) for q in self.c0.questions))

    def test_turn_content_caption_date_and_whitelist(self):
        s1 = self.c0.sessions[0]
        self.assertTrue(s1.turns[0].content.startswith("[1:00 pm on 1 May, 2023] I adopted"))
        self.assertFalse(s1.turns[1].content.startswith("["))
        self.assertTrue(s1.turns[2].content.endswith("[image: a photo of a husky on snow]"))
        blob = json.dumps([s.role_content() for s in self.c0.sessions])
        self.assertNotIn("adversarial_answer", blob)

    def test_turn_id_must_match_its_session(self):
        rec = fixture()[0]
        rec["conversation"]["session_1"][0]["dia_id"] = "D2:1"
        with self.assertRaises(lo.LocomoError):
            lo.parse_conversation(0, rec)

    def test_wrong_file_refused(self):
        with self.assertRaises(lo.LocomoError):
            lo.load(os.path.join(self._d.name, "f.json"))                 # not the registered sha256

    # ---- the turn to leaf map ---------------------------------------------------------------------------------------------
    def test_segmentation_parity_with_the_ingest_path(self):
        """The mapped leaves have exactly the texts `EvalTenant.ingest` would store (corpus.session_leaf_texts)."""
        for cap in (1536, 60):
            for s in self.c0.sessions:
                want = corpus.session_leaf_texts(s.role_content(), cap)
                self.assertEqual([e.text for e in lo.session_leaves(s, cap)], want)

    def test_long_session_splits_and_gold_is_the_right_leaf(self):
        s2 = self.c0.sessions[1]
        leaves = lo.session_leaves(s2, 60)
        self.assertGreater(len(leaves), 3)
        self.assertEqual(sorted(t for e in leaves for t in e.turn_ids), sorted(t.dia_id for t in s2.turns))   # every turn once
        stored = [("L%02d" % i, e.text) for i, e in enumerate(lo.conversation_leaves(self.c0, 60))]
        lm = lo.bind(self.c0, stored, 60)
        q = next(q for q in self.c0.questions if q.qid == "conv-t0#1")           # evidence D1:3 and D2:12 (last turn of a long session)
        gl, gs = lo.gold_of(q, lm)
        self.assertEqual(gs, ["D1", "D2"])
        self.assertEqual(len(gl), 2)
        late = lm.by_turn["D2:12"][0]
        self.assertEqual(lm.session_of()[late], "D2")
        self.assertNotEqual(late, lm.by_turn["D2:1"][0])                          # a naive "first leaf of the session" would be wrong
        self.assertIn("line 11", dict(stored)[late])

    def test_bind_refuses_a_mismatch(self):
        stored = [("L%d" % i, e.text) for i, e in enumerate(lo.conversation_leaves(self.c0, 60))]
        lo.bind(self.c0, stored, 60)
        swapped = [stored[1], stored[0]] + stored[2:]
        with self.assertRaises(lo.LocomoError):
            lo.bind(self.c0, [(i, t) for (i, _), (_, t) in zip(stored, swapped)], 60)         # same ids, texts out of order
        with self.assertRaises(lo.LocomoError):
            lo.bind(self.c0, stored[:-1], 60)                                                 # a leaf missing
        with self.assertRaises(lo.LocomoError):
            lo.bind(self.c0, [(i, t + " ") if k == 0 else (i, t) for k, (i, t) in enumerate(stored)], 60)

    def test_session_hash_binds_conversation(self):
        a, b = self.convs[0].sessions[0], self.convs[1].sessions[0]
        self.assertNotEqual(self.convs[0].session_hash(a), self.convs[1].session_hash(b))

    # ---- the subset -------------------------------------------------------------------------------------------------------
    def test_allocation_matches_the_plan(self):
        self.assertEqual(lo.allocate({1: 282, 2: 321, 3: 92, 4: 841}, 100), {1: 18, 2: 21, 3: 6, 4: 55})
        self.assertEqual(lo.allocate({1: 3, 2: 2}, 10), {1: 3, 2: 2})                    # fewer than n: all
        self.assertEqual(sum(lo.allocate({1: 5, 2: 5, 3: 5, 4: 5}, 7).values()), 7)

    def qs(self, n=300):
        return [lo.LQuestion("c%d#%d" % (i % 7, i), "c", i % 7, 1 + i % 4, "q", ("D1:1",), 0) for i in range(n)]

    def test_subset_is_seeded_stratified_and_ordered_by_hash(self):
        qs = self.qs()
        a, b = lo.select_subset(qs, 100), lo.select_subset(list(reversed(qs)), 100)
        self.assertEqual([q.qid for q in a], [q.qid for q in b])                         # independent of input order
        self.assertEqual(len(a), 100)
        self.assertEqual({c: sum(q.category == c for q in a) for c in (1, 2, 3, 4)}, {1: 25, 2: 25, 3: 25, 4: 25})
        keys = [lo.hash_key(q.qid) for q in a]
        self.assertEqual(keys, sorted(keys))
        self.assertGreaterEqual(len({q.category for q in a[:30]}), 3)                    # a prefix mixes categories
        self.assertNotEqual([q.qid for q in lo.select_subset(qs, 100, seed=1)], [q.qid for q in a])
        rest = lo.select_subset(qs, 100, exclude=[q.qid for q in a])
        self.assertFalse({q.qid for q in a} & {q.qid for q in rest})

    # ---- metrics ----------------------------------------------------------------------------------------------------------
    def test_metrics_hand_computed(self):
        ls = {"a": "D1", "b": "D1", "c": "D2", "d": "D3", "e": "D3", "f": "D4"}
        order = ["a", "b", "c", "d", "e", "f"]
        m = lo.question_metrics(order, ["c", "f"], ["D2", "D4"], ls)
        self.assertEqual(m["leaf_r5"], 0.5)          # c at rank 3 in the first 5, f at 6 is out
        self.assertEqual(m["leaf_r10"], 1.0)
        self.assertEqual(m["sess_r5"], 1.0)          # distinct sessions D1, D2, D3, D4 within 5
        self.assertAlmostEqual(m["mrr10"], 1 / 3)
        self.assertEqual(m["allgold10"], 1.0)
        m = lo.question_metrics(order, ["e"], ["D3"], ls)
        self.assertIsNone(m["allgold10"])            # one gold leaf: excluded from all-gold
        self.assertIsNone(m["allgold10_sess"])
        self.assertAlmostEqual(m["mrr10"], 1 / 5)

    def test_metrics_failed_scores_zero_and_short_lists_are_not_padded(self):
        ls = {"a": "D1", "b": "D2"}
        z = lo.question_metrics(["a", "b"], ["a", "b"], ["D1", "D2"], ls, failed=True)
        self.assertEqual([z[k] for k in ("leaf_r5", "leaf_r10", "sess_r5", "mrr10", "allgold10")], [0.0] * 5)
        s = lo.question_metrics(["a"], ["a", "b"], ["D1", "D2"], ls)
        self.assertEqual((s["leaf_r10"], s["allgold10"]), (0.5, 0.0))

    def test_a_leaf_level_metric_is_not_a_session_level_one(self):
        """Two gold leaves in one session: leaf recall counts both, session recall counts the session once."""
        ls = {"a": "D1", "b": "D1", "c": "D2"}
        m = lo.question_metrics(["a", "c"], ["a", "b"], ["D1"], ls)
        self.assertEqual((m["leaf_r10"], m["sess_r10"]), (0.5, 1.0))

    def test_chance_reference(self):
        self.assertEqual(lo.chance_leaf_recall(40, 10), 0.25)
        self.assertEqual(lo.chance_leaf_recall(8, 10), 1.0)


@unittest.skipUnless(os.environ.get("LOCOMO_PATH"), "LOCOMO_PATH (locomo10.json) is not set")
class RealFile(unittest.TestCase):
    def test_registered_counts(self):
        cs = lo.load(os.environ["LOCOMO_PATH"])
        self.assertEqual(len(cs), 10)
        qs = [q for c in cs for q in c.questions]
        self.assertEqual(len(qs), 1536)
        self.assertEqual({c: sum(q.category == c for q in qs) for c in (1, 2, 3, 4)}, {1: 282, 2: 321, 3: 92, 4: 841})
        self.assertEqual(sum(c.n_excluded for c in cs), 4)
        self.assertEqual(sum(q.n_unresolved > 0 for q in qs), 2)
        self.assertEqual([len(lo.conversation_leaves(c, 1536)) for c in cs], [34, 25, 47, 42, 52, 60, 49, 50, 36, 43])


if __name__ == "__main__":
    unittest.main()
