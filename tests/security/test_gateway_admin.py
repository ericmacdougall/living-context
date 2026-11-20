"""BUILD-PLAN Step 15 through the gateway's call path (SEC-R3, R4, R5): grant, revoke, class-grant removal and the operator's
break-glass revoke are requests to `Gateway.handle`, decided by `policy.decide`, executed on the epoch journal of the tenant.
No Elasticsearch is needed. Each check has a broken variant that must turn it red for its stated reason:
  * no self-grant (broken: an administrator grants itself);
  * revoking an administrator or a revoker needs two distinct administrators (broken: one administrator is enough);
  * class-grant removal is an epoch bump (broken: the grants shrink, the old sessions keep the class);
  * the operator's break-glass depends on nothing a tenant role holds (broken: its authority is a grant a tenant administrator revokes);
  * after a break-glass recovery only the named administrators may grant (broken: the recovery list is not consulted);
  * the revoke has its own admission lane (broken: it waits in the tenant's general queue behind a flood)."""
import json
import socket
import tempfile
import threading
import time
import unittest
from pathlib import Path

from living_context.gateway import admission, epoch_journal
from living_context.gateway.admin import Admin, GrantStore
from living_context.gateway.fence import SendFence
from living_context.gateway.handler import Gateway
from living_context.gateway.revocation import OPEN, RevocationService
from living_context.gateway.server import Server
from living_context.gateway.sessions import EpochTable, Sessions
from living_context.policy import ClassLattice, Principal, Role

T = "tenaaa"
LAT = ClassLattice(["base", "restricted"], [("restricted", "base")])


class Idp:
    def verify(self, tenant, proof):
        return proof[len(b"proof-"):].decode() if proof.startswith(b"proof-") else None


def tok(tenant, who):
    return f"tok-{who}"


class World:
    def __init__(self, test, **admin_kw):
        self.tmp = tempfile.TemporaryDirectory()
        test.addCleanup(self.tmp.cleanup)
        d = Path(self.tmp.name) / T
        epoch_journal.provision(d, 16384)
        self.dir, self.fence = d, SendFence()
        self.ep = EpochTable(lambda t: self.fence)
        self.svc = RevocationService(T, self.ep, d)
        test.addCleanup(lambda: self.svc._j and self.svc._j.close())
        assert self.svc.start() == OPEN
        self.store = GrantStore()
        self.sessions = Sessions({T: Idp(), "sysop": Idp()}, self.ep, tok, lambda t, p: self.store.get(t, p))
        self.admin = Admin(self.store, self.ep, {T: LAT}, lambda t: self.svc, tok, **admin_kw)
        self.gw = Gateway(None, self.sessions, {T: LAT}, admin=self.admin)
        for a in ("a1", "a2", "a3"):
            self.grant(T, a, {Role.TENANT_ADMIN})
        self.grant(T, "rv", {Role.REVOKER})
        self.grant(T, "r1", {Role.READER}, read={"base", "restricted"})
        self.grant(T, "r2", {Role.READER}, read={"base"})
        self.grant(T, "op", {Role.OPERATOR})                     # a hostile or legacy tenant-side copy of the operator's principal
        self.grant("sysop", "op", {Role.OPERATOR})

    def grant(self, tenant, who, roles, read=(), write=()):
        self.store.put(Principal(tenant, tok(tenant, who), frozenset(roles), frozenset(read), frozenset(write)))

    def login(self, name, tenant=T):
        return self.sessions.mint(tenant, f"proof-{name}".encode())

    def call(self, name, _tenant=T, **req):
        return self.gw.handle(self.login(name, _tenant), req)

    def revoked(self, who):
        return self.ep.is_revoked(T, tok(T, who))


