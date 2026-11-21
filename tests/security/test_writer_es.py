"""BUILD-PLAN Step 6 on a real Elasticsearch: every check drives the real `Writer` (AC-7, AC-16).

Needs LC_TEST_ES_URL, LC_TEST_CA and LC_TEST_PW. Without them the module FAILS (it never skips).
Each failing-first bullet runs its check against the real writer and against a broken variant, and
the check must go red for the stated reason on the broken one.
"""
import base64
import json
import os
import re
import tempfile
import threading
import uuid
import unittest
from pathlib import Path
from unittest import mock

from living_context.gateway.es import EsClient
from living_context.gateway.handler import EsGate, Gateway, HELD as HELD_ANSWER, REFUSED
from living_context.gateway.provision import provision_tenant
from living_context.gateway.fence import SendFence
from living_context.gateway.sessions import EpochTable, Sessions
from living_context.ingest import segment, title
from living_context.ingest.segment import Turn
from living_context.ingest.subjects import Guard, TokenSource
from living_context.gateway.indices import class_index
from living_context.ingest import writer as wmod
from living_context.ingest.writer import Writer
from living_context.keystore import Keystore
from living_context.keystore.store import KeystoreError
from living_context.policy import ClassLattice, Principal, Role

URL, CA, PW = (os.environ.get(k) for k in ("LC_TEST_ES_URL", "LC_TEST_CA", "LC_TEST_PW"))
HAVE = bool(URL and CA and PW)
T = "wrta"
LAT = ClassLattice(["base"], [])
CAP = 1536


class Crash(Exception):
    pass


def reset(admin):
    st, rows = admin.request("GET", "/_cat/indices/lc-*?format=json&h=index")
    for row in rows if st == 200 and isinstance(rows, list) else []:
        if row["index"].startswith(f"lc-{T}-") or row["index"] == "lc-sys-registry":
            admin.request("DELETE", "/" + row["index"])


class Adapter:
    def verify(self, tenant, proof):
        return proof.decode() if proof.startswith(b"user") else None


