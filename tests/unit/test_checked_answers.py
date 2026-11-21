"""Round 6 (class C7, the sibling sites of F18 and F19): every Elasticsearch answer that feeds an authorization, erasure, verifier, taint,
journal-retention or durability decision is judged by a `reads` validator (or the verifier's own) as it returns. At 4e7ee85 each site
below decided on the HTTP status alone, or on a field it did not check belonged to the document asked. Each test drives one site with a
scripted or fake answer and asserts the decision refuses (or stays undecided) instead; each has a positive control. The pool-grant PUT
(F18) is in tests/security/test_pool_tokens2.py, the taint write (F19) in tests/unit/test_taint_load.py, the purge count in
tests/unit/test_purge_threshold.py and the repair reads in tests/unit/test_writer_repair_reads.py."""
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from living_context.erasure import scan
from living_context.erasure.executor import Executor, State
from living_context.gateway import boot, keyowner, pool_boot
from living_context.gateway.class_reader import ClassReader, ReaderRefused
from living_context.gateway.fence import SendFence
from living_context.gateway.gate import WriteGate
from living_context.gateway.handler import Gateway
from living_context.gateway.indices import REGISTRY, class_index, pool_index
from living_context.gateway.provision import ProvisionRefused, provision_tenant
from living_context.ids import KIND_LEAF, Pointer, encode_pointer, new_leaf_id
from living_context.ingest import writer as writer_mod
from living_context.ingest.writer import Writer, WriterError
from living_context.keystore import Keystore
from living_context.policy import ClassLattice, Principal, Role
from living_context.structure import watermark as watermark_mod
from living_context.structure.watermark import WatermarkError, WritePath
from living_context.verifier.check import VerifierError, pool_residue
from tests.unit.fake_es import SHARDS_ONE, DocStore, FakeAuthz, FakeEs, ShardedFake, behind, conflict, doc_answer, unconfirmed_puts

T = "tena"
FAILED_SHARD = {"total": 1, "successful": 0, "skipped": 0, "failed": 1}


# ---- the key owner: offboarding and rotation ---------------------------------------------------------------------------------------
class Owner:
    """The key owner's view of the cluster: `keys` {id: name} active; `answers[(method, path prefix)]` replaces those answers."""

    def __init__(self, keys=None):
        self.keys, self.answers, self.sent = dict(keys or {}), {}, []

    def request(self, method, path, body=None, **kw):
        self.sent.append((method, path))
        for (m, prefix), answer in self.answers.items():
            if m == method and path.startswith(prefix):
                return answer
        if method == "GET":
            return 200, {"api_keys": [{"id": i, "name": n, "invalidated": False} for i, n in sorted(self.keys.items())]}
        if method == "DELETE":
            gone = [i for i in body["ids"] if self.keys.pop(i, None) is not None]
            return 200, {"error_count": 0, "invalidated_api_keys": gone, "previously_invalidated_api_keys": []}
        if method == "POST":
            kid = f"k{len(self.sent)}"
            self.keys[kid] = body["name"]
            return 200, {"id": kid, "name": body["name"], "api_key": "x", "encoded": "eA==", "expiration": 1}
        raise AssertionError(path)


