"""F10 and F16, class guard (e): every pool an answer names is re-checked live at its hand-off.

The universe is enumerated, not listed: every operation a scoped token may call (`handler.SCOPED_OPS`, which includes every pool tool)
and every pool people operation (`pool_admin.POOL_OPS`), each run through `Gateway.serve` over the pools world of
tests/unit/pool_world.py, by every caller that can see pools through it (pool_list and pool_describe by a scoped token, a member's plain
session and a tenant administrator's plain session; the hold review by a human token). For every answer (successful or not) and every
pool X it names (a pool id anywhere in it, or the pool sealed in a pointer it carries):
* X is in `reply.needs`, what the hand-off re-checks (POOLS 2.4: "for every pool whose content the response carries"; F16: and every
  pool it names, with `VIEW`);
* the hand-off passes as built (`Gateway.send_ok`), so the re-check is not a blanket refusal;
* once the caller's access to X is removed (the member's grant through the real journalled ungrant, or, for the administrator, the pool
  leaving the directory), the hand-off refuses, and once it is restored it passes again.
There is no exemption table: a member with no request row, or one that answers nothing in this world, fails the guard (round 4: the
EXEMPT table of round 3 kept pool_list and pool_describe out, review F16).

Also here: the bearer's own revoke receipt is the one answer without a token check, keyed on its outcome; the collector is reset per
request and unions repeated checks; a query over more pools than one call's cap is re-checked whole; the composition root's handler
takes the full send check; a handle-only handler is never checked by the epoch alone.
"""
import ast
import inspect
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

from living_context.gateway import app as app_mod
from living_context.gateway import pointers, pool_admin, pool_authz, pool_tools
from living_context.gateway.handler import SCOPED_OPS, Gateway, Reply
from living_context.gateway.pool_admin import POOL_OPS
from living_context.gateway.pool_tools import PoolTools
from living_context.gateway.server import Server
from living_context.policy import pools as pl
from tests.unit.pool_world import OPS, T, TEXT, UNAV, World


def erased(cls, pool):
    """The eraser of this world (the erasure itself is tested in tests/unit/test_pool_erase_discovery.py and the `_es` tests)."""
    return types.SimpleNamespace(docs_deleted=0, indices_dropped=[])


def requests(w, tok, human, hid, pa, pb):
    """The request rows: op -> [(caller, token, request, before)], `before` run first (or None)."""
    A, B = w.pools
    member = w.plain()
    first_delete = lambda: w.gw.handle(w.admin, {"op": "pool_delete", "pool": B})   # the first of the two administrators
    return {
        "pool_save": [("scoped", tok, {"pool": A, "text": "the gannet roost is on the arch", "key": "g1"}, None)],
        "pool_update": [("scoped", tok, {"pointer": pa, "mode": "correct", "text": TEXT.replace("stack", "arch"), "key": "g2"}, None)],
        "pool_query": [("scoped", tok, {"query": "gannet roost", "pools": [A, B]}, None)],
        "pool_fetch": [("scoped", tok, {"pointer": pa}, None)],
        "pool_holds": [("scoped", tok, {"pool": A}, None)],
        "pool_hold_show": [("human", human, {"pool": A, "hold": hid}, None)],
        "pool_release": [("human", human, {"pool": A, "hold": hid, "decision": "discard"}, None)],
        "pool_retract": [("scoped", tok, {"pointers": [pb], "reason": "other", "key": "g3"}, None)],
        "pool_list": [("scoped", tok, {}, None), ("member", member, {}, None), ("admin", w.admin, {}, None)],
        "pool_describe": [("scoped", tok, {"pool": A}, None), ("member", member, {"pool": A}, None), ("admin", w.admin, {"pool": A}, None)],
        "token_revoke": [("scoped", tok, {}, None)],
        "token_renew": [("scoped", tok, {}, None)],
        "token_delegate": [("scoped", tok, {"scope": {A: ["query"]}, "ttl_s": 60}, None)],
        "ping": [("scoped", tok, {}, None)],
        "pool_create": [("admin", w.admin, {"cls": "base", "kind": "team", "label": "charlie"}, None)],
        "pool_detach": [("admin", w.admin, {"pool": B}, None)],
        "pool_reattach": [("admin", w.admin, {"pool": B}, None)],
        "pool_grant": [("admin", w.admin, {"who": "user-alice", "pool": B, "ops": ["query"]}, None)],
        "pool_ungrant": [("admin", w.admin, {"who": "user-alice", "pool": B}, None)],
        "pool_delete": [("admin", w.admin2, {"pool": B}, first_delete)],
    }


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


