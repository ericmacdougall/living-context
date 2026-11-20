"""Pools delta review 1 (goodqa#16441, finding F6, class C2): the product path, end to end through the local socket. The gateway is
started by the composition root (`app.start_app`), which reads the grants and the pool state back and starts each tenant's journal
once; every token is obtained by a `mint` line on the socket (an authentication proof, never a bare principal id), and every pool
operation is a socket line. A held save is reviewed and released by a token whose `human` actor comes only from the identity adapter's
presence check of the proof, never from a request field. Needs LC_TEST_ES_URL, LC_TEST_CA and LC_TEST_PW."""
import base64
import json
import socket
import tempfile
import unittest
from pathlib import Path

from living_context.config import Constants
from living_context.gateway import boot, epoch_journal
from living_context.policy import Principal, Role
from tests.security.test_pools_es import LAT, Base, T

UNAV = {"ok": False, "error": "unavailable"}


class PresenceAdapter:
    """An identity adapter of an interactive interface (POOLS 2.3): it verifies the proof, and it alone says whether the proof carries a
    fresh user-presence assertion (here: the suffix `+present`, ASSUMPTION for the test adapter only)."""
    human_presence = True

    def verify(self, tenant, proof):
        return proof.decode().split("+")[0] if proof.startswith(b"user") else None

    def presence(self, tenant, proof):
        return proof.endswith(b"+present")


class Line:
    def __init__(self, address):
        self.c = socket.create_connection(address, timeout=30)
        self.f = self.c.makefile("rb")

    def send(self, obj):
        self.c.sendall((json.dumps(obj) + "\n").encode())
        return json.loads(self.f.readline())

    def mint(self, proof, request):
        return self.send({"mint": {"tenant": T, "proof": base64.b64encode(proof).decode(), "request": request}})

    def call(self, token, **req):
        return self.send({"token": token, "request": req})

    def close(self):
        self.f.close()
        self.c.close()


