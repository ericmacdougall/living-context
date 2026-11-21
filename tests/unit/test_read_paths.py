"""Class guard (a), review C1/C7: every Elasticsearch request of the decision modules is a validated read or a commented exception.

The universe is enumerated, not listed: an AST walk over every module of the package finds every `<client>.request(...)` call.

* A module with a request call is either GUARDED (the authz, erasure, verifier, start-up, pool, gateway and ingest modules: they take
  authorization, revocation, erasure, residue and repair decisions from what Elasticsearch answers) or on EXCLUDED with its reason. A
  new module with a request call fails the test until it is classified.
* In a GUARDED module each call site is VALIDATED: its answer goes straight into a validator (`reads.complete_hits`,
  `reads.complete_count`, or in the verifier, which shares no read path with the gateway by DESIGN 14.7, its own `_complete` and
  `_refreshed`), or is assigned to `(status, body)` names that the same function passes to one. Or it is on ALLOWED, keyed by
  (module, function, HTTP method), with the number of such calls and the reason. A `checked` reason is itself verified: the status the
  call returns must be compared in the function. The match is exact: an unlisted call site fails, and so does a stale entry.

No decision module may page with a `_doc` sort (review F12: it is unique only inside one shard); multi-page reads go through
`reads.scan` (a PIT and `_shard_doc`).

The planted-bypass tests run the same checker over a copy of a guarded module with one extra unvalidated search, a validator call
removed, a status discarded, a `_doc` cursor, or a new unclassified module, and require each to be reported.
"""
import ast
import unittest
from pathlib import Path

PKG = Path(__file__).resolve().parents[2] / "living_context"

GUARDED_DIRS = ("erasure", "verifier", "gateway")
GUARDED_FILES = ("ingest/writer.py",)

# Modules with request calls that take no authorization, revocation, erasure, residue or repair decision from a read. The pools review
# (C1/C7) scoped the class to the decision modules above; these are recall and structure-building paths, where a partial answer
# changes a ranked result or a derived structure, not who may see what or what was erased.
EXCLUDED = {
    "feedback/log.py": "protocol-log rows: create-only writes and a flag update; no read decides anything",
    "feedback/reflection.py": "note writer: a create-only write",
    "graph/edges.py": "edge versioning and link jobs (derived structure; its CAS writes carry if_seq_no)",
    "graph/expand.py": "recall-time graph expansion: a ranked answer",
    "model/cache.py": "model-call cache: a miss on a failed read recomputes",
    "recall/direct.py": "class recall search: a ranked answer",
    "recall/lookup.py": "class lookup reads: a ranked answer",
    "recall/view.py": "recall view reads: a ranked answer",
    "structure/placement.py": "structure placement reads (derived structure)",
    "structure/publish.py": "structure publication (derived structure)",
    "structure/rebuild.py": "structure rebuild (derived structure; generations are gated)",
    "structure/retention.py": "structure retention (derived structure)",
    "structure/watermark.py": "write-path watermark (its CAS writes carry if_seq_no)",
}

VALIDATORS = {"complete_hits", "complete_count"}
VERIFIER_VALIDATORS = {"_complete", "_refreshed"}   # the verifier's own (DESIGN 14.7), accepted only in verifier/check.py