class PeopleOps(unittest.TestCase):
    # ---- no self-grant ------------------------------------------------------------------------------------------------
    def check_self_grant(self, **kw):
        w = World(self, **kw)
        r = w.call("a1", op="grant", who="a1", grant_roles=["tenant_admin", "reader"], read=["base", "restricted"])
        self.assertFalse(r.get("ok"), "an administrator granted itself a role and classes")
        self.assertEqual(w.store.get(T, "tok-a1").roles, frozenset({Role.TENANT_ADMIN}))
        ok = w.call("a1", op="grant", who="newbie", grant_roles=["reader"], read=["base"])
        self.assertTrue(ok.get("ok"), ok)
        self.assertEqual(w.store.get(T, "tok-newbie").read_set, frozenset({"base"}))
        for bad in (dict(who="x1", grant_roles=["operator"]), dict(who="x2", grant_roles=["reader"], read=["restricted"]),
                    dict(who="x3", grant_roles=["nonsense"]), dict(who="r1", grant_roles=["reader"], read=["base"])):
            if bad["who"] == "r1":
                w.call("a2", op="revoke", who="r1")
            self.assertFalse(w.call("a1", op="grant", **bad).get("ok"), bad)
        self.assertFalse(w.call("r2", op="grant", who="r2", grant_roles=["tenant_admin"]).get("ok"), "a reader granted a role")

    def test_an_administrator_cannot_grant_itself_and_grants_are_validated(self):
        self.check_self_grant()

    def test_broken_self_grant_allowed_is_red(self):
        with self.assertRaises(AssertionError) as cm:
            self.check_self_grant(broken_self_grant=True)
        self.assertIn("granted itself", str(cm.exception))

    # ---- two administrators to revoke an administrator or a revoker -------------------------------------------------------------
    def check_two_administrators(self, **kw):
        w = World(self, **kw)
        victim = w.login("rv")
        first = w.call("a1", op="revoke", who="rv")
        self.assertFalse(w.revoked("rv"), "a single administrator revoked a revoker")
        self.assertEqual(first, {"ok": False, "error": "pending"})
        self.assertFalse(w.call("a1", op="revoke", who="rv").get("ok"), "the same administrator signed twice")
        self.assertFalse(w.revoked("rv"), "the same administrator signed twice")
        self.assertFalse(w.call("rv", op="revoke", who="a3").get("ok"), "a revoker revoked an administrator")
        self.assertFalse(w.call("r2", op="revoke", who="r1").get("ok"), "a reader revoked a principal")
        done = w.call("a2", op="revoke", who="rv")
        self.assertTrue(done.get("ok") and done["durable"], done)
        self.assertTrue(w.revoked("rv"))
        self.assertIsNone(w.sessions.resolve(victim), "the revoked revoker's session still resolves")
        self.assertFalse(w.call("rv", op="revoke", who="r2").get("ok"), "the revoked revoker could still act")

    def test_revoking_a_revoker_needs_two_distinct_administrators(self):
        self.check_two_administrators()

    def test_broken_single_administrator_revoke_of_a_revoker_is_red(self):
        with self.assertRaises(AssertionError) as cm:
            self.check_two_administrators(broken_single_admin_revokes_privileged=True)
        self.assertIn("single administrator revoked a revoker", str(cm.exception))

    def test_a_revoker_revokes_a_reader_and_the_receipt_reports_the_durable_point(self):
        w = World(self)
        r = w.call("rv", op="revoke", who="r1")
        self.assertTrue(r["ok"] and r["denied"] and r["durable"], r)
        self.assertTrue(w.revoked("r1"))
        self.assertFalse(w.revoked("r2"))

    def test_an_expired_first_signature_does_not_count(self):
        t = [1000.0]
        w = World(self, clock=lambda: t[0], pending_s=60)
        w.call("a1", op="revoke", who="rv")
        t[0] += 120
        self.assertEqual(w.call("a2", op="revoke", who="rv"), {"ok": False, "error": "pending"})
        self.assertFalse(w.revoked("rv"))

    # ---- class-grant removal is an epoch bump -----------------------------------------------------------------------------
    def check_class_removal(self, **kw):
        w = World(self, **kw)
        old = w.login("r1")
        before = w.ep.epoch(T, "tok-r1")
        r = w.call("a1", op="remove_class", who="r1", cls="restricted")
        self.assertTrue(r.get("ok"), r)
        self.assertEqual(w.store.get(T, "tok-r1").read_set, frozenset({"base"}))
        self.assertGreater(w.ep.epoch(T, "tok-r1"), before, "the removal did not bump the epoch")
        self.assertIsNone(w.sessions.resolve(old), "a session minted before the removal still holds the removed class")
        new = w.sessions.resolve(w.login("r1"))
        self.assertEqual(w.sessions.principal(new).read_set, frozenset({"base"}))
        self.assertFalse(w.revoked("r1"), "removing a class revoked the principal")
        self.assertFalse(w.call("r2", op="remove_class", who="r1", cls="base").get("ok"))
        self.assertFalse(w.call("a1", op="remove_class", who="r1", cls="nonexistent").get("ok"))

    def test_class_grant_removal_bumps_the_epoch_and_the_next_session_carries_the_reduced_grants(self):
        self.check_class_removal()

    def test_broken_class_removal_without_an_epoch_bump_is_red(self):
        with self.assertRaises(AssertionError) as cm:
            self.check_class_removal(broken_removal_without_bump=True)
        self.assertRegex(str(cm.exception), "did not bump the epoch|minted before the removal")

    def test_removing_a_base_class_removes_the_classes_that_dominate_it(self):
        w = World(self)
        w.call("a1", op="remove_class", who="r1", cls="base")
        self.assertEqual(w.store.get(T, "tok-r1").read_set, frozenset())   # restricted dominates base: a down-closed set cannot keep it

    # ---- the operator's break-glass -------------------------------------------------------------------------------------
    def check_break_glass(self, **kw):
        w = World(self, **kw)
        # the tenant's administrators are all compromised: two of them revoke the tenant-side copy of the operator's principal
        w.call("a1", op="revoke", who="op")
        w.call("a2", op="revoke", who="op")
        victim = w.login("a3")
        r = w.call("op", _tenant="sysop", op="break_glass_revoke", target_tenant=T, who="a3")
        self.assertTrue(r.get("ok"), "the operator's break-glass failed after the tenant's administrators acted")
        self.assertIsNone(w.sessions.resolve(victim))
        self.assertFalse(w.call("a1", op="break_glass_revoke", target_tenant=T, who="a2").get("ok"),
                         "a tenant administrator used the break-glass")
        self.assertFalse(w.revoked("a2"))

    def test_the_operators_break_glass_works_when_every_administrator_is_compromised(self):
        self.check_break_glass()

    def test_broken_break_glass_removable_by_a_tenant_administrator_is_red(self):
        with self.assertRaises(AssertionError) as cm:
            self.check_break_glass(broken_operator_authority_in_tenant=True)
        self.assertIn("break-glass failed", str(cm.exception))

    # ---- a closed tenant and a recovery ------------------------------------------------------------------------------------
    def test_a_closed_tenant_answers_unavailable_to_every_people_operation(self):
        w = World(self)
        w.svc._fault("test fault")
        for who, req in (("a1", dict(op="revoke", who="r1")), ("a1", dict(op="grant", who="n", grant_roles=["reader"], read=["base"])),
                         ("a1", dict(op="remove_class", who="r1", cls="restricted"))):
            r = w.call(who, **req)
            self.assertFalse(r.get("ok"), req)
            self.assertFalse(w.revoked("r1"))

    def check_recovery_admins(self, **kw):
        w = World(self, **kw)
        w.svc._fault("test fault")
        w.svc.break_glass(revokes=["tok-r2"], admins=["tok-a2"])
        self.assertTrue(w.revoked("r2"))
        refused = w.call("a1", op="grant", who="n1", grant_roles=["reader"], read=["base"])
        self.assertFalse(refused.get("ok"), "an administrator the operator did not name granted during the recovery")
        self.assertTrue(w.call("a2", op="grant", who="n2", grant_roles=["reader"], read=["base"]).get("ok"))

    def test_after_a_recovery_only_the_named_administrators_may_grant(self):
        self.check_recovery_admins()

    def test_broken_recovery_list_not_consulted_is_red(self):
        with self.assertRaises(AssertionError) as cm:
            self.check_recovery_admins(broken_ignore_recovery_admins=True)
        self.assertIn("did not name granted", str(cm.exception))


