"""Static registration contract; Python 3.9, stdlib only, no project imports.

Source: registration task. These checks compare independently discovered producers
and consumers, rather than treating an existing consumer list as the universe.
Wildcard coverage is checked as code, not assumed from a module's docstring.
This is a registration guard, not a proof of runtime erasure or authorization.
"""
import ast
import fnmatch
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[2] / "living_context"

# Intentional retained rows: executor.KEEP_KINDS must match this table exactly.
# They are still scanned and verified; only deletion/recount excludes them.
EXEMPT = {
    "manifest": "Pool manifest contains index ids/state needed for recovery, not content.",
    "tomb": "Erasure tombstone must survive to prevent restoration of erased data.",
    "counter": "Tenant sequence counter contains no subject content.",
    "idem_scope": "Tenant idempotency marker retains only a keyed body digest.",
}

# (kind/operation, missing registration, file, line) -> one-line explanation.
# Exact equality below rejects BOTH new gaps and fixed-but-not-removed entries.
# No registration gaps at this revision: retraction, pool repair and default-deny
# are already present. Add entries only after inspecting the missing consumer.
KNOWN_GAPS = {}

# Independent policy classification; never derive DENIED from all_ops - ALLOWED.
SCOPED_ALLOWED = frozenset({
    "ping", "pool_list", "pool_describe", "token_revoke", "token_renew",
    "token_delegate", "pool_save", "pool_update", "pool_query", "pool_fetch",
    "pool_holds", "pool_hold_show", "pool_release", "pool_retract",
})  # token_revoke is self-only; the additional request guard is checked below.
SCOPED_DENIED = frozenset({
    "search", "lookup", "write", "grant", "revoke", "remove_class",
    "break_glass_revoke", "offboard_tenant", "pool_create", "pool_detach",
    "pool_reattach", "pool_grant", "pool_ungrant", "pool_delete",
})


def parse(rel):
    return ast.parse((ROOT / rel).read_text(encoding="utf-8"), filename=rel)


def symbol(tree, name):
    matches = [n for n in ast.walk(tree)
               if isinstance(n, (ast.FunctionDef, ast.ClassDef)) and n.name == name]
    if len(matches) != 1:
        raise AssertionError("expected one symbol: " + name)
    return matches[0]


def strings(node):
    return {n.value for n in ast.walk(node)
            if isinstance(n, ast.Constant) and isinstance(n.value, str)}


def literal(node):
    if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
        raise AssertionError("unresolved kind at line %s: %s" %
                             (node.lineno, ast.dump(node)))
    return node.value


def evaluate(node, env):
    """Only the set expressions used by registration declarations; fail closed."""
    if isinstance(node, (ast.Tuple, ast.List, ast.Set)):
        return {literal(e) for e in node.elts}
    if isinstance(node, ast.Name):
        return env[node.id]
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "frozenset":
        return evaluate(node.args[0], env)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
        return evaluate(node.left, env) | evaluate(node.right, env)
    raise AssertionError("unresolved registration: " + ast.dump(node))


def declarations(tree, names, env=None):
    env = dict(env or {})
    for n in tree.body:
        if isinstance(n, ast.Assign):
            for target in n.targets:
                if isinstance(target, ast.Name) and target.id in names:
                    env[target.id] = evaluate(n.value, env)
    for name in names:
        if name not in env:
            raise AssertionError("missing declaration " + name)
    return env


def call_name(node):
    return getattr(node.func, "attr", getattr(node.func, "id", ""))


def row_kinds(tree):
    out = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Call) and call_name(n) == "_base":
            # Writer._base(kind, ...); pool_admin._base(tenant, cls, kind, ...).
            out.add(literal(n.args[0 if isinstance(n.func, ast.Attribute) else 2]))
        if isinstance(n, ast.Dict):
            for k, v in zip(n.keys, n.values):
                # A document, rather than a query filter or a response's pool type.
                if isinstance(k, ast.Constant) and k.value == "kind" and "lin_subjects" in {
                        x.value for x in n.keys if isinstance(x, ast.Constant)}:
                    if isinstance(v, ast.Name) and v.id == "kind":
                        continue  # _base's parameter, resolved at its callers above.
                    out.add(literal(v))
    return out


