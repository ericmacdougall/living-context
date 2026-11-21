"""Round 5 (the F11 class on the conversation taint): the taint only rises and is durable (POOLS 2.8), and after a restart it is read
back from the tenant control index. At f16ea94 `es_load` read ANY 404 as "no taint yet" and any 200 dict as the stored record, so an
empty or foreign 404, or a 200 that is not this record found, lowered the conversation to the floor. Only a verified absence of exactly
this record (a 404 `found: false` about its `_index` and `_id`, through the control alias, or the index absent) reads as no taint now;
anything else refuses (`StateError`: the caller answers `unavailable`)."""
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from living_context.gateway import session_state
from living_context.gateway.session_state import SessionStates, StateError, Taint, es_load, es_persist
from living_context.policy import ClassLattice
from tests.unit.fake_es import DocStore, behind, conflict, doc_answer, unconfirmed_puts

T = "tena"
KEY = (T, "pt-user-alice", "conv1")
CTL = f"lc-{T}-ctl"
DID = session_state._doc_id(KEY)
RAISED = Taint("restricted", frozenset({"p" * 16}))


class TaintLoad(unittest.TestCase):
    def setUp(self):
        self.es = DocStore()
        self.load = es_load(lambda: self.es, T)

    def test_a_stored_taint_reads_back(self):   # the control: written by the real compare-and-set persist
        self.assertEqual(es_persist(lambda: self.es, T)(KEY, lambda stored: RAISED), RAISED)
        self.assertEqual(self.load(KEY), RAISED)

    def test_a_verified_absence_is_no_taint(self):
        self.assertIsNone(self.load(KEY))                                       # 404 found false about exactly this record
        self.es.doc_answers[("GET", CTL, DID)] = [(404, {"error": {"type": "index_not_found_exception", "index": CTL,
                                                                   "resource.id": CTL}, "status": 404})]
        self.assertIsNone(self.load(KEY))                                       # the control alias itself is absent

    def test_an_answer_that_proves_no_absence_never_lowers_the_taint(self):
        es_persist(lambda: self.es, T)(KEY, lambda stored: RAISED)
        for name, answer in {
            "404 with an empty body": (404, {}),
            "404 found false for another _id": (404, {"_index": behind(CTL), "_id": DID + "x", "found": False}),
            "404 found false for another index": (404, {"_index": f"lc-{T}-other", "_id": DID, "found": False}),
            "404 index_not_found naming another index": (404, {"error": {"type": "index_not_found_exception", "index": f"lc-{T}-other",
                                                                         "resource.id": f"lc-{T}-other"}, "status": 404}),
            "404 that says found": (404, {"_index": behind(CTL), "_id": DID, "found": True, "_source": {}}),
            "200 without found": (200, {"_index": behind(CTL), "_id": DID, "_source": {}, "_seq_no": 1, "_primary_term": 1}),
            "200 found for another _id": doc_answer("GET", behind(CTL), DID + "x", True, {}),
            "200 with an empty body": (200, {}),
        }.items():
            with self.subTest(name):
                self.es.doc_answers[("GET", CTL, DID)] = [answer]
                with self.assertRaises(StateError):
                    self.load(KEY)
        self.assertEqual(self.load(KEY), RAISED)


def at_least_base(stored):
    """A raise to `base` (the merge a raise hands `persist`): the stored taint joined with base, which `restricted` dominates."""
    if stored is None or stored.cls is None:
        return Taint("base", stored.private if stored else frozenset())
    return stored


def sent(es, method):
    return sum(1 for m, p in es.sent if m == method and p == f"/{CTL}/_doc/{DID}")


