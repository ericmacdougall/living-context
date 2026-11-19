"""AC-16: only `build_title` produces a title field (static scan of the package, with planted violations)."""
import ast
import unittest
from pathlib import Path

PKG = Path(__file__).resolve().parents[2] / "living_context"


def is_build_title_call(node) -> bool:
    if not isinstance(node, ast.Call):
        return False
    f = node.func
    return (isinstance(f, ast.Name) and f.id == "build_title") or (isinstance(f, ast.Attribute) and f.attr == "build_title")


def is_allowed_value(node) -> bool:
    """build_title(...); a read of an existing title (`x.get("title", ...)`, output shaping); a field mapping
    (`{"type": ...}`), which declares the field and writes no value."""
    if is_build_title_call(node):
        return True
    if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "get"
            and node.args and isinstance(node.args[0], ast.Constant) and node.args[0].value == "title"):
        return True
    return isinstance(node, ast.Dict) and any(isinstance(k, ast.Constant) and k.value == "type" for k in node.keys)


def title_writes(source: str) -> list[int]:
    """Line numbers where a `title` field is given a value that is not a direct call of build_title:
    keyword `title=...`, a dict literal key "title", or a store to a subscript ["title"]."""
    bad = []
    for n in ast.walk(ast.parse(source)):
        if isinstance(n, ast.keyword) and n.arg == "title" and not is_allowed_value(n.value):
            bad.append(n.value.lineno)
        elif isinstance(n, ast.Dict):
            for k, v in zip(n.keys, n.values):
                if isinstance(k, ast.Constant) and k.value == "title" and not is_allowed_value(v):
                    bad.append(k.lineno)
        elif isinstance(n, ast.Assign):
            for t in n.targets:
                if (isinstance(t, ast.Subscript) and isinstance(t.slice, ast.Constant) and t.slice.value == "title"
                        and not is_allowed_value(n.value)):
                    bad.append(n.lineno)
    return bad


class TitlePaths(unittest.TestCase):
    def test_no_module_writes_a_title_except_through_build_title(self):
        offenders = {}
        for f in PKG.rglob("*.py"):
            lines = title_writes(f.read_text())
            if lines:
                offenders[str(f.relative_to(PKG))] = lines
        self.assertEqual(offenders, {})

    def test_the_scan_sees_planted_second_paths(self):
        for planted in ('d = {"title": model_text}', 'doc["title"] = summary', "make(title=text[:40])",
                        'x = {"title": build_title(a) + "x"}', 'x = {"title": src.get("summary")}'):
            self.assertTrue(title_writes(planted), planted)
        for fine in ('d = {"title": build_title(t)}', "make(title=title.build_title(t))", 'x = r.get("title")', 'x = {"title": src.get("title", "")}', 'm = {"title": {"type": "text"}}'):
            self.assertEqual(title_writes(fine), [], fine)

    def test_build_title_is_defined_once(self):
        defs = [str(f.relative_to(PKG)) for f in PKG.rglob("*.py")
                if any(isinstance(n, ast.FunctionDef) and n.name == "build_title" for n in ast.walk(ast.parse(f.read_text())))]
        self.assertEqual(defs, ["ingest/title.py"])


if __name__ == "__main__":
    unittest.main()