class KeyOwner(unittest.TestCase):
    LISTING = ("GET", "/_security/api_key?owner=true&name=")

    def test_control_offboarding_invalidates_every_active_key(self):
        o = Owner({"k1": f"lc-{T}-reader", "k2": f"lc-{T}-writer"})
        self.assertEqual(sorted(keyowner.invalidate_all(o, T)), ["k1", "k2"])
        self.assertEqual(o.keys, {})

    def test_a_key_listing_without_the_list_is_not_no_keys(self):
        # at 4e7ee85 `(200, {})` read as "no keys": offboarding invalidated nothing and reported success
        for answer in ((200, {}), (200, {"api_keys": "k1"}), (200, {"api_keys": [{"id": "k1", "name": f"lc-{T}-reader"}]})):
            with self.subTest(answer):
                o = Owner({"k1": f"lc-{T}-reader"})
                o.answers[self.LISTING] = answer
                with self.assertRaises(keyowner.RotationError):
                    keyowner.invalidate_all(o, T)

    def test_an_invalidation_is_confirmed_by_reading_the_active_keys_back(self):
        # at 4e7ee85 the answer's list was returned as it came: an empty or malformed answer invalidated nothing, silently. The cluster
        # answers an id it did not invalidate with both lists empty and no error, so only the readback proves the key is dead
        for name, answer in {"both lists empty": (200, {"error_count": 0, "invalidated_api_keys": [], "previously_invalidated_api_keys": []}),
                             "200 with an empty body": (200, {}),
                             "an error count": (200, {"error_count": 1, "invalidated_api_keys": [], "previously_invalidated_api_keys": []})}.items():
            with self.subTest(name):
                o = Owner({"k1": f"lc-{T}-reader"})
                o.answers[("DELETE", "/_security/api_key")] = answer
                with self.assertRaises(keyowner.RotationError):
                    keyowner.invalidate_all(o, T)
                self.assertIn("k1", o.keys)

    def test_a_created_key_must_be_the_one_asked(self):
        # at 4e7ee85 any HTTP 200 was the new key: a key of another name (another role's privileges) was staged as this role's
        for answer in ((200, {}), (200, {"id": "kx", "name": f"lc-{T}-other", "encoded": "eA=="}), (200, {"id": "kx", "name": f"lc-{T}-reader"})):
            with self.subTest(answer), tempfile.TemporaryDirectory() as tmp:
                o = Owner({"old": f"lc-{T}-reader"})
                o.answers[("POST", "/_security/api_key")] = answer
                with self.assertRaises(keyowner.RotationError):
                    keyowner.rotate(o, T, ["base"], Path(tmp))
                self.assertIn("old", o.keys)                     # nothing was rotated away
                self.assertEqual(list(Path(tmp).rglob("*.key")), [])


# ---- the class reader and the pool tools: heads, leaves ---------------------------------------------------------------------------
class Reader(unittest.TestCase):
    POOL = "p" * 16

    def setUp(self):
        self.es = FakeEs()
        self.ctl = class_index(T, "base", "ctl")
        self.did = f"lh:{self.POOL}:L1"
        self.es.put(self.ctl, self.did, {"pool": self.POOL, "current_lv": 1, "status": "live"})
        self.r = ClassReader(lambda cls: self.es, T)

    def answer(self, method, suffix, answer):
        """Answer the request of `method` whose path ends with `suffix` with `answer`; serve every other request normally."""
        real = FakeEs.request.__get__(self.es)
        self.es.request = lambda m, p, b=None, **kw: answer if m == method and p.split("?")[0].endswith(suffix) else real(m, p, b, **kw)

    def test_control_a_head_reads_and_a_missing_one_is_none(self):
        self.assertEqual(self.r.get("base", "ctl", self.did, self.POOL).source["current_lv"], 1)
        self.assertIsNone(self.r.get("base", "ctl", f"lh:{self.POOL}:L2", self.POOL))
        self.assertEqual(sorted(self.r.mget("base", "ctl", [self.did, f"lh:{self.POOL}:L2"], self.POOL)), [self.did, f"lh:{self.POOL}:L2"])

    def test_a_head_answer_that_is_not_this_head_refuses(self):
        # at 4e7ee85 a get read `(200, {})` and every 404 as "no head" (a retract then answered `retracted` with nothing retracted)
        for name, answer in {"200 with an empty body": (200, {}), "404 with an empty body": (404, {}),
                             "200 found for another _id": doc_answer("GET", behind(self.ctl), self.did + "x", True, {"pool": self.POOL}),
                             "200 found without sequence fields": (200, {"_index": behind(self.ctl), "_id": self.did, "found": True,
                                                                         "_source": {"pool": self.POOL}})}.items():
            with self.subTest(name):
                self.answer("GET", self.did, answer)
                with self.assertRaises(ReaderRefused):
                    self.r.get("base", "ctl", self.did, self.POOL)

    def test_a_multi_get_item_for_another_id_refuses(self):
        # at 4e7ee85 the answered `_id` was trusted, so a foreign item filled another key and the asked one read as missing
        self.answer("POST", "_mget", (200, {"docs": [{"_index": behind(self.ctl), "_id": self.did + "x", "found": False}]}))
        with self.assertRaises(ReaderRefused):
            self.r.mget("base", "ctl", [self.did], self.POOL)

    def test_a_search_or_count_answered_404_refuses(self):
        # at 4e7ee85 a 404 to a search read as an empty result (no hold) and to a count as 0
        self.answer("POST", "_search", (404, {}))
        with self.assertRaises(ReaderRefused):
            self.r.search("base", "ctl", self.POOL, [{"term": {"kind": "quarantine"}}])
        self.answer("POST", "_count", (404, {}))
        with self.assertRaises(ReaderRefused):
            self.r.count("base", "ctl", self.POOL, [{"term": {"kind": "quarantine"}}])


