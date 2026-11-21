"""Class guard, review round 8 (F3): a module that builds authority from stored records reads them only through `reads.restored`.

F3 had one shape: a start-up path read the authz copy (`reads.scan`, then `hit["_source"]`) and built a grant, a pool, a pool grant
or a token from every hit, with no check that the hit was the record it claimed (index, id, tenant, kind). The validated way is
`reads.restore_scan` (a whole kind) or `reads.restored` (one hit or document): both judge the record before anything is built.

An AST walk over EVERY module of the package (no module list) finds each module that builds authority (it calls a SINK: a
constructor of a grant, a pool, a session record or a taint, or a restore of one) and that also reads stored records raw (it calls
`scan`, subscripts `["_source"]` or calls `.get("_source")`). Every such module must be on ALLOWED with the reason its raw reads never
become authority; an entry that no longer matches fails, so the list cannot rot. Planted modules check that the walk sees each form.

Stated limit: the walk is per module. A raw `_source` handed from one module to a sink in another is not seen; today the only modules
that read the authz copy at start-up (gateway/boot.py, gateway/pool_boot.py) are sink modules and read through `restore_scan`."""
import ast
import unittest

from tests.unit.test_read_paths import package_modules

SINKS = frozenset({"Principal", "Pool", "Record", "Taint", "restore", "restore_grant", "restore_token", "restore_token_revoke",
                   "loaded", "confirm_against_authz"})

ALLOWED = {
    "gateway/pool_tools.py": (
        {"Taint"}, {"_source"},
        "content reads only: the leaf text and titles of a pool (`_leaf` through `reads.doc_read`, the search fusion filtered by the "
        "hit's own `pool`); its one Taint (`private_taint`) is built from the pool directory in memory, never from a stored record"),
}


def _name(func) -> str:
    return func.attr if isinstance(func, ast.Attribute) else func.id if isinstance(func, ast.Name) else ""


def raw_reads(tree) -> set:
    """The raw-record forms a module uses: `scan` (a call of `reads.scan`), `_source` (a subscript or a `.get`)."""
    out = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Call):
            if _name(n.func) == "scan":
                out.add("scan")
            if _name(n.func) == "get" and n.args and isinstance(n.args[0], ast.Constant) and n.args[0].value == "_source":
                out.add("_source")
        if isinstance(n, ast.Subscript) and isinstance(n.slice, ast.Constant) and n.slice.value == "_source":
            out.add("_source")
    return out


def sinks(tree) -> set:
    return {_name(n.func) for n in ast.walk(tree) if isinstance(n, ast.Call)} & SINKS


def violations(modules: dict) -> dict:
    """{module: (sinks, raw forms)} for every module that builds authority and reads records raw."""
    out = {}
    for mod, src in modules.items():
        if mod == "gateway/reads.py":
            continue   # the judge itself
        tree = ast.parse(src)
        s, r = sinks(tree), raw_reads(tree)
        if s and r:
            out[mod] = (s, r)
    return out


class RestorePaths(unittest.TestCase):
    def test_no_module_builds_authority_from_a_record_it_read_raw(self):
        found = violations(package_modules())
        unexpected = {m: v for m, v in found.items() if m not in ALLOWED}
        self.assertEqual(unexpected, {}, "a module builds authority from a raw record: read it through reads.restore_scan or "
                                         "reads.restored, or add it to ALLOWED with the reason")
        for mod, (s, r, why) in ALLOWED.items():
            with self.subTest(mod):
                self.assertIn(mod, found, "a stale ALLOWED entry")
                self.assertEqual(found[mod], (s, r), "an ALLOWED entry no longer matches its module")
                self.assertTrue(why)

    def test_the_start_up_readers_go_through_the_judge(self):
        mods = package_modules()
        for mod in ("gateway/boot.py", "gateway/pool_boot.py", "gateway/session_state.py"):
            with self.subTest(mod):
                names = {_name(n.func) for n in ast.walk(ast.parse(mods[mod])) if isinstance(n, ast.Call)}
                self.assertTrue(names & {"restore_scan", "restored"}, "the start-up reader does not use the judge")
                self.assertFalse(raw_reads(ast.parse(mods[mod])))

    def test_planted_raw_restore_paths_are_seen(self):
        planted = {
            "a _source subscript": "def load(hits, d):\n    for h in hits:\n        d.restore_grant(h['_source']['principal'])\n",
            "a _source get": "def load(h):\n    return Principal(h.get('_source'))\n",
            "a raw scan": "from .reads import scan\ndef load(c, d):\n    for s in scan(c, 'i', {}):\n        d.restore(s)\n",
            "a token restore": "def load(hits, s):\n    [s.restore_token(h['_source']['hash'], h) for h in hits]\n",
            "a pool built": "def load(h):\n    return pl.Pool(**h['_source'])\n",
        }
        for name, src in planted.items():
            with self.subTest(name):
                self.assertIn("gateway/planted.py", violations({"gateway/planted.py": src}))
        clean = "from .reads import restore_scan\ndef load(c, d):\n    for s in restore_scan(c, 'i', 't', {})[0]:\n        d.restore(s)\n"
        self.assertEqual(violations({"gateway/planted.py": clean}), {})


if __name__ == "__main__":
    unittest.main()
