"""Class guard, pools reviews C1/C7 (rounds 3 to 6): no Elasticsearch answer is accepted on its HTTP status.

The universe is enumerated, not listed: an AST walk over EVERY module of the package finds every `<client>.request(...)` call (and every
`.request` attribute that is not called, so the method cannot be passed around and called under another name). Each call site is one of

* ROUTED: the call is itself the starred argument of a validator, `v(*client.request(...), ...)`, where `v` is one of
  `gateway.reads.VALIDATORS` imported from gateway/reads.py into that module (or, in verifier/check.py, which shares no read path with
  the gateway by DESIGN 14.7, one of its own VERIFIER_VALIDATORS defined there). The raw answer is never bound to a name, so no path
  of the caller can read it unchecked. A local function with a validator's name does not count.
* ALLOWED: on the per-site allow-list below, keyed by (module, function, HTTP method), with the exact number of such calls and the
  reason a status-only answer there cannot change an authorization, erasure, verifier, taint, journal-retention or durability decision.

Nothing else passes. There is no "the status is compared" pass (round 5 permitted the F18 and F19 sites on exactly that ground), no
module-level exclusion (a new call site in any module needs a decision), and a method that is not a string literal fails. An allow-list
entry that no longer matches a call site fails too, so the list cannot rot.

Every validator and every request-owning helper of reads.py must also refuse the malformed-answer matrix of
tests/unit/test_reads_contract.py, so a new status-only helper inside reads.py cannot pass either.

No decision module may page with a `_doc` sort (review F12: it is unique only inside one shard); multi-page reads go through `reads.scan`.

`inventory()` is the enumeration the handoff reports (python3 -m tests.unit.test_read_paths --inventory).
"""
import ast
import sys
import unittest
from pathlib import Path

PKG = Path(__file__).resolve().parents[2] / "living_context"
READS = "gateway/reads.py"
VERIFIER = "verifier/check.py"
VERIFIER_VALIDATORS = ("_complete", "_refreshed", "_listed")
CURSOR_DIRS = ("erasure", "verifier", "gateway")
CURSOR_FILES = ("ingest/writer.py",)

_AFTER_BOUNDARY = ("after the erasure's boundary (the tombstone journal append, fsynced, is what a restart and a restore replay): this "
                   "document is a write-only mirror that no code reads (no reader of `tb:` or kind `tomb` in the package), so its answer "
                   "decides nothing, and raising here would leave an erasure that is journalled but not applied in memory")
_DERIVED = ("derived structure (DESIGN 5-7: summary trees, members, edges, generations): every document carries lineage, so the "
            "erasure closure finds it by `lin_*` scan whatever this answer said; no authorization, erasure, taint, journal-retention "
            "or durability decision is taken from it")
_RANKED = ("a recall read: a partial, failed or malformed answer gives fewer or other ranked candidates of the same class index, which "
           "the caller's key already may read; no authorization, erasure, taint or durability decision is taken from it")