class PoolReads(unittest.TestCase):
    """Through the pool tools of tests/unit/pool_world.py: a retract whose head read is malformed, and a fetch whose leaf read answers
    another document."""

    def setUp(self):
        from tests.unit import pool_world as pw
        self.pw = pw
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.w = pw.World(Path(self._tmp.name), n_pools=1)
        self.addCleanup(self.w.close)
        self.P = self.w.pools[0]
        self.tok = self.w.scoped("c1")
        out = self.w.gw.handle(self.tok, {"op": "pool_save", "pool": self.P, "text": pw.TEXT, "key": "k1"})
        self.assertTrue(out.get("ok"), out)
        self.ptr = out["pointer"]
        self.head = next((ix, did) for (ix, did) in self.w.es.docs if did.startswith(f"lh:{self.P}:"))
        leaf_ix = pool_index(pw.T, "base", self.P, "leaf")
        self.leaf = next((ix, did) for (ix, did) in self.w.es.docs if ix == leaf_ix)

    def retract(self):
        return self.w.gw.handle(self.tok, {"op": "pool_retract", "pointers": [self.ptr], "reason": "harmful", "key": "r1"})

    def test_a_retract_whose_head_read_is_malformed_refuses(self):
        for answer in ((200, {}), (404, {})):
            with self.subTest(answer):
                self.w.es.doc_answers[("GET", *self.head)] = [answer]
                self.assertEqual(self.retract(), self.pw.UNAV)            # at 4e7ee85: {"results": ["retracted"]}, nothing retracted
                self.assertEqual(self.w.es.docs[self.head][0]["status"], "live")

    def test_control_a_retract_retracts(self):
        self.assertEqual(self.retract()["results"], ["retracted"])
        self.assertEqual(self.w.es.docs[self.head][0]["status"], "retracted")

    def test_a_fetch_whose_leaf_read_answers_another_document_refuses(self):
        ix, did = self.leaf
        src = dict(self.w.es.docs[self.leaf][0], text="another version's text")
        self.w.es.doc_answers[("GET", ix, did)] = [doc_answer("GET", behind(ix), did.split(":")[0] + ":9", True, src)]
        got = self.w.gw.handle(self.tok, {"op": "pool_fetch", "pointer": self.ptr})
        self.assertEqual(got, self.pw.UNAV)                              # at 4e7ee85: the other document's text was handed off
        got = self.w.gw.handle(self.tok, {"op": "pool_fetch", "pointer": self.ptr})   # the control
        self.assertTrue(got.get("ok") and "gannet roost" in got.get("text", ""), got)


class Lookup(unittest.TestCase):
    def test_a_lookup_answered_with_another_document_refuses(self):
        lid = new_leaf_id()
        leaf = class_index(T, "base", "leaf")
        answers = []

        class Client:
            def request(self, method, path, body=None, **kw):
                return answers[0]

        class Gate:
            def client(self, tenant, role):
                return Client()

        lat = ClassLattice(["base"], [])
        gw = Gateway(Gate(), None, {T: lat})
        p = Principal(T, "pt-user-alice", frozenset({Role.READER}), frozenset({"base"}), frozenset({"base"}))
        req = {"pointer": encode_pointer(Pointer(KIND_LEAF, 0, lid, 1))}
        mine = {"_index": behind(leaf), "_id": f"{lid}:1", "found": True, "_seq_no": 1, "_primary_term": 1, "_source": {"text": "mine"}}
        answers[:] = [(200, {"docs": [mine]})]
        self.assertEqual(gw._lookup(T, p, lat, req)["text"], "mine")    # the control
        for name, answer in {"another _id": (200, {"docs": [dict(mine, _id=f"{lid}:2", _source={"text": "other"})]}),
                             "another index": (200, {"docs": [dict(mine, _index=f"lc-{T}-base-other", _source={"text": "other"})]})}.items():
            with self.subTest(name):
                answers[:] = [answer]
                self.assertEqual(gw._lookup(T, p, lat, req), {"ok": False, "error": "unavailable"})