# (module, function, method) -> (number of call sites, kind, reason). kind "checked": the status is compared in the function (verified);
# "write": a document write or delete; "best-effort": the result is intentionally not used.
ALLOWED = {
    # --- reads.py: the helpers themselves ----------------------------------------------------------------------------------------
    ("gateway/reads.py", "refresh", "POST"): (1, "checked", "the checked refresh: status and _shards.failed are verified"),
    ("gateway/reads.py", "aliases", "GET"): (1, "checked", "the checked exact-name alias lookup: a well-formed 200 about the name, or a 404 naming it, else it raises"),
    ("gateway/reads.py", "scan", "POST"): (1, "checked", "opening the PIT: status, id and _shards are verified (the page search is validated)"),
    ("gateway/reads.py", "scan", "DELETE"): (1, "best-effort", "closing the PIT: an unclosed PIT expires after keep_alive"),
    # --- erasure ------------------------------------------------------------------------------------------------------------------
    ("erasure/executor.py", "Executor.erase_subject", "POST"): (2, "best-effort",
        "refreshes: the first is redundant (every closure read is scan.find, which runs the checked reads.refresh); the second makes "
        "the tb: mirror visible, and the journal append before it is the durable boundary"),
    ("erasure/executor.py", "Executor.erase_subject", "PUT"): (1, "best-effort",
        "the tb: document mirrors the tombstone journal record appended just before it, which is the durable boundary"),
    ("erasure/executor.py", "Executor.replay", "POST"): (2, "best-effort", "as erase_subject: redundant refresh, then the tb: mirror"),
    ("erasure/executor.py", "Executor.replay", "PUT"): (1, "best-effort", "as erase_subject: the tb: mirror of the journal record"),
    ("erasure/executor.py", "Executor.relabel_erase", "POST"): (1, "best-effort", "redundant: class_closure reads through scan.find"),
    ("erasure/executor.py", "Executor.replay_relabel", "POST"): (1, "best-effort", "redundant: class_closure reads through scan.find"),
    ("erasure/executor.py", "Executor._record_and_publish", "PUT"): (1, "best-effort", "the tb: mirror of the journal record"),
    ("erasure/executor.py", "Executor._record_and_publish", "POST"): (1, "best-effort", "makes the tb: mirror visible"),
    ("erasure/executor.py", "Executor._physical.one", "POST"): (1, "best-effort",
        "refresh before a purge; the purge reports what is left (checked stats read) and the verifier decides residue"),
    ("erasure/executor.py", "Executor._physical", "POST"): (1, "best-effort", "redundant: the residue read after it is scan.find"),
    ("erasure/executor.py", "Executor._rewrite_node", "GET"): (1, "checked",
        "a node to rewrite; a failed read counts 0 rewritten and the final scan.find residue read (complete) reports what remains"),
    ("erasure/executor.py", "Executor._rewrite_node", "PUT"): (1, "checked", "the CAS rewrite (if_seq_no); a failure counts 0, as above"),
    ("erasure/executor.py", "Executor._strip_or_delete_log_row", "GET"): (1, "checked", "a failed read raises ScanError"),
    ("erasure/executor.py", "Executor._strip_or_delete_log_row", "PUT"): (1, "checked", "a failed CAS rewrite raises ScanError"),
    ("erasure/pool_scope.py", "PoolEraser._del", "DELETE"): (1, "checked", "anything but 200 or 404 raises PoolEraseError"),
    ("erasure/purge.py", "deleted_count", "GET"): (1, "checked", "a failed stats read raises PurgeError"),
    ("erasure/purge.py", "ensure_expunge_threshold", "PUT"): (1, "checked", "if refused, the setting is read back and must be 0"),
    ("erasure/purge.py", "ensure_expunge_threshold", "GET"): (1, "checked", "the read-back; anything but 0 raises PurgeError"),
    ("erasure/purge.py", "purge_index", "POST"): (3, "checked",
        "refresh and flush are best effort before the merge; the forcemerge status is checked and the deleted count is read back"),
    ("erasure/scan.py", "delete", "DELETE"): (1, "checked", "anything but 200 or 404 raises ScanError"),
    ("erasure/scan.py", "find_text", "POST"): (2, "best-effort",
        "NOT the closure: the string-search demonstration of E2, used only by the executor's broken variant"),
    # --- verifier -------------------------------------------------------------------------------------------------------------------
    ("verifier/check.py", "pool_residue", "GET"): (1, "checked",
        "the index listing (_resolve): anything but 200 with an `indices` list raises VerifierError"),
    # --- gateway --------------------------------------------------------------------------------------------------------------------
    ("gateway/boot.py", "grant_persister.persist", "PUT"): (1, "checked", "a failed grant write raises"),
    ("gateway/handler.py", "Gateway._lookup", "POST"): (1, "checked",
        "class lookup by id (_mget): ids of the caller's read set; a failed read answers unavailable, a missing doc is not offered"),
    ("gateway/keyowner.py", "provision_owner", "PUT"): (2, "checked", "administrator provisioning of the key owner: a failure raises"),
    ("gateway/keyowner.py", "_owned_key_ids", "GET"): (1, "checked", "a failed key listing raises RotationError"),
    ("gateway/keyowner.py", "invalidate", "DELETE"): (1, "checked", "a failed invalidation raises RotationError"),
    ("gateway/keyowner.py", "rotate", "GET"): (1, "checked", "the key owner's identity read; a failure raises RotationError"),
    ("gateway/keyowner.py", "rotate", "POST"): (1, "checked", "a failed key creation raises RotationError"),
    ("gateway/pool_admin.py", "PoolService._authz_put", "PUT"): (1, "checked", "a failed authz write is reported to the caller"),
    ("gateway/pool_admin.py", "PoolService._authz_delete", "DELETE"): (1, "checked", "a failed authz delete is reported to the caller"),
    ("gateway/pool_admin.py", "PoolService.create", "PUT"): (1, "write",
        "the pool manifest; a failure is caught by create's handler, which drops the pool and answers unavailable"),
    ("gateway/pool_boot.py", "token_persister.persist", "PUT"): (1, "checked", "a failed token write raises"),
    ("gateway/pool_boot.py", "load", "DELETE"): (1, "best-effort", "deleting an expired token record: the record is ignored anyway"),
    ("gateway/pool_tools.py", "PoolTools._leaf", "GET"): (1, "checked", "anything but 200 is no leaf: every caller then fails closed"),
    ("gateway/pool_tools.py", "PoolTools._pool_candidates", "POST"): (1, "checked",
        "recall candidates (_msearch): a ranked answer; a failed or partial response gives fewer candidates, never wider access"),
    ("gateway/provision.py", "provision_tenant", "GET"): (2, "checked", "administrator provisioning: a failure raises"),
    ("gateway/provision.py", "provision_tenant", "PUT"): (2, "checked", "administrator provisioning: a failure raises"),
    ("gateway/provision.py", "provision_tenant", "POST"): (1, "checked", "administrator provisioning: a failure raises"),
    ("gateway/session_state.py", "es_persist.persist", "GET"): (1, "checked", "the taint CAS read; 404 is a fresh record, anything else raises"),
    ("gateway/session_state.py", "es_persist.persist", "PUT"): (1, "checked", "the taint CAS write; a conflict or failure raises"),
    ("gateway/session_state.py", "es_load.load", "GET"): (1, "checked", "404 is no record; anything else but 200 raises"),
    # --- ingest ---------------------------------------------------------------------------------------------------------------------
    ("ingest/writer.py", "Writer._put", "PUT"): (1, "checked", "the one document write: a failure raises WriterError"),
    ("ingest/writer.py", "Writer._get", "GET"): (1, "checked", "404 is None; anything else but 200 raises"),
    ("ingest/writer.py", "Writer._remember", "POST"): (1, "best-effort", "refresh after a projection write (visibility only)"),
    ("ingest/writer.py", "Writer.quarantine_list", "POST"): (1, "best-effort", "refresh before the validated quarantine listing"),
    ("ingest/writer.py", "Writer._hold_tainted", "POST"): (1, "best-effort", "refresh after a hold write (visibility only)"),
    ("ingest/writer.py", "Writer._release_held", "POST"): (1, "best-effort", "refresh after a release write (visibility only)"),
    ("ingest/writer.py", "Writer.repair", "POST"): (2, "checked",
        "the source lookup (_mget): status, shape and every per-document answer are verified, only found:false deletes; and the "
        "trailing refresh (visibility only; the scans before it use the checked reads.refresh)"),
    ("ingest/writer.py", "Writer.repair", "DELETE"): (1, "checked", "deleting an orphan projection: a failure raises"),
}


