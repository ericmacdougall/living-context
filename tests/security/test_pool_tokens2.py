"""BUILD-PLAN Step 25 (POOLS 2, 11), continued: token scope, attenuation, per-token and per-pool revocation on the epoch journal,
restart. Every negative case runs against the real code and against a broken variant (`broken_*` flags) that must turn it red."""
import tempfile
import unittest

from living_context.config import Constants
from living_context.gateway.sessions import TokenError
from living_context.policy import pools as pl
from tests.security.test_pool_tokens import Base, T, TB, UNAV, World, q
from tests.unit.fake_es import unconfirmed_puts


class Tokens(Base):
    def test_a_token_for_pool_a_is_refused_on_b_and_on_a_missing_pool_alike(self):
        w = self.world()
        a, b = w.pool("alpha"), w.pool("beta")
        tok = w.mint("alice", {a: q("query", "save")})
        self.assertIsNotNone(w.auth(tok, {a: q("query")}))
        self.assertIsNone(w.auth(tok, {b: q("query")}))
        self.assertIsNone(w.auth(tok, {"z" * 16: q("query")}))      # a pool that does not exist: the same None
        self.assertIsNone(w.auth(tok, {"bad id": q("query")}))
        # BROKEN: the token scope is not consulted, the live grant alone decides
        self.assertIsNotNone(w.auth(tok, {b: q("query")}, broken_scope_not_checked=True))

    def test_operations_are_checked_per_pool_and_none_implies_another(self):
        w = self.world()
        a = w.pool("alpha", ops={"query", "save", "update"})
        qonly = w.mint("alice", {a: q("query")})
        self.assertIsNone(w.auth(qonly, {a: q("save")}))
        self.assertIsNone(w.auth(qonly, {a: q("save", "update")}))
        upd = w.mint("alice", {a: q("update")}, conv="c2")
        self.assertIsNone(w.auth(upd, {a: q("save", "update")}))      # update without save cannot pool_update
        self.assertIsNotNone(w.auth(w.mint("alice", {a: q("save", "update")}, conv="c3"), {a: q("save", "update")}))
        self.assertIsNotNone(w.auth(qonly, {a: q("save")}, broken_scope_not_checked=True))   # BROKEN: operations not checked per pool

    def test_a_plain_session_and_an_empty_scope_reach_no_pool(self):
        w = self.world()
        a = w.pool("alpha")
        self.assertIsNone(w.auth(w.sessions.mint(T, b"user-alice"), {a: q("query")}))           # NEG-1: no scope, least privilege
        self.assertIsNone(w.auth(w.mint("alice", {}, conv="e"), {a: q("query")}))

    def test_a_pool_not_held_cannot_be_minted_into_and_reveals_nothing(self):
        w = self.world()
        a, b = w.pool("alpha"), w.pool("beta", grant_to=())
        for pid in (b, "z" * 16):
            with self.assertRaises(TokenError) as e:
                w.mint("alice", {pid: q("query")})
            self.assertEqual(e.exception.code, "unavailable")       # the same code for a pool that exists and one that does not
        with self.assertRaises(TokenError):
            w.mint("alice", {a: q("review")})                        # an operation not held

    def test_expiry_and_the_ttl_cap(self):
        w = self.world()
        a = w.pool("alpha")
        tok = w.mint("alice", {a: q("query")}, ttl_s=10**9)         # the request cannot exceed TOKEN_TTL
        self.assertIsNotNone(w.auth(tok, {a: q("query")}))
        w.clock.t += w.consts.token_ttl_s + 1
        self.assertIsNone(w.auth(tok, {a: q("query")}))
        with self.assertRaises(TokenError) as e:
            w.sessions.renew(tok)
        self.assertEqual(e.exception.code, "expired")                # an expired token cannot renew
        w2 = World(tempfile.mkdtemp(), sess_kw={"broken_no_expiry": True}).start()   # BROKEN: expiry not checked
        a2 = w2.pool("alpha")
        t2 = w2.mint("alice", {a2: q("query")})
        w2.clock.t += w2.consts.token_ttl_s + 1
        self.assertIsNotNone(w2.auth(t2, {a2: q("query")}))

    def test_epoch_bump_ends_a_token(self):
        w = self.world()
        a = w.pool("alpha")
        tok = w.mint("alice", {a: q("query")})
        w.rev.bump("pt-user-alice")
        self.assertIsNone(w.auth(tok, {a: q("query")}))

    def test_replay_in_another_tenant_or_with_a_lookalike_prefix_fails(self):
        w = self.world()
        a = w.pool("alpha")
        tok = w.mint("alice", {a: q("query")})
        _, _, rest = tok.partition(".")
        for forged in (f"{TB}.{rest}", f"{T}x.{rest}", f"tenaa.{rest}"):
            self.assertIsNone(w.sessions.resolve(forged), forged)

    def test_a_call_naming_two_pools_one_not_granted_fails_whole(self):
        w = self.world()
        a, b = w.pool("alpha"), w.pool("beta", grant_to=())
        tok = w.mint("alice", {a: q("query")})
        self.assertIsNone(w.auth(tok, {a: q("query"), b: q("query")}))
        got = w.auth(tok, {a: q("query"), b: q("query")}, broken_whole_call=True)   # BROKEN: a partial answer
        self.assertEqual(set(got.pools), {a})

    def test_detached_after_mint_is_unavailable(self):
        w = self.world()
        a = w.pool("alpha")
        tok = w.mint("alice", {a: q("query", "save")})
        self.assertIsNotNone(w.auth(tok, {a: q("save")}))
        self.assertTrue(w.admin("pool_detach", pool=a)["ok"])
        self.assertIsNone(w.auth(tok, {a: q("save")}))
        self.assertIsNotNone(w.auth(tok, {a: q("save")}, broken_state_from_mint=True))   # BROKEN: state read at mint only
        self.assertTrue(w.admin("pool_reattach", pool=a)["ok"])
        self.assertIsNotNone(w.auth(tok, {a: q("save")}))

    def test_actor_type_is_set_at_mint_by_the_adapter_never_by_the_request(self):
        w = self.world()
        a = w.pool("alpha", ops={"query", "save", "review"})
        h = w.sessions.resolve(w.mint("alice", {a: q("query")}, human=True))
        g = w.sessions.resolve(w.mint("alice", {a: q("query")}, conv="c2"))
        self.assertEqual((h.actor, g.actor), ("human", "agent"))
        child = w.sessions.resolve(w.sessions.delegate(w.mint("alice", {a: q("query")}, conv="c3", human=True), {a: q("query")}, 60))
        self.assertEqual(child.actor, "agent")                    # a delegated token is always an agent