class SocketE2E(Base):
    def setUp(self):
        super().setUp()
        persist = boot.grant_persister(self.gate, T)
        for who, read, write, roles in (("admin", {"base"}, {"base"}, {Role.TENANT_ADMIN}),
                                        ("carol", {"base", "restricted"}, {"base", "restricted"}, {Role.READER, Role.WRITER})):
            persist(Principal(T, f"pt-user-{who}", frozenset(roles), frozenset(read), frozenset(write)))
        self.app = None
        self.start()

    def start(self):
        from living_context.gateway.app import TenantSetup, start_app
        d = Path(tempfile.mkdtemp(dir=self.root))
        if getattr(self, "jdir", None) is None:
            self.jdir = d / "j"
            epoch_journal.provision(self.jdir, 65536)
            self.paths = dict(tj=(d / "tj" / "t.log", d / "tr" / "t.log"), holds=d / "holds.json", pk=d / "pk")
        setup = TenantSetup(T, LAT[T], self.jdir, self.paths["tj"], self.ks, self.paths["holds"], self.paths["pk"],
                            create_index=self._create, drop_index=lambda name: self.admin.request("DELETE", f"/{name}"))
        self.app = start_app(self.gate, [setup], {T: PresenceAdapter()}, lambda t, w: f"pt-{w}",
                             consts=Constants(tok_live_max=100, mint_rate_per_h=1000))
        self.line = Line(self.app.server.address)

    def restart(self):
        self.line.close()
        self.app.stop()
        self.start()

    def tearDown(self):
        if self.app is not None:
            self.line.close()
            self.app.stop()

    def scoped(self, proof, scope, conv):
        r = self.line.mint(proof, {"conversation": conv, "scope": scope})
        self.assertTrue(r.get("ok"), r)
        return r["token"]

    def test_f6_mint_pools_review_and_revocation_through_the_socket_from_the_composition_root(self):
        ln = self.line
        adm = ln.mint(b"user-admin", {"session": "plain"})
        self.assertTrue(adm.get("ok"), adm)
        adm = adm["token"]
        a = ln.call(adm, op="pool_create", cls="base", kind="team", label="socket crew")
        r = ln.call(adm, op="pool_create", cls="restricted", kind="team", label="socket vault")
        self.assertTrue(a["ok"] and r["ok"], (a, r))
        a, r = a["pool"], r["pool"]
        self.assertTrue(ln.call(adm, op="pool_grant", who="user-carol", pool=a, ops=["query", "save", "review"])["ok"])
        self.assertTrue(ln.call(adm, op="pool_grant", who="user-carol", pool=r, ops=["query", "save"])["ok"])
        agent = self.scoped(b"user-carol", {a: ["query", "save"], r: ["query", "save"]}, "conv-a")
        # a request field never makes a human token: this one asks for it, is an agent, and cannot show or release a hold
        fake = ln.mint(b"user-carol", {"conversation": "conv-b", "scope": {a: ["query", "save", "review"]}, "human": True})
        self.assertTrue(fake.get("ok"), fake)
        fake = fake["token"]
        s = ln.call(agent, op="pool_save", pool=r, text="the otter holt is under the alder roots", key="k1")
        self.assertTrue(s["ok"], s)
        f = ln.call(agent, op="pool_fetch", pointer=s["pointer"])
        self.assertEqual(f.get("status"), "ok", f)                         # the conversation is now tainted to `restricted`
        held = ln.call(agent, op="pool_save", pool=a, text="otters were seen near the alder", key="k2")
        self.assertEqual(held, {"ok": False, "error": "held", "reason": "taint"})
        human = self.scoped(b"user-carol+present", {a: ["query", "save", "review"]}, "conv-h")
        holds = ln.call(human, op="pool_holds", pool=a)
        self.assertEqual(len(holds.get("holds", [])), 1, holds)
        hid = holds["holds"][0]["hold"]
        self.assertEqual(ln.call(fake, op="pool_hold_show", pool=a, hold=hid), UNAV)
        shown = ln.call(human, op="pool_hold_show", pool=a, hold=hid)
        self.assertTrue(shown.get("ok"), shown)
        out = ln.call(human, op="pool_release", pool=a, hold=hid, decision="admit", content_hash=shown["content_hash"])
        self.assertTrue(out.get("ok"), out)
        # harness token operations over the socket: delegate (finite lifetime only), renew, revoke
        self.assertEqual(ln.send({"token": agent, "request": {"op": "token_delegate", "scope": {a: ["query"]}, "ttl_s": 1e400}}),
                         {"error": "refused", "ok": False})               # an Infinity literal never parses
        d = ln.call(agent, op="token_delegate", scope={a: ["query"]}, ttl_s=60)
        self.assertTrue(d.get("ok"), d)
        self.assertTrue(ln.call(d["token"], op="pool_query", query="otters alder").get("ok"))
        self.assertTrue(ln.call(agent, op="token_renew").get("ok"))
        self.assertTrue(ln.call(agent, op="token_revoke").get("ok"))
        self.assertEqual(ln.call(d["token"], op="pool_query", query="otters"), UNAV)
        # a restart from the composition root: the pool state and the grants come back, a removed grant stays removed
        self.assertTrue(ln.call(adm, op="pool_ungrant", who="user-carol", pool=r)["ok"])
        self.restart()
        ln = self.line
        self.assertEqual(ln.mint(b"user-carol", {"conversation": "conv-z", "scope": {r: ["query"]}}), UNAV)
        t2 = self.scoped(b"user-carol", {a: ["query"]}, "conv-y")
        q = ln.call(t2, op="pool_query", query="otters alder")
        self.assertEqual(len(q.get("items", [])), 1, q)                    # the released save is a leaf of the pool
        self.assertEqual(ln.mint(b"nobody", {"conversation": "c", "scope": {a: ["query"]}}), UNAV)
        self.assertEqual(ln.send({"mint": {"tenant": T, "proof": "%%%", "request": {}}}), UNAV)


if __name__ == "__main__":
    unittest.main()
