"""Step 11: the behavioural-use audit sees an item that changes the answer, opened or not (broken: only opened rows)."""
import random
import tempfile
import unittest
from pathlib import Path

from living_context.feedback.audit import audit
from living_context.model.budget import Budget
from living_context.model.cache import DictStore
from living_context.model.port import ModelPort
from living_context.model.stub import StubBackend
from living_context.policy import ClassLattice

ITEMS = [{"row": "a", "text": "The orchard gate code is blue heron.", "opened": False},
         {"row": "b", "text": "Compost drainage needs gravel beds.", "opened": True},
         {"row": "c", "text": "Tomato staking uses cedar poles.", "opened": True}]


class Audit(unittest.TestCase):
    def run_audit(self, **kw):
        out = {}
        with tempfile.TemporaryDirectory() as d:
            port = ModelPort(StubBackend(), Budget(Path(d) / "b", 1000), ClassLattice(["base"], []), DictStore())
            n = audit(port, "tena", "base", "px", "what is the orchard gate code", ITEMS, rate=1.0, rng=random.Random(1),
                      write=lambda row, used: out.__setitem__(row, used), **kw)
        return n, out

    def test_the_unopened_item_that_carries_the_answer_is_seen_as_used(self):
        n, out = self.run_audit()
        self.assertEqual((n, out), (3, {"a": True, "b": False, "c": False}))

    def test_broken_sampling_only_opened_rows_misses_it(self):
        n, out = self.run_audit(broken_only_opened=True)
        self.assertNotIn("a", out)
        with self.assertRaises(AssertionError):
            self.assertEqual(out.get("a"), True)

    def test_the_sampling_rate_is_respected(self):
        with tempfile.TemporaryDirectory() as d:
            port = ModelPort(StubBackend(), Budget(Path(d) / "b", 1000), ClassLattice(["base"], []), DictStore())
            n = audit(port, "tena", "base", "px", "q", ITEMS, rate=0.0, rng=random.Random(1), write=lambda r, u: None)
        self.assertEqual(n, 0)


if __name__ == "__main__":
    unittest.main()