def rel(p: Path) -> str:
    return p.relative_to(PKG).as_posix()


def request_modules() -> dict:
    return {rel(p): p.read_text() for p in sorted(PKG.rglob("*.py")) if ".request(" in p.read_text()}


def guarded(module: str) -> bool:
    return module.split("/")[0] in GUARDED_DIRS or module in GUARDED_FILES


def _name(f) -> str:
    return f.id if isinstance(f, ast.Name) else f.attr if isinstance(f, ast.Attribute) else ""


def _binds(node) -> set:
    """Names an assignment-like statement binds."""
    targets = node.targets if isinstance(node, ast.Assign) else [node.target] if isinstance(node, (ast.AugAssign, ast.AnnAssign, ast.For)) else []
    return {n.id for t in targets for n in ast.walk(t) if isinstance(n, ast.Name)}


def local_validators(tree, base: set) -> set:
    """`base` plus every function of the module that passes its first two parameters, in order, to a validator (a wrapper such as
    the writer's `_page`), transitively."""
    out, grew = set(base), True
    while grew:
        grew = False
        for f in ast.walk(tree):
            if isinstance(f, (ast.FunctionDef, ast.AsyncFunctionDef)) and f.name not in out:
                params = [a.arg for a in f.args.args if a.arg not in ("self", "cls")][:2]
                if len(params) == 2 and any(isinstance(n, ast.Call) and _name(n.func) in out and len(n.args) >= 2
                                            and all(isinstance(a, ast.Name) for a in n.args[:2]) and [a.id for a in n.args[:2]] == params
                                            for n in ast.walk(f)):
                    out.add(f.name)
                    grew = True
    return out


