"""E1: the verifier greps every file under the data root outside Elasticsearch's data directory (BUILD-PLAN Step 14).

A model-port wrapper that keeps outputs in a file outside the indices (the removed evaluation cache) must be found by that grep,
a port that keeps them in the lineage-carrying store must not, and the grep proves it runs with a control marker."""
import tempfile
import unittest
from pathlib import Path

from living_context.model.budget import Budget
from living_context.model.cache import DictStore
from living_context.model.port import CallContext, ModelPort, Task
from living_context.model.stub import StubBackend
from living_context.policy import ClassLattice
from living_context.verifier.check import Canaries, Verifier, VerifierError, grep_data_roots

CANARY = "zephyrcanaryqq"
LEAVES = [{"text": f"The {CANARY} orchard needs pruning in winter.", "title": "orchard"}]


class FileWrapper(ModelPort):
    """BROKEN: a port wrapper that also keeps every output in a file under the data root."""

    def __init__(self, *a, root, **kw):
        super().__init__(*a, **kw)
        self._root = Path(root)

    def call(self, task, inputs, *, ctx):
        res = super().call(task, inputs, ctx=ctx)
        if res.ok:
            (self._root / "model-out.log").open("a").write(repr(res.output) + "\n")
        return res


class DataRoot(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.TemporaryDirectory()
        self.addCleanup(self.d.cleanup)
        self.root = Path(self.d.name) / "data"
        self.root.mkdir()
        (self.root / "gateway-worker.out").write_text("DATAMARKER9 control line\n")
        self.ctx = CallContext("t", "base", "px", ("base",), lin_leaf_ids=("l1",), lin_subjects=("s1",))

    def port(self, cls):
        kw = {"root": self.root} if cls is FileWrapper else {}
        return cls(StubBackend(), Budget(Path(self.d.name) / "b", 100), ClassLattice(["base"], []), DictStore(), **kw)

    def verifier(self):
        return Verifier(None, "t", data_roots=[self.root])

    def run_case(self, cls):
        out = self.port(cls).call(Task.BUILD_L1, {"leaves": LEAVES}, ctx=self.ctx).output
        self.assertIn(CANARY, str(out))
        return grep_data_roots([self.root], [CANARY, "DATAMARKER9"])

    def test_a_port_without_a_side_file_leaves_nothing_and_the_control_marker_is_found(self):
        g = self.run_case(ModelPort)
        self.assertEqual((g[CANARY], g["DATAMARKER9"]), (0, 1))

    def test_broken_wrapper_keeping_outputs_in_a_file_outside_the_indices_is_found(self):
        g = self.run_case(FileWrapper)
        self.assertEqual(g[CANARY], 1, "the data-root grep missed a model output kept outside the indices")

    def test_a_root_that_is_not_a_directory_is_an_error_not_a_clean_result(self):
        with self.assertRaises(VerifierError):
            grep_data_roots([self.root / "nothing"], [CANARY])

    def test_the_verifier_step_counts_the_file_and_refuses_a_run_that_lost_its_control_marker(self):
        self.run_case(FileWrapper)
        c = Canaries(short=CANARY, data_marker="DATAMARKER9")
        v = self.verifier()
        self.assertEqual(v._data_root([CANARY, "DATAMARKER9"]), {CANARY: 1, "DATAMARKER9": 1})
        (self.root / "gateway-worker.out").unlink()
        g = v._data_root([c.data_marker])
        self.assertEqual(g["DATAMARKER9"], 0)


if __name__ == "__main__":
    unittest.main()
