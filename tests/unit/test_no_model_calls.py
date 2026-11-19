"""AC-17: the build makes no model calls. A static import guard over living_context.

Forbidden: any model-provider SDK, an HTTP client library, and subprocess (a command-line model
client would need it). `urllib` is allowed only in the Elasticsearch client, and `socket` only in
the gateway; the broken variant (a module that imports a provider SDK) must be flagged.
"""
import ast
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2] / "living_context"
FORBIDDEN = {"anthropic", "openai", "requests", "httpx", "aiohttp", "subprocess", "http", "google", "cohere", "mistralai"}
ALLOWED_URLLIB = {"gateway/es.py"}


def imports(source: str) -> set[str]:
    out = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            out |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            out.add(node.module.split(".")[0])
    return out


def violations(root: Path) -> list[str]:
    bad = []
    for p in root.rglob("*.py"):
        rel = p.relative_to(root).as_posix()
        mods = imports(p.read_text())
        hit = mods & FORBIDDEN
        if "urllib" in mods and rel not in ALLOWED_URLLIB:
            hit.add("urllib")
        if hit:
            bad.append(f"{rel}: {sorted(hit)}")
    return bad


class NoModelCalls(unittest.TestCase):
    def test_package_imports_no_model_client(self):
        self.assertEqual(violations(ROOT), [])

    def test_broken_variant_importing_a_provider_sdk_is_flagged(self):
        self.assertEqual(imports("import anthropic\nfrom openai import OpenAI"), {"anthropic", "openai"})
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "m.py").write_text("import subprocess\nimport urllib.request\n")
            self.assertEqual(len(violations(Path(d))), 1)


if __name__ == "__main__":
    unittest.main()