def call_sites(module: str, source: str) -> list:
    """(qualname, method, validated, status_compared) of every `.request(` call of the module."""
    tree = ast.parse(source)
    validators = local_validators(tree, VALIDATORS | (VERIFIER_VALIDATORS if module == "verifier/check.py" else set()))
    out = []

    def visit(node, qual, fn):
        for child in ast.iter_child_nodes(node):
            q, f = qual, fn
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                q = qual + [child.name]
                f = child if not isinstance(child, ast.ClassDef) else fn
            if isinstance(child, ast.Call) and isinstance(child.func, ast.Attribute) and child.func.attr == "request":
                out.append(classify(child, q, f, validators))
            visit(child, q, f)

    visit(tree, [], None)
    return out


def classify(call, qual, fn, validators):
    """A call is validated when its answer goes straight into a validator, or when it is assigned to `(status, body)` names and a
    validator call with exactly those names follows it before either name is bound again."""
    method = call.args[0].value if call.args and isinstance(call.args[0], ast.Constant) else "?"
    nodes = list(ast.walk(fn)) if fn is not None else []
    validated, status = False, None
    for n in nodes:
        if isinstance(n, ast.Call) and _name(n.func) in validators and any(call in list(ast.walk(a)) for a in n.args):
            validated = True
    assign = next((n for n in nodes if isinstance(n, ast.Assign) and n.value is call and isinstance(n.targets[0], ast.Tuple)
                   and len(n.targets[0].elts) == 2 and all(isinstance(e, ast.Name) for e in n.targets[0].elts)), None)
    if assign is not None:
        status, body = (e.id for e in assign.targets[0].elts)
        at = (assign.end_lineno, assign.end_col_offset)
        rebinds = sorted((n.end_lineno, n.end_col_offset) for n in nodes if n is not assign and _binds(n) & {status, body}
                         and (n.end_lineno, n.end_col_offset) > at)   # a statement binds once its value is evaluated
        until = rebinds[0] if rebinds else (10 ** 9, 0)
        for m in nodes:
            if isinstance(m, ast.Call) and _name(m.func) in validators and len(m.args) >= 2 and \
                    all(isinstance(a, ast.Name) for a in m.args[:2]) and [a.id for a in m.args[:2]] == [status, body] and \
                    at < (m.lineno, m.col_offset) < until:
                validated = True
    compared = status is not None and status != "_" and any(
        isinstance(n, ast.Compare) and any(isinstance(x, ast.Name) and x.id == status for x in [n.left, *n.comparators])
        for n in nodes)
    return ".".join(qual), method, validated, compared