# ---- the writer: the read and the write every repair and counter decision is taken from --------------------------------------------
class WriterAnswers(unittest.TestCase):
    def setUp(self):
        self.es = DocStore()
        self.w = Writer(lambda: self.es, None, T, None)
        self.ctl = f"lc-{T}-ctl"

    def test_control_the_counter_allocates(self):
        self.assertEqual([self.w.next_seq(), self.w.next_seq()], [1, 2])

    def test_a_counter_write_that_did_not_land_is_an_error(self):
        # at 4e7ee85 any 200 or 201 to the PUT was the allocation, and a bare 409 a lost race (retried)
        for name, answer in unconfirmed_puts(self.ctl, "seq").items():
            with self.subTest(name):
                self.setUp()
                self.es.doc_answers[("PUT", self.ctl, "seq")] = [answer]
                with self.assertRaises(WriterError):
                    self.w.next_seq()

    def test_a_read_that_proves_no_absence_is_an_error(self):
        # at 4e7ee85 every 404 was "missing" (repair then re-projected, recreated a head or re-queued) and every 200 dict the document
        for name, answer in {"404 with an empty body": (404, {}),
                             "404 found false for another _id": (404, {"_index": behind(self.ctl), "_id": "seqx", "found": False}),
                             "200 found for another _id": doc_answer("GET", behind(self.ctl), "seqx", True, {"value": 9}),
                             "200 with an empty body": (200, {})}.items():
            with self.subTest(name):
                self.es.doc_answers[("GET", self.ctl, "seq")] = [answer]
                with self.assertRaises(WriterError):
                    self.w._get(self.ctl, "seq")
        self.assertIsNone(self.w._get(self.ctl, "seq"))                  # the control: a verified absence

    def test_retries_are_bounded(self):
        # a permanently malformed answer refuses at the first write; a permanent typed conflict ends after CAS_TRIES writes, never spins
        for name, answer, puts in (("malformed", (200, {}), 1), ("typed conflict", conflict(behind(self.ctl)), writer_mod.CAS_TRIES)):
            with self.subTest(name), mock.patch.object(writer_mod.time, "sleep", lambda s: None):
                self.setUp()
                self.es.doc_answers[("PUT", self.ctl, "seq")] = [answer] * 1000
                with self.assertRaises(WriterError):
                    self.w.next_seq()
                self.assertEqual(self.es.sent.count(("PUT", f"/{self.ctl}/_doc/seq")), puts)


# ---- the write-path watermark P -----------------------------------------------------------------------------------------------------
class Barrier(DocStore):
    """DocStore whose refreshes answer with a failed shard while `fail_refresh` is set."""

    fail_refresh = False

    def request(self, method, path, body=None, **kw):
        if self.fail_refresh and path.split("?")[0].endswith("/_refresh"):
            self.sent.append((method, path.split("?")[0]))
            return 200, {"_shards": FAILED_SHARD}
        return super().request(method, path, body, **kw)


