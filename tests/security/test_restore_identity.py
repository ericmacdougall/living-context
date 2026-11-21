"""Review round 8, F3: start-up never builds authority, and never confirms a reduction or a removal, from a record that is not exactly
what it claims.

At ad6aa1a the start-up reads of the authz copy (`boot.load_grant_revs`, `pool_boot.load`) checked that the search was complete, then
built a grant, a pool, a pool grant or a token from every hit without asking whether the hit was the record it claimed to be: another
tenant's index or `tenant`, a record filed under another record's id, a kind the read did not ask for. Such a hit restored access, and
its counter or revision could confirm (discard) a pending reduction or removal. The rule now (`reads.restored`, one judge for every
restore path): the hit's index, id, tenant and kind are checked first; a hit that fails is not restored and the read is not complete,
so nothing is confirmed from it.

Each case plants ONE hit with ONE identity field off (the rest of the copy is genuine) and asserts that no access is restored and
nothing is confirmed; the control plants the same hit unbroken and it is restored. No Elasticsearch: the fakes answer the start-up
read, and the rest is the real code (`boot.start_tenant` in both start orders, `pool_boot.load` with the epoch journal)."""
import copy
import json
import tempfile
import unittest

from living_context.gateway.sessions import TokenError
from living_context.policy import Role
from tests.security import test_admin_reductions as ar
from tests.security.test_pool_tokens import TB, T as PT, World, q
from tests.unit.fake_es import behind


def plant(fake, hits):
    """Every search page of `fake` also answers `hits()` (after the genuine hits)."""
    real = fake.request

    def request(method, path, body=None, **kw):
        st, r = real(method, path, body, **kw)
        if method == "POST" and path.startswith("/_search") and st == 200:
            have = r["hits"]["hits"]
            r = dict(r, hits={"hits": have + [dict(h, sort=[len(have) + i]) for i, h in enumerate(hits())]})
        return st, r
    fake.request = request


def drop(path):
    def f(h):
        h = copy.deepcopy(h)
        x = h
        for k in path[:-1]:
            x = x[k]
        del x[path[-1]]
        return h
    return f


def put(path, value):
    def f(h):
        h = copy.deepcopy(h)
        x = h
        for k in path[:-1]:
            x = x[k]
        x[path[-1]] = value
        return h
    return f


def identity_mutations(tenant, other_kind, other_id):
    """One identity field of a hit off: (name, function of the hit)."""
    return {
        "_index of another tenant": put(("_index",), f"lc-{TB}-authz"),
        "_index of another tenant's physical index": put(("_index",), behind(f"lc-{TB}-authz")),
        "_index of another index of the tenant": put(("_index",), f"lc-{tenant}-base-ctl"),
        "_index missing": drop(("_index",)),
        "_id of another record": put(("_id",), other_id),
        "_id missing": drop(("_id",)),
        "_source.tenant another tenant": put(("_source", "tenant"), TB),
        "_source.tenant missing": drop(("_source", "tenant")),
        "_source.kind not asked for": put(("_source", "kind"), other_kind),
        "_source.kind missing": drop(("_source", "kind")),
    }


