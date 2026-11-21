"""F1 and class guard (b): every handler operation and every mint form passes the tenant's journal admission (DESIGN 4.4, POOLS 2.5: a
journal fault closes the tenant) before anything is dispatched.

The universe is enumerated, not listed: the operation sets the handler imports (ADMIN_OPS, POOL_OPS, POOL_TOOLS, SCOPED_OPS) plus every
string the handler compares `op` with in `handle` and `_token_op` (AST), plus both forms of the socket's mint line (plain and scoped).
Every member is sent to a Gateway whose dependencies are recorders (sessions, pool runtime, writers, Elasticsearch, admin). With the
tenant not admitting, each must answer `unavailable` and reach no dependency except the admission check itself; with the tenant
admitting, each must reach a dependency (so the test is not vacuous). The one commented exemption is ADMIN_OPS: the administrator's
lane has its own admission (`Admin.handle`: a closed tenant answers unavailable to every people operation, only the named recovery
administrators may grant on a recovering tenant, and break-glass is the operator's), tested in tests/security/test_gateway_admin.py
(`test_a_closed_tenant_answers_unavailable_to_every_people_operation`, `test_after_a_recovery_only_the_named_administrators_may_grant`,
`test_the_operators_break_glass_works_when_every_administrator_is_compromised`).

A Gateway built without a pool runtime has no fail-open default: it needs an explicit `admits`, and with neither it admits nothing.
"""
import ast
import base64
import inspect
import unittest

from living_context.gateway import handler as H
from living_context.gateway.handler import ADMIN_OPS, POOL_TOOLS, SCOPED_OPS, UNAVAILABLE, Gateway
from living_context.gateway.pool_admin import POOL_OPS
from living_context.gateway.sessions import Record
from living_context.ids import KIND_LEAF, Pointer, encode_pointer, new_leaf_id
from living_context.policy import ClassLattice, Principal, Role

T = "tena"
LAT = {T: ClassLattice(["base"], [])}
ADMISSION_EXEMPT = ADMIN_OPS   # the administrator's own lane (see the module docstring)


def op_universe() -> set:
    tree = ast.parse(inspect.getsource(H))
    gw = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "Gateway")
    ops = set(ADMIN_OPS | POOL_OPS | POOL_TOOLS | SCOPED_OPS)
    for f in gw.body:
        if isinstance(f, ast.FunctionDef) and f.name in ("handle", "serve", "_token_op", "_dispatch"):
            for n in ast.walk(f):
                if isinstance(n, ast.Compare) and isinstance(n.left, ast.Name) and n.left.id == "op":
                    for c in n.comparators:
                        vals = c.elts if isinstance(c, (ast.Tuple, ast.Set, ast.List)) else [c]
                        ops |= {v.value for v in vals if isinstance(v, ast.Constant) and isinstance(v.value, str)}
    return ops


class Log(list):
    def hit(self, what):
        self.append(what)


class FakeSessions:
    def __init__(self, log, scoped=False):
        self.log, self.scoped = log, scoped

    def resolve(self, token):
        return Record(T, "ptok", frozenset({Role.READER, Role.WRITER, Role.TENANT_ADMIN}), frozenset({"base"}), frozenset({"base"}), None, 0,
                      scope={"pzzzzzzzzzzzzzzz": frozenset({"query"})} if self.scoped else None, hash="h" * 64)

    def principal(self, rec):
        return Principal(rec.tenant, rec.principal_token, rec.roles, rec.read_set, rec.write_set, rec.own_class)

    def __getattr__(self, name):   # mint, mint_scoped, renew, delegate, revoke, ...: a dispatch
        def call(*a, **kw):
            self.log.hit(f"sessions.{name}")
            return "tok"
        return call


class Recorder:
    def __init__(self, log, name):
        self.log, self.name = log, name

    def __call__(self, *a, **kw):
        self.log.hit(self.name)
        return self

    def __getattr__(self, attr):
        if attr == "admit":
            raise AttributeError(attr)
        return Recorder(self.log, f"{self.name}.{attr}")


