"""F10, class guard (e): what the send hand-off re-checks covers what the answer carries.

The universe is enumerated, not listed: every operation a scoped token may call (`handler.SCOPED_OPS`, which includes every pool tool)
and every pool people operation (`pool_admin.POOL_OPS`, called by the administrator), each run through `Gateway.serve` over the pools
world of tests/unit/pool_world.py. For each successful answer, every pool it names (a pool id anywhere in it, or the pool sealed in a
pointer it carries) must be in `reply.needs`, the pools the hand-off re-checks (POOLS 2.4: "for every pool whose content the response
carries"). A member with no request row below fails the guard, so a new tool cannot join unchecked. The exemptions are commented in
EXEMPT; the members this world cannot drive to a successful answer are in NOT_DRIVEN, each with the reason and where it is covered.

Also here: the bearer's own revoke receipt is the one answer without a token check, keyed on its outcome; the collector is reset per
request and unions repeated checks; a query over more pools than one call's cap is re-checked whole; the composition root's handler
takes the full send check; a handle-only handler is never checked by the epoch alone.
"""
import ast
import inspect
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from living_context.gateway import app as app_mod
from living_context.gateway import pointers, pool_authz, pool_tools
from living_context.gateway.handler import SCOPED_OPS, Gateway, Reply
from living_context.gateway.pool_admin import POOL_OPS
from living_context.gateway.pool_tools import PoolTools
from living_context.gateway.server import Server
from living_context.policy import pools as pl
from tests.unit.pool_world import T, TEXT, UNAV, World

# The pool people operations answer pool metadata (id, label, class, kind, state, card) or a receipt, never memory content; the hand-off
# re-checks their token (checks 1 to 3) only. ASSUMPTION (docs/decisions/design-readings.md, F10): metadata is not "content" in 2.4.
EXEMPT = {
    "pool_list": "pool metadata, visible by `PoolService._visible` at the call",
    "pool_describe": "pool metadata, visible by `PoolService._visible` at the call",
    "pool_create": "the receipt names the new pool, created by this administrator",
}
# Members this world cannot bring to a successful answer, and where their re-check is covered instead.
NOT_DRIVEN = {
    "pool_hold_show": "needs a held save (a tainted conversation, SessionStates over Elasticsearch); its checks are `_hold_auth`'s "
                      "`authorize` calls, carried like every other (test_every_tool_authorization_is_carried below)",
    "pool_release": "as pool_hold_show",
    "pool_delete": "the two-administrator erasure; its receipt names the deleted pool, which is gone",
    "pool_create": "needs index creation and the authz copy (Elasticsearch); its receipt is EXEMPT above",
}


def requests(w, tok, pa, pb):
    """One request row per member: (token, request)."""
    A, B = w.pools
    rows = {
        "pool_save": (tok, {"pool": A, "text": "the gannet roost is on the arch", "key": "g1"}),
        "pool_update": (tok, {"pointer": pa, "mode": "correct", "text": TEXT.replace("stack", "arch"), "key": "g2"}),
        "pool_query": (tok, {"query": "gannet roost", "pools": [A, B]}),
        "pool_fetch": (tok, {"pointer": pa}),
        "pool_holds": (tok, {"pool": A}),
        "pool_hold_show": (tok, {"pool": A, "hold": "h"}),
        "pool_release": (tok, {"pool": A, "hold": "h", "decision": "discard"}),
        "pool_retract": (tok, {"pointers": [pb], "reason": "other", "key": "g3"}),
        "pool_list": (tok, {}),
        "pool_describe": (tok, {"pool": A}),
        "token_revoke": (tok, {}),
        "token_renew": (tok, {}),
        "token_delegate": (tok, {"scope": {A: ["query"]}, "ttl_s": 60}),
        "ping": (tok, {}),
        "pool_create": (w.admin, {"cls": "base", "kind": "team", "label": "charlie"}),
        "pool_detach": (w.admin, {"pool": B}),
        "pool_reattach": (w.admin, {"pool": B}),
        "pool_grant": (w.admin, {"who": "user-alice", "pool": B, "ops": ["query"]}),
        "pool_ungrant": (w.admin, {"who": "user-alice", "pool": B}),
        "pool_delete": (w.admin, {"pool": B}),
    }
    return rows


def universe() -> set:
    return set(SCOPED_OPS) | set(POOL_OPS)