# ---- the class grants: boot.start_tenant ------------------------------------------------------------------------------------------
class ClassGrants(unittest.TestCase):
    """r1 holds {base, restricted}; its class removal is journalled and its authz write refused, so the reduction is pending. The
    restart's read also answers a wider grant of r1 (an administrator, at a revision past the reduction) and a grant of r9, with one
    identity field off."""

    def restart(self, w):
        svc = w.node.rt.revocation
        svc.compact()
        svc.clean_stop()
        w.node = ar.Node(self, w)
        self.assertEqual(w.node.rt.revocation.state, ar.OPEN)

    def hits(self, w, mutate):
        src = w.authz.docs["gr:tok-r1"]
        wide = {"_index": ar.AUTHZ, "_id": "gr:tok-r1",
                "_source": dict(src, roles=["reader", "tenant_admin"], read=["base", "restricted"], rev=2 ** 62)}
        r9 = {"_index": ar.AUTHZ, "_id": "gr:tok-r9", "_source": dict(src, principal="tok-r9", lin_subjects=["tok-r9"])}
        return [mutate(wide), mutate(r9)] if mutate else [r9]

    def run_case(self, order, mutate, prepare=None):
        w = ar.World(self, order)
        w.authz.refuse = ar.REFUSALS["a 503"]
        self.assertTrue(ar.remove_restricted(w.node).get("ok"))
        w.authz.refuse = None
        planted = prepare(w) if prepare else self.hits(w, mutate)
        plant(w.authz, lambda: planted)
        self.restart(w)
        return w

    def test_an_incomplete_read_confirms_no_reduction(self):
        # a read that refused a record is no account of what the copy holds: it confirms the pending reduction of r1 neither by a
        # genuine record of r1 at a revision past the reduction, nor by r1's absence when r1's own record is the one refused
        off = identity_mutations(ar.T, "pgrant", "gr:tok-r2")["_source.tenant another tenant"]

        def later_record(w):
            src = w.authz.docs["gr:tok-r1"]
            w.authz.docs["gr:tok-r1"] = dict(src, rev=2 ** 62)
            return [off({"_index": ar.AUTHZ, "_id": "gr:tok-r9", "_source": dict(src, principal="tok-r9", lin_subjects=["tok-r9"])})]

        def own_record_refused(w):
            w.authz.docs["gr:tok-r1"] = dict(w.authz.docs["gr:tok-r1"], tenant="tenbbb")
            return []
        for name, prepare in (("a genuine later record of r1", later_record), ("r1's own record refused", own_record_refused)):
            for order in ar.ORDERS:
                with self.subTest(case=name, order=order):
                    w = self.run_case(order, None, prepare)
                    g = w.node.get("r1")
                    self.assertTrue(g is None or "restricted" not in g.read_set, "the reduced class came back")
                    self.assertIn((ar.T, "tok-r1"), w.node.rt.grants._unconfirmed, "an incomplete read confirmed the reduction")

    def test_a_misfiled_grant_record_restores_nothing_and_confirms_no_reduction(self):
        for order in ar.ORDERS:
            for name, mutate in identity_mutations(ar.T, "pgrant", "gr:tok-r2").items():
                with self.subTest(order=order, mutation=name):
                    w = self.run_case(order, mutate)
                    g = w.node.get("r1")
                    self.assertIsNotNone(g)
                    self.assertNotIn("restricted", g.read_set, "the wider record was restored")
                    self.assertNotIn(Role.TENANT_ADMIN, g.roles, "the wider record was restored")
                    self.assertIsNone(w.node.get("r9"), "a grant was restored from a record that is not exactly a grant")
                    self.assertIn((ar.T, "tok-r1"), w.node.rt.grants._unconfirmed, "the record's revision confirmed the reduction")

    def test_control_the_same_record_unbroken_is_restored(self):
        for order in ar.ORDERS:
            with self.subTest(order=order):
                w = self.run_case(order, None)
                self.assertIsNotNone(w.node.get("r9"))
                self.assertNotIn("restricted", w.node.get("r1").read_set)
                self.assertIn((ar.T, "tok-r1"), w.node.rt.grants._unconfirmed)   # the genuine copy is older than the reduction


