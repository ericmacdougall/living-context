"""BUILD-PLAN Step 15: the authority epoch is checked at admission and at each fan-out wave (DESIGN 4.4). A revoke or a class-grant
removal that lands while the fan-out runs aborts the recall: no later wave is dispatched and no digest is produced. Broken variant:
the epoch is checked once, at admission only (the recall finishes and returns pointers to a revoked principal)."""
import tempfile
import unittest

from living_context.recall.fanout import Recall, RecallAborted
from living_context.recall.view import MemView
from tests.unit.test_fanout import Spy, mk, questions, tree


class EpochAtEachWave(unittest.TestCase):
    def run_recall(self, **kw):
        spy = Spy()
        top = len(MemView(tree()).top())
        flips = {"after": 1}   # the epoch flips once the first holder call was made

        def epoch_ok():
            return len(spy.seen) < flips["after"]

        with tempfile.TemporaryDirectory() as d:
            port, _ = mk(d, spy)
            res = Recall(port, "tena", "base", "px", epoch_ok=epoch_ok, **kw).recall(questions(1)[0][0], MemView(tree()))
        return res, spy, top

    def check_aborts(self, **kw):
        try:
            res, spy, top = self.run_recall(**kw)
        except RecallAborted:
            return
        self.fail("the recall returned a digest after the epoch had flipped during the fan-out")

    def test_a_flip_during_the_fan_out_aborts_the_recall_and_dispatches_no_later_wave(self):
        spy = Spy()
        calls_at_abort = []

        def epoch_ok():
            calls_at_abort.append(len(spy.seen))
            return len(spy.seen) < 1

        with tempfile.TemporaryDirectory() as d:
            port, _ = mk(d, spy)
            with self.assertRaises(RecallAborted):
                Recall(port, "tena", "base", "px", epoch_ok=epoch_ok, workers=1).recall(questions(1)[0][0], MemView(tree()))
        self.assertLessEqual(len(spy.seen), 2, "holder calls kept being dispatched after the epoch flipped")
        self.assertGreaterEqual(len(calls_at_abort), 2, "the epoch was not checked again after the first wave")

    def test_a_recall_with_a_current_epoch_completes(self):
        with tempfile.TemporaryDirectory() as d:
            port, _ = mk(d)
            res = Recall(port, "tena", "base", "px", epoch_ok=lambda: True).recall(questions(1)[0][0], MemView(tree()))
        self.assertTrue(res.digest["sources"]["tree"])

    def test_broken_epoch_checked_at_admission_only_is_red(self):
        with self.assertRaises(AssertionError) as cm:
            self.check_aborts(broken_epoch_at_admission_only=True)
        self.assertIn("returned a digest after the epoch had flipped", str(cm.exception))


if __name__ == "__main__":
    unittest.main()