class Delegation(Base):
    def setUp(self):
        super().setUp()
        self.w = self.world()
        self.a = self.w.pool("alpha", ops={"query", "save", "update"})
        self.b = self.w.pool("beta", ops={"query", "save"})

    def root(self, **kw):
        return self.w.mint("alice", {self.a: q("query", "save"), self.b: q("query")}, **kw)

    def test_attenuation_is_allowed_and_widening_is_refused(self):
        w = self.w
        r = self.root()
        child = w.sessions.delegate(r, {self.a: q("query")}, 60)
        self.assertIsNotNone(w.auth(child, {self.a: q("query")}))
        self.assertIsNone(w.auth(child, {self.b: q("query")}))
        self.assertIsNone(w.auth(child, {self.a: q("save")}))
        for scope, ttl in (({self.a: q("query"), "z" * 16: q("query")}, 60),      # wider in pools
                           ({self.a: q("query", "update")}, 60),                  # wider in operations
                           ({self.b: q("save")}, 60),
                           ({self.a: q("query")}, w.consts.token_ttl_s + 5)):     # wider in lifetime
            with self.assertRaises(TokenError) as e:
                w.sessions.delegate(r, scope, ttl)
            self.assertEqual(e.exception.code, "scope_widened")

    def test_a_child_cannot_delegate(self):
        child = self.w.sessions.delegate(self.root(), {self.a: q("query")}, 60)
        with self.assertRaises(TokenError):
            self.w.sessions.delegate(child, {self.a: q("query")}, 30)

    def test_broken_delegation_variants_accept_what_the_real_code_refuses(self):
        for flag, scope_of, ttl in (("broken_scope_not_attenuated", lambda a, b: {b: q("save")}, 60),
                                    ("broken_ttl_not_capped", lambda a, b: {a: q("query")}, 7200)):   # past the parent, inside max_age
            w = World(tempfile.mkdtemp(), sess_kw={flag: True}).start()
            a, b = w.pool("alpha", ops={"query", "save", "update"}), w.pool("beta", ops={"query", "save"})
            r = w.mint("alice", {a: q("query", "save"), b: q("query")})
            w.sessions.delegate(r, scope_of(a, b), ttl)    # accepted: red for the check above
        w = World(tempfile.mkdtemp(), sess_kw={"broken_delegate_depth": True}).start()
        a = w.pool("alpha")
        child = w.sessions.delegate(w.mint("alice", {a: q("query")}), {a: q("query")}, 60)
        w.sessions.delegate(child, {a: q("query")}, 30)

    def test_live_token_cap_counts_revoked_tokens_until_expiry(self):
        w = self.w
        n = w.consts.tok_live_max
        toks = [w.mint("alice", {self.a: q("query")}, conv=f"k{i}") for i in range(n)]
        with self.assertRaises(TokenError) as e:
            w.mint("alice", {self.a: q("query")}, conv="over")
        self.assertEqual(e.exception.code, "rate_limited")
        w.sessions.revoke_hash(w.sessions.resolve(toks[0]).hash, T)   # a loop of mint and revoke cannot grow the records
        with self.assertRaises(TokenError):
            w.mint("alice", {self.a: q("query")}, conv="over2")
        w.clock.t += w.consts.token_ttl_s + 1
        w.mint("alice", {self.a: q("query")}, conv="after")
        w2 = World(tempfile.mkdtemp(), sess_kw={"broken_no_live_cap": True}).start()   # BROKEN: no cap
        a2 = w2.pool("alpha")
        for i in range(n + 3):
            w2.mint("alice", {a2: q("query")}, conv=f"k{i}")

    def test_mint_rate(self):
        rate = Constants().mint_rate_per_h
        w = World(tempfile.mkdtemp(), consts=Constants(tok_live_max=10**6)).start()
        a = w.pool("alpha")
        for i in range(rate):
            w.mint("alice", {a: q("query")}, conv=f"r{i}")
        with self.assertRaises(TokenError) as e:
            w.mint("alice", {a: q("query")}, conv="past-rate")
        self.assertEqual(e.exception.code, "rate_limited")


