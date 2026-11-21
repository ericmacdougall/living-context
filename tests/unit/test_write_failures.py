"""Class guard, pools review C7 (round 7): a refused or failed write is never caught with the caller told ok.

F18 (round 6, the pool lane) and the admin-lane gap (round 7) had one shape: the write of a decision's durable copy raised, a handler
caught it, counted it, and the call answered ok, so the decision was gone after a restart. The universe is enumerated, not listed: an
AST walk over EVERY module of the package finds every WRITE, a call of a persister (a callable whose name contains `persist`, the
write helpers of gateway/reads.py `put_doc`, `put_doc_landed` and `delete_doc`, or the pool lane's `_authz_put` and `_authz_delete`),
and reports a SITE where a failure of that write can reach the success path:

* a `try` whose body makes a write and that has a handler which neither raises nor returns a failure answer (it falls through, or it
  returns ok: `OK_ANSWER`, a dict with `"ok": True`, `True`, a bare `return`);
* a `with suppress(...)` (contextlib) around a write;
* a `put_doc(...)` whose result is discarded (False is a typed version conflict: nothing landed). `delete_doc`'s False is a verified
  absence, which is what a delete wants, and any other answer raises.

Every site must be on ALLOWED, keyed by (module, function), with the exact count and the reason a lost write there cannot reverse a
decision; an entry that no longer matches fails, so the list cannot rot. No function of REDUCTION_PATHS may hold a site at all, even
an allowed one. On the class-grant lane the one call that may still fall through, `GrantStore.put`, refuses a reduction (tests/security/
test_admin_reductions.py), and `Admin` calls it only for an addition or for the rollback of a reduction whose epoch bump failed (in a
handler that re-raises); that is pinned below."""
import ast
import sys
import unittest

from tests.unit.test_read_paths import package_modules

WRITE_HELPERS = frozenset({"put_doc", "put_doc_landed", "delete_doc", "_authz_put", "_authz_delete"})

_ADDITION = ("an ADDITION, best effort by design: a write that does not land loses the new access at the next start (fail safe); it "
             "takes nothing away, so no decision is reversed")
ALLOWED = {
    ("gateway/admin.py", "GrantStore.put"): (1, _ADDITION + "; `put` refuses any change that takes something away (that is "
                                                            "`GrantStore.reduce`, journalled before the answer)"),
    ("gateway/pool_admin.py", "PoolService.grant"): (1, _ADDITION + " (a pool grant; TICKET_LATER from the round-7 triage)"),
    ("gateway/revocation.py", "RevocationService.journalled"): (
        1, "the persist runs AFTER the journal record was appended and fsynced; the record is the change's durable form (replayed at "
           "start-up, kept by compaction until a validated write confirms it), and the receipt carries `persisted: False`"),
    ("gateway/revocation.py", "RevocationService._flip"): (
        1, "a revoke or epoch bump: the epoch journal record is durable before the persist, and the journal, not this copy, is what a "
           "restart replays; the receipt carries `persisted: False`"),
    ("gateway/sessions.py", "Sessions._persist"): (
        1, "the token copy: the journal (revokes, epoch bumps) is what ends a token, not this copy (TICKET_LATER from the round-7 "
           "triage)"),
    ("gateway/pool_boot.py", "load"): (
        2, "start-up delete of an expired token's document (the inner handler, and the record-parse handler around it): counted only "
           "when it landed; the token is not restored either way, and the next start retries the delete"),
}

# functions that take something away: none may hold a site, whatever ALLOWED says
REDUCTION_PATHS = frozenset({
    ("gateway/admin.py", "GrantStore.reduce"), ("gateway/admin.py", "GrantStore._persist_at"), ("gateway/admin.py", "Admin.grant"),
    ("gateway/admin.py", "Admin.remove_class"), ("gateway/admin.py", "Admin.revoke"), ("gateway/admin.py", "Admin.break_glass"),
    ("gateway/pool_admin.py", "PoolService.ungrant"), ("gateway/pool_admin.py", "PoolService.confirm_against_authz"),
})


def _name(func) -> str:
    return func.attr if isinstance(func, ast.Attribute) else func.id if isinstance(func, ast.Name) else ""


def is_write(call) -> bool:
    n = _name(call.func)
    return "persist" in n.lower() or n in WRITE_HELPERS


def _walk_local(node):
    """The nodes of `node` outside nested functions and classes (their code does not run in this handler)."""
    todo = [node]
    while todo:
        n = todo.pop()
        yield n
        todo.extend(c for c in ast.iter_child_nodes(n) if not isinstance(c, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda,
                                                                                ast.ClassDef)))


def _ok_shaped(v) -> bool:
    if v is None or (isinstance(v, ast.Constant) and v.value in (None, True)):
        return True
    if isinstance(v, ast.Name) and v.id == "OK_ANSWER":
        return True
    return isinstance(v, ast.Dict) and any(isinstance(k, ast.Constant) and k.value == "ok" and isinstance(x, ast.Constant)
                                           and x.value is True for k, x in zip(v.keys, v.values))


