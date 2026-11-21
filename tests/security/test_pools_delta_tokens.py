"""Pools delta review 1 (goodqa#16441), classes C2 and C1 on the gateway's own state, no Elasticsearch: every token operation (mint,
delegate, renew, revoke) goes through one validation choke point (finite, positive, bounded lifetime; tenant binding), the socket parser
refuses non-standard JSON numbers, and the pool and token operations are refused while the tenant's journal does not admit.
Each case was red on e979747 (F7, F8, F1 runtime half) before the fix."""
import base64
import json
import math
import unittest

from living_context.gateway import admission
from living_context.gateway.handler import Gateway
from living_context.gateway.pool_tools import PoolRouter
from living_context.gateway.sessions import Record, TokenError
from living_context.policy import Principal, Role
from tests.security.test_pool_tokens import LAT, T, TB, UNAV, Base

BAD_TTLS = (float("nan"), float("inf"), float("-inf"), 0.0, -5.0)


class Ttl(Base):
    def test_f7_a_non_finite_or_non_positive_lifetime_is_refused_on_every_token_operation(self):
        w = self.world()
        a = w.pool("alpha")
        for ttl in BAD_TTLS:
            with self.assertRaises(TokenError, msg=f"mint ttl {ttl}"):
                w.mint("alice", {a: {"query"}}, conv=f"m{ttl}", ttl_s=ttl)
        root = w.mint("alice", {a: {"query"}})
        for ttl in BAD_TTLS:
            with self.assertRaises(TokenError, msg=f"delegate ttl {ttl}"):
                w.sessions.delegate(root, {a: frozenset({"query"})}, ttl)
        # no token in the table has a non-finite expiry
        self.assertTrue(all(math.isfinite(r.exp) for r in w.sessions._by_hash.values() if r.scope is not None))

    def test_f7_through_the_handler_a_nan_delegate_is_unavailable_and_the_child_cannot_outlive_its_parent(self):
        w = self.world()
        a = w.pool("alpha")
        gw = Gateway(None, w.sessions, LAT, pools=lambda t: PoolRouter(w.svc, None) if t == T else None)
        root = w.mint("alice", {a: {"query"}})
        for ttl in BAD_TTLS:
            self.assertEqual(gw.handle(root, {"op": "token_delegate", "scope": {a: ["query"]}, "ttl_s": ttl}), UNAV, ttl)
        ok = gw.handle(root, {"op": "token_delegate", "scope": {a: ["query"]}, "ttl_s": 60})
        self.assertTrue(ok["ok"], ok)
        # renewing the root never carries a child past its own lifetime
        child = w.sessions.resolve(ok["token"])
        w.clock.t += 61
        self.assertTrue(gw.handle(root, {"op": "token_renew"})["ok"])
        self.assertIsNone(w.sessions.resolve(ok["token"]), child)

    def test_f7_a_restored_token_record_with_a_non_finite_expiry_is_not_restored(self):
        w = self.world()
        w.sessions.restore_token("e" * 64, {"tenant": T, "principal": "pt-user-alice", "epoch": 0, "exp": float("nan"), "scope": {"x": ["query"]}})
        self.assertNotIn("e" * 64, w.sessions._by_hash)

    def test_f7_the_socket_parser_refuses_nan_and_infinity(self):
        for raw in (b'{"token": "x", "request": {"ttl_s": NaN}}', b'{"token": "x", "request": {"ttl_s": Infinity}}',
                    b'{"token": "x", "request": {"ttl_s": -Infinity}}', b'{"token": "x", "request": {"ttl_s": 1e400}}'):
            with self.assertRaises(admission.Refused, msg=raw):
                admission.parse(raw, max_structures=100)
        self.assertEqual(admission.parse(b'{"a": 1.5}', max_structures=10), {"a": 1.5})