class Revocation(Base):
    def _revoke_self(self, w, tok):
        rec = w.sessions.resolve(tok)
        return w.svc.handle("token_revoke", w.sessions.principal(rec), rec, {})

    def test_single_token_revoke_ends_children_not_siblings_and_survives_restart(self):
        w = self.world()
        a = w.pool("alpha")
        parent = w.mint("alice", {a: q("query")}, conv="c1")
        sibling = w.mint("alice", {a: q("query")}, conv="c2")
        child = w.sessions.delegate(parent, {a: q("query")}, 60)
        r = self._revoke_self(w, parent)
        self.assertEqual((r["ok"], r["denied"], r["durable"]), (True, True, True))
        self.assertIsNone(w.auth(parent, {a: q("query")}))
        self.assertIsNone(w.auth(child, {a: q("query")}))            # check 3: the child ends with its parent
        self.assertIsNotNone(w.auth(sibling, {a: q("query")}))
        with self.assertRaises(TokenError):
            w.sessions.renew(parent)                                 # a revoked token cannot renew
        w.restart()
        self.assertIsNone(w.auth(parent, {a: q("query")}))
        self.assertIsNone(w.auth(child, {a: q("query")}))
        self.assertIsNotNone(w.auth(sibling, {a: q("query")}))

    def test_a_revoked_token_works_again_after_a_restart_that_ignores_the_journal(self):
        w = self.world()
        a = w.pool("alpha")
        tok = w.mint("alice", {a: q("query")})
        self._revoke_self(w, tok)
        w.restart(apply_journal=False)                               # BROKEN: authz copy alone
        self.assertIsNotNone(w.auth(tok, {a: q("query")}))

    def test_broken_revoke_by_epoch_ends_the_sibling(self):
        w = World(tempfile.mkdtemp(), sess_kw={"broken_revoke_by_epoch": True}).start()
        a = w.pool("alpha")
        t1, t2 = w.mint("alice", {a: q("query")}, conv="c1"), w.mint("alice", {a: q("query")}, conv="c2")
        self._revoke_self(w, t1)
        self.assertIsNone(w.auth(t2, {a: q("query")}))               # the sibling died: the defect the real code does not have

    def test_an_administrator_revokes_any_token_by_id_and_a_member_cannot(self):
        w = self.world()
        a = w.pool("alpha")
        tok = w.mint("alice", {a: q("query")})
        tid = w.sessions.token_id(w.sessions.resolve(tok))
        bob = w.sessions.mint(T, b"user-bob")
        brec = w.sessions.resolve(bob)
        self.assertEqual(w.svc.handle("token_revoke", w.sessions.principal(brec), brec, {"token_id": tid}), UNAV)
        self.assertIsNotNone(w.auth(tok, {a: q("query")}))
        self.assertTrue(w.admin("token_revoke", token_id=tid)["ok"])
        self.assertIsNone(w.auth(tok, {a: q("query")}))

    def test_grant_removal_is_effective_at_once_and_stays_removed_across_a_restart_with_authz_blocked(self):
        w = self.world()
        a, b = w.pool("alpha"), w.pool("beta")
        tok = w.mint("alice", {a: q("query"), b: q("query")})
        w.authz.down = True                                          # the best-effort authz write cannot land
        r = w.admin("pool_ungrant", who="user-alice", pool=a)
        self.assertEqual((r["ok"], r["denied"], r["durable"]), (True, True, True))
        self.assertIsNone(w.auth(tok, {a: q("query")}))              # effective at the next call
        self.assertIsNotNone(w.auth(tok, {b: q("query")}))
        w.authz.down = False
        w.restart()
        self.assertEqual(w.dir.live_ops(T, "pt-user-alice", a), frozenset())    # the stale authz copy still held it; the journal won
        self.assertIsNone(w.auth(tok, {a: q("query")}))
        with self.assertRaises(TokenError):
            w.mint("alice", {a: q("query")}, conv="again")           # a re-mint does not get it back
        # a grant re-made later is a new grant with a higher counter and is not confused with the removed one
        self.assertTrue(w.admin("pool_grant", who="user-alice", pool=a)["ok"])
        w.restart()
        self.assertEqual(w.dir.live_ops(T, "pt-user-alice", a), pl.DEFAULT_GRANT)

    def test_broken_grant_table_rebuilt_from_authz_alone_brings_the_removed_grant_back(self):
        w = self.world()
        a = w.pool("alpha")
        tok = w.mint("alice", {a: q("query")})
        w.authz.down = True
        w.admin("pool_ungrant", who="user-alice", pool=a)
        w.authz.down = False
        w.restart(apply_journal=False)
        self.assertIsNotNone(w.auth(tok, {a: q("query")}))           # the removed grant returned: red for the real check above

    def test_broken_removal_outside_the_journal_is_lost_at_restart(self):
        w = World(tempfile.mkdtemp(), svc_kw={"broken_removal_outside_journal": True}).start()
        a = w.pool("alpha")
        w.authz.down = True
        w.admin("pool_ungrant", who="user-alice", pool=a)
        w.authz.down = False
        w.restart()
        self.assertEqual(w.dir.live_ops(T, "pt-user-alice", a), pl.DEFAULT_GRANT)

    def test_detach_is_journalled_and_survives_restart_and_a_broken_variant_does_not(self):
        w = self.world()
        a = w.pool("alpha")
        tok = w.mint("alice", {a: q("query")})
        w.authz.down = True
        self.assertTrue(w.admin("pool_detach", pool=a)["durable"])
        w.authz.down = False
        w.restart()
        self.assertEqual(w.dir.get(T, a).state, pl.DETACHED)
        self.assertIsNone(w.auth(tok, {a: q("query")}))
        w = World(tempfile.mkdtemp(), svc_kw={"broken_state_not_journalled": True}).start()
        a = w.pool("alpha")
        w.authz.down = True
        w.admin("pool_detach", pool=a)
        w.authz.down = False
        w.restart()
        self.assertEqual(w.dir.get(T, a).state, pl.ACTIVE)           # the detach came back: red for the real check

    def test_renewal_drops_the_removed_pool_and_keeps_the_rest(self):
        w = self.world()
        a, b = w.pool("alpha"), w.pool("beta")
        tok = w.mint("alice", {a: q("query"), b: q("query")})
        w.admin("pool_ungrant", who="user-alice", pool=a)
        w.clock.t += 100
        exp = w.sessions.renew(tok)
        self.assertGreater(exp, w.clock.t)
        self.assertEqual(set(w.sessions.resolve(tok).scope), {b})
        w.admin("pool_ungrant", who="user-alice", pool=b)
        with self.assertRaises(TokenError) as e:
            w.sessions.renew(tok)                                    # an empty result is refused with `expired`
        self.assertEqual(e.exception.code, "expired")
        w2 = World(tempfile.mkdtemp(), sess_kw={"broken_renew_keeps_removed": True}).start()
        a2 = w2.pool("alpha")
        t2 = w2.mint("alice", {a2: q("query")})
        w2.admin("pool_ungrant", who="user-alice", pool=a2)
        w2.sessions.renew(t2)
        self.assertIn(a2, w2.sessions.resolve(t2).scope)             # BROKEN: the removed pool stays in the scope

    def test_renewal_never_extends_past_max_age(self):
        w = self.world()
        a = w.pool("alpha")
        tok = w.mint("alice", {a: q("query")})
        start = w.clock.t
        for _ in range(40):
            w.clock.t += w.consts.token_ttl_s - 1
            try:
                w.sessions.renew(tok)
            except TokenError:
                break
        self.assertGreaterEqual(w.clock.t, start + w.consts.token_max_age_s - w.consts.token_ttl_s)
        self.assertIsNone(w.sessions.resolve(tok))                   # past max_age the token is over, however often it renewed

    def test_compaction_keeps_what_is_still_needed(self):
        w = self.world()
        a = w.pool("alpha")
        tok = w.mint("alice", {a: q("query")})
        w.authz.down = True
        w.admin("pool_ungrant", who="user-alice", pool=a)           # an unconfirmed removal
        self._revoke_self(w, tok)
        w.authz.down = False
        w.rev.compact()
        w.restart()
        self.assertEqual(w.dir.live_ops(T, "pt-user-alice", a), frozenset())
        self.assertIsNone(w.auth(tok, {a: q("query")}))


    def test_a_grant_removal_stays_unconfirmed_until_its_authz_write_is_confirmed(self):
        # Round 5: at f16ea94 any 404 or 200 to the record's delete confirmed the removal, so compaction dropped its journal record and a
        # restart rebuilt the grant from the authz copy, which still held it. Round 8: a removal is a write of the record with no
        # operations (never a delete), conditioned on the record it read; only a well-formed acknowledgement about exactly this record,
        # through the authz alias, confirms it. Any other answer to the write, or to the read it is conditioned on, leaves the removal
        # unconfirmed (at the call and at the next start's write), and the journal keeps it.
        gid0 = "pg:x"
        cases = {f"PUT {n}": ("PUT", n) for n in unconfirmed_puts(f"lc-{T}-authz", gid0)}
        cases.update({"GET 200 with an empty body": ("GET", (200, {})), "GET 404 with an empty body": ("GET", (404, {})),
                      "GET 503": ("GET", (503, {}))})
        for name, (method, answer) in cases.items():
            with self.subTest(name), tempfile.TemporaryDirectory() as tmp:
                w = World(tmp).start()
                a = w.pool("alpha")
                gid = f"pg:pt-user-alice:{a}"
                tok = w.mint("alice", {a: q("query")})
                w.authz.doc_answers[(method, gid)] = unconfirmed_puts(f"lc-{T}-authz", gid)[answer] if method == "PUT" else answer
                r = w.admin("pool_ungrant", who="user-alice", pool=a)
                self.assertEqual((r["ok"], r["denied"], r["durable"]), (True, True, True))
                self.assertIsNone(w.auth(tok, {a: q("query")}))
                self.assertEqual(w.authz.docs[gid]["ops"], ["query", "save"])   # the answer was no write: the copy still holds the grant
                self.assertEqual(len(w.svc._unconfirmed), 1)
                w.rev.compact()
                w.restart()
                self.assertEqual(w.dir.live_ops(T, "pt-user-alice", a), frozenset())   # the journal kept the removal
                self.assertIsNone(w.auth(tok, {a: q("query")}))
                self.assertEqual(len(w.svc._unconfirmed), 1)                         # the start-up write got the same answer
                w.rev.clean_stop()

    def test_a_partial_grant_removal_stays_unconfirmed_until_its_authz_put_is_confirmed(self):
        # Round 6 (F18): at 4e7ee85 the replacement PUT of a partial removal (remove `query`, keep `save`) was accepted on its HTTP status
        # alone, so an answer that wrote nothing cleared the journal retention entry; compaction dropped the record and a restart rebuilt
        # `query` from the authz copy. Only a well-formed acknowledgement about exactly this record (`reads.doc_written` through the
        # authz alias) confirms it now; anything else leaves the removal unconfirmed and the journal keeps it.
        for name in unconfirmed_puts(f"lc-{T}-authz", "pg:x"):
            with self.subTest(name), tempfile.TemporaryDirectory() as tmp:
                w = World(tmp).start()
                a = w.pool("alpha", ops={"query", "save"})
                gid = f"pg:pt-user-alice:{a}"
                tok = w.mint("alice", {a: q("query", "save")})
                w.authz.doc_answers[("PUT", gid)] = unconfirmed_puts(f"lc-{T}-authz", gid)[name]
                r = w.admin("pool_ungrant", who="user-alice", pool=a, ops=["query"])
                self.assertEqual((r["ok"], r["denied"], r["durable"]), (True, True, True))
                self.assertEqual(w.authz.docs[gid]["ops"], ["query", "save"])   # the answer was no write: the authz copy still holds query
                pending = len(w.svc._unconfirmed)
                w.rev.compact()
                w.restart()
                self.assertEqual(w.dir.live_ops(T, "pt-user-alice", a), frozenset({"save"}))   # the journal kept the removal
                self.assertIsNone(w.auth(tok, {a: q("query")}))
                self.assertIsNotNone(w.auth(tok, {a: q("save")}))
                self.assertEqual(pending, 1)                                     # because it stayed unconfirmed
                w.rev.clean_stop()

    def test_control_a_confirmed_partial_removal_is_dropped_by_compaction_and_survives_a_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            w = World(tmp).start()
            a = w.pool("alpha", ops={"query", "save"})
            gid = f"pg:pt-user-alice:{a}"
            tok = w.mint("alice", {a: q("query", "save")})
            r = w.admin("pool_ungrant", who="user-alice", pool=a, ops=["query"])
            self.assertEqual((r["ok"], r["denied"], r["durable"]), (True, True, True))
            self.assertEqual(w.authz.docs[gid]["ops"], ["save"])
            self.assertEqual(w.svc._unconfirmed, {})                              # a confirmed write: the record is not needed
            w.rev.compact()
            w.restart()
            self.assertEqual(w.dir.live_ops(T, "pt-user-alice", a), frozenset({"save"}))   # rebuilt from the authz copy
            self.assertIsNone(w.auth(tok, {a: q("query")}))
            self.assertIsNotNone(w.auth(tok, {a: q("save")}))
            w.rev.clean_stop()


if __name__ == "__main__":
    unittest.main()