class Runtime(Recorder):
    def __init__(self, log, admits):
        super().__init__(log, "pools")
        self.admits = admits

    def admit(self):
        self.log.hit("admit")
        return self.admits


def gateway(log, admits, scoped=False, cls=Gateway):
    rt = Runtime(log, admits)
    return cls(Recorder(log, "es"), FakeSessions(log, scoped), LAT, writers=Recorder(log, "writers"), admin=Recorder(log, "admin"),
               pools=lambda t: rt)


POINTER = encode_pointer(Pointer(KIND_LEAF, 0, new_leaf_id(), 1))


def request(op):
    return {"op": op, "pool": "pzzzzzzzzzzzzzzz", "cls": "base", "text": "x", "query": "x", "doc": {"text": "x"},
            "pointer": POINTER, "scope": {"pzzzzzzzzzzzzzzz": ["query"]}, "ttl_s": 60}


MINTS = {"plain": {"session": "plain"}, "scoped": {"conversation": "c", "scope": {"pzzzzzzzzzzzzzzz": ["query"]}}}


def bypasses(cls=Gateway) -> list:
    """(form, what was reached) for every operation or mint form that reached a dependency while the tenant did not admit."""
    out = []
    for scoped in (False, True):
        for op in sorted(op_universe() - ADMISSION_EXEMPT):
            log = Log()
            ans = gateway(log, False, scoped, cls).handle("tok", request(op))
            reached = [x for x in log if x != "admit"]
            if reached or ans != UNAVAILABLE or "admit" not in log:
                out.append((("scoped " if scoped else "plain ") + op, reached, ans))
    for form, req in MINTS.items():
        log = Log()
        ans, tok = gateway(log, False, cls=cls).mint_line({"tenant": T, "proof": base64.b64encode(b"p").decode(), "request": req})
        reached = [x for x in log if x != "admit"]
        if reached or ans != UNAVAILABLE or tok is not None or "admit" not in log:
            out.append(("mint " + form, reached, ans))
    return out


class AdmissionGuard(unittest.TestCase):
    def test_the_universe_is_enumerated(self):
        ops = op_universe()
        self.assertTrue({"search", "lookup", "write", "ping", "token_renew", "token_delegate", "token_revoke", "pool_fetch",
                         "pool_query", "grant"} <= ops, ops)

    def test_no_operation_or_mint_form_is_dispatched_while_the_tenant_does_not_admit(self):
        self.assertEqual(bypasses(), [])

    def test_with_the_tenant_admitting_every_member_reaches_a_dependency(self):
        quiet = []
        for op in sorted(op_universe() - ADMISSION_EXEMPT - {"ping"}):   # ping answers from the handler itself
            log = Log()
            gateway(log, True).handle("tok", request(op))
            if not [x for x in log if x != "admit"]:
                quiet.append(op)
        self.assertEqual(quiet, [])
        for form, req in MINTS.items():
            log = Log()
            gateway(log, True).mint_line({"tenant": T, "proof": base64.b64encode(b"p").decode(), "request": req})
            self.assertTrue(any(x.startswith("sessions.") for x in log), form)

    def test_ping_answers_only_while_admitting(self):
        self.assertEqual(gateway(Log(), False).handle("tok", {"op": "ping"}), UNAVAILABLE)
        self.assertEqual(gateway(Log(), True).handle("tok", {"op": "ping"}), {"ok": True})

    def test_a_gateway_without_a_runtime_or_an_explicit_admission_admits_nothing(self):
        log = Log()
        gw = Gateway(Recorder(log, "es"), FakeSessions(log), LAT, writers=Recorder(log, "writers"))
        self.assertEqual(gw.handle("tok", {"op": "search", "cls": "base", "text": "x"}), UNAVAILABLE)
        self.assertEqual([x for x in log], [])

    def test_planted_bypass_is_caught(self):
        class Planted(Gateway):   # a new operation answered before the admission check
            def handle(self, token, request):
                if isinstance(request, dict) and request.get("op") == "search":
                    self._writers(T)
                    return {"ok": True}
                return super().handle(token, request)

        self.assertTrue(any(form == "plain search" for form, _, _ in bypasses(Planted)))


if __name__ == "__main__":
    unittest.main()