def index_calls(tree):
    out = {"class_index": set(), "tenant_index": set(), "pool_index": set()}
    for n in ast.walk(tree):
        if isinstance(n, ast.Call) and call_name(n) in out:
            arg = n.args[3 if call_name(n) == "pool_index" else 2 if call_name(n) == "class_index" else 1]
            if isinstance(arg, ast.Name) and arg.id == "kind":
                continue  # writer._cget forwarding, resolved separately below.
            out[call_name(n)].add(literal(arg))
    return out


def operations(tree):
    """Discover comparisons and mapping dispatch, including delegated handlers."""
    out = set()
    for fn in (n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)
               and n.name in ("handle", "_token_op")):
        for n in ast.walk(fn):
            if isinstance(n, ast.Compare) and isinstance(n.left, ast.Name) and n.left.id == "op":
                for right in n.comparators:
                    if isinstance(right, (ast.Constant, ast.Tuple, ast.List, ast.Set)):
                        out |= strings(right)
            if isinstance(n, ast.Subscript) and isinstance(n.value, ast.Dict):
                if isinstance(n.slice, ast.Name) and n.slice.id == "op":
                    out |= {literal(k) for k in n.value.keys}
    return out


def registration_gaps(rows, covered_rows, indexes, covered_indexes, ops,
                      actual_allowed, reads, served):
    """Pure checker, also exercised with fabricated registrations below."""
    gaps = set()
    for kind in rows - covered_rows:
        gaps.add((kind, "erasure row classification", "erasure/executor.py", 27))
    for kind in indexes - covered_indexes:
        gaps.add((kind, "erasure/verifier index coverage", "erasure/scan.py", 48))
    for op in ops - (SCOPED_ALLOWED | SCOPED_DENIED):
        gaps.add((op, "scoped classification", "gateway/handler.py", 125))
    for op in actual_allowed ^ SCOPED_ALLOWED:
        gaps.add((op, "scoped allow-list", "gateway/handler.py", 35))
    for kind in reads - served:
        gaps.add((kind, "class reader", "gateway/class_reader.py", 31))
    return gaps