def remove_access(w, caller, pool):
    """Take `pool` away from the caller; returns the restore. Alice's tokens and sessions lose her grant through the real journalled
    ungrant; the administrator sees every pool of the tenant, so for it the pool leaves the directory (under the fence, as a delete)."""
    if caller == "admin":
        gone = w.dir.get(T, pool)
        held = w.dir.live_ops(T, "pt-user-alice", pool)
        w.epochs.under_fence(T, lambda: w.dir.drop(T, pool))

        def restore():
            w.dir.restore(gone)
            if held:
                w.dir.grant(T, "pt-user-alice", pool, held)
        return restore
    assert w.admin_op(op="pool_ungrant", who="user-alice", pool=pool).get("ok") is True
    return lambda: w.dir.grant(T, "pt-user-alice", pool, OPS)


def findings(patches=()) -> tuple:
    """(uncovered, driven): (op, caller, what) for every answer that names a pool its hand-off does not re-check live, and the members
    that answered `ok` at least once."""
    uncovered, driven = [], set()
    for op in sorted(universe()):
        with tempfile.TemporaryDirectory() as d:
            probe = World(Path(d))
            tok = probe.scoped("c1")
            rows = requests(probe, tok, tok, "h", "", "")
            probe.close()
        if op not in rows:
            uncovered.append((op, "-", "no request row: add one so the guard can call it"))
            continue
        for i in range(len(rows[op])):
            with tempfile.TemporaryDirectory() as d:
                w = World(Path(d), eraser=erased)
                w.all_pools = lambda w=w: {p.id for p in w.dir.pools_of(T)} | set(w.pools)
                tok, human = w.scoped("c1"), w.scoped("h1", human=True)
                pa = w.gw.handle(tok, {"op": "pool_save", "pool": w.pools[0], "text": TEXT, "key": "a1"})["pointer"]
                pb = w.gw.handle(tok, {"op": "pool_save", "pool": w.pools[1], "text": TEXT, "key": "b1"})["pointer"]
                hid = w.hold(w.pools[0], [w.pools[1]])
                caller, token, req, before = requests(w, tok, human, hid, pa, pb)[op][i]
                if before is not None:
                    before()
                with mock.patch.object(PoolTools, "_pool_candidates", w.leaf_candidates()):
                    for p in patches:
                        p.start()
                    try:
                        reply = w.gw.serve(token, {"op": op, **req})
                        if isinstance(reply.body, dict) and reply.body.get("ok") is True:
                            driven.add(op)
                        named = named_pools(w, reply.body)
                        missing = named - set(reply.needs)
                        if missing:
                            uncovered.append((op, caller, f"not re-checked: {sorted(missing)}"))
                        elif named and not w.gw.send_ok(reply):
                            uncovered.append((op, caller, "the hand-off refuses the answer as built"))
                        else:
                            for x in sorted(named):
                                restore = remove_access(w, caller, x)
                                if w.gw.send_ok(reply):
                                    uncovered.append((op, caller, f"sent after access to {x} was removed"))
                                restore()
                                if not w.gw.send_ok(reply):
                                    uncovered.append((op, caller, f"still refused after access to {x} was restored"))
                    finally:
                        for p in patches:
                            p.stop()
                w.close()
    return uncovered, driven


def scope_membership_only(sessions, directory, lattices, rec, pool_id):
    """The visibility of round 3 (planted): a scoped token sees its scope, any other session every pool; no live grant, no class."""
    return directory.get(rec.tenant, pool_id) if rec.scope is None or pool_id in rec.scope else None