class TenantBoundRevoke(Base):
    def test_f8_an_administrator_of_one_tenant_cannot_revoke_a_token_of_another_tenant_sharing_the_table(self):
        w = self.world()
        # tenant B in the same session table and directory: a pool, a grant and a live scoped token of bob@B
        w.store.restore(Principal(TB, "pt-user-bob", frozenset({Role.READER}), frozenset({"base"}), frozenset({"base"})))
        pb = w.dir.create(TB, "base", "team", "bravo", "")
        w.dir.grant(TB, "pt-user-bob", pb.id, frozenset({"query"}))
        tb = w.sessions.mint_scoped(TB, b"user-bob", conversation="cb", scope={pb.id: frozenset({"query"})})
        rec_b = w.sessions.resolve(tb)
        self.assertIsNotNone(rec_b)
        tid = w.sessions.token_id(rec_b)
        out = w.admin("token_revoke", token_id=tid)
        self.assertEqual(out, UNAV, out)                                   # not a token of this tenant: the one answer
        self.assertIsNotNone(w.sessions.resolve(tb), "a token of tenant B was revoked by tenant A's administrator")
        # the lookup itself is tenant-bound: another tenant's token never makes an id ambiguous or resolvable
        self.assertIsNone(w.sessions.find_by_id(T, tid))
        self.assertEqual(w.sessions.find_by_id(TB, tid), rec_b.hash)
        # and an administrator of B can
        a = w.pool("alpha")
        ta = w.mint("alice", {a: {"query"}})
        self.assertTrue(w.admin("token_revoke", token_id=w.sessions.token_id(w.sessions.resolve(ta)))["ok"])
        self.assertIsNone(w.sessions.resolve(ta))


class JournalAdmission(Base):
    def test_f1_while_the_tenant_journal_is_faulted_every_pool_and_token_operation_is_unavailable(self):
        w = self.world()
        a = w.pool("alpha")
        router = PoolRouter(w.svc, None)                                    # admission is read from the service's journal
        gw = Gateway(None, w.sessions, LAT, pools=lambda t: router if t == T else None)
        root = w.mint("alice", {a: {"query"}})
        self.assertTrue(gw.handle(root, {"op": "token_delegate", "scope": {a: ["query"]}, "ttl_s": 60})["ok"])
        w.rev._fault("injected journal fault")                              # the state a journal write error leaves (revocation._append)
        for req in ({"op": "token_delegate", "scope": {a: ["query"]}, "ttl_s": 60}, {"op": "token_renew"}, {"op": "pool_list"},
                    {"op": "pool_describe", "pool": a}, {"op": "pool_query", "query": "x"}):
            self.assertEqual(gw.handle(root, req), UNAV, req["op"])
        self.assertEqual(gw.mint(T, b"user-alice", {"conversation": "c2", "scope": {a: ["query"]}}), UNAV)

    def test_f1_rescue_a_faulted_journal_closes_plain_mints_and_class_operations_too(self):
        """Review round 3 F1: the plain-session mint line and the class operations (ping, search, lookup, write) skipped admission."""
        w = self.world()
        router = PoolRouter(w.svc, None)
        calls = []
        writer = type("W", (), {"remember": lambda self, *a, **kw: calls.append("remember")})()
        gw = Gateway(None, w.sessions, LAT, writers=lambda t: writer, pools=lambda t: router if t == T else None)
        proof = base64.b64encode(b"user-alice").decode()
        ans, plain = gw.mint_line({"tenant": T, "proof": proof, "request": {"session": "plain"}})
        self.assertTrue(ans["ok"])                                                     # the control: an open journal mints
        self.assertEqual(gw.handle(plain, {"op": "ping"}), {"ok": True})
        w.rev._fault("injected journal fault")
        self.assertEqual(gw.mint_line({"tenant": T, "proof": proof, "request": {"session": "plain"}}), (UNAV, None))
        for req in ({"op": "ping"}, {"op": "search", "cls": "base", "text": "x"}, {"op": "lookup", "pointer": "x"},
                    {"op": "write", "cls": "base", "doc": {"text": "x"}}):
            self.assertEqual(gw.handle(plain, req), UNAV, req["op"])
        self.assertEqual(calls, [])


if __name__ == "__main__":
    unittest.main()