# (module, function, method) -> (number of call sites, reason). Reviewed per site in round 6; see the handoff enumeration.
ALLOWED = {
    # --- reads.py --------------------------------------------------------------------------------------------------------------------
    ("gateway/reads.py", "scan", "DELETE"): (1, "closing the PIT after the scan has ended (best effort): an unclosed PIT expires after "
                                               "keep_alive; the scan's result was already validated page by page"),
    # --- erasure: after the boundary, best effort by design ------------------------------------------------------------------------
    ("erasure/executor.py", "Executor.erase_subject", "PUT"): (1, "the tb: mirror " + _AFTER_BOUNDARY),
    ("erasure/executor.py", "Executor.erase_subject", "POST"): (1, "refresh of the tb: mirror " + _AFTER_BOUNDARY),
    ("erasure/executor.py", "Executor.replay", "PUT"): (1, "the tb: mirror of a restore " + _AFTER_BOUNDARY),
    ("erasure/executor.py", "Executor.replay", "POST"): (1, "refresh of the tb: mirror of a restore " + _AFTER_BOUNDARY),
    ("erasure/executor.py", "Executor._record_and_publish", "PUT"): (1, "the tb: mirror of a relabel " + _AFTER_BOUNDARY),
    ("erasure/executor.py", "Executor._record_and_publish", "POST"): (1, "refresh of the tb: mirror of a relabel " + _AFTER_BOUNDARY),
    ("erasure/purge.py", "purge_index", "POST"): (2,
        "the refresh and flush before the expunge passes (best effort): the purge's `ok` is decided only by the validated deleted-docs "
        "readback after each validated force merge, so a refresh or flush that did nothing can only leave `ok` false"),
    # --- gateway --------------------------------------------------------------------------------------------------------------------
    ("gateway/keyowner.py", "provision_owner", "PUT"): (2,
        "operator provisioning of the key owner's role and user with the bootstrap credential: nothing is granted, certified or "
        "invalidated on this answer; a role or user that was not created makes the owner's first call (the validated key listing of "
        "rotate or invalidate_all) fail, which raises RotationError"),
    ("gateway/keyowner.py", "rotate", "GET"): (1,
        "BROKEN variant only (broken_needs_superuser): its answer is used for nothing but raising RotationError"),
    ("gateway/pool_tools.py", "PoolTools._pool_candidates", "POST"): (1,
        "recall candidates (_msearch) on the pool's own leaf index: every candidate is re-checked (its `pool` field, its head through the "
        "validated class-reader multi-get: live and current version) and the send hand-off re-checks the live grant, so a partial, "
        "failed or malformed answer only yields fewer candidates, never a wider, a staler or a retracted one"),
    ("gateway/provision.py", "provision_tenant", "GET"): (1,
        "the shard-budget estimate (_cat/shards), a capacity check and not an authorization one: a failed listing counts as the full "
        "budget (refused); a short one can only let an administrator provision over the budget, which the cluster bounds itself"),
    # --- ingest ---------------------------------------------------------------------------------------------------------------------
    ("ingest/writer.py", "Writer._remember", "POST"): (1,
        "visibility refresh after a projection whose write was validated (reads.doc_written): durability does not depend on it; a "
        "following read of the leaf goes by id, and the repair scans run the checked reads.refresh"),
    ("ingest/writer.py", "Writer._hold_tainted", "POST"): (1, "visibility refresh after a hold write validated by reads.doc_written"),
    ("ingest/writer.py", "Writer._release_held", "POST"): (1, "visibility refresh after a release write validated by reads.doc_written"),
    ("ingest/writer.py", "Writer.repair", "POST"): (1,
        "the trailing visibility refresh after every repair step was validated (the scans before it run the checked reads.refresh)"),
    # --- feedback (protocol log, notes): create-only rows and a flag update ---------------------------------------------------------
    ("feedback/log.py", "ProtocolLog._put", "PUT"): (1,
        "a protocol-log row (create-only): a row that did not land is counted in write_failures and is simply absent; it carries "
        "lineage, so the erasure closure finds it if it exists; no decision reads the write's answer"),
    ("feedback/log.py", "ProtocolLog._flip", "POST"): (1,
        "a usage flag (`opened`, `asked`) on a protocol-log row: feedback statistics only; no authorization, erasure or durability "
        "decision"),
    ("feedback/reflection.py", "NoteWriter.write", "PUT"): (1, "a reflection note (create-only, counted on failure); it carries lineage, "
                                                               "so the erasure closure finds it if it exists"),
    # --- model cache ----------------------------------------------------------------------------------------------------------------
    ("model/cache.py", "EsCacheStore.get", "GET"): (1,
        "the model-call cache of one class index (stub backend only): a miss recomputes; the entry is the output of the same call, read "
        "with the class's own key; no authorization, erasure, taint or durability decision"),
    ("model/cache.py", "EsCacheStore.put", "PUT"): (1,
        "a cache entry with the call's lineage: one that did not land is a later miss; the erasure closure finds the ones that did"),
    # --- recall -----------------------------------------------------------------------------------------------------------------------
    ("recall/direct.py", "DirectSearch.search", "POST"): (1, "class recall (_msearch): " + _RANKED),
    ("recall/lookup.py", "EsLeafStore._search", "POST"): (1, "class leaf lookup: " + _RANKED),
    ("recall/lookup.py", "EsLeafStore._node_answer", "POST"): (1, "tree node answer: " + _RANKED),
    ("recall/view.py", "EsView._get", "GET"): (1, "a pinned generation's manifest or node by id: " + _RANKED),
    ("recall/view.py", "EsView._feeders", "POST"): (1, "the feeders of a pinned view: " + _RANKED),
    ("recall/view.py", "EsView.pending", "POST"): (1, "the pending members of a view: " + _RANKED),
    # --- graph and structure ---------------------------------------------------------------------------------------------------------
    ("graph/edges.py", "EdgeStore._next_version", "GET"): (1, "edge version counter: " + _DERIVED),
    ("graph/edges.py", "EdgeStore._next_version", "PUT"): (2, "edge version counter compare-and-set: " + _DERIVED),
    ("graph/edges.py", "EdgeStore.live_exists", "GET"): (2, "edge liveness: " + _DERIVED),
    ("graph/edges.py", "EdgeStore.degree", "POST"): (1, "edge degree count: " + _DERIVED),
    ("graph/edges.py", "EdgeStore._supersedes_reaches", "POST"): (1, "supersession reachability: " + _DERIVED),
    ("graph/edges.py", "EdgeStore.link", "PUT"): (1, "an edge (create-only): " + _DERIVED),
    ("graph/edges.py", "EdgeStore.retire", "POST"): (1, "an edge retirement: " + _DERIVED),
    ("graph/edges.py", "LinkJob._search", "POST"): (1, "link-job candidates: " + _DERIVED),
    ("graph/edges.py", "LinkJob.run", "GET"): (1, "link-job source read: " + _DERIVED),
    ("graph/expand.py", "Expander._edges", "POST"): (1, "recall-time graph expansion: " + _RANKED),
    ("graph/expand.py", "Expander._degrees", "POST"): (1, "recall-time graph expansion: " + _RANKED),
    ("graph/expand.py", "Expander._leaves", "POST"): (1, "recall-time graph expansion: " + _RANKED),
    ("graph/expand.py", "Expander._add_partners", "POST"): (1, "recall-time graph expansion: " + _RANKED),
    ("structure/watermark.py", "WritePath.advance", "POST"): (1,
        "the refresh of the `wp` document after its compare-and-set was validated (reads.doc_written): P is read back by GET by id, which "
        "is real time, so whether `wp` is searchable decides nothing; the barrier refreshes before the advance are the checked "
        "reads.refresh"),
    ("structure/placement.py", "Placer._get", "GET"): (1, "placement read: " + _DERIVED),
    ("structure/publish.py", "Publisher._put", "PUT"): (1, "tree publication: " + _DERIVED),
    ("structure/publish.py", "Publisher._get", "GET"): (1, "tree publication read: " + _DERIVED),
    ("structure/publish.py", "Publisher.publish", "POST"): (1, "tree publication refresh: " + _DERIVED),
    ("structure/publish.py", "Publisher.publish", "DELETE"): (1, "tree publication cleanup: " + _DERIVED),
    ("structure/rebuild.py", "Rebuilder._release", "DELETE"): (1, "rebuild lease release: " + _DERIVED),
    ("structure/rebuild.py", "Rebuilder.leaf_recs", "POST"): (1, "rebuild input read: " + _DERIVED),
    ("structure/rebuild.py", "Rebuilder.bootstrap", "POST"): (1, "rebuild refresh: " + _DERIVED),
    ("structure/rebuild.py", "Rebuilder._pending", "POST"): (1, "rebuild pending read: " + _DERIVED),
    ("structure/rebuild.py", "Rebuilder._forwards", "POST"): (1, "rebuild forwards read: " + _DERIVED),
    ("structure/rebuild.py", "Rebuilder._attempt", "POST"): (1, "rebuild refresh: " + _DERIVED),
    ("structure/rebuild.py", "Rebuilder._write", "POST"): (1, "rebuild member update: " + _DERIVED),
    ("structure/rebuild.py", "Rebuilder._flip", "POST"): (1, "rebuild refresh before the head flip: " + _DERIVED),
    ("structure/rebuild.py", "Rebuilder._discard", "DELETE"): (1, "a discarded generation's document: " + _DERIVED),
    ("structure/rebuild.py", "Rebuilder._discard", "POST"): (3, "a discarded generation's refresh and member update: " + _DERIVED),
    ("structure/retention.py", "Retention.expire", "DELETE"): (1, "generation retention: " + _DERIVED),
    ("structure/retention.py", "Retention.expire", "POST"): (1, "generation retention refresh: " + _DERIVED),
    ("structure/retention.py", "Retention._drop_dead_members", "POST"): (2, "generation retention: " + _DERIVED),
    ("structure/retention.py", "Retention.discard_generation", "DELETE"): (1, "generation discard: " + _DERIVED),
    ("structure/retention.py", "Retention.discard_generation", "POST"): (6, "generation discard: " + _DERIVED),
}