class TaintPersist(unittest.TestCase):
    """Round 6 (F19): the compare-and-set loop of `es_persist`. At 4e7ee85 it read the stored record with its own unchecked GET (any 200
    with `found` was the record, whatever `_index` or `_id` it named, and anything else was "no record yet") and took any 200 or 201 to
    its PUT as durable. A raised taint that was never written was reported stored and the reply handed off; a foreign answer's source
    and sequence numbers could drive a compare-and-set that overwrote the real record with a lower taint. Now the read is
    `reads.doc_read` and the write `reads.doc_written`, both through the control alias: a malformed or foreign answer refuses at once
    (`StateError`, the caller answers `unavailable`), only the typed version conflict is retried, and that retry is bounded."""

    def setUp(self):
        self.es = DocStore()
        self.persist = es_persist(lambda: self.es, T)
        self.load = es_load(lambda: self.es, T)

    def test_a_malformed_or_foreign_read_refuses_and_writes_nothing(self):
        self.persist(KEY, lambda stored: RAISED)
        for name, answer in {
            "200 with an empty body": (200, {}),
            "200 with a list body": (200, []),
            "200 found for another _id": doc_answer("GET", behind(CTL), DID + "x", True, {"taint_cls": ""}),
            "200 found for another index": doc_answer("GET", f"lc-{T}-other", DID, True, {"taint_cls": ""}),
            "200 found without sequence fields": (200, {"_index": behind(CTL), "_id": DID, "found": True, "_source": {"taint_cls": ""}}),
            "200 without found": (200, {"_index": behind(CTL), "_id": DID, "_source": {}, "_seq_no": 1, "_primary_term": 1}),
            "404 with an empty body": (404, {}),
            "404 found false for another _id": (404, {"_index": behind(CTL), "_id": DID + "x", "found": False}),
            "404 index_not_found naming another index": (404, {"error": {"type": "index_not_found_exception", "index": f"lc-{T}-other",
                                                                         "resource.id": f"lc-{T}-other"}, "status": 404}),
            "503": (503, {"error": {"type": "unavailable_shards_exception"}}),
        }.items():
            with self.subTest(name):
                self.es.doc_answers[("GET", CTL, DID)] = [answer] * 50   # permanently malformed: refused at once, never retried
                before = sent(self.es, "GET"), sent(self.es, "PUT")
                with self.assertRaises(StateError):
                    self.persist(KEY, at_least_base)
                self.assertEqual((sent(self.es, "GET") - before[0], sent(self.es, "PUT") - before[1]), (1, 0))
                self.es.doc_answers.clear()
                self.assertEqual(self.load(KEY), RAISED)          # the real record was never overwritten with a lower taint

    def test_a_malformed_write_acknowledgement_is_not_a_stored_taint(self):
        for name, answer in unconfirmed_puts(CTL, DID).items():
            with self.subTest(name):
                self.es = DocStore()
                self.persist, self.load = es_persist(lambda: self.es, T), es_load(lambda: self.es, T)
                self.es.doc_answers[("PUT", CTL, DID)] = [answer] * 50
                with self.assertRaises(StateError):
                    self.persist(KEY, lambda stored: RAISED)
                self.assertEqual(sent(self.es, "PUT"), 1)         # refused at once: a permanently malformed answer never spins
                self.es.doc_answers.clear()
                self.assertIsNone(self.load(KEY))                 # and nothing was stored

    def test_a_lost_compare_and_set_is_retried_and_the_retry_is_bounded(self):
        self.persist(KEY, lambda stored: Taint("base"))
        self.es.doc_answers[("PUT", CTL, DID)] = [conflict(behind(CTL))] * 3
        self.assertEqual(self.persist(KEY, lambda stored: RAISED), RAISED)   # three lost races, then the fourth write lands
        self.assertEqual(self.load(KEY), RAISED)
        self.es.doc_answers[("PUT", CTL, DID)] = [conflict(behind(CTL))] * 100
        before = sent(self.es, "PUT")
        with self.assertRaises(StateError):
            self.persist(KEY, lambda stored: RAISED)
        self.assertEqual(sent(self.es, "PUT") - before, 20)          # bounded: a conflict that never clears surfaces as a refusal

    def test_a_restart_keeps_every_disclosed_taint(self):
        rec = SimpleNamespace(tenant=T, principal_token=KEY[1], conversation=KEY[2], hash="h1")
        lat = {T: ClassLattice(["base", "restricted"], [("restricted", "base")])}
        states = SessionStates(lat, self.persist, self.load)
        pools = [SimpleNamespace(id="a" * 16, cls="restricted", private=False), SimpleNamespace(id="p" * 16, cls="base", private=True),
                 SimpleNamespace(id="q" * 16, cls="base", private=True)]
        self.assertTrue(states.raise_to(rec, states.raised_by(rec, pools[:1])))       # disclosed: restricted
        self.assertTrue(states.raise_to(rec, states.raised_by(rec, pools[1:2])))      # disclosed: private pool p
        self.es.doc_answers[("PUT", CTL, DID)] = [(200, {})]
        with self.assertRaises(StateError):                                            # not stored, so the reply for q is aborted
            states.raise_to(rec, states.raised_by(rec, pools[2:]))
        states.forget_memory()                                                         # a gateway restart
        self.assertEqual(states.get(rec), Taint("restricted", frozenset({"p" * 16})))
        self.assertEqual(self.load(KEY), Taint("restricted", frozenset({"p" * 16})))