class ReplyNeedsGuard(unittest.TestCase):
    def test_every_pool_an_answer_names_is_re_checked_live_at_its_hand_off(self):
        uncovered, driven = findings()
        self.assertEqual(uncovered, [])
        self.assertEqual(universe() - driven, set(), "a member answered nothing in this world: drive it, there is no exemption")
        # not vacuous: the content tools and the pool directory answers named pools (each was then removed and restored above)
        self.assertTrue({"pool_save", "pool_update", "pool_query", "pool_fetch", "pool_retract", "pool_hold_show", "pool_list",
                         "pool_describe", "pool_create"} <= driven, driven)

    def test_planted_bypasses_are_caught(self):
        # a query that chooses its pools but does not carry them (the collector call removed)
        uncovered, _ = findings([mock.patch.object(pool_tools, "carry", lambda needs: None)])
        self.assertIn("pool_query", {op for op, _, _ in uncovered})
        # every tool authorization made without carrying (a tool that checks but does not record what it reads)
        real = pool_tools.authorize
        uncovered, _ = findings([mock.patch.object(pool_tools, "authorize", lambda *a, **kw: real(*a, **{**kw, "carry_needs": False}))])
        self.assertTrue({"pool_fetch", "pool_save", "pool_update", "pool_hold_show"} <= {op for op, _, _ in uncovered}, uncovered)
        # the directory answers checked at the call but not carried (round 3: list, describe and the create receipt were exempt)
        real_view = pool_admin.view
        uncovered, _ = findings([mock.patch.object(pool_admin, "view", lambda *a, **kw: real_view(*a, **{**kw, "carry_needs": False})),
                                 mock.patch.object(pool_admin, "carry", lambda needs: None)])
        got = {(op, caller) for op, caller, _ in uncovered}
        self.assertTrue({("pool_list", c) for c in ("scoped", "member", "admin")} | {("pool_describe", c) for c in ("scoped", "member",
                        "admin")} | {("pool_create", "admin")} <= got, uncovered)
        # visibility by scope membership (round 3), at the call and at the hand-off: an ungranted pool is still sent
        uncovered, _ = findings([mock.patch.object(pool_authz, "sees", scope_membership_only)])
        got = {(op, caller) for op, caller, what in uncovered if what.startswith("sent after")}
        self.assertTrue({("pool_list", "scoped"), ("pool_list", "member"), ("pool_describe", "scoped"), ("pool_describe", "member")}
                        <= got, uncovered)
        # a hand-off that re-checks content operations only (round 3's `send_ok`): it refuses every answer that names a pool by VIEW
        uncovered, _ = findings([mock.patch.object(PoolTools, "send_ok", lambda self, token, needs: pool_authz.authorize(
            self.sessions, self.dir, self.lattices, token, needs, carry_needs=False, limit=None) is not None)])
        got = {(op, caller) for op, caller, what in uncovered if what == "the hand-off refuses the answer as built"}
        self.assertTrue({("pool_list", "admin"), ("pool_describe", "member"), ("pool_create", "admin")} <= got, uncovered)

    def test_every_tool_authorization_is_carried(self):
        # the tools reach pools only through `authorize`; the only call that does not carry is query's choice of G (it carries S
        # itself); the hand-off's re-check is `pool_authz.recheck`
        src = inspect.getsource(pool_tools)
        quiet = set()
        for f in ast.walk(ast.parse(src)):
            if isinstance(f, ast.FunctionDef):
                for n in ast.walk(f):
                    if isinstance(n, ast.Call) and getattr(n.func, "id", "") == "authorize" and any(k.arg == "carry_needs" for k in n.keywords):
                        quiet.add(f.name)
        self.assertEqual(quiet, {"query"})
        self.assertIn("carry({pid: {pl.QUERY} for pid in S})", src)
        self.assertIn("return recheck(self.sessions, self.dir, self.lattices, token, needs)", inspect.getsource(PoolTools.send_ok))


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