def rel(p: Path) -> str:
    return p.relative_to(PKG).as_posix()


def package_modules() -> dict:
    """{relative path: source} of EVERY module of the package (no substring prefilter)."""
    return {rel(p): p.read_text() for p in sorted(PKG.rglob("*.py"))}


def reads_validators() -> tuple:
    import living_context.gateway.reads as r
    return tuple(r.VALIDATORS)


def _module_package(module: str) -> list:
    return ["living_context"] + module[:-3].split("/")[:-1]


def _absolute(module: str, node: ast.ImportFrom) -> str:
    if not node.level:
        return node.module or ""
    base = _module_package(module)[: len(_module_package(module)) - (node.level - 1)]
    return ".".join(base + ([node.module] if node.module else []))


def bindings(module: str, tree) -> tuple:
    """(names bound to a reads validator in `module`, names bound to the reads module itself)."""
    validators, mods = set(reads_validators()), set()
    names = {}
    for n in ast.walk(tree):
        if isinstance(n, ast.ImportFrom):
            src = _absolute(module, n)
            for a in n.names:
                if src == "living_context.gateway.reads" and a.name in validators:
                    names[a.asname or a.name] = a.name
                if src == "living_context.gateway" and a.name == "reads":
                    mods.add(a.asname or "reads")
        elif isinstance(n, ast.Import):
            for a in n.names:
                if a.name == "living_context.gateway.reads" and a.asname:
                    mods.add(a.asname)
    if module == READS:
        names.update({v: v for v in validators})
    if module == VERIFIER:
        names.update({v: v for v in VERIFIER_VALIDATORS})
    return names, mods


