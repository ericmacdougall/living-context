"""Tuning kit generators: determinism, independence of the size and complexity knobs, ground-truth validity. Pure python, no
Elasticsearch, no model."""
import unittest

from tunekit import synth

STAT_KEYS = ("topic_jaccard", "entity_reuse", "dup_frac", "old_frac", "mean_session_len", "mean_words", "leaves_per_topic",
             "q_alias_frac", "q_para_frac", "q_mean_topic_words")


import re


def toks(text):
    return set(re.findall(r"[A-Za-z0-9]+", text))


def avg_stats(n, level, seeds=(1, 2, 3), **over):
    acc = {k: 0.0 for k in STAT_KEYS}
    for s in seeds:
        st = synth.generate(synth.knobs_at(n, s, level, **over)).manifest["stats"]
        for k in STAT_KEYS:
            acc[k] += st[k] / len(seeds)
    return acc


SIZE_INDEPENDENT = STAT_KEYS


def violations(a, b, rel=0.20, absol=0.03):
    """Statistics that differ between two corpora by more than the tolerance (relative, with an absolute floor for small ones)."""
    return sorted(k for k in SIZE_INDEPENDENT if abs(a[k] - b[k]) > max(absol, rel * max(abs(a[k]), abs(b[k]))))


class Determinism(unittest.TestCase):
    def test_same_seed_same_bytes_and_other_seed_differs(self):
        a = synth.generate(synth.knobs_at(500, 7, 0.5))
        b = synth.generate(synth.knobs_at(500, 7, 0.5))
        c = synth.generate(synth.knobs_at(500, 8, 0.5))
        self.assertEqual(a.manifest["content_sha256"], b.manifest["content_sha256"])
        self.assertEqual([L["text"] for L in a.leaves], [L["text"] for L in b.leaves])
        self.assertEqual(a.questions, b.questions)
        self.assertNotEqual(a.manifest["content_sha256"], c.manifest["content_sha256"])

    def test_manifest_records_parameters_and_seed(self):
        m = synth.generate(synth.knobs_at(200, 5, 1.0)).manifest
        self.assertEqual(m["knobs"]["seed"], 5)
        self.assertEqual(m["knobs"]["n_leaves"], 200)
        self.assertEqual(m["generator"], synth.GEN_VERSION)
        self.assertIn("not natural language", m["text_caveat"])

    def test_bad_knobs_are_refused(self):
        for bad in (dict(n_leaves=4), dict(topic_overlap=1.5), dict(entity_reuse=0.5), dict(growth="sideways")):
            kw = dict(n_leaves=100, seed=1)
            kw.update(bad)
            with self.assertRaises(ValueError):
                synth.generate(synth.Knobs(**kw))


class SizeIsExact(unittest.TestCase):
    def test_leaf_count_is_the_size_knob_at_every_complexity(self):
        for n in (64, 257, 1000):
            for lv in (0.0, 0.5, 1.0):
                self.assertEqual(len(synth.generate(synth.knobs_at(n, 3, lv)).leaves), n, (n, lv))


class Independence(unittest.TestCase):
    """Changing size leaves the complexity statistics unchanged within tolerance, and changing complexity leaves size and leaf
    length unchanged. The deep-growth control must turn the size check red, so the check cannot pass vacuously."""

    def test_size_does_not_move_complexity_statistics_in_wide_growth(self):
        for lv in (0.0, 0.5, 1.0):
            small, large = avg_stats(600, lv), avg_stats(6000, lv)
            self.assertEqual(violations(small, large), [], (lv, small, large))

    def test_question_spec_is_fixed_and_the_drop_rate_is_reported(self):
        for n in (600, 6000):
            c = synth.generate(synth.knobs_at(n, 1, 1.0))
            self.assertEqual({len(q["text"].split(" regarding ")[1].rstrip("?").split()) for q in c.questions}, {1})   # one_word = 1 at c = 1
            self.assertIn("question_drop_rate", c.manifest)
            self.assertGreaterEqual(c.manifest["question_drop_rate"], 0.0)

    def test_the_deep_growth_control_turns_the_size_check_red(self):
        small, large = avg_stats(600, 0.5, growth="deep", fixed_topics=24), avg_stats(6000, 0.5, growth="deep", fixed_topics=24)
        bad = violations(small, large)
        self.assertIn("leaves_per_topic", bad)

    def test_complexity_moves_its_statistics_and_not_the_size(self):
        lo, hi = avg_stats(2000, 0.0), avg_stats(2000, 1.0)
        for k in ("entity_reuse", "dup_frac", "old_frac", "q_alias_frac", "q_para_frac", "topic_jaccard"):
            self.assertGreater(hi[k], lo[k] + 0.01, k)
        self.assertLess(hi["q_mean_topic_words"], lo["q_mean_topic_words"])      # one_word questions name fewer topic words
        for k in ("leaves_per_topic", "mean_session_len"):
            self.assertLess(abs(hi[k] - lo[k]), 0.2 * lo[k], k)
        self.assertLess(abs(hi["mean_words"] - lo["mean_words"]), 0.1 * lo["mean_words"])

    def test_each_complexity_knob_moves_only_its_own_statistic(self):
        base = avg_stats(2000, 0.5)
        moved = {
            "near_dup": ("dup_frac", avg_stats(2000, 0.5, near_dup=0.4)),
            "update_rate": ("old_frac", avg_stats(2000, 0.5, update_rate=0.4)),
            "entity_reuse": ("entity_reuse", avg_stats(2000, 0.5, entity_reuse=5.0)),
            "topic_overlap": ("topic_jaccard", avg_stats(2000, 0.5, topic_overlap=0.8)),
            "paraphrase": ("q_para_frac", avg_stats(2000, 0.5, paraphrase=0.8)),
        }
        one = avg_stats(2000, 0.5, one_word=1.0)
        self.assertLess(one["q_mean_topic_words"], base["q_mean_topic_words"] - 0.2)
        for k in ("dup_frac", "old_frac", "leaves_per_topic", "mean_session_len"):
            self.assertLess(abs(one[k] - base[k]), max(0.03, 0.25 * base[k]), k)
        for knob, (stat, got) in moved.items():
            self.assertGreater(got[stat], base[stat] + 0.01, knob)
            others = [k for k in ("dup_frac", "old_frac", "leaves_per_topic", "mean_session_len") if k != stat]
            for k in others:
                self.assertLess(abs(got[k] - base[k]), max(0.03, 0.25 * base[k]), (knob, k))


