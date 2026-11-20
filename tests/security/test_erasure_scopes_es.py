"""BUILD-PLAN Step 14, the E-series items that no earlier module covered (SEC-E1, E2, E3, E5, E9), on a real Elasticsearch.
The module FAILS without LC_TEST_ES_URL, LC_TEST_CA, LC_TEST_PW. It reuses the planted canaries of the verifier module.

Each check has a broken variant that must turn it red for its stated reason:
  * E5   a legal hold queues the erasure (broken: the hold is ignored and the erasure runs);
  * item an item erasure removes one leaf and what derives from it (broken: keys destroyed, plaintext left);
  * E3   the erased-identifier HMACs in the journal rebuild the guard after a restart (broken: the journal records none);
  * E3   the key of a held (quarantined) blob is named by the subject's tokens (broken: sealed under an unlinked key);
  * E1   every retained generation (broken: only the newest node version is erased);
  * E2   a node that paraphrases the subject is found by lineage (broken: the closure is a string search);
  * E9   what an erased search shaped is removed through the second net (broken: closure over the subject's own lineage only);
  * E0/E1 the verifier's percolate, query-only and query-shaped probes (broken: a closure without the second net leaves a shaped edge;
    a stored query that only its percolator words reach is found by the percolate probe alone), and a closure without the authz
    index is red at erase time, not only at restore."""
import collections
import dataclasses
import datetime
import unittest

from living_context.erasure import scan
from living_context.erasure.hold import HoldRegistry, release_hold, request_erasure
from living_context.erasure.executor import Held
from living_context.erasure.receipt import full_receipt, recount
from living_context.ingest.subjects import Guard, HELD, OK, REFUSED, TokenSource
from living_context.ingest.writer import Writer
from living_context.verifier.check import Canaries
from tests.security import test_verifier_restore_es as V


def now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


