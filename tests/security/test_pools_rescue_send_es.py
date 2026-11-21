"""F10 on the product path: the paused-sender cases of tests/unit/test_send_fence_pools.py, repeated through the composition root
(`app.start_app`: the gateway, the real epoch journal, Elasticsearch, and the Linux kernel queue term of the 64 KiB cap). Every token is
minted on the socket and every removal is a socket line of the administrator. The sender is paused by wrapping the server's handler
(`Pausing`), between build and hand-off, or inside the fence at its first chunk (chunks bounded at 1 KiB).

Round 4 (F16): seeing a pool on the product path. After an ungrant, a fresh pool_describe is `unavailable` (no card) and a fresh pool_list
leaves the pool out; a paused pool_describe with its card and a paused pool_list are aborted by the ungrant; an ungrant of a pool the
answer does not name leaves it whole. Needs LC_TEST_ES_URL, LC_TEST_CA and LC_TEST_PW."""
import json
import threading
import time
from unittest import mock

from living_context.gateway import fence
from tests.security.test_pools_socket_e2e_es import UNAV, Line, SocketE2E
from tests.unit.test_send_fence_pools import Pausing

TEXT = "the gannet roost is on the stack above the north landing, " * 40
CARD = "the fence card: north landing roost, ask the warden first"