def _is_validator(func, names: dict, mods: set) -> bool:
    if isinstance(func, ast.Name):
        return func.id in names
    return isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name) and func.value.id in mods \
        and func.attr in reads_validators()


def sites(module: str, source: str) -> list:
    """(qualname, method, line, routed, problem) for every `.request` of the module."""
    tree = ast.parse(source)
    names, mods = bindings(module, tree)
    parent = {c: p for p in ast.walk(tree) for c in ast.iter_child_nodes(p)}
    out = []

    def qual_of(node):
        q, p = [], parent.get(node)
        while p is not None:
            if isinstance(p, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                q.append(p.name)
            p = parent.get(p)
        return ".".join(reversed(q))

    for n in ast.walk(tree):
        if not (isinstance(n, ast.Attribute) and n.attr == "request"):
            continue
        if isinstance(n.value, ast.Name) and n.value.id == "urllib":
            continue   # the standard-library module (gateway/es.py, the client itself)
        call = parent.get(n)
        if not (isinstance(call, ast.Call) and call.func is n):
            out.append((qual_of(n), "?", n.lineno, False, "a `.request` attribute that is not called: the method escapes the guard"))
            continue
        method = call.args[0].value if call.args and isinstance(call.args[0], ast.Constant) and isinstance(call.args[0].value, str) else "?"
        star, vcall = parent.get(call), parent.get(parent.get(call))
        routed = isinstance(star, ast.Starred) and isinstance(vcall, ast.Call) and star in vcall.args \
            and _is_validator(vcall.func, names, mods)
        problem = "" if method != "?" else "a request whose method is not a string literal"
        out.append((qual_of(call), method, call.lineno, routed, problem))
    return out


def verifier_validators_defined(source: str) -> list:
    tree = ast.parse(source)
    have = {f.name for f in tree.body if isinstance(f, ast.FunctionDef)}
    return [v for v in VERIFIER_VALIDATORS if v not in have]


def findings(modules: dict) -> list:
    """Every violation of the guard over `modules` ({relative path: source})."""
    bad, seen = [], {}
    import living_context.gateway.reads as r
    for v in r.VALIDATORS:
        f = getattr(r, v, None)
        if not callable(f):
            bad.append(f"reads.VALIDATORS names {v}, which reads.py does not define")
    if VERIFIER in modules:
        bad += [f"{VERIFIER}: validator {v} is not defined there" for v in verifier_validators_defined(modules[VERIFIER])]
    for module in sorted(modules):
        for qual, method, line, routed, problem in sites(module, modules[module]):
            if problem:
                bad.append(f"{module}:{line}: {qual}: {problem}")
            elif not routed:
                seen.setdefault((module, qual, method), []).append(line)
    for key, lines in sorted(seen.items()):
        if key not in ALLOWED:
            bad.append(f"{key[0]}:{lines[0]}: {key[1]} {key[2]}: an answer that is neither validated as it returns "
                       f"(v(*client.request(...)) with v from reads.VALIDATORS) nor on ALLOWED")
            continue
        n, reason = ALLOWED[key]
        if n != len(lines):
            bad.append(f"{key}: ALLOWED says {n} call sites, found {len(lines)} (lines {lines})")
        if not isinstance(reason, str) or len(reason) < 40:
            bad.append(f"{key}: an ALLOWED entry needs its reason")
    for key in sorted(set(ALLOWED) - set(seen)):
        if key[0] in modules:
            bad.append(f"{key}: on ALLOWED but no such unvalidated call site (stale entry)")
    for module in sorted(m for m in modules if m.split("/")[0] in CURSOR_DIRS or m in CURSOR_FILES):   # F12
        for n in ast.walk(ast.parse(modules[module])):
            if isinstance(n, ast.Dict) and any(isinstance(k, ast.Constant) and k.value == "_doc" for k in n.keys):
                bad.append(f"{module}:{n.lineno}: a `_doc` sort (not unique across shards or indices): use reads.scan")
    return bad


def inventory(modules: dict = None) -> list:
    """One row per call site: module:line, function, method, and how its answer is judged."""
    modules = modules or package_modules()
    rows = []
    for module in sorted(modules):
        tree = ast.parse(modules[module])
        parent = {c: p for p in ast.walk(tree) for c in ast.iter_child_nodes(p)}
        calls = {n.lineno: n for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                 and n.func.attr == "request"}
        for qual, method, line, routed, problem in sites(module, modules[module]):
            how = problem
            if routed:
                v = parent[parent[calls[line]]].func
                how = "validated: " + (v.id if isinstance(v, ast.Name) else f"{v.value.id}.{v.attr}")
            elif not problem:
                how = "ALLOWED: " + ALLOWED.get((module, qual, method), (0, "NOT LISTED"))[1][:90]
            rows.append(f"{module}:{line}\t{qual}\t{method}\t{how}")
    return rows


class ReadPaths(unittest.TestCase):
    def test_every_request_of_the_package_is_validated_or_allowed(self):
        self.assertEqual(findings(package_modules()), [])

    def test_the_universe_is_the_whole_package(self):
        mods = package_modules()
        every = [(m, s) for m in mods for s in sites(m, mods[m])]
        self.assertGreater(len(every), 100)
        self.assertGreaterEqual(sum(1 for _, s in every if s[3]), 35)   # validated sites exist, so `routed` is not always False
        self.assertTrue({m.split("/")[0] for m, _ in every} >= {"gateway", "erasure", "verifier", "ingest", "structure", "recall"})

    def plant(self, module, code, **extra):
        mods = package_modules()
        return findings(dict(mods, **{module: mods[module] + code}, **extra))

    def test_a_status_comparison_is_never_a_pass(self):
        # the F18 form (pool_admin._authz_put at 4e7ee85) and the F19 form (session_state.es_persist at 4e7ee85)
        for module, code in (
                ("gateway/pool_admin.py", "\n\ndef planted(c):\n    st, _ = c.request('PUT', '/lc-t-authz/_doc/x', {})\n"
                                          "    if st not in (200, 201):\n        raise RuntimeError\n"),
                ("gateway/session_state.py", "\n\ndef planted(c):\n    st, cur = c.request('GET', '/lc-t-ctl/_doc/x')\n"
                                             "    if st == 200 and cur.get('found'):\n        return cur\n")):
            with self.subTest(module):
                self.assertTrue(any(f.startswith(module + ":") and "planted" in f and "neither validated" in f
                                    for f in self.plant(module, code)))

    def test_a_validator_applied_after_binding_the_answer_is_not_a_pass(self):
        code = ("\n\ndef planted(es, idx):\n    st, r = es.request('POST', f'/{idx}/_search', {})\n"
                "    if st == 200:\n        return r['hits']['hits']\n    return complete_hits(st, r)\n")
        self.assertTrue(any("planted" in f for f in self.plant("ingest/writer.py", code)))

    def test_planted_unvalidated_search_is_caught(self):
        code = "\n\ndef planted(er, pattern, body):\n    return er.request('POST', f'/{pattern}/_search', body)[1]['hits']['hits']\n"
        self.assertTrue(any(f.startswith("erasure/scan.py:") and "planted POST" in f for f in self.plant("erasure/scan.py", code)))

    def test_a_local_function_named_like_a_validator_is_not_a_pass(self):
        code = ("\n\ndef complete_hits(st, r):\n    return []\n\n\ndef planted(es):\n"
                "    return complete_hits(*es.request('POST', '/x/_search', {}))\n")
        self.assertTrue(any("planted POST" in f for f in self.plant("recall/direct.py", code)))

    def test_a_routed_site_with_its_validator_removed_is_caught(self):
        mods = package_modules()
        for module, routed in (("gateway/pool_tools.py", "doc_read(*self.es_for(cls).request("),
                               ("gateway/handler.py", "mget_items(*self._es.client(tenant, f\"read-{cls}\").request(")):
            with self.subTest(module):
                src = mods[module]
                self.assertIn(routed, src)
                planted = dict(mods, **{module: src.replace(routed, "(lambda *a: a[1])(*" + routed.split("(*", 1)[1], 1)})
                self.assertTrue(any(f.startswith(module + ":") and "neither validated" in f for f in findings(planted)))

    def test_planted_new_module_is_caught(self):
        mods = dict(package_modules(), **{"recall/planted.py": "def f(es):\n    return es.request('GET', '/x/_doc/1')\n"})
        self.assertTrue(any(f.startswith("recall/planted.py:") for f in findings(mods)))

    def test_an_escaping_request_method_and_a_dynamic_method_are_caught(self):
        self.assertTrue(any("not called" in f for f in self.plant("gateway/pool_boot.py", "\n\ndef planted(c):\n    send = c.request\n"
                                                                                          "    return send('GET', '/x/_doc/1')\n")))
        self.assertTrue(any("not a string literal" in f for f in self.plant(
            "gateway/pool_boot.py", "\n\ndef planted(c, m):\n    from .reads import doc_read\n"
                                    "    return doc_read(*c.request(m, '/x/_doc/1'), 'x', '1')\n")))

    def test_stale_and_miscounted_allow_list_entries_are_caught(self):
        mods = package_modules()
        src = mods["model/cache.py"]
        self.assertIn('self._es_fn().request("GET"', src)
        gone = dict(mods, **{"model/cache.py": src.replace('self._es_fn().request("GET"', 'self._es_fn().fetch("GET"')})
        self.assertIn("('model/cache.py', 'EsCacheStore.get', 'GET'): on ALLOWED but no such unvalidated call site (stale entry)",
                      findings(gone))
        two = self.plant("model/cache.py", "\n\nclass EsCacheStore(EsCacheStore):\n    def get(self, cls, key):\n"
                                           "        return self._es_fn().request('GET', '/x/_doc/1')\n")
        self.assertTrue(any("ALLOWED says 1 call sites, found 2" in f for f in two), two)

    def test_planted_doc_cursor_is_caught(self):
        self.assertTrue(any(f.startswith("gateway/pool_boot.py:") and "`_doc` sort" in f
                            for f in self.plant("gateway/pool_boot.py", "\nBODY = {'sort': [{'_doc': 'asc'}]}\n")))

    def test_a_verifier_validator_must_be_defined_in_the_verifier(self):
        mods = package_modules()
        src = mods[VERIFIER]
        self.assertIn("def _listed(", src)
        planted = dict(mods, **{VERIFIER: src.replace("def _listed(", "def _listed_gone(")})
        self.assertIn(f"{VERIFIER}: validator _listed is not defined there", findings(planted))


if __name__ == "__main__":
    if "--inventory" in sys.argv:
        print("\n".join(inventory()))
    else:
        unittest.main()
