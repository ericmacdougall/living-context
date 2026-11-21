"""PIN for goodqa#16576 (review round 8, F1 and F2 of the class-grant lane): the class-level administrator operations are not reachable
from the product.

F1 (a late authz write of a class grant can land after a confirmed reduction and survive compaction and a restart; and the revision
order rests on the wall clock) and F2 (an unconditional `GrantStore.rollback` can undo another administrator's reduction) were triaged
TICKET_LATER as goodqa#16576 on one condition: no socket request of this build reaches `admin.Admin`, so neither a reducing grant, nor
`remove_class`, nor `GrantStore.rollback` runs in the product. This module makes that condition enforceable:

* every operation of `admin.ADMIN_OPS`, sent over the real local socket of the composition root (`app.start_app`) by a tenant
  administrator's session, answers `refused`; and the same requests DO reach an `Admin` when one is wired into the gateway (a spy), so
  the refusals come from the missing administrator, not from an earlier check;
* no module of the package builds an `Admin`, passes `admin=` to a `Gateway`, or calls `reduce`, `rollback` or `remove_class` outside
  gateway/admin.py; planted sources check that the walk sees each form.

This pin may be removed only together with the fixes of F1 and F2 (goodqa#16576): wiring the administrator operations into the product
without them reopens both findings."""
import ast
import base64
import functools
import json
import socket
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from living_context.config import Constants
from living_context.gateway import app as app_mod
from living_context.gateway import boot, epoch_journal, fence
from living_context.gateway.admin import ADMIN_OPS
from living_context.gateway.handler import Gateway
from living_context.keystore.store import Keystore
from living_context.policy import ClassLattice, Principal, Role
from tests.unit.fake_es import FakeAuthz
from tests.unit.test_read_paths import package_modules

T = "tenaaa"
LAT = ClassLattice(["base", "restricted"], [("restricted", "base")])
REFUSED = {"ok": False, "error": "refused"}
# one well-formed request per administrator operation (no field the handler forbids in a body)
REQUESTS = {
    "grant": {"who": "user-bob", "grant_roles": ["reader"], "read": ["base"]},
    "revoke": {"who": "user-bob"},
    "remove_class": {"who": "user-bob", "cls": "restricted"},
    "break_glass_revoke": {"target_tenant": T, "who": "user-bob"},
    "offboard_tenant": {"target_tenant": T},
}


class Gate:
    """The cluster as the composition root sees it at start-up: only the tenant's authz index is read."""

    def __init__(self):
        self.authz = FakeAuthz()

    def client(self, tenant, role):
        if role != "authz":
            raise AssertionError(f"unexpected client {role} at start-up")
        return self.authz

    def drop_tenant(self, tenant):
        pass


class Adapter:
    human_presence = True

    def verify(self, tenant, proof):
        return proof.decode() if proof.startswith(b"user-") else None

    def presence(self, tenant, proof):
        return False


class Spy:
    """An administrator wired into the gateway (the mutant): it answers every operation it is handed."""

    def __init__(self):
        self.seen = []

    def handle(self, op, tenant, p, req):
        self.seen.append(op)
        return {"ok": True, "spy": op}


class AdminOpsFromTheProduct(unittest.TestCase):
    def setUp(self):
        if sys.platform == "darwin":   # the send fence's kernel queue term is Linux's SIOCOUTQ (as tests/unit/test_send_fence_pools.py)
            p = mock.patch.object(fence, "queued", lambda sock: 0)
            p.start()
            self.addCleanup(p.stop)

    def start(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        d = Path(tmp.name)
        gate = Gate()
        persist = boot.grant_persister(gate, T)
        for who, roles in (("user-admin", {Role.TENANT_ADMIN}), ("user-bob", {Role.READER})):
            persist(Principal(T, f"pt-{who}", frozenset(roles), frozenset({"base", "restricted"}), frozenset({"base"})))
        epoch_journal.provision(d / "j", 65536)
        setup = app_mod.TenantSetup(T, LAT, d / "j", (d / "tj" / "t.log", d / "tr" / "t.log"), Keystore(d / "ks.db", T),
                                    d / "holds.json", d / "pk", create_index=lambda name, body: None, drop_index=lambda name: None)
        app = app_mod.start_app(gate, [setup], {T: Adapter()}, lambda t, w: f"pt-{w}", consts=Constants(tok_live_max=100))
        self.addCleanup(app.stop)
        c = socket.create_connection(app.server.address, timeout=30)
        f = c.makefile("rb")
        self.addCleanup(c.close)
        self.addCleanup(f.close)

        def send(obj):
            c.sendall((json.dumps(obj) + "\n").encode())
            return json.loads(f.readline())
        m = send({"mint": {"tenant": T, "proof": base64.b64encode(b"user-admin").decode(), "request": {"session": "plain"}}})
        self.assertTrue(m.get("ok"), m)
        return lambda op: send({"token": m["token"], "request": dict(REQUESTS[op], op=op)})

    def test_every_administrator_operation_answers_refused_from_the_composition_root(self):
        self.assertEqual(set(REQUESTS), set(ADMIN_OPS), "an administrator operation without a request here")
        call = self.start()
        for op in sorted(ADMIN_OPS):
            with self.subTest(op):
                self.assertEqual(call(op), REFUSED)

    def test_control_the_same_requests_reach_an_administrator_when_one_is_wired(self):
        spy = Spy()
        with mock.patch.object(app_mod, "Gateway", functools.partial(Gateway, admin=spy)):
            call = self.start()
        for op in sorted(ADMIN_OPS):
            with self.subTest(op):
                self.assertEqual(call(op), {"ok": True, "spy": op})
        self.assertEqual(sorted(spy.seen), sorted(ADMIN_OPS))


REACH = ("reduce", "rollback", "remove_class")


def _name(func):
    return func.attr if isinstance(func, ast.Attribute) else func.id if isinstance(func, ast.Name) else ""


def admin_wiring(modules: dict) -> list:
    """(module, line, what) for every place the package could wire the administrator operations."""
    out = []
    for mod, src in modules.items():
        for n in ast.walk(ast.parse(src)):
            if isinstance(n, ast.Call):
                name = _name(n.func)
                if name == "Admin":
                    out.append((mod, n.lineno, "Admin built"))
                if name == "Gateway" and any(k.arg == "admin" for k in n.keywords):
                    out.append((mod, n.lineno, "admin= passed to a Gateway"))
                if name in REACH and mod != "gateway/admin.py":
                    out.append((mod, n.lineno, f"{name} called"))
            if isinstance(n, ast.ImportFrom) and any(a.name == "Admin" for a in n.names):
                out.append((mod, n.lineno, "Admin imported"))
    return out


class NoAdminInThePackage(unittest.TestCase):
    def test_no_module_builds_or_wires_an_administrator(self):
        self.assertEqual(admin_wiring(package_modules()), [])

    def test_planted_wiring_is_seen(self):
        planted = {
            "built": "x = Admin(store, epochs, lattices, rev, tok)\n",
            "imported": "from .admin import Admin\n",
            "wired": "gw = Gateway(gate, sessions, lattices, admin=adm)\n",
            "a reduction": "store.reduce(p)\n",
            "a rollback": "store.rollback(p)\n",
            "a class removal": "adm.remove_class(p, t, lat, who, cls)\n",
        }
        for name, src in planted.items():
            with self.subTest(name):
                self.assertTrue(admin_wiring({"gateway/app.py": src}))


if __name__ == "__main__":
    unittest.main()