class Watermark(unittest.TestCase):
    def setUp(self):
        self.es = Barrier()
        self.wp = WritePath(lambda: self.es, T)
        self.ctl, self.member = f"lc-{T}-ctl", f"lc-{T}-base-member"

    def doc(self):
        return {"tenant": T, "cls": "base", "kind": "member", "lin_leaves": [], "lin_leaf_ids": ["a" * 32], "lin_nodes": [],
                "lin_subjects": [], "lin_queries": [], "derivation": "write_path", "erasure_rewrite": 0,
                "created_at": "2026-01-01T00:00:00+00:00", "schema_v": 1}

    def test_control_p_moves_after_the_barrier(self):
        j = self.wp.begin(1)
        self.assertTrue(self.wp.write(j, self.member, "m1", self.doc()))
        self.wp.finish(j)
        self.assertEqual((self.wp.advance(), self.wp.read_p()), (1, 1))
        self.assertIn(("POST", f"/{self.member}/_refresh"), self.es.sent)

    def test_p_does_not_move_past_a_barrier_refresh_that_failed(self):
        # at 4e7ee85 the refresh answer was ignored: P moved although the members were not searchable
        j = self.wp.begin(1)
        self.wp.write(j, self.member, "m1", self.doc())
        self.wp.finish(j)
        self.es.fail_refresh = True
        with self.assertRaises(WatermarkError):
            self.wp.advance()
        self.assertEqual(self.wp.read_p(), 0)
        self.es.fail_refresh = False
        self.assertEqual(self.wp.advance(), 1)                           # the indices are refreshed again at the next advance

    def test_a_p_write_or_read_that_is_not_about_wp_is_an_error(self):
        # at 4e7ee85 any 200 or 201 stored P, and a 200 GET was read without checking it was `wp`
        self.wp.finish(self.wp.begin(1))                                 # P's target is 1, nothing to refresh
        for name, answer in unconfirmed_puts(self.ctl, "wp").items():
            with self.subTest(name):
                self.es.doc_answers[("PUT", self.ctl, "wp")] = [answer]
                with self.assertRaises(WatermarkError):
                    self.wp.advance()
                self.es.doc_answers.clear()
                self.assertEqual(self.wp.read_p(), 0)
        for answer in ((200, {}), (404, {}), doc_answer("GET", behind(self.ctl), "wpx", True, {"P": 7})):
            with self.subTest(answer):
                self.es.doc_answers[("GET", self.ctl, "wp")] = [answer]
                with self.assertRaises(WatermarkError):
                    self.wp.read_p()

    def test_retries_are_bounded(self):
        # a permanently malformed answer refuses at the first write; a permanent typed conflict ends after CAS_TRIES writes, never spins
        for name, answer, puts in (("malformed", (200, {}), 1), ("typed conflict", conflict(behind(self.ctl)), watermark_mod.CAS_TRIES)):
            with self.subTest(name):
                self.setUp()
                self.wp.finish(self.wp.begin(1))
                self.es.doc_answers[("PUT", self.ctl, "wp")] = [answer] * 1000
                with self.assertRaises(WatermarkError):
                    self.wp.advance()
                self.assertEqual(self.es.sent.count(("PUT", f"/{self.ctl}/_doc/wp")), puts)

    def test_a_cancelled_jobs_delete_must_be_confirmed(self):
        # at 4e7ee85 the delete answer was ignored: P moved past a job whose document was still there
        j = self.wp.begin(1)
        self.wp.write(j, self.member, "m1", self.doc())
        self.es.doc_answers[("DELETE", self.member, "m1")] = [(200, {})]
        self.wp._clock = lambda: 1e12                                    # the job is overdue
        with self.assertRaises(WatermarkError):
            self.wp.advance()
        self.assertIn(1, self.wp._open)                                  # the job stays open, so P stays behind it
        self.assertEqual(self.wp.read_p(), 0)

    def test_a_job_write_that_did_not_land_is_an_error(self):
        j = self.wp.begin(1)
        self.es.doc_answers[("PUT", self.member, "m1")] = [(200, {})]
        with self.assertRaises(WatermarkError):                         # at 4e7ee85: True, the write "landed"
            self.wp.write(j, self.member, "m1", self.doc())
        self.assertIn((self.member, "m1"), j.written)                    # it may have landed: a cancel deletes it