class GroundTruth(unittest.TestCase):
    def check(self, c):
        leaves, topics = c.leaves, c.topics
        names = {}
        for q in c.questions:
            g = leaves[q["gold"][0]]
            self.assertNotEqual(g["kind"], "old")
            self.assertIn(g["value"], g["text"])
            self.assertEqual(sum(g["value"] in L["text"] for L in leaves), 1, "answer value is unique to its leaf")
            words = q["text"].rstrip("?").split(" regarding ")[1].split() if " regarding " in q["text"] else []
            for w in words:
                self.assertIn(w, g["tws"])
                self.assertIn(w, toks(g["text"]))
            ent_in_q = q["text"].rstrip("?").split(" for ")[1].split(" regarding ")[0]
            self.assertTrue(set(ent_in_q.split()) <= toks(g["text"]))
            concept = synth.ATTRS[g["attr"]]
            phrase = q["text"].split("What is the ")[1].split(" for ")[0]
            self.assertIn(phrase, concept)
            # uniqueness at the text level: exactly one non-superseded leaf contains the entity string, both words and a form of the concept
            etoks = ent_in_q.split()
            hits = [L["id"] for L in leaves if L["kind"] != "old" and set(etoks) <= toks(L["text"]) and set(words) <= toks(L["text"])
                    and L["attr"] == g["attr"]]
            self.assertEqual(hits, [g["id"]], q["text"])
            for s in q["stale"]:
                self.assertEqual(leaves[s]["key"], g["key"])
                self.assertLess(s, g["id"])           # leaf ids are chronological: the superseded one is earlier
                self.assertNotEqual(leaves[s]["value"], g["value"])
                self.assertEqual(leaves[s]["kind"], "old")
            self.assertEqual(q["kind"] == "updated", bool(q["stale"]))

    def test_every_leaf_states_its_own_attribute_form_and_value(self):
        c = synth.generate(synth.knobs_at(1000, 3, 1.0))
        for L in c.leaves:
            self.assertIn(synth.ATTRS[L["attr"]][L["form"]], L["text"])
            self.assertIn(L["value"], L["text"])

    def test_low_and_high_complexity(self):
        for lv in (0.0, 1.0):
            c = synth.generate(synth.knobs_at(1500, 11, lv))
            self.assertGreater(len(c.questions), 300)
            self.check(c)

    def test_a_broken_gold_pointer_is_caught(self):
        c = synth.generate(synth.knobs_at(300, 2, 0.5))
        c.questions[0]["gold"] = [(c.questions[0]["gold"][0] + 1) % len(c.leaves)]
        with self.assertRaises(AssertionError):
            self.check(c)

    def test_session_and_time_order(self):
        c = synth.generate(synth.knobs_at(800, 4, 0.5))
        dates = [L["date"] for L in c.leaves]
        self.assertEqual(dates, sorted(dates))                       # written chronologically
        sess = {}
        for L in c.leaves:
            sess.setdefault(L["session"], set()).add(L["topic"])
        self.assertTrue(all(len(v) == 1 for v in sess.values()))     # a session stays in one topic


class QuestionKnobs(unittest.TestCase):
    def test_paraphrase_changes_surface_form_only(self):
        a = synth.generate(synth.knobs_at(1000, 6, 0.5, paraphrase=0.0))
        b = synth.generate(synth.knobs_at(1000, 6, 0.5, paraphrase=1.0))
        self.assertEqual([L["text"] for L in a.leaves], [L["text"] for L in b.leaves])     # the corpus is unchanged
        self.assertEqual(sum(q["para"] for q in a.questions), 0)
        self.assertEqual(sum(q["para"] for q in b.questions), len(b.questions))
        for q in b.questions:
            g = b.leaves[q["gold"][0]]
            phrase = q["text"].split("What is the ")[1].split(" for ")[0]
            self.assertNotEqual(phrase, synth.ATTRS[g["attr"]][g["form"]])


if __name__ == "__main__":
    unittest.main()