def surfaces(handler) -> bool:
    """The handler raises, or returns an answer that is not ok."""
    return any(isinstance(n, ast.Raise) or (isinstance(n, ast.Return) and not _ok_shaped(n.value)) for n in _walk_local(handler))


def _writes(stmts) -> list:
    return sorted({_name(n.func) for s in stmts for n in ast.walk(s) if isinstance(n, ast.Call) and is_write(n)})


def sites(source: str) -> list:
    """(qualname, line, form, writes) for every site of the module."""
    tree = ast.parse(source)
    parent = {c: p for p in ast.walk(tree) for c in ast.iter_child_nodes(p)}

    def qual_of(node):
        q, p = [], parent.get(node)
        while p is not None:
            if isinstance(p, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                q.append(p.name)
            p = parent.get(p)
        return ".".join(reversed(q))

    out = []
    for n in ast.walk(tree):
        if isinstance(n, ast.Try) and n.handlers:
            w = _writes(n.body)
            if w and not all(surfaces(h) for h in n.handlers):
                out.append((qual_of(n), n.lineno, "a handler that neither raises nor returns a failure", w))
        elif isinstance(n, (ast.With, ast.AsyncWith)):
            if any(isinstance(i.context_expr, ast.Call) and _name(i.context_expr.func) == "suppress" for i in n.items):
                w = _writes(n.body)
                if w:
                    out.append((qual_of(n), n.lineno, "suppress() around a write", w))
        elif isinstance(n, ast.Expr) and isinstance(n.value, ast.Call) and _name(n.value.func) == "put_doc":
            out.append((qual_of(n), n.lineno, "a put_doc result discarded", ["put_doc"]))
    return out


def findings(modules: dict, allowed: dict = None) -> list:
    allowed = ALLOWED if allowed is None else allowed
    out, seen = [], {}
    for m, src in sorted(modules.items()):
        for qual, line, form, w in sites(src):
            seen[(m, qual)] = seen.get((m, qual), 0) + 1
            if (m, qual) in REDUCTION_PATHS:
                out.append(f"{m}:{line} {qual}: {form} ({', '.join(w)}) on a reduction path")
            elif (m, qual) not in allowed:
                out.append(f"{m}:{line} {qual}: {form} ({', '.join(w)}): a failed write reaches the success path")
    for key, (count, _) in sorted(allowed.items()):
        if key not in seen:
            out.append(f"{key}: on ALLOWED but no such site (stale entry)")
        elif seen[key] != count and key not in REDUCTION_PATHS:
            out.append(f"{key}: ALLOWED says {count} sites, found {seen[key]}")
    return out


def admin_put_calls(source: str) -> list:
    """(function, line, why) for every `self._st.put(...)` of class Admin, with why it cannot be a reduction: `addition` (the body of
    an `if not reduces:`) or `rollback` (inside a handler that re-raises); `?` otherwise."""
    tree = ast.parse(source)
    parent = {c: p for p in ast.walk(tree) for c in ast.iter_child_nodes(p)}
    admin = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "Admin")
    out = []
    for fn in (n for n in admin.body if isinstance(n, ast.FunctionDef)):
        for n in ast.walk(fn):
            f = n.func if isinstance(n, ast.Call) else None
            if not (isinstance(f, ast.Attribute) and f.attr == "put" and isinstance(f.value, ast.Attribute) and f.value.attr == "_st"):
                continue
            why, p, child = "?", parent.get(n), n
            while p is not None and p is not fn:
                if isinstance(p, ast.ExceptHandler) and any(isinstance(x, ast.Raise) for x in ast.walk(p)):
                    why = "rollback"
                    break
                if isinstance(p, ast.If) and child in p.body and isinstance(p.test, ast.UnaryOp) and isinstance(p.test.op, ast.Not) \
                        and isinstance(p.test.operand, ast.Name) and p.test.operand.id == "reduces":
                    why = "addition"
                    break
                p, child = parent.get(p), p
            out.append((fn.name, n.lineno, why))
    return out