# ---- the persisted copies of grants and tokens, and the pool manifest ----------------------------------------------------------------
class Persisters(unittest.TestCase):
    def test_a_grant_or_token_copy_that_did_not_land_is_a_failed_write(self):
        p = Principal(T, "pt-user-alice", frozenset({Role.READER}), frozenset({"base"}), frozenset({"base"}))

        class Gate:
            def __init__(self, fa):
                self.fa = fa

            def client(self, tenant, role):
                return self.fa

        names = list(unconfirmed_puts(f"lc-{T}-authz", "x"))
        for name in names:
            with self.subTest(name):
                fa = FakeAuthz()
                fa.doc_answers[("PUT", "gr:pt-user-alice")] = unconfirmed_puts(f"lc-{T}-authz", "gr:pt-user-alice")[name]
                fa.doc_answers[("PUT", "tk:h1")] = unconfirmed_puts(f"lc-{T}-authz", "tk:h1")[name]
                with self.assertRaises(RuntimeError):                    # at 4e7ee85: accepted on its status
                    boot.grant_persister(Gate(fa), T)(p)
                with self.assertRaises(RuntimeError):
                    pool_boot.token_persister(lambda: fa, T)("h1", {"principal": "pt-user-alice", "exp": 5.0})
        fa = FakeAuthz()                                                  # the control
        boot.grant_persister(Gate(fa), T)(p)
        pool_boot.token_persister(lambda: fa, T)("h1", {"principal": "pt-user-alice", "exp": 5.0})
        self.assertEqual(sorted(fa.docs), ["gr:pt-user-alice", "tk:h1"])


class PoolManifest(unittest.TestCase):
    def test_a_pool_whose_manifest_did_not_land_is_not_created(self):
        from tests.security.test_pool_tokens import World
        cases = list(unconfirmed_puts(f"lc-{T}-base-ctl", "pm:x").items())[:4] + [("control", None)]
        for name, answer in cases:
            with self.subTest(name), tempfile.TemporaryDirectory() as tmp:
                ctl = DocStore()
                if answer is not None:
                    real = ctl.request
                    ctl.request = lambda m, p, b=None, real=real, answer=answer, **kw: \
                        answer if m == "PUT" and "/_doc/pm:" in p else real(m, p, b, **kw)
                w = World(tmp, svc_kw={"create_index": lambda phys, body: None, "ctl_client": lambda cls: ctl}).start()
                try:
                    r = w.admin("pool_create", cls="base", kind="team", label="alpha")
                    if answer is None:
                        self.assertTrue(r["ok"], r)
                        self.assertEqual(len(w.dir.pools_of(w.tenant)), 1)
                    else:
                        self.assertEqual(r, {"ok": False, "error": "unavailable"})   # at 4e7ee85: created, with no manifest for erasure
                        self.assertEqual(w.dir.pools_of(w.tenant), [])
                finally:
                    w.rev.clean_stop()


class Admin:
    """The bootstrap client of a provisioning: the registry record is absent unless `registry_get` says otherwise; every index and key
    is created as asked; `registry_put` replaces the answer to the registry write."""

    def __init__(self, registry_get=None, registry_put=None):
        self.registry_get, self.registry_put, self.sent = registry_get, registry_put, []

    def request(self, method, path, body=None, **kw):
        p = path.split("?")[0]
        self.sent.append((method, p))
        if p == "/_cat/shards":
            return 200, []
        if p == f"/{REGISTRY}/_doc/{T}":
            if method == "GET":
                return self.registry_get or doc_answer("GET", behind(REGISTRY), T, False)
            return self.registry_put or doc_answer("PUT", behind(REGISTRY), T, True, created=True)
        if method == "PUT" and "/" not in p[1:]:
            return 200, {"acknowledged": True, "shards_acknowledged": True, "index": p[1:]}
        if method == "POST" and p == "/_security/api_key":
            return 200, {"id": f"k{len(self.sent)}", "name": body["name"], "api_key": "x", "encoded": "eA==", "expiration": 1}
        return 503, {}


