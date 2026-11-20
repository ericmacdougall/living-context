"""Static guards for the pool code (POOLS 1.2, 3; stage 0): no model call can be made from it, and the class-level indices are read only
through the class reader."""
import ast
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2] / "living_context"
POOL_MODULES = ["gateway/pool_tools.py", "gateway/pool_admin.py", "gateway/pool_authz.py", "gateway/pool_boot.py", "gateway/pointers.py",
                "gateway/session_state.py", "gateway/class_reader.py", "recall/envelope.py", "policy/pools.py", "erasure/pool_scope.py"]


class Static(unittest.TestCase):
    def test_no_pool_module_imports_the_model_port_or_the_feedback_judge(self):
        for rel in POOL_MODULES:
            tree = ast.parse((ROOT / rel).read_text())
            for node in ast.walk(tree):
                mods = [node.module or ""] if isinstance(node, ast.ImportFrom) else [a.name for a in node.names] if isinstance(node, ast.Import) else []
                for m in mods:
                    self.assertNotRegex(m, r"(^|\.)model(\.|$)|feedback", f"{rel} imports {m}")

    def test_the_tools_read_class_level_indices_only_through_the_reader(self):
        src = (ROOT / "gateway/pool_tools.py").read_text().splitlines()
        for n, line in enumerate(src, 1):
            if re.search(r'"(ctl|log|sq)"', line):
                self.assertIn("self.reader.", line, f"pool_tools.py:{n} names a class-level kind outside the reader: {line.strip()}")
        for n, line in enumerate(src, 1):
            self.assertNotRegex(line, r"-(ctl|log|sq)\b", f"pool_tools.py:{n}")

    def test_the_writer_reads_pool_heads_and_claims_through_the_reader(self):
        text = (ROOT / "ingest/writer.py").read_text()
        body = text[text.index("def _cget"):text.index("def _get(self")]
        self.assertIn("self._reader.get(", body)
        for fn in ("advance_head", "_update_head"):
            seg = text[text.index(f"def {fn}"):]
            seg = seg[:seg.index("\n    def ", 10)]
            self.assertIn("_cget(", seg, fn)
            self.assertNotIn("self._get(", seg, fn)


if __name__ == "__main__":
    unittest.main()
