"""The offline top-50 direct log rebuilds the run's own A0 and A0+LEN arms exactly, for any beta, from the log alone.
In-memory corpus, no Elasticsearch, no model."""
import unittest

from evalkit import arms, corpus, dataset as ds, direct50_offline as d50, fixture

FX = fixture.make("advantage", 6, mates=10)
MC = corpus.MemCorpus(FX.sessions)
HASHES = {sid: ds.turns_hash(t) for q in FX.questions for sid, t in q.sessions}
DIRECT = corpus.MemDirect(MC.leaves)
BETAS = (0.0, 0.001, 0.004, 0.016, 0.5)


def run_arm(q, beta, name):
    out = arms.run_question(q.question, DIRECT, lambda _q: arms.TreeOut([]), MC.world, beta=beta)
    return [ds.opaque_ref(s) for s in out[name].sessions]


class Direct50(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.rows = d50.direct50_rows(FX.questions, HASHES, DIRECT, MC.world)

    def test_one_row_per_question_with_at_most_the_window(self):
        self.assertEqual(len(self.rows), len(FX.questions))
        for r in self.rows:
            self.assertLessEqual(len(r["direct50"]), arms.WINDOW)
            self.assertGreater(len(r["direct50"]), arms.K)       # the log must hold more than the ledger's top 10
            self.assertTrue(all(len(e) == 4 for e in r["direct50"]))

    def test_sessions_and_gold_are_opaque_refs_only(self):
        raw = set(HASHES.values())
        for r in self.rows:
            self.assertFalse(raw & set(r["gold"]))
            self.assertFalse(raw & {e[1] for e in r["direct50"]})
            self.assertTrue(all(s.startswith("s") and len(s) == 13 for s in r["gold"]))

    def test_rebuilt_a0_equals_the_run_arm(self):
        for q, r in zip(FX.questions, self.rows):
            self.assertEqual(d50.a0_from_row(r), run_arm(q, 0.0, "A0"))

    def test_rebuilt_a0_len_equals_the_run_arm_for_every_beta(self):
        changed = 0
        for beta in BETAS:
            for q, r in zip(FX.questions, self.rows):
                got = d50.a0_len_from_row(r, beta)
                self.assertEqual(got, run_arm(q, beta, "A0+LEN"), beta)
                changed += got != d50.a0_from_row(r)
        self.assertGreater(changed, 0)                            # the fixture must exercise the length prior, or the test is vacuous

    def test_a_log_cut_at_the_top_10_cannot_rebuild_a0_len(self):
        # broken variant: what the ledger keeps today (the top 10 only) is not enough for the length control
        bad = 0
        for beta in BETAS[1:]:
            for q, r in zip(FX.questions, self.rows):
                cut = dict(r, direct50=r["direct50"][:arms.K])
                bad += d50.a0_len_from_row(cut, beta) != run_arm(q, beta, "A0+LEN")
        self.assertGreater(bad, 0)


if __name__ == "__main__":
    unittest.main()