class Provision(unittest.TestCase):
    def provision(self, admin, tmp):
        return provision_tenant(admin, T, ["base"], key_dir=Path(tmp) / "keys", data_root=Path(tmp) / "data", journal_size=65536)

    def test_control_a_tenant_is_provisioned_and_registered(self):
        with tempfile.TemporaryDirectory() as tmp:
            admin = Admin()
            self.assertEqual(self.provision(admin, tmp)["tenant"], T)
            self.assertIn(("PUT", f"/{REGISTRY}/_doc/{T}"), admin.sent)

    def test_a_registry_read_that_proves_no_absence_refuses(self):
        # at 4e7ee85 anything but a 200 found read as "no such tenant", so a tenant whose record could not be read was provisioned again
        for answer in ((200, {}), (404, {}), (503, {}), doc_answer("GET", behind(REGISTRY), "other", False)):
            with self.subTest(answer), tempfile.TemporaryDirectory() as tmp:
                admin = Admin(registry_get=answer)
                with self.assertRaises(ProvisionRefused) as e:
                    self.provision(admin, tmp)
                self.assertEqual(str(e.exception), "registry unreadable")
                self.assertEqual([m for m, _ in admin.sent], ["GET", "GET"])    # nothing created

    def test_a_registry_record_that_did_not_land_is_an_error(self):
        # at 4e7ee85 the registry write's answer was ignored: a second provisioning of the tenant would not be refused
        for name, answer in list(unconfirmed_puts(REGISTRY, T).items())[:3]:
            with self.subTest(name), tempfile.TemporaryDirectory() as tmp:
                with self.assertRaises(ProvisionRefused) as e:
                    self.provision(Admin(registry_put=answer), tmp)
                self.assertEqual(str(e.exception), "registry record not written")


# ---- erasure and the verifier --------------------------------------------------------------------------------------------------
class Journal:
    def __init__(self):
        self.appended = []

    def append(self, rec):
        self.appended.append(rec)
        return len(self.appended)


class NoHolds:
    def covered(self, *a):
        return False


class ErasureRefresh(unittest.TestCase):
    def test_the_closure_is_not_read_after_a_refresh_that_failed(self):
        # at 4e7ee85 the refresh before the closure was not checked: an erasure could publish a closure that missed what the refresh
        # did not make searchable
        for partial in (1, 0):
            with self.subTest(partial=partial), tempfile.TemporaryDirectory() as tmp:
                es = ShardedFake()
                es.put(f"lc-{T}-src", "s1", {"kind": "src", "lin_subjects": ["subj"]})
                es.partial = partial                                       # the next answer (the pre-closure refresh) has a failed shard
                ks = Keystore(Path(tmp) / "ks.db", T)
                self.addCleanup(ks.close)
                j = Journal()
                ex = Executor(lambda: es, lambda: es, T, ks, j, WriteGate(), SendFence(), State(), holds=NoHolds())
                with mock.patch.object(Executor, "_physical", lambda self, *a, **kw: "physical"):
                    if partial:
                        with self.assertRaises(scan.ScanError):
                            ex.erase_subject("subj")
                        self.assertEqual(j.appended, [])                   # nothing published
                        self.assertEqual(ex.state.seq, 0)
                    else:
                        self.assertEqual(ex.erase_subject("subj"), "physical")   # the control
                        self.assertEqual(len(j.appended), 1)
                        self.assertIn("s1", ex.state.redact)


class VerifierListing(unittest.TestCase):
    def test_an_index_listing_with_an_unnamed_item_is_not_clean(self):
        # at 4e7ee85 an item without a name was skipped, so the pool's own index could be missing from the count
        class Es:
            def __init__(self, rows):
                self.rows = rows

            def request(self, method, path, body=None, **kw):
                if "_refresh" in path:
                    return 200, {"_shards": SHARDS_ONE}
                if path.startswith("/_resolve/index/"):
                    return 200, {"indices": self.rows}
                return 200, {"timed_out": False, "_shards": SHARDS_ONE, "hits": {"total": {"value": 0, "relation": "eq"}, "hits": []},
                             "aggregations": {"by": {"buckets": []}}}

        pool = "p" * 16
        named = {"name": f"lc-{T}-base-{pool}-leaf-p0a0b0c0d"}
        self.assertEqual(pool_residue(Es([named]), T, pool)["indices"], 1)   # the control
        for rows in ([named, {"aliases": []}], [{"name": ""}], ["lc-x"]):
            with self.subTest(rows):
                with self.assertRaises(VerifierError):
                    pool_residue(Es(rows), T, pool)


if __name__ == "__main__":
    unittest.main()
