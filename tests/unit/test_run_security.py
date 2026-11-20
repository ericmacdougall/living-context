"""AC-13: the security runner parses `skipped=` and fails on any skip, on any failure and on zero checks."""
import tempfile
import textwrap
import unittest
from pathlib import Path

from tools import run_security as rs


def project(body: str) -> str:
    root = Path(tempfile.mkdtemp(prefix="lcrs-"))
    t = root / "tsec"
    t.mkdir()
    (root / "__init__.py").write_text("")
    (t / "__init__.py").write_text("")
    (t / "test_x.py").write_text(textwrap.dedent(body))
    return str(root)


PASS = "import unittest\nclass A(unittest.TestCase):\n    def test_a(self): self.assertTrue(True)\n"
SKIP = PASS + "    @unittest.skip('no cluster')\n    def test_s(self): pass\n"
FAIL = PASS + "    def test_f(self): self.assertTrue(False)\n"
EMPTY = "import unittest\n"


class Runner(unittest.TestCase):
    def test_a_clean_run_passes(self):
        c, why, _ = rs.run("tsec", project(PASS))
        self.assertEqual((c["ran"], c["skipped"], why), (1, 0, []))

    def test_a_skip_is_refused(self):
        c, why, _ = rs.run("tsec", project(SKIP))
        self.assertEqual(c["skipped"], 1)
        self.assertTrue(any("skipped=1" in w for w in why), why)

    def test_a_failure_and_zero_checks_are_refused(self):
        self.assertTrue(any("failures=1" in w for w in rs.run("tsec", project(FAIL))[1]))
        self.assertTrue(any("zero checks" in w for w in rs.run("tsec", project(EMPTY))[1]))

    def test_broken_runner_that_ignores_skips_is_red(self):
        c, why, _ = rs.run("tsec", project(SKIP), broken_ignore_skips=True)
        self.assertEqual(why, [], "premise: the broken parser passes a skipped run")
        with self.assertRaises(AssertionError):
            self.assertTrue(rs.verdict(rs.run("tsec", project(SKIP), broken_ignore_skips=True)[0]), "a skipped check passed the runner")

    def test_parse_summary_reads_every_count(self):
        c = rs.parse_summary("Ran 9 tests in 1.0s\n\nFAILED (failures=2, errors=1, skipped=3)\n")
        self.assertEqual((c["ran"], c["failures"], c["errors"], c["skipped"], c["ok"]), (9, 2, 1, 3, False))
        self.assertEqual(rs.parse_summary("Ran 4 tests in 1s\n\nOK (skipped=1)\n")["skipped"], 1)


if __name__ == "__main__":
    unittest.main()
