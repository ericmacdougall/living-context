"""F3 and class guard (d): a resumed pool save projects the STORED source's provenance and lineage, never the retrying caller's.

Review round 3 F3: save with a human token, interrupt after the source append, retry the same text and key with an agent token of the same
principal and conversation. Before the fix the projection said `agent` while the authoritative `prov_json` said `human`, which lets an
agent correct or retract a human item directly. The writer runs over a document store with ES write semantics (create, CAS)."""
import ast
import tempfile
import unittest
from pathlib import Path

from living_context.gateway.class_reader import ClassReader
from living_context.gateway.indices import pool_index, tenant_index
from living_context.ingest import subjects
from living_context.ingest.writer import Writer
from living_context.keystore import Keystore
from tests.unit.fake_es import DocStore

T, P = "tena", "pzzzzzzzzzzzzzzz"
PKG = Path(__file__).resolve().parents[2] / "living_context"
STORED = ("_append_source", "_source_doc", "_get")   # calls whose result is a document read from (or just written to) storage


def _fn_index(tree):
    out = {}

    def visit(node, cls):
        for c in ast.iter_child_nodes(node):
            if isinstance(c, ast.ClassDef):
                visit(c, c.name)
            elif isinstance(c, ast.FunctionDef):
                out[c.name] = c
                visit(c, cls)
    visit(tree, None)
    return out


def _stored(name: str, fn, fns, depth=0) -> bool:
    """Is `name` in `fn` a source document read from storage: bound from a STORED call, from `<x>["_source"]` (a hit or a GET body),
    or a parameter of `fn` that every caller (in the module) passes such a document for."""
    for n in ast.walk(fn):
        if isinstance(n, (ast.Assign, ast.For)) and any(isinstance(t, ast.Name) and t.id == name for t in ast.walk(
                n.targets[0] if isinstance(n, ast.Assign) else n.target)):
            v = n.value if isinstance(n, ast.Assign) else None
            if isinstance(v, ast.Call) and isinstance(v.func, ast.Attribute) and v.func.attr in STORED:
                return True
            if isinstance(v, ast.Subscript) and isinstance(v.slice, ast.Constant) and v.slice.value == "_source":
                return True
            return False
    params = [a.arg for a in fn.args.args]
    if name in params and depth < 3:
        i = params.index(name) - 1   # without self
        calls = [c for f in fns.values() for c in ast.walk(f) if isinstance(c, ast.Call) and isinstance(c.func, ast.Attribute)
                 and c.func.attr == fn.name]
        callers = {id(c): f for f in fns.values() for c in ast.walk(f) if isinstance(c, ast.Call) and isinstance(c.func, ast.Attribute)
                   and c.func.attr == fn.name}
        return bool(calls) and all(len(c.args) > i and isinstance(c.args[i], ast.Name) and _stored(c.args[i].id, callers[id(c)], fns, depth + 1)
                                   for c in calls)
    return False


def projection_findings(sources: dict) -> list:
    """Class guard (d) over {module: source}: every leaf-index write is inside Writer._project; every _project call passes a stored
    source document; _project takes no provenance, lineage or sequence from its caller and derives provenance with source_prov(src)."""
    bad = []
    for module, src in sorted(sources.items()):
        tree = ast.parse(src)
        fns = _fn_index(tree)
        for fname, fn in fns.items():
            leaf_names = {t.id for n in ast.walk(fn) if isinstance(n, ast.Assign) for t in n.targets if isinstance(t, ast.Name)
                          and any(isinstance(c, ast.Call) and _name(c) in ("pool_index", "class_index") and c.args
                                  and isinstance(c.args[-1], ast.Constant) and c.args[-1].value == "leaf" for c in ast.walk(n.value))}
            for c in ast.walk(fn):
                if not isinstance(c, ast.Call):
                    continue
                if _name(c) == "_put" and c.args and (_names_leaf(c.args[0]) or (isinstance(c.args[0], ast.Name) and c.args[0].id in leaf_names)):
                    if fname != "_project":
                        bad.append(f"{module}:{fname}: writes a leaf index outside _project")
                if _name(c) == "_project" and fname != "_project":
                    arg = c.args[4] if len(c.args) > 4 else next((k.value for k in c.keywords if k.arg == "src"), None)
                    if not (isinstance(arg, ast.Name) and _stored(arg.id, fn, fns)):
                        bad.append(f"{module}:{fname}: _project called without a stored source document")
            if fname == "_project":
                params = {a.arg for a in fn.args.args}
                if params & {"prov", "tokens", "seq"} or "src" not in params:
                    bad.append(f"{module}:_project takes provenance, lineage or sequence from its caller")
                if not any(isinstance(c, ast.Call) and _name(c) == "source_prov" and [a.id for a in c.args if isinstance(a, ast.Name)] == ["src"]
                           for c in ast.walk(fn)):
                    bad.append(f"{module}:_project does not derive provenance from the source")
    return bad


