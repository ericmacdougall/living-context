"""Review finding M3: a scoped or delegated token reaches only the pool tools, listing and describing its scope, and its own revocation.
Every other operation the handler knows, and an operation it does not know, is refused for it; nothing behind the handler is called."""
import tempfile
import unittest

from living_context.gateway.admin import ADMIN_OPS
from living_context.gateway.handler import POOL_TOOLS, Gateway
from living_context.gateway.pool_admin import POOL_OPS
from tests.security.test_pool_tokens import Base, T, UNAV, World, q

SCOPED_OK = {"pool_save", "pool_update", "pool_query", "pool_fetch", "pool_holds", "pool_hold_show", "pool_release", "pool_retract", "pool_list",
             "pool_describe", "token_revoke", "token_renew", "token_delegate", "ping"}


class Recorder:
    def __init__(self, inner=None):
        self.calls, self.inner = [], inner

    def handle(self, op, *a, **kw):
        self.calls.append(op)
        return {"ok": True}


class ScopedDefaultDeny(Base):
    def gateway(self, w):
        self.svc, self.adm, self.wr = Recorder(), Recorder(), Recorder()
        return Gateway(None, w.sessions, {T: __import__("tests.security.test_pool_tokens", fromlist=["x"]).LAT[T]}, writers=lambda t: self.wr,
                       admin=self.adm, pools=lambda t: self.svc)

    def universe(self):
        return sorted(set(ADMIN_OPS) | set(POOL_OPS) | POOL_TOOLS | {"ping", "search", "lookup", "write", "mint_scoped", "delegate", "nope"})

    def check(self, gw, token, who):
        for op in self.universe():
            before = (len(self.svc.calls), len(self.adm.calls), len(self.wr.calls))
            r = gw.handle(token, {"op": op, "pool": "x" * 16, "cls": "base", "text": "t", "doc": {"text": "t"}, "pointer": "p", "query": "q"})
            if op in SCOPED_OK:
                continue
            self.assertEqual(r, UNAV, f"{who}: {op}")
            self.assertEqual(before, (len(self.svc.calls), len(self.adm.calls), len(self.wr.calls)), f"{who}: {op} reached a handler")
        # a scoped token may revoke only itself: naming another token is refused
        before = len(self.svc.calls)
        self.assertEqual(gw.handle(token, {"op": "token_revoke", "token_id": "0" * 16}), UNAV, who)
        self.assertEqual(len(self.svc.calls), before)

    def test_every_operation_outside_the_scoped_allow_list_is_refused_for_a_scoped_and_a_delegated_token(self):
        w = self.world()
        a = w.pool("alpha")
        gw = self.gateway(w)
        scoped = w.mint("alice", {a: q("query", "save")})
        self.check(gw, scoped, "scoped")
        child = w.sessions.delegate(scoped, {a: q("query")}, 60)
        self.check(gw, child, "child")
        # the administrator's own scoped token (an empty scope) and its delegated child: the review's repro
        adm = w.sessions.mint_scoped(T, b"user-admin", conversation="c-adm", scope={})
        self.check(gw, adm, "admin scoped")
        self.check(gw, w.sessions.delegate(adm, {}, 60), "admin child")
        # the allowed operations do reach the pool service
        n = len(self.svc.calls)
        gw.handle(scoped, {"op": "pool_query", "query": "x"})
        self.assertEqual(len(self.svc.calls), n + 1)

    def test_an_unscoped_administrator_session_still_reaches_the_administrator_operations(self):
        w = self.world()
        gw = self.gateway(w)
        tok, _ = w.admin_rec()
        self.assertEqual(gw.handle(tok, {"op": "pool_detach", "pool": "x" * 16}), {"ok": True})
        self.assertEqual(self.svc.calls, ["pool_detach"])


if __name__ == "__main__":
    unittest.main()