def findings(modules: dict) -> list:
    """Every violation of the guard over `modules` ({relative path: source})."""
    bad = []
    for module in sorted(set(modules) - set(EXCLUDED)):
        if not guarded(module):
            bad.append(f"{module}: has ES requests but is neither guarded nor on EXCLUDED")
    for module in sorted(EXCLUDED):
        if module not in modules:
            bad.append(f"{module}: on EXCLUDED but has no ES request (stale entry)")
        elif guarded(module):
            bad.append(f"{module}: a decision module cannot be excluded")
    for module in sorted(m for m in modules if guarded(m)):   # the cursor rule (F12): no `_doc` sort in a decision module
        for n in ast.walk(ast.parse(modules[module])):
            if isinstance(n, ast.Dict) and any(isinstance(k, ast.Constant) and k.value == "_doc" for k in n.keys):
                bad.append(f"{module}:{n.lineno}: a `_doc` sort (not unique across shards or indices): use reads.scan")
    seen = {}
    for module in sorted(m for m in modules if guarded(m)):
        for qual, method, validated, compared in call_sites(module, modules[module]):
            if validated:
                continue
            key = (module, qual, method)
            seen.setdefault(key, []).append(compared)
    for key, comps in sorted(seen.items()):
        if key not in ALLOWED:
            bad.append(f"{key}: an unvalidated ES request that is not on ALLOWED")
            continue
        n, kind, _ = ALLOWED[key]
        if n != len(comps):
            bad.append(f"{key}: ALLOWED says {n} call sites, found {len(comps)}")
        if kind == "checked" and not any(comps):
            bad.append(f"{key}: listed as checked but no status of these calls is compared")
    for key, (n, _, _) in ALLOWED.items():
        if n and key not in seen and key[0] in modules:
            bad.append(f"{key}: on ALLOWED but no such unvalidated call site (stale entry)")
    return bad


class ReadPaths(unittest.TestCase):
    def test_every_request_of_the_decision_modules_is_validated_or_allowed(self):
        self.assertEqual(findings(request_modules()), [])

    def test_the_universe_is_not_empty(self):
        mods = request_modules()
        sites = [s for m in mods if guarded(m) for s in call_sites(m, mods[m])]
        self.assertGreater(len(sites), 60)
        self.assertGreaterEqual(sum(1 for s in sites if s[2]), 8)   # validated call sites exist, so `validated` is not always False

    def test_planted_unvalidated_search_is_caught(self):
        mods = request_modules()
        planted = dict(mods)
        planted["erasure/pool_scope.py"] = mods["erasure/pool_scope.py"] + (
            "\n\ndef planted(er, pattern, body):\n"
            "    st, r = er.request('POST', f'/{pattern}/_search', body)\n"
            "    if st != 200:\n"
            "        raise RuntimeError\n"
            "    return r['hits']['hits']\n")
        self.assertIn("('erasure/pool_scope.py', 'planted', 'POST'): an unvalidated ES request that is not on ALLOWED", findings(planted))

    def test_planted_removed_validator_is_caught(self):
        mods = request_modules()
        src = mods["verifier/check.py"]
        self.assertIn('return _complete(st, r, "direct read")["hits"]["hits"]', src)
        planted = dict(mods, **{"verifier/check.py": src.replace('return _complete(st, r, "direct read")["hits"]["hits"]',
                                                                  'return r["hits"]["hits"]')})
        self.assertTrue(any("verifier/check.py', 'Verifier._search'" in f for f in findings(planted)))

    def test_planted_doc_cursor_is_caught(self):
        mods = request_modules()
        planted = dict(mods, **{"gateway/pool_boot.py": mods["gateway/pool_boot.py"] + "\nBODY = {'sort': [{'_doc': 'asc'}]}\n"})
        self.assertTrue(any(f.startswith("gateway/pool_boot.py:") and "`_doc` sort" in f for f in findings(planted)))

    def test_planted_new_module_is_caught(self):
        mods = dict(request_modules(), **{"recall/planted.py": "def f(es):\n    return es.request('GET', '/x/_doc/1')\n"})
        self.assertIn("recall/planted.py: has ES requests but is neither guarded nor on EXCLUDED", findings(mods))

    def test_a_listed_checked_site_must_compare_its_status(self):
        mods = request_modules()
        src = mods["erasure/scan.py"]
        line = 'st, _ = es.request("DELETE", f"/{index}/_doc/{doc_id}")'
        self.assertIn(line, src)
        planted = dict(mods, **{"erasure/scan.py": src.replace(line, 'es.request("DELETE", f"/{index}/_doc/{doc_id}"); st = 200')})
        self.assertIn("('erasure/scan.py', 'delete', 'DELETE'): listed as checked but no status of these calls is compared",
                      findings(planted))


if __name__ == "__main__":
    unittest.main()