def _name(c):
    f = c.func
    return f.id if isinstance(f, ast.Name) else f.attr if isinstance(f, ast.Attribute) else ""


def _names_leaf(node) -> bool:
    return any(isinstance(c, ast.Call) and _name(c) in ("pool_index", "class_index") and c.args and isinstance(c.args[-1], ast.Constant)
               and c.args[-1].value == "leaf" for c in ast.walk(node))


def package_sources() -> dict:
    return {p.relative_to(PKG).as_posix(): p.read_text() for p in sorted(PKG.rglob("*.py"))}


class Boom(Exception):
    pass


class OkGuard:
    def check(self, text):
        return subjects.OK


def prov(actor):
    return {"writer": "ptok", "writer_actor": actor, "writer_session": "s" * 32, "wrote_at": "2026-11-20T00:00:00+00:00",
            "mode": "save", "origin": actor}


class ResumeProvenance(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.es, self.ks = DocStore(), Keystore(Path(self.tmp.name) / "tena.db", T)
        self.stop_at = None

        def pause(step):
            if step == self.stop_at:
                self.stop_at = None
                raise Boom(step)

        self.w = Writer(lambda: self.es, self.ks, T, OkGuard(), reader=ClassReader(lambda cls: self.es, T), pause=pause)

    def tearDown(self):
        self.ks.close()
        self.tmp.cleanup()

    def leaf(self, leaf_id, lv=1):
        return self.es.docs[(pool_index(T, "base", P, "leaf"), f"{leaf_id}:{lv}")][0]

    def test_a_retry_by_another_actor_projects_the_stored_human_provenance(self):
        self.stop_at = "source"   # the source (with the human's provenance) is durable, the projection is not
        with self.assertRaises(Boom):
            self.w.remember("ptok", "base", "a fact the human saved", key="k1", pool=P, prov=prov("human"))
        res = self.w.remember("ptok", "base", "a fact the human saved", key="k1", pool=P, prov=prov("agent"))
        self.assertEqual(res.status, "stored")
        src = self.es.docs[(tenant_index(T, "src"), f"{res.leaf_id}:1")][0]
        self.assertIn('"origin": "human"', src["prov_json"])
        doc = self.leaf(res.leaf_id)
        self.assertEqual((doc.get("origin"), doc.get("writer_actor")), ("human", "human"))
        self.assertEqual(doc["lin_subjects"], src["lin_subjects"])
        self.assertEqual(doc["src_seq"], src["src_seq"])

    def test_guard_every_projection_path_takes_the_stored_source(self):
        srcs = package_sources()
        self.assertEqual(projection_findings(srcs), [])
        calls = [c for c in ast.walk(ast.parse(srcs["ingest/writer.py"])) if isinstance(c, ast.Call) and _name(c) == "_project"]
        self.assertGreaterEqual(len(calls), 3)   # save/resume/release, the pool repair and the class repair: the universe is not empty

    def test_guard_catches_planted_bypasses(self):
        srcs = package_sources()
        w = srcs["ingest/writer.py"]
        planted_call = w.replace("            self._project(cls, leaf_id, lv, text, src, pool)   # from the STORED source",
                                 "            self._project(cls, leaf_id, lv, text, {'prov_json': json.dumps(prov), 'src_seq': seq}, pool)   # from the STORED source")
        self.assertNotEqual(planted_call, w)
        self.assertIn("ingest/writer.py:_remember: _project called without a stored source document",
                      projection_findings(dict(srcs, **{"ingest/writer.py": planted_call})))
        planted_put = w + "\n\ndef leak(self, cls, pool, leaf_id, doc):\n    self._put(pool_index(self.tenant, cls, pool, 'leaf'), leaf_id, doc, cls=cls)\n"
        self.assertIn("ingest/writer.py:leak: writes a leaf index outside _project", projection_findings(dict(srcs, **{"ingest/writer.py": planted_put})))
        planted_sig = w.replace("def _project(self, cls, leaf_id, lv, text, src, pool=None) -> None:",
                                "def _project(self, cls, leaf_id, lv, text, src, pool=None, prov=None) -> None:")
        self.assertIn("ingest/writer.py:_project takes provenance, lineage or sequence from its caller",
                      projection_findings(dict(srcs, **{"ingest/writer.py": planted_sig})))

    def test_a_fresh_save_projects_what_it_stored(self):   # the control
        res = self.w.remember("ptok", "base", "an agent's note", key="k2", pool=P, prov=prov("agent"))
        self.assertEqual(self.leaf(res.leaf_id).get("origin"), "agent")


if __name__ == "__main__":
    unittest.main()
