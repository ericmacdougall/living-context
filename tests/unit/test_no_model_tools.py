"""The two measurement tools that must make no model call (the A0 sanity run and the S2 budget) contain no path to one."""
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
FORBIDDEN = re.compile(r"CappedPort|CliRunner|subprocess|ModelPort|ResilientPort|import claude_cli|from \. import .*claude_cli\b")


class NoModel(unittest.TestCase):
    def test_a0_sanity_has_no_model_port(self):
        src = (ROOT / "evalkit/a0_sanity.py").read_text()
        code = "\n".join(l for l in src.splitlines() if not l.lstrip().startswith(("#", '"""')))
        self.assertIsNone(FORBIDDEN.search(code.split('"""')[-1]))

    def test_s2_budget_only_renders_prompts(self):
        src = (ROOT / "evalkit/s2_budget.py").read_text()
        for bad in ("CappedPort", "CliRunner", "subprocess", "ModelPort", "ResilientPort", "Budget("):
            self.assertNotIn(bad, src.split('"""')[2], bad)

    def test_the_embedding_backend_has_no_model_call_either(self):
        src = (ROOT / "living_context/embed/e5.py").read_text()
        self.assertNotIn("subprocess", src)
        self.assertNotIn("claude", src.lower().replace("claude code", ""))


if __name__ == "__main__":
    unittest.main()