def named_pools(w, body) -> set:
    """Every pool an answer names: a pool id in any string (an envelope carries `pool="..."`), or the pool sealed in a pointer."""
    out, stack = set(), [body]
    while stack:
        x = stack.pop()
        if isinstance(x, dict):
            stack.extend(x.keys())
            stack.extend(x.values())
        elif isinstance(x, list):
            stack.extend(x)
        elif isinstance(x, str):
            out |= {p for p in w.all_pools() if p in x}
            s = pointers.open_(w.keys, T, x) if len(x) > 40 else None
            if s is not None:
                out.add(s.pool)
    return out


def findings(patches=()) -> tuple:
    """(uncovered, driven): members whose successful answer names a pool outside `reply.needs`, and the members that answered `ok`."""
    uncovered, driven = [], set()
    for op in sorted(universe()):
        with tempfile.TemporaryDirectory() as d:
            w = World(Path(d))
            w.all_pools = lambda w=w: {p.id for p in w.dir.pools_of(T)}
            tok = w.scoped("c1")
            pa = w.gw.handle(tok, {"op": "pool_save", "pool": w.pools[0], "text": TEXT, "key": "a1"})["pointer"]
            pb = w.gw.handle(tok, {"op": "pool_save", "pool": w.pools[1], "text": TEXT, "key": "b1"})["pointer"]
            rows = requests(w, tok, pa, pb)
            if op not in rows:
                uncovered.append((op, "no request row: add one so the guard can call it"))
                w.close()
                continue
            token, req = rows[op]
            with mock.patch.object(PoolTools, "_pool_candidates", w.leaf_candidates()):
                for p in patches:
                    p.start()
                try:
                    reply = w.gw.serve(token, {"op": op, **req})
                finally:
                    for p in patches:
                        p.stop()
            if isinstance(reply.body, dict) and reply.body.get("ok") is True:
                driven.add(op)
                missing = named_pools(w, reply.body) - set(reply.needs)
                if missing and op not in EXEMPT:
                    uncovered.append((op, sorted(missing)))
            w.close()
    return uncovered, driven


class ReplyNeedsGuard(unittest.TestCase):
    def test_every_pool_an_answer_names_is_re_checked_at_its_hand_off(self):
        uncovered, driven = findings()
        self.assertEqual(uncovered, [])
        # not vacuous: the content-carrying tools answered and named pools
        self.assertTrue({"pool_save", "pool_update", "pool_query", "pool_fetch", "pool_retract"} <= driven, driven)
        self.assertEqual(universe() - driven - set(NOT_DRIVEN), set(), "a member answered nothing here and has no NOT_DRIVEN reason")

    def test_planted_bypasses_are_caught(self):
        # a query that chooses its pools but does not carry them (the collector call removed)
        uncovered, _ = findings([mock.patch.object(pool_tools, "carry", lambda needs: None)])
        self.assertIn("pool_query", {op for op, _ in uncovered})
        # every tool authorization made without carrying (a tool that checks but does not record what it reads)
        real = pool_tools.authorize
        uncovered, _ = findings([mock.patch.object(pool_tools, "authorize", lambda *a, **kw: real(*a, **{**kw, "carry_needs": False}))])
        self.assertTrue({"pool_fetch", "pool_save", "pool_update"} <= {op for op, _ in uncovered}, uncovered)

    def test_every_tool_authorization_is_carried(self):
        # the members NOT_DRIVEN here reach pools only through `authorize`; the only calls that do not carry are query's choice of G
        # (it carries S itself) and the hand-off's own re-check
        src = inspect.getsource(pool_tools)
        quiet = set()
        for f in ast.walk(ast.parse(src)):
            if isinstance(f, ast.FunctionDef):
                for n in ast.walk(f):
                    if isinstance(n, ast.Call) and getattr(n.func, "id", "") == "authorize" and any(k.arg == "carry_needs" for k in n.keywords):
                        quiet.add(f.name)
        self.assertEqual(quiet, {"query", "send_ok"})
        self.assertIn("carry({pid: {pl.QUERY} for pid in S})", src)