class WriterOnElasticsearch(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not HAVE:
            raise RuntimeError("LC_TEST_ES_URL, LC_TEST_CA and LC_TEST_PW must point at a scratch cluster")
        cls.tmp = tempfile.TemporaryDirectory()
        root = Path(cls.tmp.name)
        cls.admin = EsClient(URL, CA, PW, scheme="basic", user="elastic")
        reset(cls.admin)
        r = provision_tenant(cls.admin, T, ["base", "restricted"], key_dir=root / "keys", data_root=root / "data", journal_size=65536)
        cls.gate = EsGate(URL, CA, {T: r["keys"]})
        cls.ks = Keystore(root / "ks" / f"{T}.db", T)
        cls.guard = Guard(TokenSource(cls.ks))

    @classmethod
    def tearDownClass(cls):
        if HAVE:
            reset(cls.admin)
            cls.ks.close()
            cls.tmp.cleanup()

    # ---- helpers -----------------------------------------------------------------------------
    def writer(self, cls=Writer, **kw):
        return cls(lambda: self.gate.client(T, "ingest"), self.ks, T, self.guard, **kw)

    def count(self, kind):
        self.admin.request("POST", f"/lc-{T}-*/_refresh")
        idx = {"src": f"lc-{T}-src", "leaf": f"lc-{T}-base-leaf", "ctl": f"lc-{T}-base-ctl"}[kind]
        st, r = self.admin.request("GET", f"/{idx}/_count")
        return r["count"]

    def doc(self, index, doc_id):
        st, r = self.admin.request("GET", f"/{index}/_doc/{doc_id}?_source_exclude_vectors=false")
        return r["_source"] if st == 200 else None

    def run_checked(self, check, *args):
        """Run a check; return the AssertionError text if it goes red, else None."""
        try:
            check(*args)
        except AssertionError as e:
            return str(e)
        return None

    # ---- the segment cap, end to end ----------------------------------------------------------
    def test_longest_session_writes_no_leaf_over_the_cap(self):
        w = self.writer()
        leaves, _ = segment.segment([[Turn("user", "word " * 15_600)]], CAP)
        ids = []
        for leaf in leaves:
            res = w.remember("tok", "base", segment.render(leaf), key=None)
            self.assertTrue(res.ok, res.status)
            ids.append(f"{res.leaf_id}:{res.lv}")
        sizes = [self.doc(f"lc-{T}-base-leaf", i)["size_tokens"] for i in ids]
        self.assertGreater(len(sizes), 20)
        self.assertLessEqual(max(sizes), CAP)
        # broken: pieces that were only split at round boundaries keep one over-cap leaf; the writer refuses it
        big, _ = segment.segment([[Turn("user", "word " * 15_600)]], CAP, rounds_only=True)
        res = w.remember("tok", "base", segment.render(big[0]))
        self.assertEqual(res.status, wmod.TOO_LARGE)

    def test_empty_input_is_refused_and_writes_nothing(self):
        w = self.writer()
        before = (self.count("src"), self.count("leaf"))
        for bad in ("", "   \n\t", None):
            self.assertEqual(w.remember("tok", "base", bad).status, wmod.EMPTY)
        self.assertEqual((self.count("src"), self.count("leaf")), before)

    # ---- idempotency ----------------------------------------------------------------------------
    def check_key_behaviour(self, w):
        k1 = "k-" + uuid.uuid4().hex[:8]
        a = w.remember("tok", "base", "idempotency needle alpha", key=k1)
        b = w.remember("tok", "base", "idempotency needle alpha", key=k1)
        self.assertTrue(a.ok and b.ok)
        self.assertEqual((a.leaf_id, a.lv, a.src_seq), (b.leaf_id, b.lv, b.src_seq), "retry did not return the same leaf")
        c = w.remember("tok", "base", "idempotency needle BRAVO", key=k1)
        self.assertEqual(c.status, wmod.KEY_REUSE, "same key with another body was not refused")
        d = w.remember("other", "base", "idempotency needle BRAVO", key=k1)  # scope includes the principal
        self.assertTrue(d.ok)

    def test_idempotent_retry_and_key_reuse(self):
        n0 = self.count("leaf")
        self.check_key_behaviour(self.writer())
        self.assertEqual(self.count("leaf") - n0, 2)  # one for the retried pair, one for the other principal

    def test_broken_key_check_that_returns_the_first_result_is_red(self):
        class Lax(Writer):
            def _claim(self, principal, key, body, cls):
                return super()._claim(principal, key, b"constant", cls)  # body never differs

        msg = self.run_checked(self.check_key_behaviour, self.writer(Lax))
        self.assertIsNotNone(msg)
        self.assertIn("another body was not refused", msg)

    def test_idempotency_scope_is_tenant_principal_key_across_classes(self):
        w = self.writer()
        k = "xk-" + uuid.uuid4().hex[:8]
        a = w.remember("tok", "base", "scope body one", key=k)
        self.assertTrue(a.ok)
        b = w.remember("tok", "restricted", "scope body one", key=k)  # same key, another class: another body
        self.assertEqual(b.status, wmod.KEY_REUSE)
        self.assertEqual(w.remember("tok", "base", "scope body one", key=k).leaf_id, a.leaf_id)
        self.assertTrue(w.remember("tok2", "restricted", "scope body one", key=k).ok)  # another principal

    def test_broken_per_class_scope_is_red(self):
        class PerClass(Writer):
            def _scope_marker(self, *a, **kw):
                return None  # the claim is found per class only: the same key in another class is a new claim

        w = self.writer(PerClass)
        k = "xk-" + uuid.uuid4().hex[:8]
        self.assertTrue(w.remember("tok", "base", "scope body two", key=k).ok)
        self.assertTrue(w.remember("tok", "restricted", "scope body two", key=k).ok)  # accepted: what DESIGN 7.1 forbids

    def race(self, lock_factory):
        """Retry A finishes between retry B's source check and B's key step; the stored ciphertext must still open."""
        k = "race-" + uuid.uuid4().hex[:8]
        text = "race body " + k
        b_at, b_go = threading.Event(), threading.Event()

        def pause_b(step):
            if step == "src_checked" and not b_at.is_set():
                b_at.set()
                self.assertTrue(b_go.wait(20))

        # one open claim: the first writer claimed and crashed before the source step
        w0 = self.writer(pause=lambda s: (_ for _ in ()).throw(Crash()) if s == "allocated" else None)
        with self.assertRaises(Crash):
            w0.remember("tok", "base", text, key=k)
        out = {}
        with mock.patch.object(wmod, "_src_lock", lock_factory):
            tb = threading.Thread(target=lambda: out.__setitem__("b", self.writer(pause=pause_b).remember("tok", "base", text, key=k)))
            tb.start()
            self.assertTrue(b_at.wait(20))
            ta = threading.Thread(target=lambda: out.__setitem__("a", self.writer().remember("tok", "base", text, key=k)))
            ta.start()
            ta.join(1.5)  # fixed code: A waits for B's lock; broken code: A finishes here
            b_go.set()
            tb.join(30)
            ta.join(30)
        res = out["a"]
        src = self.doc(f"lc-{T}-src", f"{res.leaf_id}:{res.lv}")
        try:
            plain = self.ks.decrypt(res.leaf_id, res.lv, base64.b64decode(src["ct"]), base64.b64decode(src["nonce"]))
        except KeystoreError:
            self.fail("stored ciphertext lost its key")
        self.assertEqual(plain.decode(), text)

    def test_concurrent_retries_of_an_open_claim_keep_the_stored_key(self):
        self.race(wmod._src_lock)

    def test_broken_unlocked_source_step_destroys_the_stored_key(self):
        msg = self.run_checked(self.race, lambda *a: threading.Lock())
        self.assertIsNotNone(msg)
        self.assertIn("lost its key", msg)

    def test_crash_after_each_step_then_retry_with_the_same_key_completes_one_leaf(self):
        for step in ("allocated", "source", "projection", "head"):
            hit = []

            def pause(s, step=step):
                if s == step and not hit:
                    hit.append(1)
                    raise Crash(step)

            w = self.writer(pause=pause)
            text = f"crash resume needle {step}"
            with self.assertRaises(Crash):
                w.remember("tok", "base", text, key=f"crash-{step}")
            res = self.writer().remember("tok", "base", text, key=f"crash-{step}")
            self.assertTrue(res.ok, step)
            lid = f"{res.leaf_id}:1"
            self.assertIsNotNone(self.doc(f"lc-{T}-src", lid), step)
            self.assertIsNotNone(self.doc(f"lc-{T}-base-leaf", lid), step)
            self.assertIsNotNone(self.doc(f"lc-{T}-base-ctl", f"lh:{res.leaf_id}"), step)
            self.assertIsNotNone(self.doc(f"lc-{T}-base-ctl", f"dq:{res.leaf_id}:1"), step)
            self.admin.request("POST", f"/lc-{T}-src/_refresh")
            st, r = self.admin.request("POST", f"/lc-{T}-src/_search", {"query": {"term": {"leaf_id": res.leaf_id}}})
            self.assertEqual(r["hits"]["total"]["value"], 1, f"{step}: duplicate source documents")

    # ---- orphan repair -------------------------------------------------------------------------
    def check_repair(self, w):
        tok = "zebra" + uuid.uuid4().hex[:8]

        def crash_after_source(s):
            if s == "source":
                raise Crash()
        lost = self.writer(pause=crash_after_source)
        with self.assertRaises(Crash):
            lost.remember("tok", "base", f"orphan source needle {tok}")
        # a projection whose source document never existed
        self.admin.request("PUT", f"/lc-{T}-base-leaf/_doc/ghost0001:1?refresh=true", {
            "tenant": T, "cls": "base", "kind": "leaf", "text": "ghost projection needle", "src_seq": 10**9,
            "lin_subjects": [], "lin_leaf_ids": ["ghost0001"], "lin_leaves": [], "lin_nodes": [], "lin_queries": [],
            "derivation": "write", "erasure_rewrite": 0, "created_at": "2026-01-01T00:00:00Z", "schema_v": 1})
        out = w.repair(["base"])
        self.assertGreaterEqual(out["reprojected"], 1, "a source document without a projection was not re-projected")
        self.assertGreaterEqual(out["projections_deleted"], 1, "a projection without a source document was not deleted")
        st, r = self.admin.request("POST", f"/lc-{T}-base-leaf/_search", {"query": {"match": {"text": tok}}})
        self.assertEqual(r["hits"]["total"]["value"], 1)
        self.assertIsNone(self.doc(f"lc-{T}-base-leaf", "ghost0001:1"))

    def test_repair_reprojects_and_deletes_orphans(self):
        self.check_repair(self.writer())

    def test_broken_repair_that_does_nothing_is_red(self):
        class Idle(Writer):
            def repair(self, classes):
                return {"reprojected": 0, "projections_deleted": 0}

        msg = self.run_checked(self.check_repair, self.writer(Idle))
        self.assertIsNotNone(msg)
        self.assertIn("was not re-projected", msg)
        self.writer().repair(["base"])  # clean up for later tests

    def test_repair_leaves_an_erased_leaf_alone(self):
        def crash(s):
            if s == "source":
                raise Crash()
        with self.assertRaises(Crash):
            self.writer(pause=crash).remember("tok", "base", "erased orphan needle quokka")
        self.admin.request("POST", f"/lc-{T}-src/_refresh")
        st, r = self.admin.request("POST", f"/lc-{T}-src/_search", {"query": {"match_all": {}}, "sort": [{"src_seq": "desc"}], "size": 1})
        leaf_id = r["hits"]["hits"][0]["_source"]["leaf_id"]
        self.ks.destroy(leaf_id=leaf_id)
        out = self.writer().repair(["base"])
        self.assertGreaterEqual(out["erased_skipped"], 1)
        st, r = self.admin.request("POST", f"/lc-{T}-base-leaf/_search", {"query": {"match": {"text": "quokka"}}})
        self.assertEqual(r["hits"]["total"]["value"], 0)  # never re-created from a destroyed key

    # ---- quarantine of held items ---------------------------------------------------------------------
    def check_quarantine(self, w):
        self.guard.erase_identifier("nina.k@example.com", bare_name="Nina Kowalski")
        mark = "quillfeather" + uuid.uuid4().hex[:6]
        before = (self.count("src"), self.count("leaf"))
        res = w.remember("tok", "base", f"Nina Kowalski repaired the {mark} fence")
        self.assertEqual(res.status, wmod.HELD)
        self.assertEqual((self.count("src"), self.count("leaf")), before, "a held write reached the leaf or source index")
        held = [h for h in w.quarantine_list("base")]
        self.assertTrue(held, "a held write was dropped instead of quarantined")
        st, raw = self.admin.request("POST", f"/lc-{T}-*/_search", {"query": {"query_string": {"query": mark}}})
        self.assertEqual(raw["hits"]["total"]["value"], 0, "held text is stored in plaintext")
        return mark, held[-1]

    def test_held_items_are_quarantined_as_ciphertext_and_reviewable(self):
        w = self.writer()
        mark, item = self.check_quarantine(w)
        raw = self.doc(f"lc-{T}-base-ctl", f"qh:{item['qid']}")
        self.assertEqual((raw["state"], raw["kind"]), ("held", "quarantine"))
        self.assertTrue(raw["lin_subjects"] and raw["lin_leaf_ids"])  # inside the erasure closure
        # admit: stored as an ordinary leaf, the held ciphertext stripped and its key gone
        adm = w.quarantine_review("base", item["qid"], "admit")
        self.assertTrue(adm.ok)
        self.assertEqual(self.doc(f"lc-{T}-base-ctl", f"qh:{item['qid']}")["state"], "admitted")
        self.assertNotIn("ct", self.doc(f"lc-{T}-base-ctl", f"qh:{item['qid']}"))
        self.assertFalse(self.ks.has_key(item["qid"], 1))
        self.assertEqual(w.quarantine_review("base", item["qid"], "admit").status, wmod.REFUSED)  # no second review
        # reject: dropped, nothing stored as a leaf
        res = w.remember("tok", "base", "Nina Kowalski also wrote about a second topic")
        item2 = w.quarantine_list("base")[-1]
        n = self.count("leaf")
        self.assertEqual(w.quarantine_review("base", item2["qid"], "reject").status, "rejected")
        self.assertEqual(self.count("leaf"), n)

    def test_broken_silent_drop_and_plaintext_quarantine_are_red(self):
        class Drop(Writer):
            def _quarantine(self, *a, **kw):
                return None

            def quarantine_list(self, cls):
                return []

        msg = self.run_checked(self.check_quarantine, self.writer(Drop))
        self.assertIn("dropped instead of quarantined", msg or "")

        class Plain(Writer):
            def _quarantine(self, principal, cls, text, declared, source_ref):
                doc = self._base("quarantine", cls, lin_subjects=["x"], lin_leaf_ids=["plainq"], qid="plainq",
                                 principal=principal, state="held", text_plain=text)
                self._put(class_index(self.tenant, cls, "ctl"), "qh:plainq", doc, cls=cls)

            def quarantine_list(self, cls):
                return [{"qid": "plainq", "created_at": "", "state": "held"}]

        msg = self.run_checked(self.check_quarantine, self.writer(Plain))
        self.assertIn("plaintext", msg or "")

    # ---- subjects and holds ----------------------------------------------------------------------
    def test_erased_identifier_is_refused_and_a_bare_name_is_held_without_a_write(self):
        self.guard.erase_identifier("marta.silva@example.com", bare_name="Marta Silva")
        w = self.writer()
        before = (self.count("src"), self.count("leaf"))
        self.assertEqual(w.remember("tok", "base", "mail marta.silva@example.com now").status, wmod.REFUSED)
        self.assertEqual(w.remember("tok", "base", "Yesterday Marta Silva called the office").status, wmod.HELD)
        self.assertEqual((self.count("src"), self.count("leaf")), before)
        # through the gateway a writer sees the same typed answers as for any refusal or hold
        grants = {(T, "tok-user"): Principal(T, "tok-user", frozenset([Role.WRITER]), frozenset(["base"]), frozenset(["base"]))}
        fences = {}
        sessions = Sessions({T: Adapter()}, EpochTable(lambda t: fences.setdefault(t, SendFence())),
                            lambda t, who: "tok-user", lambda t, p: grants.get((t, p)))
        gw = Gateway(self.gate, sessions, {T: LAT}, writers=lambda t: w, admits=lambda t: True)   # no journal here: explicit admission
        tk = sessions.mint(T, b"user")
        self.assertEqual(gw.handle(tk, {"op": "write", "cls": "base", "doc": {"text": "Marta Silva wrote back"}}), HELD_ANSWER)
        self.assertEqual(gw.handle(tk, {"op": "write", "cls": "base", "doc": {"text": "x marta.silva@example.com"}}), REFUSED)
        self.assertEqual(gw.handle(tk, {"op": "write", "cls": "base", "doc": {"text": "", "title": "forged"}}), REFUSED)
        r = gw.handle(tk, {"op": "write", "cls": "base", "doc": {"text": "gateway write needle", "title": "FORGED TITLE"}})
        self.assertTrue(r["ok"])
        st, s = self.admin.request("POST", f"/lc-{T}-base-leaf/_search", {"query": {"match": {"text": "gateway write needle"}}})
        self.assertNotIn("forged", s["hits"]["hits"][0]["_source"]["title"].lower())  # a client title is never stored

    # ---- the code-built title -----------------------------------------------------------------------
    TEXT = ("Payment of $1,250.00 on 2024-03-15 for account AC-99381 by March budget review garden garden "
            "garden budget budget plan plan")

    def check_title_has_no_digit_token(self, w):
        res = w.remember("tok", "base", self.TEXT + " " * 0 + f" unique{os.getpid()}x")
        stored = self.doc(f"lc-{T}-base-leaf", f"{res.leaf_id}:{res.lv}")["title"]
        bad = [t for t in stored.split() if title.has_digit(t)]
        self.assertEqual(bad, [], "a stored title keeps a token containing a digit")
        return stored

    def test_stored_title_has_no_digit_token_and_may_keep_a_month_name(self):
        stored = self.check_title_has_no_digit_token(self.writer())
        self.assertTrue(stored)

    def test_broken_title_builder_that_keeps_digit_tokens_is_red(self):
        def keeps_digits(text, df=None, n_docs=0, n=4, mode=None):
            return " ".join(sorted(set(re.findall(r"\S+", text)), key=lambda t: (-len(t), t))[:n])
        with mock.patch.object(title, "build_title", keeps_digits):
            msg = self.run_checked(self.check_title_has_no_digit_token, self.writer())
        self.assertIsNotNone(msg)
        self.assertIn("keeps a token containing a digit", msg)

    def test_runtime_sentinel_every_stored_title_comes_from_build_title(self):
        with mock.patch.object(title, "build_title", lambda *a, **k: "SENTINELTITLE"):
            res = self.writer().remember("tok", "base", "sentinel check needle one")
        self.assertEqual(self.doc(f"lc-{T}-base-leaf", f"{res.leaf_id}:1")["title"], "SENTINELTITLE")

    def test_no_content_title_mode_gives_an_empty_title_independent_of_the_body(self):
        title.set_mode(title.MODE_NO_CONTENT)
        try:
            a = self.writer().remember("tok", "base", "no content mode alpha text")
            b = self.writer().remember("tok", "base", "no content mode totally other body")
        finally:
            title.set_mode(title.MODE_TFIDF)
        for res in (a, b):
            self.assertEqual(self.doc(f"lc-{T}-base-leaf", f"{res.leaf_id}:1")["title"], "")

    # ---- the source log, the keystore and the projection ------------------------------------------------
    def test_source_holds_ciphertext_only_and_the_key_destroys_it(self):
        res = self.writer().remember("tok", "base", "ciphertext check needle okapi", subjects_declared=["lena@example.org"])
        src = self.doc(f"lc-{T}-src", f"{res.leaf_id}:1")
        self.assertNotIn("okapi", json.dumps(src))
        self.assertEqual(src["lin_subjects"], [self.ks.subject_token("lena@example.org")])
        text = self.ks.decrypt(res.leaf_id, 1, base64.b64decode(src["ct"]), base64.b64decode(src["nonce"])).decode()
        self.assertIn("okapi", text)
        leaf = self.doc(f"lc-{T}-base-leaf", f"{res.leaf_id}:1")
        self.assertEqual(leaf["src_seq"], src["src_seq"])
        self.assertEqual(len(leaf["embedding"]), 384)
        self.ks.destroy(subject_token=self.ks.subject_token("lena@example.org"))
        with self.assertRaises(KeystoreError):
            self.ks.decrypt(res.leaf_id, 1, base64.b64decode(src["ct"]), base64.b64decode(src["nonce"]))

    def test_written_leaf_is_searchable_at_once(self):
        res = self.writer().remember("tok", "base", "immediate visibility needle capybara")
        st, r = self.admin.request("POST", f"/lc-{T}-base-leaf/_search", {"query": {"match": {"text": "capybara"}}})
        self.assertEqual([h["_id"] for h in r["hits"]["hits"]], [f"{res.leaf_id}:1"])

    # ---- compare-and-set -----------------------------------------------------------------------------------
    def check_counter_no_duplicates(self, use_cas):
        """Writer A reads the counter, B takes a number, A then writes: with the compare-and-set A retries."""
        self.writer().next_seq()  # the counter exists: the first allocation is a create, which is its own compare-and-set
        gate = threading.Event()
        proceed = threading.Event()

        def pause_a(step):
            if step == "seq_read" and not gate.is_set():
                gate.set()
                proceed.wait(10)

        a, b = self.writer(pause=pause_a, use_cas=use_cas), self.writer()
        got = {}
        t = threading.Thread(target=lambda: got.setdefault("a", a.next_seq()))
        t.start()
        gate.wait(10)
        got["b"] = b.next_seq()
        proceed.set()
        t.join(20)
        self.assertNotEqual(got["a"], got["b"], "two writers were given the same source sequence number")
        self.assertEqual(sorted(got.values()), [got["b"], got["b"] + 1])

    def test_source_counter_is_compare_and_set(self):
        self.check_counter_no_duplicates(True)

    def test_broken_counter_without_compare_and_set_is_red(self):
        msg = self.run_checked(self.check_counter_no_duplicates, False)
        self.assertIsNotNone(msg)
        self.assertIn("same source sequence number", msg)

    def check_head_not_lost(self, use_cas):
        """A reads the head at lv 1 and is about to write lv 2; B advances it to lv 3 first."""
        w0 = self.writer()
        lid = f"headlost{use_cas:d}{os.getpid()}"
        w0.advance_head("base", lid, 1)
        gate, proceed = threading.Event(), threading.Event()

        def pause_a(step):
            if step == "head_read" and not gate.is_set():
                gate.set()
                proceed.wait(10)

        a = self.writer(pause=pause_a, use_cas=use_cas)
        t = threading.Thread(target=lambda: a.advance_head("base", lid, 2))
        t.start()
        gate.wait(10)
        self.writer().advance_head("base", lid, 3)
        proceed.set()
        t.join(20)
        self.assertEqual(self.doc(f"lc-{T}-base-ctl", f"lh:{lid}")["current_lv"], 3, "the head moved backwards (lost update)")

    def test_head_flip_is_compare_and_set(self):
        self.check_head_not_lost(True)

    def test_broken_head_flip_without_compare_and_set_is_red(self):
        msg = self.run_checked(self.check_head_not_lost, False)
        self.assertIsNotNone(msg)
        self.assertIn("lost update", msg)

    def test_concurrent_writers_get_distinct_contiguous_sequence_numbers(self):
        n_threads, per = 4, 5
        seqs, errs = [], []

        def run(k):
            try:
                w = self.writer()
                for i in range(per):
                    seqs.append(w.next_seq())
            except Exception as e:  # pragma: no cover
                errs.append(e)

        ts = [threading.Thread(target=run, args=(k,)) for k in range(n_threads)]
        [t.start() for t in ts]
        [t.join(60) for t in ts]
        self.assertEqual(errs, [])
        self.assertEqual(len(seqs), len(set(seqs)))
        self.assertEqual(max(seqs) - min(seqs) + 1, len(seqs))

    def make_gateway(self, writers):
        grants = {(T, "tok-user"): Principal(T, "tok-user", frozenset([Role.WRITER]), frozenset(["base"]), frozenset(["base"]))}
        fences = {}
        sessions = Sessions({T: Adapter()}, EpochTable(lambda t: fences.setdefault(t, SendFence())),
                            lambda t, who: "tok-user", lambda t, p: grants.get((t, p)))
        return Gateway(self.gate, sessions, {T: LAT}, writers=writers, admits=lambda t: True), sessions.mint(T, b"user")   # explicit admission

    def test_a_write_handled_on_another_thread_works(self):
        """The server handles each connection on its own thread; the writer and keystore were built on this one."""
        w = self.writer()
        gw, tk = self.make_gateway(lambda t: w)
        out = []
        th = threading.Thread(target=lambda: out.append(gw.handle(tk, {"op": "write", "cls": "base", "doc": {"text": "thread write needle ocelot"}})))
        th.start()
        th.join(30)
        self.assertTrue(out and out[0].get("ok"), out)

    def test_every_internal_failure_is_the_one_typed_answer(self):
        from living_context.gateway.handler import UNAVAILABLE

        class Boom(Writer):
            def remember(self, *a, **k):
                raise wmod.WriterError("step failed (503)")

        gw, tk = self.make_gateway(lambda t: self.writer(Boom))
        self.assertEqual(gw.handle(tk, {"op": "write", "cls": "base", "doc": {"text": "x y z"}}), UNAVAILABLE)

    def test_every_document_is_validated_before_it_is_sent(self):
        w = self.writer()
        bad = w._base("leaf", "base")
        del bad["lin_subjects"]
        from living_context.gateway import validator
        with self.assertRaises(validator.Refused):
            w._put(f"lc-{T}-base-leaf", "x:1", bad, cls="base")
        other = w._base("leaf", "base", tenant="tenb")
        with self.assertRaises(validator.Refused):
            w._put(f"lc-{T}-base-leaf", "x:2", other, cls="base")


if __name__ == "__main__":
    unittest.main()