class Scopes(V.VerifierAndRestore):
    T = "vsco"

    def guard_fn(self):
        return lambda: self.guard.erase_identifier(self.email)

    def doc(self, index, doc_id, **fields):
        base = {"tenant": self.T, "cls": "base", "lin_leaves": [], "lin_leaf_ids": [], "lin_nodes": [], "lin_subjects": [],
                "lin_queries": [], "derivation": "test", "erasure_rewrite": 0, "created_at": now(), "schema_v": 1}
        base.update(fields)
        st, r = self.admin.request("PUT", f"/lc-{self.T}-base-{index}/_doc/{doc_id}?refresh=true", base)
        self.assertIn(st, (200, 201), r)

    def exists(self, index, doc_id):
        st, _ = self.admin.request("GET", f"/lc-{self.T}-base-{index}/_doc/{doc_id}")
        return st == 200

    def named(self, leaf_ids, kinds=None):
        return [h for h in scan.find(self.admin, self.T, leaf_ids=leaf_ids) if kinds is None or h.kind in kinds]

    # ---- E5: legal hold ---------------------------------------------------------------------------------------------------
    def check_hold(self, honour=True):
        self.plant()
        v = self.verifier()
        before = v.positive_control(self.canaries)
        t = [1000.0]
        path = self.dir / "holds" / "holds.json"
        reg = HoldRegistry(path, clock=lambda: t[0], honour_holds=honour)
        reg.place(subject=self.subject)
        ex = self.executor(holds=reg)
        out = request_erasure(ex, self.subject, apply_guard=self.guard_fn())
        self.assertTrue(out.queued, "an erasure ran under a legal hold")
        self.assertEqual((self.state.seq, self.journal.last_seq), (0, 0))
        self.assertGreater(v.probe(self.canaries).total, 0, "a held scope's content was touched")
        reg2 = HoldRegistry(path, clock=lambda: t[0])   # a gateway restart: the queue is durable
        ex = self.executor(holds=reg2)
        self.assertEqual(len(reg2.queued()), 1)
        t[0] += 7200
        self.assertEqual(release_hold(ex, subject="another-subject"), [], "releasing another scope ran the erasure")
        outs = release_hold(ex, subject=self.subject, apply_guard=self.guard_fn())
        self.assertEqual(len(outs), 1)
        rc = outs[0].receipt
        after = v.residue(self.canaries, before)
        self.assertEqual(after.total, 0, f"residue after the released erasure: {after.found}")
        rec = full_receipt(rc, residue=recount(self.admin, self.T, self.subject, self.canaries.leaf_ids), verifier_steps=after.steps)
        self.assertTrue(rec["done"])
        self.assertGreaterEqual(rec["hold_delay_s"], 7200, "the receipt does not state the delay")
        self.assertEqual(reg2.queued(), [])

    def test_a_legal_hold_queues_the_erasure_and_the_release_completes_it_with_the_delay_in_the_receipt(self):
        self.check_hold()

    def test_broken_hold_ignored_runs_the_erasure_at_once(self):
        with self.assertRaises(AssertionError) as cm:
            self.check_hold(honour=False)
        self.assertIn("ran under a legal hold", str(cm.exception))

    def check_hold_closure(self, honour=True):
        self.plant()
        a, b = list(self.canaries.leaf_ids)
        v = self.verifier()
        reg = HoldRegistry(self.dir / "holds2" / "h.json", honour_holds=honour)
        ex = self.executor(holds=reg)
        reg.place(leaf_ids=[a])   # a hold on one leaf, not on the person
        out = request_erasure(ex, self.subject, apply_guard=self.guard_fn())
        self.assertTrue(out.queued, "an erasure ran although its closure reaches a leaf under a legal hold")
        self.assertEqual((self.state.seq, self.journal.last_seq), (0, 0))
        reg2 = HoldRegistry(self.dir / "holds3" / "h.json", honour_holds=honour)
        reg2.place(subject=self.subject)   # a hold on the person: an item of theirs is covered too
        ex = self.executor(holds=reg2)
        out = request_erasure(ex, "", leaf_ids=[b])
        self.assertTrue(out.queued, "an item erasure ran although its leaf belongs to a person under a legal hold")
        self.assertGreater(v.probe(self.canaries).total, 0, "held content was touched")
        self.assertEqual(release_hold(ex, leaf_ids=["unrelated"]), [], "releasing another scope ran the erasure")

    def test_a_hold_on_a_leaf_or_on_a_person_covers_what_the_closure_reaches(self):
        self.check_hold_closure()

    def test_broken_hold_ignored_for_the_closure_is_red(self):
        with self.assertRaises(AssertionError) as cm:
            self.check_hold_closure(honour=False)
        self.assertIn("legal hold", str(cm.exception))


    # ---- the hold check cannot be skipped from a public entry point (DESIGN 14.1; ASSUMPTION: it covers all three scopes) ----
    def check_direct_entries(self, **kw):
        self.plant()
        a, b = list(self.canaries.leaf_ids)
        v = self.verifier()
        reg = HoldRegistry(self.dir / "holds4" / "h.json")
        reg.place(subject=self.subject)
        reg.place(leaf_ids=[a])
        ex = self.executor(holds=reg, **kw)
        for name, call in (("erase_subject", lambda: ex.erase_subject(self.subject, apply_guard=self.guard_fn())),
                           ("erase_item", lambda: ex.erase_item([a])),
                           ("erase_item of a held person's leaf", lambda: ex.erase_item([b]))):
            with self.assertRaises(Held, msg=f"{name} ran under a legal hold"):
                call()
            self.assertEqual((self.state.seq, self.journal.last_seq), (0, 0), f"{name} published under a hold")
            self.assertGreater(v.probe(self.canaries).total, 0, f"{name} touched held content")

    def test_every_public_erasure_entry_point_refuses_under_a_hold(self):
        self.check_direct_entries()

    def test_broken_executor_that_skips_the_hold_check_is_red(self):
        with self.assertRaises(AssertionError) as cm:
            self.check_direct_entries(broken_skip_holds=True)
        self.assertIn("ran under a legal hold", str(cm.exception))

    # ---- a queued request leaves the queue only after its receipt ---------------------------------------------------------
    def check_crash_safe_release(self, broken=False):
        self.plant()
        v = self.verifier()
        before = v.positive_control(self.canaries)
        path = self.dir / "holds5" / "h.json"
        reg = HoldRegistry(path)
        reg.place(subject=self.subject)
        self.assertTrue(request_erasure(self.executor(holds=reg), self.subject, apply_guard=self.guard_fn()).queued)
        boom = {"armed": True}

        def pause(stage):   # a failure after the tombstone is published and before the physical erasure and the receipt
            if stage == "boundary" and boom["armed"]:
                boom["armed"] = False
                raise RuntimeError("injected failure between the queue and the receipt")

        with self.assertRaises(RuntimeError):
            release_hold(self.executor(holds=reg, pause=pause), subject=self.subject, apply_guard=self.guard_fn(),
                         _broken_pop_first=broken)
        survivor = HoldRegistry(path).queued()   # the gateway restarts
        self.assertEqual(len(survivor), 1, "the queued erasure was lost by a failure after it left the queue")
        outs = release_hold(self.executor(holds=HoldRegistry(path)), subject=self.subject, apply_guard=self.guard_fn())
        self.assertEqual(len(outs), 1)
        self.assertEqual(v.residue(self.canaries, before).total, 0)
        self.assertEqual(HoldRegistry(path).queued(), [])

    def test_a_failure_between_the_queue_and_the_receipt_loses_no_request(self):
        self.check_crash_safe_release()

    def test_broken_release_that_pops_the_queue_before_running_is_red(self):
        with self.assertRaises(AssertionError) as cm:
            self.check_crash_safe_release(broken=True)
        self.assertIn("was lost", str(cm.exception))

    # ---- an item erasure --------------------------------------------------------------------------------------------------
    def check_item(self, **kw):
        self.plant()
        a, b = list(self.canaries.leaf_ids)
        rc = self.executor(**kw).erase_item([a])
        left = self.named([a], set(["src", "leaf", "member", "edge", "log", "cache"]))
        self.assertEqual(left, [], "an erased item still has documents")
        self.assertEqual(rc.recount, 0)
        self.assertFalse(self.ks.has_key(a, 1), "the erased item's key survived")
        self.assertTrue(self.ks.has_key(b, 1), "another item of the same subject lost its key")
        self.assertGreaterEqual(len(self.named([b], {"src", "leaf"})), 2, "another item of the same subject was erased")
        self.assertEqual(self.journal.records()[-1]["scope"]["subject"], "")
        self.assertEqual(self.verifier().probe(Canaries(short=self.canaries.short, leaf_ids=[a])).total, 0,
                         "the erased item is still found by the verifier")

    def test_an_item_erasure_removes_one_leaf_and_what_derives_from_it_and_no_other_leaf(self):
        self.check_item()

    def test_broken_item_erasure_that_destroys_only_the_key_is_red(self):
        with self.assertRaises(AssertionError) as cm:
            self.check_item(destroy_only_keys=True)
        self.assertIn("still has documents", str(cm.exception))

    # ---- E3: the guard is rebuilt from the journal -----------------------------------------------------------------------
    def check_guard(self, **kw):
        self.plant()
        letters = "".join("abcdefghij"[int(c)] if c.isdigit() else c for c in self.uniq)
        name = f"Quen{letters} Brenwick"
        bare = TokenSource(self.ks).bare(name)
        self.executor(**kw).erase_subject(
            self.subject, identifiers=[self.subject], bare_tokens=[bare],
            apply_guard=lambda: self.guard.erase_identifier(self.email, bare_name=name))
        fresh = Guard(TokenSource(self.ks))   # a restart or a restore: the guard lives in memory only
        self.assertEqual((fresh.check(f"mail {self.email}"), fresh.check(f"{name} rang")), (OK, OK), "premise: an empty guard accepts")
        fresh.load_journal(self.journal.records())
        self.assertEqual(fresh.check(f"mail {self.email}"), REFUSED, "an erased identifier was accepted again after the restart")
        self.assertEqual(fresh.check(f"{name} rang"), HELD, "an erased bare name was accepted again after the restart")

    def test_the_journal_rebuilds_the_guard_so_an_erased_identifier_is_refused_after_a_restart(self):
        self.check_guard()

    def test_broken_journal_without_the_erased_identifier_hmacs_is_red(self):
        with self.assertRaises(AssertionError) as cm:
            self.check_guard(broken_no_identifier_hmacs=True)
        self.assertIn("accepted again after the restart", str(cm.exception))

    # ---- E3: a held blob's key is named by the subject's tokens ----------------------------------------------------------
    def check_quarantine_key(self, **wkw):
        self.plant()
        letters = "".join("abcdefghij"[int(c)] if c.isdigit() else c for c in self.uniq)
        name = f"Hold{letters} Ormsby"
        self.guard.erase_identifier(f"ormsby{self.uniq}@example.com", bare_name=name)
        w = Writer(self.ingest(), self.ks, self.T, self.guard, gate=self.wgate, **wkw)
        self.assertEqual(w.remember("tok", "base", f"{name} wrote a short note").status, "held")
        qid = w.quarantine_list("base")[-1]["qid"]
        self.assertTrue(self.ks.has_key(qid, 1), "premise: the held blob has a key")
        self.executor().erase_subject(TokenSource(self.ks).bare(name))
        self.assertFalse(self.ks.has_key(qid, 1), "a held item's key survived the erasure of its subject")
        self.assertFalse(self.exists("ctl", f"qh:{qid}"), "the held item's document survived")

    def test_the_key_of_a_held_blob_is_destroyed_with_its_subject(self):
        self.check_quarantine_key()

    def test_broken_blob_sealed_under_a_key_no_subject_token_names_is_red(self):
        with self.assertRaises(AssertionError) as cm:
            self.check_quarantine_key(broken_quarantine_key_unlinked=True)
        self.assertIn("key survived the erasure", str(cm.exception))

    # ---- E1: every retained generation ------------------------------------------------------------------------------------
    def check_generations(self, **kw):
        self.plant()
        ids = list(self.canaries.leaf_ids)
        v = self.verifier()
        before = v.positive_control(self.canaries)
        nodes = [h for h in self.named(ids) if h.kind == "node"]
        self.assertTrue(nodes, "premise: a node names the planted leaves")
        for h in nodes:   # a retained generation: the next version of the node (the rebuild's copy), the older one stays for pinned readers
            stem, _, ver = h.id.rpartition(":")
            _, src = self.admin.request("GET", f"/{h.index}/_doc/{h.id}")
            st, r = self.admin.request("PUT", f"/{h.index}/_doc/{stem}:{int(ver) + 1}?refresh=true", src["_source"])
            self.assertIn(st, (200, 201), r)
        stems = collections.Counter(h.id.rpartition(":")[0] for h in self.named(ids) if h.kind == "node")
        self.assertTrue(any(n >= 2 for n in stems.values()), "premise: two versions of a node name the erased leaves")
        self.executor(**kw).erase_subject(self.subject, apply_guard=self.guard_fn())
        after = v.residue(self.canaries, before)
        self.assertEqual(after.total, 0, f"residue after erasure: {after.found}")
        self.assertEqual([h for h in self.named(ids) if h.kind == "node"], [], "a retained generation still names the erased leaves")

    def test_every_retained_generation_of_a_node_is_rewritten(self):
        self.check_generations()

    def test_broken_erasure_of_the_current_generation_only_is_red(self):
        with self.assertRaises(AssertionError) as cm:
            self.check_generations(broken_current_generation_only=True)
        self.assertRegex(str(cm.exception), "residue after erasure|retained generation still names")

    # ---- E2: a paraphrase is found by lineage ------------------------------------------------------------------------------
    def check_paraphrase(self, **kw):
        self.plant()
        ids = list(self.canaries.leaf_ids)
        st, r = self.admin.request("POST", f"/lc-{self.T}-base-node/_update_by_query?refresh=true&conflicts=proceed", {
            "query": {"terms": {"lin_leaf_ids": ids}},
            "script": {"source": "ctx._source.summary = 'an orchard visit by a colleague'; ctx._source.title = 'orchard visit'",
                       "lang": "painless"}})
        self.assertEqual(st, 200, r)
        self.assertGreaterEqual(r["updated"], 1, "premise: a node carries the planted leaves")
        self.assertEqual([h for h in scan.find_text(self.admin, self.T, self.canaries.short) if h.kind == "node"], [],
                         "premise: no node holds the canary string")
        if kw.get("broken_string_closure") is True:
            kw["broken_string_closure"] = self.canaries.short
        self.executor(**kw).erase_subject(self.subject, apply_guard=self.guard_fn())
        self.assertEqual([h for h in self.named(ids) if h.kind == "node"], [], "a node that paraphrases the subject survived")

    def test_a_node_that_paraphrases_the_subject_is_found_by_lineage(self):
        self.check_paraphrase()

    def test_broken_closure_by_string_search_misses_the_paraphrase(self):
        with self.assertRaises(AssertionError) as cm:
            self.check_paraphrase(broken_string_closure=True)
        self.assertIn("paraphrases the subject survived", str(cm.exception))

    # ---- E9: query-shaped artefacts ---------------------------------------------------------------------------------------
    def check_queries(self, **kw):
        self.plant()
        sid, other_sid = f"srch{self.uniq}", f"srcho{self.uniq}"
        other = f"other{self.uniq}"
        # the subject's own query (a row and a stored query), and an edge and a member that carry only that search's id
        self.doc("log", f"row{self.uniq}", kind="log", lin_subjects=[self.subject], actor=self.subject, search_id=sid, item_subjects=[],
                 pointer={}, derivation="protocol")
        self.doc("sq", sid, kind="sq", lin_subjects=[self.subject], search_id=sid, text="my own query", derivation="protocol")
        self.doc("edge", f"co1{self.uniq}", kind="edge", type="co_used", evidence=[sid], edge_id=f"co1{self.uniq}", derivation="edge")
        self.doc("member", f"mb1{self.uniq}", kind="member", shaped_by=sid, parent_id="p", child_id="c", derivation="placement")
        # another actor's row that only offered an item of the subject, and what its own search shaped: both stay
        self.doc("log", f"rowo{self.uniq}", kind="log", lin_subjects=[other, self.subject], actor=other, search_id=other_sid,
                 item_subjects=[self.subject], pointer={"leaf_id": "x", "lv": 1}, derivation="protocol")
        self.doc("edge", f"co2{self.uniq}", kind="edge", type="co_used", evidence=[other_sid], edge_id=f"co2{self.uniq}", derivation="edge")
        self.executor(**kw).erase_subject(self.subject, apply_guard=self.guard_fn())
        self.assertFalse(self.exists("log", f"row{self.uniq}") or self.exists("sq", sid), "the subject's own query survived")
        self.assertFalse(self.exists("edge", f"co1{self.uniq}") or self.exists("member", f"mb1{self.uniq}"),
                         "an artefact shaped by an erased search survived")
        self.assertTrue(self.exists("log", f"rowo{self.uniq}"), "another actor's history was deleted")
        self.assertTrue(self.exists("edge", f"co2{self.uniq}"), "an artefact shaped by another actor's search was deleted")

    def test_what_an_erased_search_shaped_is_removed_and_another_actors_search_is_left(self):
        self.check_queries()

    def test_broken_closure_over_the_subjects_own_lineage_only_is_red(self):
        with self.assertRaises(AssertionError) as cm:
            self.check_queries(broken_no_query_net=True)
        self.assertIn("shaped by an erased search survived", str(cm.exception))

    # ---- E0/E1: the verifier's percolate, query-only and query-shaped canaries (BUILD-PLAN Step 14) --------------------------
    def plant_query_canaries(self, *, reachable=True):
        """A person who appears only in another actor's query: a stored query (its percolator words match the canary document), a
        row, and an edge and a member shaped by that search. `reachable=False` plants a stored query that carries the canary only
        in its percolator and names no subject (a defect of subject detection): no lineage reaches it."""
        self.plant()
        qtok = self.ks.subject_token(f"qonly{self.uniq}@example.com")
        sid, word = f"qsrch{self.uniq}", f"qcanary{self.uniq}"
        actor = f"asker{self.uniq}"
        q = {"match": {"text": {"query": word}}}
        if reachable:
            self.doc("sq", sid, kind="sq", lin_subjects=[actor, qtok], search_id=sid, text=f"what does {word} do", q=q, derivation="protocol")
            self.doc("log", f"qrow{self.uniq}", kind="log", lin_subjects=[actor, qtok], actor=actor, search_id=sid, item_subjects=[],
                     pointer={}, derivation="protocol")
        else:
            self.doc("sq", sid, kind="sq", lin_subjects=[actor], search_id=sid, text="", q=q, derivation="protocol")
        self.doc("edge", f"qco{self.uniq}", kind="edge", type="co_used", evidence=[sid], edge_id=f"qco{self.uniq}", derivation="edge")
        self.doc("member", f"qmb{self.uniq}", kind="member", shaped_by=sid, parent_id="p", child_id="c", derivation="placement")
        self.canaries = dataclasses.replace(self.canaries, percolate_text=f"{word} asks about the orchard", query_subject_token=qtok,
                                            search_ids=[sid])
        return qtok

    def check_query_canaries(self, **kw):
        qtok = self.plant_query_canaries()
        v = self.verifier()
        before = v.positive_control(self.canaries)   # raises if any of the three new probes is blind
        for step in ("percolate", "query_only", "query_shaped"):
            self.assertGreater(before.found[step], 0, f"the {step} probe is blind before the erasure")
        self.executor(**kw).erase_subject(qtok, apply_guard=self.guard_fn())
        after = v.residue(self.canaries, before)
        for step in ("percolate", "query_only", "query_shaped"):
            self.assertEqual(after.found[step], 0, f"residue after erasure: the {step} probe still finds {after.found[step]}")
        return after

    def test_the_verifier_finds_the_percolate_query_only_and_query_shaped_canaries_and_none_remains_after_erasure(self):
        after = self.check_query_canaries()
        self.assertEqual([n for n, p, _ in after.steps if n in ("percolate", "query_only", "query_shaped")],
                         ["percolate", "query_only", "query_shaped"])
        self.assertTrue(all(p == 1 for n, p, _ in after.steps if n in ("percolate", "query_only", "query_shaped")))

    def test_broken_closure_without_the_second_net_leaves_a_shaped_artefact_the_verifier_finds(self):
        with self.assertRaises(AssertionError) as cm:
            self.check_query_canaries(broken_no_query_net=True)
        self.assertIn("query_shaped probe still finds", str(cm.exception))

    def test_a_stored_query_reached_only_by_its_percolator_words_is_found_by_the_percolate_probe_alone(self):
        qtok = self.plant_query_canaries(reachable=False)
        # only the percolate canary is asked of this verifier: no lineage names the query-only subject or the search
        self.canaries = dataclasses.replace(self.canaries, query_subject_token="", search_ids=())
        v = self.verifier()
        before = v.positive_control(self.canaries)
        self.assertGreater(before.found["percolate"], 0)
        self.assertEqual(v._text(f"qcanary{self.uniq}"), 0, "premise: the text probe cannot see a percolator-only stored query")
        self.executor().erase_subject(qtok, apply_guard=self.guard_fn())
        after = v.residue(self.canaries, before)
        self.assertGreater(after.found["percolate"], 0, "the percolate probe missed a stored query that still matches the canary")

    def test_broken_closure_without_the_authz_index_is_red_at_erase_time(self):
        with self.assertRaises(AssertionError) as cm:
            self.check_erasure(broken_skip_authz=True)
        self.assertIn("'authz': 1", str(cm.exception))

    # ---- E1: files outside the indices ------------------------------------------------------------------------------------
    def test_a_model_output_file_under_the_data_root_is_counted_as_residue(self):
        self.plant()
        root = self.dir / "dataroot"
        root.mkdir()
        marker = f"DATAMARK{self.uniq}"
        (root / "worker.out").write_text(f"{marker} control line\n")
        self.canaries.data_marker = marker
        v = self.verifier(data_roots=[root])
        before = v.positive_control(self.canaries)
        print(f"verifier kNN best score {v.knn_best:.4f} (rescored)")
        self.assertIn("data_root", [n for n, _, _ in before.steps])
        self.executor().erase_subject(self.subject, apply_guard=self.guard_fn())
        self.assertEqual(v.residue(self.canaries, before).total, 0)
        (root / "model-out.log").write_text(f"output {self.canaries.short} kept outside the indices\n")
        after = v.residue(self.canaries, before)
        self.assertEqual(after.found["data_root"], 1, "a model output kept in a file under the data root was not counted")


for _n in [n for n in dir(V.VerifierAndRestore) if n.startswith("test_") and n not in Scopes.__dict__]:
    setattr(Scopes, _n, None)   # only this module's tests run; the verifier module's scenario helpers are reused


if __name__ == "__main__":
    unittest.main()