class Exemptions(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.w = World(Path(self._tmp.name))
        self.tok = self.w.scoped("c1")

    def tearDown(self):
        self.w.close()
        self._tmp.cleanup()

    def test_only_the_bearers_own_successful_revoke_receipt_goes_without_a_token_check(self):
        self.assertIsNone(self.w.gw.serve(self.tok, {"op": "token_revoke"}).token)
        other = self.w.scoped("c2")
        tid = self.w.sessions.token_id(self.w.sessions.resolve(other))
        by_id = self.w.gw.serve(self.w.admin, {"op": "token_revoke", "token_id": tid})
        self.assertTrue(by_id.body.get("ok"))
        self.assertEqual(by_id.token, self.w.admin)       # an administrator's receipt keeps the administrator's token check
        third = self.w.scoped("c3")
        self.assertEqual(self.w.gw.serve(third, {"op": "token_revoke", "token_id": "x"}).token, third)   # refused: checked
        with mock.patch.object(self.w.journal, "journalled", side_effect=KeyError("refused")):
            failed = self.w.gw.serve(third, {"op": "token_revoke"})
        self.assertEqual(failed.body, UNAV)
        self.assertEqual(failed.token, third)              # a revoke that did not land is checked like any answer
        self.assertFalse(self.w.gw.send_ok(Reply({"ok": True}, None, {self.w.pools[0]: frozenset({"query"})})))

    def test_the_collector_is_per_request_and_unions_repeated_checks(self):
        A, B = self.w.pools
        with pool_authz.carried() as box:
            for needs in ({A: {pl.QUERY}}, {A: {pl.SAVE}}, {B: {pl.QUERY}}):
                self.assertIsNotNone(pool_authz.authorize(self.w.sessions, self.w.dir, {T: self.w.service.lattice}, self.tok, needs))
        self.assertEqual(box, {A: {pl.QUERY, pl.SAVE}, B: {pl.QUERY}})
        self.assertIsNone(pool_authz._CARRIED.get())
        ptr = self.w.gw.handle(self.tok, {"op": "pool_save", "pool": A, "text": TEXT, "key": "k"})["pointer"]
        self.assertEqual(set(self.w.gw.serve(self.tok, {"op": "pool_fetch", "pointer": ptr}).needs), {A})
        self.assertEqual(self.w.gw.serve(self.tok, {"op": "ping"}).needs, {})   # nothing left over from the fetch

    def test_a_query_over_more_pools_than_one_calls_cap_is_re_checked_whole(self):
        with tempfile.TemporaryDirectory() as d:
            w = World(Path(d), n_pools=pool_authz.MAX_POOLS_PER_CALL + 1)
            tok = w.scoped("c1")
            for i, pid in enumerate(w.pools):
                self.assertTrue(w.gw.handle(tok, {"op": "pool_save", "pool": pid, "text": TEXT, "key": f"k{i}"})["ok"])
            with mock.patch.object(PoolTools, "_pool_candidates", w.leaf_candidates()):
                reply = w.gw.serve(tok, {"op": "pool_query", "query": "gannet roost"})
            self.assertTrue(reply.body.get("ok"), reply.body)
            self.assertEqual(set(reply.needs), set(w.pools))
            self.assertTrue(w.gw.send_ok(reply), "the hand-off refused a query the call was allowed to make")
            self.assertTrue(w.admin_op(op="pool_ungrant", who="user-alice", pool=w.pools[-1])["ok"])
            self.assertFalse(w.gw.send_ok(reply))
            w.close()

    def test_the_composition_roots_handler_takes_the_full_send_check(self):
        tree = ast.parse(inspect.getsource(app_mod))
        built = [n for n in ast.walk(tree) if isinstance(n, ast.Call) and getattr(n.func, "id", "") == "Server"]
        self.assertEqual(len(built), 1)
        self.assertEqual(getattr(built[0].args[0], "id", None), "gw")
        gw = [n for n in ast.walk(tree) if isinstance(n, ast.Assign) and any(getattr(t, "id", "") == "gw" for t in n.targets)]
        self.assertEqual([getattr(n.value.func, "id", None) for n in gw], ["Gateway"])
        self.assertTrue(callable(getattr(Gateway, "serve", None)) and callable(getattr(Gateway, "send_ok", None)))

    def test_a_handle_only_handler_is_never_checked_by_the_epoch_alone(self):
        class HandleOnly:
            def handle(self, token, request):
                return {"ok": True}

        srv = Server(HandleOnly(), self.w.sessions, self.w.epochs, self.w.fence_for)
        try:
            rec = self.w.sessions.resolve(self.tok)
            reply = srv._reply(self.tok, {"op": "ping"})
            check = srv._check(rec, reply)
            self.assertTrue(check())
            self.assertTrue(self.w.gw.handle(self.tok, {"op": "token_revoke"})["ok"])   # a token revoke: the epoch does not move
            self.assertTrue(self.w.epochs.current(rec.tenant, rec.principal_token, rec.epoch))
            self.assertFalse(check())
        finally:
            srv.stop()


if __name__ == "__main__":
    unittest.main()
