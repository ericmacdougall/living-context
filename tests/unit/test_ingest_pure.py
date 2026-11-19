"""BUILD-PLAN Step 6 (pure parts): segmentation cap, title builder, embedding stub."""
import unittest

from living_context.embed.stub import DIMS, cosine, embed
from living_context.ingest import segment, title
from living_context.ingest.segment import Turn

CAP = 1536


def over_cap(leaves, cap=CAP):
    return [i for i, l in enumerate(leaves) if segment.tokens_of(segment.render(l)) > cap]


class Segmentation(unittest.TestCase):
    def test_longest_session_shape_has_no_leaf_over_the_cap(self):
        one_turn = [Turn("user", "word " * 15_600)]  # 78,000 characters in a single turn
        leaves, cont = segment.segment([one_turn], CAP)
        self.assertEqual(over_cap(leaves), [])
        self.assertGreater(len(leaves), 20)
        self.assertEqual(len(cont), len(leaves) - 1)
        joined = "".join(t.content for l in leaves for t in l)
        self.assertEqual(joined.replace(" ", ""), ("word " * 15_600).replace(" ", ""))  # nothing lost

    def test_broken_split_only_at_round_boundaries_fails(self):
        one_turn = [Turn("user", "word " * 15_600)]
        leaves, _ = segment.segment([one_turn], CAP, rounds_only=True)
        self.assertEqual(over_cap(leaves), [0])

    def test_many_turns_split_at_round_boundaries_and_unbroken_sentence_at_characters(self):
        turns = [Turn("user" if i % 2 == 0 else "assistant", "Sentence number %d. " % i * 30) for i in range(120)]
        leaves, _ = segment.segment([turns], CAP)
        self.assertEqual(over_cap(leaves), [])
        blob = [Turn("user", "x" * 20_000)]  # no sentence boundary at all
        leaves, _ = segment.segment([blob], CAP)
        self.assertEqual(over_cap(leaves), [])

    def test_empty_input_is_refused(self):
        for bad in ([], [[]], [[Turn("user", "   \n")]]):
            with self.assertRaises(segment.Empty):
                segment.segment(bad)

    def test_session_boundaries_and_time_gaps_cut(self):
        a = [Turn("user", "hello there", 0.0), Turn("assistant", "hi", 10.0)]
        b = [Turn("user", "later", 4000.0), Turn("assistant", "ok", 4010.0)]
        leaves, _ = segment.segment([a], CAP)
        self.assertEqual(len(leaves), 1)
        leaves, _ = segment.segment([a + b], CAP)  # one session, a 66 minute gap
        self.assertEqual(len(leaves), 2)
        leaves, _ = segment.segment([a, b], CAP)
        self.assertEqual(len(leaves), 2)

    def test_embedding_valley_cuts_a_topic_change(self):
        t1 = ["garden tomatoes soil water compost seedlings"] * 6
        t2 = ["engine piston torque gearbox clutch carburettor"] * 6
        turns = [Turn("user" if i % 2 == 0 else "assistant", s) for i, s in enumerate(t1 + t2)]
        leaves, _ = segment.segment([turns], CAP)
        self.assertEqual(len(leaves), 2)


class Titles(unittest.TestCase):
    TEXT = ("On 2024-03-15 I paid $1,250.00 to account AC-99381 for the garden irrigation project. "
            "The irrigation contractor said the irrigation pump needs 3 new seals; garden irrigation again in March.")

    def test_digit_tokens_never_reach_a_title(self):
        t = title.build_title(self.TEXT)
        self.assertEqual([w for w in t.split() if title.has_digit(w)], [])
        for bad in ("2024", "1,250", "ac-99381", "99381", "$1"):
            self.assertNotIn(bad, t)
        self.assertIn("irrigation", t.split())
        self.assertLessEqual(len(t.split()), 4)
        self.assertIn("march", title.build_title("March March March budget budget plan"))  # digit-free forms may remain

    def test_broken_title_that_keeps_digit_tokens_is_caught(self):
        def broken(text, n=4):
            import re
            from collections import Counter
            c = Counter(w for w in re.findall(r"\S+", text.lower()) if len(w) > 2)
            return " ".join(sorted(c, key=lambda w: (-c[w], w))[:n])
        bad = broken("2024-03-15 2024-03-15 AC-99381 AC-99381 1,250.00 1,250.00 garden")
        self.assertTrue(any(title.has_digit(w) for w in bad.split()))
        self.assertEqual([w for w in title.build_title("2024-03-15 2024-03-15 AC-99381 AC-99381 1,250.00 garden").split()
                          if title.has_digit(w)], [])

    def test_deterministic_idf_and_ties(self):
        df = {"common": 90, "rare": 1}
        t = title.build_title("common common rare rare other", df=df, n_docs=100, n=2)
        self.assertEqual(t.split()[0], "rare")
        self.assertEqual(title.build_title("beta alpha"), title.build_title("alpha beta"))

    def test_no_content_alternative_is_behind_the_same_function(self):
        try:
            title.set_mode(title.MODE_NO_CONTENT)
            self.assertEqual(title.build_title(self.TEXT), "")
        finally:
            title.set_mode(title.MODE_TFIDF)
        self.assertNotEqual(title.build_title(self.TEXT), "")
        with self.assertRaises(ValueError):
            title.set_mode("model_written")

    def test_one_sentence_leaf_title_is_not_the_leaf(self):
        text = "Marta moved to Lisbon."
        self.assertNotEqual(title.build_title(text), text)


class Embedding(unittest.TestCase):
    def test_deterministic_normalised_and_content_dependent(self):
        a, b = embed("garden tomatoes soil"), embed("engine piston torque")
        self.assertEqual(len(a), DIMS)
        self.assertEqual(a, embed("garden tomatoes soil"))
        self.assertAlmostEqual(cosine(a, a), 1.0, places=6)
        self.assertLess(cosine(a, b), 0.5)
        self.assertEqual(embed("   "), [0.0] * DIMS)


if __name__ == "__main__":
    unittest.main()