class Registration(unittest.TestCase):
    def test_registrations(self):
        writer = parse("ingest/writer.py")
        # Include every pool module, token persistence and conversation persistence.
        producers = [writer, parse("gateway/session_state.py")] + [
            parse(str(p.relative_to(ROOT))) for p in sorted((ROOT / "gateway").glob("pool_*.py"))]
        rows = set().union(*(row_kinds(t) for t in producers))
        self.assertTrue({"src", "leaf", "retraction", "token", "pool"} <= rows)
        executor = parse("erasure/executor.py")
        erasure = declarations(executor, ("DELETE_KINDS", "REWRITE_KINDS", "KEEP_KINDS"))
        self.assertEqual(erasure["KEEP_KINDS"], set(EXEMPT))
        self.assertFalse(erasure["DELETE_KINDS"] & erasure["REWRITE_KINDS"])
        self.assertFalse((erasure["DELETE_KINDS"] | erasure["REWRITE_KINDS"]) & set(EXEMPT))
        physical = symbol(executor, "_physical")
        self.assertIn("DELETE_KINDS", {n.id for n in ast.walk(physical) if isinstance(n, ast.Name)})
        self.assertIn("REWRITE_KINDS", {n.id for n in ast.walk(physical) if isinstance(n, ast.Name)})
        recount = next(n for n in ast.walk(physical) if isinstance(n, ast.Assign)
                       and any(isinstance(t, ast.Name) and t.id == "remaining" for t in n.targets))
        self.assertIsInstance(recount.value, ast.ListComp)
        self.assertEqual(ast.unparse(recount.value.generators[0].ifs[0]),
                         "h.kind not in KEEP_KINDS | REWRITE_KINDS")
        self.assertIn("scan.find", ast.unparse(recount.value.generators[0].iter))
        self.assertIn("scan.find", ast.unparse(symbol(executor, "closure")))

        # config.py was inspected: settings/constants, no index-kind declaration.
        # Real builders and provisioning registry live in gateway/indices.py.
        parse("config.py")
        indices = declarations(parse("gateway/indices.py"), ("CLASS_KINDS", "TENANT_KINDS", "POOL_KINDS"))
        indexes = set().union(*indices.values())
        for tree in producers:
            for builder, kinds in index_calls(tree).items():
                registry = {"class_index": "CLASS_KINDS", "tenant_index": "TENANT_KINDS", "pool_index": "POOL_KINDS"}[builder]
                self.assertLessEqual(kinds, indices[registry], builder)
        self.assertEqual(declarations(parse("erasure/scan.py"), ("TENANT_WIDE",))["TENANT_WIDE"], indices["TENANT_KINDS"])

        # No finite index/row allow-list exists in these consumers. Prove their
        # all-tenant wildcard and unfiltered lineage path before crediting coverage.
        find = symbol(parse("erasure/scan.py"), "find")
        pattern = next(n.value for n in ast.walk(find) if isinstance(n, ast.Assign)
                       and any(isinstance(t, ast.Name) and t.id == "pattern" for t in n.targets))
        self.assertEqual(ast.unparse(pattern), 'f"lc-{check_id(tenant, \'tenant\')}-*"')
        self.assertIn("f'/{pattern}/_search?ignore_unavailable=true'", ast.unparse(find))
        self.assert_no_kind_filter(find)
        verifier = parse("verifier/check.py")
        self.assertEqual(ast.unparse(symbol(verifier, "_all").body[0].value), "f'lc-{self.tenant}-*'")
        for name in ("_lineage", "_query_only", "_query_shaped", "_text", "_by_id"):
            fn = symbol(verifier, name)
            self.assertIn("self._search(self._all,", ast.unparse(fn))
            self.assert_no_kind_filter(fn)
        # Derive coverage from the wildcard, separately from the producer set.
        # All three builder layouts (including physical suffixes) must match.
        def tenant_glob(node):
            value = next(n.value for n in ast.walk(node) if isinstance(n, ast.Assign)
                         and any(isinstance(t, ast.Name) and t.id == "pattern" for t in n.targets))
            return "".join(part.value if isinstance(part, ast.Constant) else "tenant"
                           for part in value.values)

        scan_pattern = tenant_glob(find)
        verifier_pattern = "".join(
            part.value if isinstance(part, ast.Constant) else "tenant"
            for part in symbol(verifier, "_all").body[0].value.values)
        covered_indexes = set()
        for registry, kinds in indices.items():
            prefix = {"TENANT_KINDS": "lc-tenant-", "CLASS_KINDS": "lc-tenant-class-",
                      "POOL_KINDS": "lc-tenant-class-pool-"}[registry]
            for kind in kinds:
                if all(fnmatch.fnmatchcase(prefix + kind + suffix, pattern)
                       for suffix in ("", "-p0123")
                       for pattern in (scan_pattern, verifier_pattern)):
                    covered_indexes.add(kind)
        self.assertTrue({"lin_subjects", "lin_leaf_ids", "lin_nodes"} <= strings(find))
        self.assertTrue({"lin_subjects", "lin_leaf_ids", "lin_nodes"} <= strings(symbol(verifier, "_lineage")))
        pool_docs = symbol(parse("erasure/pool_scope.py"), "_docs")
        self.assertIn("f'lc-{self.tenant}-*'", ast.unparse(pool_docs))
        self.assertTrue({"pool", "lin_pools"} <= strings(pool_docs))
        self.assert_no_kind_filter(pool_docs)
        self.assertIn("f'/lc-{tenant}-*/_search?ignore_unavailable=true'", ast.unparse(symbol(verifier, "pool_residue")))

        handler = parse("gateway/handler.py")
        admin = parse("gateway/admin.py")
        pool_admin = parse("gateway/pool_admin.py")
        env = declarations(admin, ("ADMIN_OPS",))
        env.update(declarations(pool_admin, ("POOL_OPS",)))
        env = declarations(handler, ("POOL_TOOLS", "SCOPED_OPS"), env)
        ops = env["ADMIN_OPS"] | env["POOL_OPS"] | env["POOL_TOOLS"]
        for tree in (handler, admin, pool_admin, parse("gateway/pool_tools.py")):
            ops |= operations(tree)
        self.assertFalse(SCOPED_ALLOWED & SCOPED_DENIED)
        self.assertEqual(SCOPED_ALLOWED | SCOPED_DENIED, ops)
        handle = symbol(handler, "handle")
        gate = next(n for n in handle.body if isinstance(n, ast.If) and "SCOPED_OPS" in ast.unparse(n.test))
        self.assertEqual(ast.unparse(gate.test), "rec.scope is not None and (op not in SCOPED_OPS or (op == 'token_revoke' and request.get('token_id') is not None))")
        self.assertEqual(ast.unparse(gate.body[0]), "return UNAVAILABLE")
        dispatch = next(n for n in handle.body if isinstance(n, ast.If) and ast.unparse(n.test) == "op in ADMIN_OPS")
        self.assertLess(gate.lineno, dispatch.lineno)

        served = declarations(parse("gateway/class_reader.py"), ("READABLE_KINDS",))["READABLE_KINDS"]
        send = symbol(parse("gateway/class_reader.py"), "_send")
        reader_guard = next(n for n in send.body if isinstance(n, ast.If)
                            and ast.unparse(n.test) == "kind not in READABLE_KINDS")
        self.assertIsInstance(reader_guard.body[0], ast.Raise)
        reads = set()
        for tree in producers:
            for n in ast.walk(tree):
                if not isinstance(n, ast.Call) or not isinstance(n.func, ast.Attribute):
                    continue
                receiver = ast.unparse(n.func.value)
                if receiver in ("self.reader", "self._reader"):
                    arg = n.args[1]
                    if isinstance(arg, ast.Name) and arg.id == "kind":
                        continue  # _cget forwarding: discover the callers instead.
                    reads.add(literal(arg))
                elif n.func.attr == "_cget":
                    reads.add(literal(n.args[1]))
        self.assertTrue(reads)
        gaps = registration_gaps(rows, set().union(*erasure.values()), indexes,
                                 covered_indexes, ops, env["SCOPED_OPS"], reads, served)
        self.assertEqual(gaps, set(KNOWN_GAPS),
                         "New gaps: %r; stale KNOWN_GAPS: %r" %
                         (sorted(gaps - set(KNOWN_GAPS)), sorted(set(KNOWN_GAPS) - gaps)))

    def assert_no_kind_filter(self, node):
        for n in ast.walk(node):
            if isinstance(n, ast.Dict):
                for key in n.keys:
                    if isinstance(key, ast.Constant):
                        self.assertNotEqual(key.value, "kind", "wildcard scan now filters row kinds")

    def test_repair_registers_pool_projection_path(self):
        writer = parse("ingest/writer.py")
        repair = symbol(writer, "repair")
        branch = next(n for n in ast.walk(repair) if isinstance(n, ast.If)
                      and ast.unparse(n.test) == "s.get('pool')")
        self.assertIn("self._repair_pool_source(s, h['_id'], out)", ast.unparse(branch))
        self.assertIsInstance(branch.body[-1], ast.Continue)
        helper = symbol(writer, "_repair_pool_source")
        self.assertEqual(index_calls(helper)["pool_index"],
                         declarations(parse("gateway/indices.py"), ("POOL_KINDS",))["POOL_KINDS"])
        self.assertIn("self._project", ast.unparse(helper))
        self.assertIn("self.advance_head", ast.unparse(helper))
        self.assertTrue({"src", "leaf", "ctl"} <= set().union(*index_calls(repair).values()))

    def test_checker_reports_fabricated_kind(self):
        gaps = registration_gaps({"fabricated_kind"}, set(), set(), set(),
                                 set(), SCOPED_ALLOWED, set(), set())
        self.assertIn(("fabricated_kind", "erasure row classification", "erasure/executor.py", 27), gaps)

    def test_checker_reports_fabricated_operation(self):
        gaps = registration_gaps(set(), set(), set(), set(), {"fabricated_op"},
                                 SCOPED_ALLOWED, set(), set())
        self.assertIn(("fabricated_op", "scoped classification", "gateway/handler.py", 125), gaps)


if __name__ == "__main__":
    unittest.main()