class TaintDisclosure(unittest.TestCase):
    """F19 end to end: a fetch from a private pool raises the conversation's taint, and the item is handed off only after the raise is
    stored. A malformed write acknowledgement aborts it (`unavailable`, no text); after a restart the stored taint holds every pool whose
    content was handed off."""

    def setUp(self):
        from tests.unit import pool_world as pw
        self.pw = pw
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.w = w = pw.World(Path(self._tmp.name), n_pools=0)
        self.addCleanup(w.close)
        self.P = w.dir.create(pw.T, "base", "person", "mine").id     # a person pool is private
        w.dir.grant(pw.T, "pt-user-alice", self.P, pw.OPS)
        self.tok = w.sessions.mint_scoped(pw.T, b"user-alice", conversation="c1", scope={self.P: pw.OPS}, ttl_s=600)
        out = w.gw.handle(self.tok, {"op": "pool_save", "pool": self.P, "text": pw.TEXT, "key": "k1"})
        self.assertTrue(out.get("ok"), out)
        self.ptr = out["pointer"]
        self.ctl, self.did = f"lc-{pw.T}-ctl", session_state._doc_id((pw.T, "pt-user-alice", "c1"))
        self.states = SessionStates(pw.LAT, es_persist(lambda: w.es, pw.T), es_load(lambda: w.es, pw.T))
        w.tools.states = self.states

    def fetch(self):
        return self.w.gw.handle(self.tok, {"op": "pool_fetch", "pointer": self.ptr})

    def stored(self):
        return es_load(lambda: self.w.es, self.pw.T)((self.pw.T, "pt-user-alice", "c1"))

    def test_a_malformed_write_acknowledgement_aborts_the_disclosure(self):
        for name, answer in unconfirmed_puts(self.ctl, self.did).items():
            with self.subTest(name):
                self.w.es.doc_answers[("PUT", self.ctl, self.did)] = [answer]
                got = self.fetch()
                self.assertEqual(got, self.pw.UNAV, "the item was handed off on an unconfirmed taint write")
                self.states.forget_memory()
                self.assertIsNone(self.stored())

    def test_control_a_confirmed_raise_hands_the_item_off_and_survives_a_restart(self):
        got = self.fetch()
        self.assertTrue(got.get("ok") and got.get("text"), got)
        self.assertEqual(got.get("taint_raised"), [self.P])
        self.states.forget_memory()
        self.assertEqual(self.stored(), Taint("base", frozenset({self.P})))
        self.assertIn(self.P, self.states.get(self.w.sessions.resolve(self.tok)).private)


if __name__ == "__main__":
    unittest.main()