class Lane(unittest.TestCase):
    """SEC-R3: a flood from the tenant's own principals fills its general slots and queue; a revoke still returns at once."""

    def check_lane(self, **server_kw):
        w = World(self)
        release = threading.Event()

        class Handler:
            def handle(self, token, req):
                if req.get("op") == "hold":
                    release.wait(15)
                    return {"ok": True}
                return w.gw.handle(token, req)

        adm = admission.Admission(slots=2, queue=0)
        srv = Server(Handler(), w.sessions, w.ep, lambda t: w.fence, adm=adm, idle_s=5.0, **server_kw).start()
        self.addCleanup(srv.stop)
        self.addCleanup(release.set)

        def conn(who):
            c = socket.create_connection(srv.address, timeout=10)
            self.addCleanup(c.close)
            return c, c.makefile("rb"), w.login(who)

        def send(cf, **req):
            c, f, token = cf
            c.sendall((json.dumps({"token": token, "request": req}) + "\n").encode())
            return f

        flooders = [conn("r1"), conn("r2")]
        for cf in flooders:
            send(cf, op="hold")
        time.sleep(0.5)   # both general slots are held
        a = conn("a1")
        t0 = time.time()
        line = send(a, op="revoke", who="r1").readline()
        dt = time.time() - t0
        self.assertTrue(json.loads(line).get("ok") and dt < 1.0, f"the revoke did not pass the flood in its own lane ({dt:.2f} s, {line!r})")
        self.assertTrue(w.revoked("r1"))
        release.set()

    def test_a_revoke_has_its_own_admission_lane_and_passes_a_flood(self):
        self.check_lane()

    def test_broken_revoke_in_the_general_queue_is_red(self):
        with self.assertRaises(AssertionError) as cm:
            self.check_lane(broken_revoke_in_general_lane=True)
        self.assertIn("did not pass the flood", str(cm.exception))


if __name__ == "__main__":
    unittest.main()
