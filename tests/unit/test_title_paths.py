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
    """build_title(...); a read of an existing title (`x.get("title", ...)`, `hit.title`: output shaping); a field mapping
    (`{"type": ...}`), which declares the field and writes no value."""
    if is_build_title_call(node):
        return True
    if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "get"
            and node.args and isinstance(node.args[0], ast.Constant) and node.args[0].value == "title"):
        return True
    if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "wrap"
            and isinstance(node.func.value, ast.Name) and node.func.value.id == "envelope"):
        # `envelope.wrap(pool, label, origin, via, pointer, title, ...)`: an existing title escaped and labelled for display (POOLS 3.7). It
        # writes no new title text, so it is allowed only when the title argument itself is an allowed value (an existing title); a title built
        # from leaf text and then wrapped is still a second path.
        return (len(node.args) >= 6 and not any(isinstance(a, ast.Starred) for a in node.args)
                and not node.keywords and is_allowed_value(node.args[5]))
    if isinstance(node, ast.Attribute) and node.attr == "title":  # reading a stored title off a hit
        return True
    if isinstance(node, ast.Subscript) and isinstance(node.slice, ast.Constant) and node.slice.value == "title":
        return True  # `entry["title"]`: a stored title passed through (output shaping), not a second source of titles
    if isinstance(node, ast.Name) and node.id == "title":
        return True  # a parameter or local named title: every assignment to such a name is scanned below
    if isinstance(node, ast.Constant) and node.value == "":
        return True  # the empty placeholder (an erased node's title, a missing entry) carries no text
    if isinstance(node, ast.IfExp):
        return is_allowed_value(node.body) and is_allowed_value(node.orelse)
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
        elif isinstance(n, (ast.AnnAssign, ast.AugAssign, ast.NamedExpr)):
            t = n.target
            if isinstance(t, ast.Name) and t.id == "title" and n.value is not None and not is_allowed_value(n.value):
                bad.append(n.lineno)
            if (isinstance(t, ast.Subscript) and isinstance(t.slice, ast.Constant) and t.slice.value == "title"
                    and n.value is not None and not is_allowed_value(n.value)):
                bad.append(n.lineno)
        elif isinstance(n, ast.Assign):
            for t in n.targets:
                if isinstance(t, (ast.Tuple, ast.List)) and any(isinstance(e, ast.Name) and e.id == "title" for e in t.elts):
                    bad.append(n.lineno)  # unpacking into `title` hides the source of the value
                if isinstance(t, ast.Name) and t.id == "title" and not is_allowed_value(n.value):
                    bad.append(n.lineno)
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
                        'x = {"title": build_title(a) + "x"}', 'x = {"title": src.get("summary")}', 'x = {"title": e["summary"]}', 'x = {"title": "free text"}',
                        'x = {"title": e["title"] if e else summary}', "title = text[:40]", "title: str = summary", "title += summary", "title, x = a, b", "(title := summary)",
                        'doc.update(dict(title=summary))',
                        'x = {"title": envelope.wrap(a, b, c, d, e, text[:40])}', 'x = {"title": envelope.wrap(a, b, c, d, e, leaf["text"])}',
                        'x = {"title": envelope.wrap(a, b, c, d, e, summary)}', 'x = {"title": envelope.wrap(*args)}',
                        'x = {"title": envelope.wrap(a, b, c, d, e, title=text[:40])}', 'x = {"title": envelope.wrap(a, b, c, d, e)}'):
            self.assertTrue(title_writes(planted), planted)
        for fine in ('d = {"title": build_title(t)}', "make(title=title.build_title(t))", 'x = r.get("title")', 'x = {"title": src.get("title", "")}', 'm = {"title": {"type": "text"}}',
                     'x = {"title": e["title"]}', 'x = {"title": ""}', 'x = {"title": e["title"] if e else ""}', "title = build_title(t)", 'x = make(title=title)',
                     'x = {"title": envelope.wrap(a, b, c, d, e, title)}', 'x = {"title": envelope.wrap(a, b, c, d, e, src.get("title", ""))}'):
            self.assertEqual(title_writes(fine), [], fine)

    def test_build_title_is_defined_once(self):
        defs = [str(f.relative_to(PKG)) for f in PKG.rglob("*.py")
                if any(isinstance(n, ast.FunctionDef) and n.name == "build_title" for n in ast.walk(ast.parse(f.read_text())))]
        self.assertEqual(defs, ["ingest/title.py"])


if __name__ == "__main__":
    unittest.main()