class WriteFailures(unittest.TestCase):
    def test_no_failed_write_reaches_the_success_path_outside_the_allowed_sites(self):
        self.assertEqual(findings(package_modules()), [])

    def test_the_universe_is_the_whole_package(self):
        mods = package_modules()
        every = [(m, s) for m in mods for s in sites(mods[m])]
        self.assertEqual(sum(c for c, _ in ALLOWED.values()), len(every))
        self.assertGreaterEqual(len({m.split("/")[0] for m in mods}), 6)
        writes = sum(1 for m in mods for n in ast.walk(ast.parse(mods[m])) if isinstance(n, ast.Call) and is_write(n))
        self.assertGreater(writes, 30)   # the detector sees the package's writes, so `no site` is not a blind spot

    def plant(self, module, code, allowed=None):
        mods = package_modules()
        return findings(dict(mods, **{module: mods[module] + code}), allowed)

    def test_the_round_6_and_round_7_forms_are_caught(self):
        forms = {
            "caught and counted": "    except Exception:\n        self.persist_failures += 1\n",
            "caught and passed": "    except Exception:\n        pass\n",
            "caught, answered OK_ANSWER": "    except Exception:\n        return OK_ANSWER\n",
            "caught, answered a dict ok": "    except Exception:\n        return {'ok': True, 'epoch': 1}\n",
            "caught, a bare return": "    except Exception:\n        return\n",
        }
        for name, handler in forms.items():
            with self.subTest(name):
                code = "\n\ndef planted(self, p):\n    try:\n        self._persist(p)\n" + handler
                self.assertTrue(any("planted" in f and "success path" in f for f in self.plant("gateway/admin.py", code)), name)

    def test_a_handler_that_raises_or_answers_a_failure_passes(self):
        for handler in ("    except Exception:\n        self.n += 1\n        raise\n",
                        "    except Exception:\n        return UNAVAILABLE\n",
                        "    except IncompleteRead as e:\n        raise RuntimeError('authz write refused') from e\n"):
            with self.subTest(handler):
                code = "\n\ndef planted(self, p):\n    try:\n        put_doc_landed(self.c, 'i', 'd', {})\n" + handler
                self.assertEqual(self.plant("gateway/admin.py", code), [])

    def test_suppress_and_a_discarded_put_doc_are_caught(self):
        for name, code in (("suppress", "\n\ndef planted(c):\n    with contextlib.suppress(Exception):\n        delete_doc(c, 'i', 'd')\n"),
                           ("bare suppress", "\n\ndef planted(c):\n    with suppress(RuntimeError):\n        c._authz_put('d', {})\n"),
                           ("discarded", "\n\ndef planted(c):\n    put_doc(c, 'i', 'd', {})\n")):
            with self.subTest(name):
                self.assertTrue(any("planted" in f for f in self.plant("gateway/pool_boot.py", code)), name)
        self.assertEqual(self.plant("gateway/pool_boot.py", "\n\ndef planted(c):\n    if not put_doc(c, 'i', 'd', {}):\n"
                                                             "        raise RuntimeError\n"), [])

    def test_a_reduction_path_holds_no_site_even_when_allowed(self):
        mods = package_modules()
        src = mods["gateway/admin.py"]
        old = "            try:\n                self._persist(p, rev)\n            except Exception:\n                self._count_failure()\n" \
              "                raise\n"
        self.assertIn(old, src)
        planted = dict(mods, **{"gateway/admin.py": src.replace(old, old[:-len("                raise\n")])})
        allowed = {**ALLOWED, ("gateway/admin.py", "GrantStore._persist_at"): (1, "planted")}
        self.assertTrue(any("GrantStore._persist_at" in f and "on a reduction path" in f for f in findings(planted, allowed)))

    def test_stale_and_miscounted_entries_are_caught(self):
        mods = package_modules()
        src = mods["gateway/sessions.py"]
        old = "            except Exception:\n                pass   # best effort"
        self.assertIn(old, src)
        gone = dict(mods, **{"gateway/sessions.py": src.replace(old, "            except Exception:\n                raise   # best effort")})
        self.assertIn("('gateway/sessions.py', 'Sessions._persist'): on ALLOWED but no such site (stale entry)", findings(gone))
        two = self.plant("gateway/sessions.py", "\n\nclass Sessions(Sessions):\n    def _persist(self, rec):\n        try:\n"
                                                "            self._token_persist(rec)\n        except Exception:\n            pass\n")
        self.assertTrue(any("ALLOWED says 1 sites, found 2" in f for f in two), two)

    def test_admin_puts_only_additions_and_rollbacks(self):
        src = package_modules()["gateway/admin.py"]
        calls = admin_put_calls(src)
        self.assertEqual(sorted((f, why) for f, _, why in calls),
                         [("grant", "addition"), ("grant", "rollback"), ("remove_class", "rollback")])
        # the round-6 form: the class removal written through `put` (best effort) instead of `reduce` (journalled)
        r6 = src.replace("self._st.reduce(Principal(tenant, tgt.token, tgt.roles, read, write, tgt.own_class))",
                         "self._st.put(Principal(tenant, tgt.token, tgt.roles, read, write, tgt.own_class))")
        self.assertNotEqual(r6, src)
        self.assertIn(("remove_class", "?"), [(f, why) for f, _, why in admin_put_calls(r6)])


if __name__ == "__main__":
    if "--inventory" in sys.argv:
        mods = package_modules()
        print("\n".join(f"{m}:{line}\t{q}\t{form}\t{','.join(w)}" for m in sorted(mods) for q, line, form, w in sites(mods[m])))
    else:
        unittest.main()