# ---- the pool lane: pool_boot.load ----------------------------------------------------------------------------------------------
class PoolLane(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def world(self):
        return World(self.tmp.name).start()

    def test_a_misfiled_pool_grant_restores_no_access_and_confirms_no_removal(self):
        # alice's grant on `a` was never written (its write timed out), and its removal could not be written either: the copy holds no
        # grant record of alice on `a`, the journal holds the removal. The restart's read answers a grant of alice on `a` with a counter
        # past the removal, one identity field off.
        for name, mutate in identity_mutations(PT, "token", "pg:pt-user-bob:x").items():
            with self.subTest(mutation=name):
                self.setUp()
                w = self.world()
                a = w.pool("alpha", grant_to=())
                gid = f"pg:pt-user-alice:{a}"
                w.authz.hold = {gid}
                self.assertTrue(w.admin("pool_grant", who="user-alice", pool=a)["ok"])
                w.authz.hold, w.authz.down = set(), True
                self.assertTrue(w.admin("pool_ungrant", who="user-alice", pool=a)["durable"])
                w.authz.down = False
                hit = {"_index": behind(f"lc-{PT}-authz"), "_id": gid,
                       "_source": {"tenant": PT, "cls": "tenantwide", "kind": "pgrant", "lin_subjects": ["pt-user-alice"],
                                   "principal": "pt-user-alice", "pool": a, "ops": ["query", "save"], "counter": 2 ** 40}}
                planted = [mutate(hit)]
                plant(w.authz, lambda: planted)
                w.rev.compact()
                w.restart()
                self.assertEqual(w.dir.live_ops(PT, "pt-user-alice", a), frozenset(), "a misfiled record restored the grant")
                with self.assertRaises(TokenError):
                    w.mint("alice", {a: q("query")}, conv="after")
                self.assertEqual(len(w.svc._unconfirmed), 1, "the removal was confirmed from a read with a misfiled record")
                w.rev.clean_stop()

    def pool_hits(self, mutate):
        pid = "q" * 16
        pool = {"_index": behind(f"lc-{PT}-authz"), "_id": f"pp:{pid}",
                "_source": {"tenant": PT, "cls": "tenantwide", "kind": "pool", "lin_subjects": [], "pool": pid, "pool_cls": "base",
                            "pool_kind": "team", "label": "planted", "card": "", "private": False, "state": "active", "counter": 5}}
        grant = {"_index": behind(f"lc-{PT}-authz"), "_id": f"pg:pt-user-alice:{pid}",
                 "_source": {"tenant": PT, "cls": "tenantwide", "kind": "pgrant", "lin_subjects": ["pt-user-alice"],
                             "principal": "pt-user-alice", "pool": pid, "ops": ["query"], "counter": 6}}
        return pid, [mutate(pool) if mutate else pool, grant]

    def test_a_misfiled_pool_record_restores_no_pool(self):
        for name, mutate in identity_mutations(PT, "pgrant", "pp:" + "r" * 16).items():
            with self.subTest(mutation=name):
                self.setUp()
                w = self.world()
                pid, planted = self.pool_hits(mutate)
                plant(w.authz, lambda: planted)
                w.restart()
                self.assertIsNone(w.dir.get(PT, pid), "a pool was restored from a record that is not exactly a pool")
                with self.assertRaises(TokenError):
                    w.mint("alice", {pid: q("query")}, conv="after")
                w.rev.clean_stop()

    def test_control_the_pool_record_unbroken_is_restored(self):
        w = self.world()
        pid, planted = self.pool_hits(None)
        plant(w.authz, lambda: planted)
        w.restart()
        self.assertIsNotNone(w.dir.get(PT, pid))
        self.assertIsNotNone(w.mint("alice", {pid: q("query")}, conv="after"))

    def token_world(self):
        w = self.world()
        b = w.pool("beta")
        tok = w.mint("alice", {b: q("query")})
        tid, = [d for d in w.authz.docs if d.startswith("tk:")]
        doc = w.authz.docs.pop(tid)   # the copy keeps only the planted hit
        return w, b, tok, {"_index": behind(f"lc-{PT}-authz"), "_id": tid, "_source": doc}

    def test_a_misfiled_token_record_restores_no_token(self):
        def inner(field, value):
            def f(h):
                h = copy.deepcopy(h)
                rec = json.loads(h["_source"]["rec_json"])
                rec[field] = value
                h["_source"]["rec_json"] = json.dumps(rec)
                return h
            return f
        cases = dict(identity_mutations(PT, "grant", "tk:" + "0" * 64))
        cases["the token record of another tenant"] = inner("tenant", TB)
        cases["the token record of another principal"] = inner("principal", "pt-user-bob")
        for name, mutate in cases.items():
            with self.subTest(mutation=name):
                self.setUp()
                w, b, tok, hit = self.token_world()
                planted = [mutate(hit)]
                plant(w.authz, lambda: planted)
                w.restart()
                _, _, rest = tok.partition(".")
                for t in (tok, f"{TB}.{rest}"):
                    self.assertIsNone(w.sessions.resolve(t), "a token was restored from a record that is not exactly its own")
                w.rev.clean_stop()

    def test_control_the_token_record_unbroken_is_restored(self):
        w, b, tok, hit = self.token_world()
        plant(w.authz, lambda: [hit])
        w.restart()
        self.assertIsNotNone(w.sessions.resolve(tok))

    def test_an_expired_token_hit_filed_under_another_record_deletes_nothing(self):
        # the start-up deletes the document of an expired token; at ad6aa1a it deleted the hit's `_id`, whatever record that was
        w = self.world()
        b = w.pool("beta")
        gid = f"pg:pt-user-alice:{b}"
        self.assertIn(gid, w.authz.docs)
        rec = {"tenant": PT, "principal": "pt-user-alice", "exp": 1.0, "epoch": 0}
        hit = {"_index": behind(f"lc-{PT}-authz"), "_id": gid,
               "_source": {"tenant": PT, "cls": "tenantwide", "kind": "token", "lin_subjects": ["pt-user-alice"], "hash": "9" * 64,
                           "exp": 1.0, "rec_json": json.dumps(rec)}}
        plant(w.authz, lambda: [hit])
        w.restart()
        self.assertIn(gid, w.authz.docs, "the start-up deleted another record under an expired token's hit")
        self.assertEqual(w.dir.live_ops(PT, "pt-user-alice", b), frozenset({"query", "save"}))


# ---- the conversation taint: session_state.es_load / es_persist -----------------------------------------------------------------
class SessionRecord(unittest.TestCase):
    """The taint read back after a restart (and read inside every raise) is built only from this conversation's own session record. A
    stored record with one identity field off is refused (`StateError`: the caller answers `unavailable`); it never reads as "no taint
    yet", never lowers the taint, and a raise over it writes nothing."""
    T, KEY = "tena", ("tena", "pt-user-alice", "conv1")

    def setUp(self):
        from living_context.gateway import session_state as ss
        from tests.unit.fake_es import DocStore
        self.ss, self.es = ss, DocStore()
        self.ctl, self.did = f"lc-{self.T}-ctl", ss._doc_id(self.KEY)
        self.raised = ss.Taint("restricted", frozenset({"p" * 16}))
        ss.es_persist(lambda: self.es, self.T)(self.KEY, lambda stored: self.raised)
        (src, _), = [v for k, v in self.es.docs.items() if k[1] == self.did]
        self.hit = {"_index": behind(self.ctl), "_id": self.did, "_version": 1, "_seq_no": 1, "_primary_term": 1, "found": True,
                    "_source": dict(src)}

    def mutations(self):
        return {
            "_index of another tenant": put(("_index",), behind("lc-tenb-ctl")),
            "_index of another index of the tenant": put(("_index",), behind(f"lc-{self.T}-authz")),
            "_id of another record": put(("_id",), self.ss._doc_id((self.T, "pt-user-alice", "conv2"))),
            "_source.tenant another tenant": put(("_source", "tenant"), "tenb"),
            "_source.tenant missing": drop(("_source", "tenant")),
            "_source.kind another kind": put(("_source", "kind"), "pgrant"),
            "_source.kind missing": drop(("_source", "kind")),
            "_source.conversation another": put(("_source", "conversation"), "conv2"),
            "_source.lin_subjects another principal": put(("_source", "lin_subjects"), ["pt-user-bob"]),
        }

    def test_a_misfiled_session_record_is_refused_by_the_load_and_by_a_raise(self):
        for name, mutate in self.mutations().items():
            with self.subTest(mutation=name):
                before = dict(self.es.docs)
                self.es.doc_answers[("GET", self.ctl, self.did)] = [(200, mutate(self.hit))]
                with self.assertRaises(self.ss.StateError):
                    self.ss.es_load(lambda: self.es, self.T)(self.KEY)
                self.es.doc_answers[("GET", self.ctl, self.did)] = [(200, mutate(self.hit))]
                with self.assertRaises(self.ss.StateError):
                    self.ss.es_persist(lambda: self.es, self.T)(self.KEY, lambda stored: stored or self.ss.Taint(None, frozenset()))
                self.assertEqual(self.es.docs, before, "a raise over a misfiled record wrote")
                self.assertEqual(self.ss.es_load(lambda: self.es, self.T)(self.KEY), self.raised)

    def test_control_the_same_record_unbroken_reads_back(self):
        self.es.doc_answers[("GET", self.ctl, self.did)] = [(200, copy.deepcopy(self.hit))]
        self.assertEqual(self.ss.es_load(lambda: self.es, self.T)(self.KEY), self.raised)


if __name__ == "__main__":
    unittest.main()
