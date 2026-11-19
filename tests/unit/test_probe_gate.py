"""AC-15: the probe gate refuses a step whose probe results are missing, stale or failed without a decision."""
import hashlib
import json
import shutil
import tempfile
import unittest
from pathlib import Path

from tools import probe_gate
from tools.probes import pcommon

REPO = Path(__file__).resolve().parents[2]


def make_root(verdicts=None):
    root = Path(tempfile.mkdtemp(prefix="lcgate-"))
    (root / "docs").mkdir()
    shutil.copy(REPO / "docs" / "HOST.md", root / "docs" / "HOST.md")
    (root / "tools").mkdir()
    shutil.copytree(REPO / "tools" / "probes", root / "tools" / "probes", ignore=shutil.ignore_patterns("results", "__pycache__"))
    res = root / "tools" / "probes" / "results"
    res.mkdir()
    pinned = json.loads((root / "tools" / "probes" / "pinned_n.json").read_text())
    for pid in probe_gate.ALL:
        script = root / "tools" / "probes" / probe_gate.PROBE_SCRIPT[pid]
        rec = {"id": pid, "verdict": (verdicts or {}).get(pid, "PASS"), "n": pinned[pid],
               "script_sha256": hashlib.sha256(script.read_bytes()).hexdigest(),
               "image_digest": probe_gate.pinned_digest(root)}
        (res / f"{pid}.json").write_text(json.dumps(rec))
    return root


def write_decision(root, pid, verdict, mitigation="gateway never forwards the error", who="main (ASSUMPTION)"):
    d = root / "docs" / "decisions"
    d.mkdir(exist_ok=True)
    (d / f"{pid}.md").write_text(f"Probe: {pid}\nVerdict: {verdict}\nDecided by: {who}\nMitigation: {mitigation}\n")


class GateTests(unittest.TestCase):
    def test_all_current_and_passing_opens_every_step(self):
        root = make_root()
        for step in range(1, 23):
            self.assertEqual(probe_gate.gate(root, step), [])

    def test_missing_result_refuses(self):
        root = make_root()
        (root / "tools" / "probes" / "results" / "P9.json").unlink()
        self.assertTrue(any("P9: no result file" in w for w in probe_gate.gate(root, 14)))
        self.assertTrue(probe_gate.gate(root, 1))  # no step 1 to 22 is green without all sixteen

    def test_edited_script_invalidates_its_result(self):
        root = make_root()
        with open(root / "tools" / "probes" / "probe_p11_seqno.py", "a") as f:
            f.write("\n# edited\n")
        self.assertTrue(any("P11: script changed" in w for w in probe_gate.gate(root, 13)))

    def test_image_digest_mismatch_refuses(self):
        root = make_root()
        host = root / "docs" / "HOST.md"
        host.write_text(host.read_text().replace("sha256:79b4", "sha256:aaaa"))
        self.assertTrue(any("image digest" in w for w in probe_gate.gate(root, 5)))

    def test_n_must_equal_the_pinned_n(self):
        root = make_root()
        f = root / "tools" / "probes" / "results" / "P7.json"
        rec = json.loads(f.read_text())
        rec["n"] -= 1
        f.write_text(json.dumps(rec))
        self.assertTrue(any("P7: N" in w for w in probe_gate.gate(root, 13)))

    def test_fail_needs_a_decision_record_and_deleting_it_reopens_the_refusal(self):
        root = make_root({"P2": "FAIL"})
        self.assertTrue(any("P2" in w and "decision" in w for w in probe_gate.gate(root, 5)))
        write_decision(root, "P2", "FAIL")
        self.assertEqual(probe_gate.gate(root, 5), [])
        (root / "docs" / "decisions" / "P2.md").unlink()
        self.assertTrue(any("P2" in w for w in probe_gate.gate(root, 5)))

    def test_decision_must_carry_every_field(self):
        root = make_root({"P2": "FAIL"})
        write_decision(root, "P2", "FAIL", who="somebody else")  # names neither the owner nor main
        self.assertTrue(probe_gate.gate(root, 5))
        write_decision(root, "P2", "FAIL", who="the project owner")  # the owner may decide
        self.assertEqual(probe_gate.gate(root, 5), [])
        write_decision(root, "P2", "FAIL", mitigation="")
        self.assertTrue(probe_gate.gate(root, 5))
        write_decision(root, "P2", "LIMITED")  # wrong verdict named
        self.assertTrue(probe_gate.gate(root, 5))

    def test_fail_with_no_decision_line_is_refused(self):
        root = make_root({"P2": "FAIL"})
        d = root / "docs" / "decisions"
        d.mkdir(exist_ok=True)
        (d / "P2.md").write_text("Probe: P2\nVerdict: FAIL\nMitigation: gateway never forwards the error\nmain owner ASSUMPTION\n")
        self.assertTrue(any("P2" in w and "decision" in w for w in probe_gate.gate(root, 5)))

    def test_error_verdict_is_never_excused_by_a_decision(self):
        root = make_root({"P15": "ERROR"})
        write_decision(root, "P15", "ERROR")
        self.assertTrue(probe_gate.gate(root, 5))

    def test_a_failing_probe_of_another_step_does_not_block_this_step_but_must_be_current(self):
        root = make_root({"P9": "FAIL"})  # step 14's probe
        self.assertEqual(probe_gate.gate(root, 5), [])
        self.assertTrue(probe_gate.gate(root, 14))

    def test_probe_verdicts_are_relative_to_the_expected_result(self):
        self.assertEqual(pcommon.p4_verdict(400, 400, 201), "PASS")  # unsupported records PASS
        self.assertEqual(pcommon.p4_verdict(200, 400, 201), "FAIL")  # success records FAIL
        self.assertEqual(pcommon.p4_verdict(400, 200, 201), "FAIL")
        self.assertEqual(pcommon.p4_verdict(400, 400, 400), "FAIL")  # control must be accepted

    def test_a_pinned_n_mismatch_records_error(self):
        tmp = Path(tempfile.mkdtemp(prefix="lcfin-"))
        script = tmp / "probe_x.py"
        script.write_text("pass\n")
        rc = pcommon.finish("P4", str(script), "e", "o", True, 99, {}, results_dir=tmp)
        self.assertEqual(rc, 1)
        self.assertEqual(json.loads((tmp / "P4.json").read_text())["verdict"], "ERROR")


class RepositoryResultsTests(unittest.TestCase):
    """The recorded results in the repository are current for the scripts and the pinned image."""

    def test_real_results_are_current(self):
        stale = []
        for pid in probe_gate.ALL:
            _, why = probe_gate.check_result(REPO, pid)
            stale += why
        self.assertEqual(stale, [])

    def test_p2_fail_has_its_decision_record(self):
        rec = json.loads((REPO / "tools" / "probes" / "results" / "P2.json").read_text())
        self.assertEqual(rec["verdict"], "FAIL")
        self.assertTrue(probe_gate.decision_ok(REPO, "P2", "FAIL"))


if __name__ == "__main__":
    unittest.main()