class SendFenceE2E(SocketE2E):
    # the parent's own end-to-end test runs in its module; only the setup is shared here
    test_f6_mint_pools_review_and_revocation_through_the_socket_from_the_composition_root = None

    k = 0

    def setUp(self):
        super().setUp()
        # pools persist across the tests of the class (the tenant is reset per class) and labels are unique per tenant, so each test
        # names its pools with its own letter
        SendFenceE2E.k += 1
        self.tag = "".join(chr(ord("a") + int(d)) for d in str(SendFenceE2E.k))
        ln = self.line
        adm = ln.mint(b"user-admin", {"session": "plain"})
        self.assertTrue(adm.get("ok"), adm)
        self.adm = adm["token"]
        made = [ln.call(self.adm, op="pool_create", cls="base", kind="team", label=f"fence {w} {self.tag}") for w in ("alpha", "bravo")]
        self.assertTrue(all(m.get("ok") for m in made), made)
        self.A, self.B = (m["pool"] for m in made)
        for p in (self.A, self.B):
            self.assertTrue(ln.call(self.adm, op="pool_grant", who="user-carol", pool=p, ops=["query", "save"])["ok"])
        # a save key is scoped to (principal, conversation, key): each test saves in its own conversation
        self.tok = self.scoped(b"user-carol", {self.A: ["query", "save"], self.B: ["query", "save"]}, f"conv-fence-{self.tag}")
        saved = [ln.call(self.tok, op="pool_save", pool=self.A, text=TEXT, key="fa"),
                 ln.call(self.tok, op="pool_save", pool=self.B, text=TEXT.replace("gannet", "puffin"), key="fb")]
        self.assertTrue(all(x.get("ok") for x in saved), saved)
        self.pa, self.pb = (x["pointer"] for x in saved)
        self.pausing = Pausing(self.app.server.handler)
        self.app.server.handler = self.pausing
        self.extra = []

    def tearDown(self):
        for c in self.extra:
            c.close()
        super().tearDown()

    def conn(self):
        ln = Line(self.app.server.address)
        self.extra.append(ln)
        return ln

    def paused(self, token, req, removal):
        c = self.conn()
        self.pausing.arm()
        c.c.sendall(json.dumps({"token": token, "request": req}).encode() + b"\n")
        self.assertTrue(self.pausing.paused.wait(5), "the sender did not reach the pause")
        removal()
        self.pausing.resume.set()
        return json.loads(c.f.readline())

    def ungrant(self, pool):
        out = self.conn().call(self.adm, op="pool_ungrant", who="user-carol", pool=pool)
        self.assertTrue(out.get("ok"), out)

    def test_an_ungrant_aborts_the_paused_response_over_the_product_path(self):
        self.assertEqual(self.paused(self.tok, {"op": "pool_fetch", "pointer": self.pa}, lambda: self.ungrant(self.A)), UNAV)
        self.assertEqual(self.app.server.aborted, 1)

    def test_the_tokens_own_revoke_aborts_its_paused_response_over_the_product_path(self):
        def revoke():
            out = self.conn().call(self.tok, op="token_revoke")
            self.assertTrue(out.get("ok"), out)
        self.assertEqual(self.paused(self.tok, {"op": "pool_fetch", "pointer": self.pa}, revoke), UNAV)

    def test_a_two_pool_query_is_aborted_whole_over_the_product_path(self):
        got = self.paused(self.tok, {"op": "pool_query", "query": "roost stack landing", "pools": [self.A, self.B]},
                          lambda: self.ungrant(self.B))
        self.assertEqual(got, UNAV)

    def test_control_an_ungrant_of_another_pool_leaves_the_response_whole_over_the_product_path(self):
        got = self.paused(self.tok, {"op": "pool_fetch", "pointer": self.pa}, lambda: self.ungrant(self.B))
        self.assertEqual(got.get("status"), "ok", got)
        self.assertEqual(self.app.server.aborted, 0)

    def test_a_removal_waits_for_the_paused_write_and_the_next_chunk_aborts_over_the_product_path(self):
        real = fence.handoff
        armed = {"on": True}
        paused, resume, done = threading.Event(), threading.Event(), threading.Event()
        log = []

        def hook():
            paused.set()
            resume.wait(5)
            log.append("first chunk written")

        def handoff(sock, data, f, check, **kw):
            if armed["on"]:
                armed["on"] = False
                kw["before_write"] = hook
            return real(sock, data, f, check, **kw)

        def removal():
            out = self.conn().call(self.adm, op="pool_ungrant", who="user-carol", pool=self.A)
            log.append("denied" if out.get("ok") else f"removal failed: {out}")
            done.set()

        c = self.conn()
        with mock.patch.object(fence, "handoff", handoff), mock.patch.object(fence, "SEND_BOUND", 1024):
            c.c.sendall(json.dumps({"token": self.tok, "request": {"op": "pool_fetch", "pointer": self.pa}}).encode() + b"\n")
            self.assertTrue(paused.wait(5))
            th = threading.Thread(target=removal)
            th.start()
            fz = self.app.server.fence_for(self.app.sessions.resolve(self.tok).tenant)
            for _ in range(5000):
                if fz._writers_waiting or done.is_set():
                    break
                time.sleep(0.001)
            waited = bool(fz._writers_waiting) and not done.is_set()
            resume.set()
            th.join(10)
            c.c.settimeout(5)
            got = b""
            while True:
                try:
                    part = c.c.recv(65536)
                except OSError:
                    break
                if not part:
                    break
                got += part
        self.assertTrue(waited, "the removal returned while a hand-off held the shared side")
        self.assertEqual(log, ["first chunk written", "denied"])
        self.assertNotIn(b"\n", got)
        self.assertLessEqual(len(got), 1024)
        self.assertEqual(self.app.server.aborted, 1)

    # ---- F16 (round 4): seeing a pool ---------------------------------------------------------------------------------------------
    def carded(self):
        c = self.conn().call(self.adm, op="pool_create", cls="base", kind="team", label=f"fence carded {self.tag}", card=CARD)
        self.assertTrue(c.get("ok"), c)
        self.assertTrue(self.conn().call(self.adm, op="pool_grant", who="user-carol", pool=c["pool"], ops=["query", "save"])["ok"])
        return c["pool"], self.scoped(b"user-carol", {c["pool"]: ["query", "save"], self.A: ["query", "save"]}, f"conv-f16-{self.tag}")

    def test_f16_a_fresh_describe_and_list_after_an_ungrant_over_the_product_path(self):
        pool, tok = self.carded()
        self.assertEqual(self.conn().call(tok, op="pool_describe", pool=pool).get("card"), CARD)
        self.ungrant(pool)
        got = self.conn().call(tok, op="pool_describe", pool=pool)
        self.assertEqual(got, UNAV)
        self.assertNotIn("fence card", json.dumps(got))
        listed = self.conn().call(tok, op="pool_list")
        self.assertEqual([r["pool"] for r in listed["pools"]], [self.A], listed)

    def test_f16_a_paused_describe_with_its_card_and_a_paused_list_are_aborted_by_an_ungrant_over_the_product_path(self):
        pool, tok = self.carded()
        self.assertEqual(self.paused(tok, {"op": "pool_describe", "pool": pool}, lambda: self.ungrant(pool)), UNAV)
        self.assertTrue(self.conn().call(self.adm, op="pool_grant", who="user-carol", pool=pool, ops=["query", "save"])["ok"])
        self.assertEqual(self.paused(tok, {"op": "pool_list"}, lambda: self.ungrant(pool)), UNAV)
        self.assertEqual(self.app.server.aborted, 2)

    def test_f16_control_an_ungrant_of_a_pool_the_answer_does_not_name_leaves_it_whole_over_the_product_path(self):
        pool, tok = self.carded()
        got = self.paused(tok, {"op": "pool_describe", "pool": pool}, lambda: self.ungrant(self.B))
        self.assertEqual(got.get("card"), CARD, got)
        self.assertEqual(self.app.server.aborted, 0)
