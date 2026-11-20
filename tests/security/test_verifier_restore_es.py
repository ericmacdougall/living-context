"""BUILD-PLAN Step 14: the independent verifier with canaries, and the restore replay of the erasure closure (DESIGN 14.6, 14.7;
SEC-E0, E3, V5). Real Elasticsearch, real writer, stub model; the module FAILS without LC_TEST_ES_URL, LC_TEST_CA, LC_TEST_PW.

Broken variants, each red for its stated reason:
  * keys destroyed but plaintext left (the verifier's residue is not zero);
  * a raw-file grep as the verifier (it must miss the long, compressed canary: the positive control fails);
  * a log line holding an erased canary (the verifier counts it);
  * a restore that serves without re-applying the journal, and a replay that does not cover the authz index (both leave residue,
    so the tenant is not enabled, and the check that it is enabled fails)."""
import datetime
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from living_context.embed import stub as embed_stub
from living_context.erasure.executor import Executor, State
from living_context.erasure.receipt import full_receipt, recount, requester_copy
from living_context.erasure.restore import restore_replay
from living_context.gateway.fence import SendFence
from living_context.gateway.gate import WriteGate
from living_context.gateway.journal import TombstoneJournal
from living_context.keystore import Keystore
from living_context.verifier.check import Canaries, Verifier, VerifierError
from tests.security.vworld import VersionsBase

CONTAINER = os.environ.get("LC_ES_CONTAINER", "")


class VerifierAndRestore(VersionsBase):
    T = "vver"
    _n = 0

    def setUp(self):
        VerifierAndRestore._n += 1
        n = VerifierAndRestore._n
        self.uniq = f"{n}q{id(self) % 9973}"
        self.wgate, self.fence, self.state = WriteGate(), SendFence(), State()
        self.dir = Path(tempfile.mkdtemp(dir=self.root))
        self.journal = TombstoneJournal(self.dir / "j" / "t.log", self.dir / "r" / "t.log")
        self.email = f"vver{self.uniq}@example.com"
        self.logf = self.dir / "gateway.log"
        self.marker = f"CONTROLMARKER{self.uniq}"
        self.logf.write_text(f"{self.marker} slow log line, no body\n")

    def executor(self, ks=None, **kw):
        return Executor(lambda: self.gate.client(self.T, "erase"), self.structure(), self.T, ks or self.ks, self.journal, self.wgate,
                        self.fence, self.state, purge_deadline_s=2, purge_interval_s=1, es_log=lambda: self.gate.client(self.T, "log"), **kw)

    def verifier(self, **kw):
        # the verifier's own client: a separate EsClient object (in production, its own read-only key file)
        from living_context.gateway.es import EsClient
        from tests.security.vworld import CA, PW, URL
        return Verifier(EsClient(URL, CA, PW, scheme="basic", user="elastic"), self.T, log_paths=[self.logf], **kw)

    def plant(self):
        """Canaries through the real writer: a short one, a long (compressed) one, a vector, the subject, an authz record."""
        w = self.world()
        self.subject = self.ks.subject_token(self.email)
        short = f"shortcanary{self.uniq}"
        unit = f"lcanary{self.uniq} "
        long_text = unit * (2800 // len(unit))   # over 2 KB (stored compressed) and under the leaf cap (3686 characters)
        texts = [f"{short} met {self.email} on the orchard", f"{long_text}{self.email}"]
        ids = []
        for t in texts:
            res = w.writer.remember("tok", "base", t)
            self.assertTrue(res.ok, res.status)
            w.new.append((res, t))
            ids.append(res.leaf_id)
            w.count += 1
        w.bootstrap(topics=3, per=4)
        prin = f"canprin{self.uniq}"
        now = datetime.datetime.now(datetime.timezone.utc).isoformat()
        st, r = self.admin.request("PUT", f"/lc-{self.T}-authz/_doc/gr{self.uniq}?refresh=true", {
            "tenant": self.T, "cls": "tenantwide", "kind": "grant", "lin_leaves": [], "lin_leaf_ids": [], "lin_nodes": [],
            "lin_subjects": [self.subject], "lin_queries": [], "derivation": "grant", "erasure_rewrite": 0, "created_at": now,
            "schema_v": 1, "principal": prin})
        self.assertIn(st, (200, 201), r)
        self.canaries = Canaries(short=short, long=unit * 4, vector=embed_stub.embed(texts[0]), subject_token=self.subject,
                                 leaf_ids=ids, principal=prin, log_marker=self.marker)
        return w

    # ---- the verifier ---------------------------------------------------------------------------------------------------
    def check_erasure(self, **kw):
        self.plant()
        v = self.verifier()
        before = v.positive_control(self.canaries)
        self.assertGreater(before.total, 0)
        self.executor(**kw).erase_subject(self.subject, apply_guard=lambda: self.guard.erase_identifier(self.email))
        after = v.residue(self.canaries, before)
        self.assertEqual(after.total, 0, f"residue after erasure: {after.found}")
        return before, after

    def test_every_canary_is_found_before_and_none_after_with_the_step_list_and_counts(self):
        before, after = self.check_erasure()
        names = [n for n, _, _ in after.steps]
        self.assertEqual(names, ["text", "knn", "get_by_id", "lineage", "authz", "logs"])
        self.assertEqual(after.probes, 2 + 1 + 1 + 1 + 1 + 2, "the number of probes run is stated and fixed")
        self.assertTrue(all(found > 0 for n, p, found in before.steps if p and n != "logs"), "a method was blind before the erasure")
        print(f"verifier: positive control {dict(before.found)}, residue {dict(after.found)}")

    def test_a_near_miss_vector_below_the_threshold_is_not_residue_but_a_copy_is(self):
        """SIMILARITY = 0.98 (design-readings): a vector at cosine 0.90 to the canary is not counted; the canary's own vector,
        scored about 0.998 by the quantised index, is."""
        self.plant()
        v = self.verifier()
        base = list(self.canaries.vector)
        other = [0.0] * len(base)
        other[0], other[1] = 1.0, -1.0
        dot = sum(a * b for a, b in zip(other, base))
        orth = [o - dot * b for o, b in zip(other, base)]
        n = sum(x * x for x in orth) ** 0.5
        orth = [x / n for x in orth]
        near = [0.90 * b + (1 - 0.90 ** 2) ** 0.5 * o for b, o in zip(base, orth)]   # cosine 0.90 to the canary
        self.assertAlmostEqual(sum(a * b for a, b in zip(near, base)), 0.90, places=6)
        v._es.request("POST", f"/lc-{self.T}-*/_refresh")
        self.assertEqual(v._knn(near), 0, "a near miss below the threshold was reported as residue")
        self.assertGreaterEqual(v._knn(base), 1, "a copy of the canary vector was not found")

    def test_broken_keys_destroyed_but_plaintext_left_leaves_residue_the_verifier_finds(self):
        with self.assertRaises(AssertionError) as cm:
            self.check_erasure(destroy_only_keys=True)
        self.assertIn("residue after erasure", str(cm.exception))

    def test_a_log_line_holding_an_erased_canary_is_counted(self):
        self.plant()
        v = self.verifier()
        before = v.positive_control(self.canaries)
        self.executor().erase_subject(self.subject, apply_guard=lambda: self.guard.erase_identifier(self.email))
        with open(self.logf, "a") as f:
            f.write(f"defect: {self.canaries.long} leaked\n")
        after = v.residue(self.canaries, before)
        self.assertEqual(after.found["logs"], 1)
        self.assertEqual(after.total, 1)

    def test_a_residue_run_of_another_shape_or_without_a_control_marker_is_refused(self):
        self.plant()
        v = self.verifier()
        before = v.positive_control(self.canaries)
        c2 = Canaries(short=self.canaries.short)
        with self.assertRaises(VerifierError):
            v.residue(c2, before)
        self.logf.write_text("nothing here\n")
        with self.assertRaises(VerifierError):
            v.positive_control(self.canaries)

    def test_broken_raw_file_grep_as_the_verifier_misses_the_long_canary(self):
        self.assertTrue(CONTAINER, "a raw-file grep needs the container's data directory: set LC_ES_CONTAINER (a failure, not a skip)")
        self.plant()
        self.admin.request("POST", f"/lc-{self.T}-*/_flush?force=true")

        def grep(needle):
            r = subprocess.run(["docker", "exec", CONTAINER, "grep", "-rlF", "--", needle, "/usr/share/elasticsearch/data"],
                               capture_output=True, text=True, timeout=120)
            return len([x for x in r.stdout.splitlines() if x])

        short_hits, long_hits = grep(self.canaries.short), grep(self.canaries.long)
        v = self.verifier()
        self.assertGreater(v.positive_control(self.canaries).found["text"], 1, "the direct reads find both canaries")
        self.assertGreater(short_hits, 0, "the grep is not blind to the short canary (it is a plausible verifier)")
        self.assertEqual(long_hits, 0, "a raw-file grep must miss the long, compressed canary")

    # ---- receipts ---------------------------------------------------------------------------------------------------------
    def receipt(self, *, stray_log_row=False, **kw):
        self.plant()
        v = self.verifier()
        before = v.positive_control(self.canaries)
        rc = self.executor().erase_subject(self.subject, apply_guard=lambda: self.guard.erase_identifier(self.email))
        if stray_log_row:   # a row the closure never reached still names the subject
            now = datetime.datetime.now(datetime.timezone.utc).isoformat()
            self.admin.request("PUT", f"/lc-{self.T}-base-log/_doc/stray{self.uniq}?refresh=true", {
                "tenant": self.T, "cls": "base", "kind": "log", "lin_leaves": [], "lin_leaf_ids": [], "lin_nodes": [],
                "lin_subjects": [self.subject], "lin_queries": [], "derivation": "recall", "erasure_rewrite": 0,
                "created_at": now, "schema_v": 1})
        left = recount(self.admin, self.T, self.subject, self.canaries.leaf_ids)
        return rc, full_receipt(rc, residue=left, verifier_steps=v.residue(self.canaries, before).steps, **kw)

    def test_the_receipt_recounts_states_counts_by_class_and_the_requester_sees_totals_over_its_read_set_only(self):
        rc, full = self.receipt()
        self.assertTrue(full["done"], full)
        self.assertEqual(full["residue"], 0)
        self.assertIn("base", full["counts_by_class"])
        self.assertGreater(sum(full["counts_by_class"]["base"].values()), 0)
        self.assertTrue(full["within_b_erase"])
        self.assertEqual(full["verifier"]["probes"], 8, "the receipt states the verifier's probe count")
        self.assertEqual(full["retention"], {"r_keys_days": 35, "r_logs_days": 7})
        mine = requester_copy(full, ["base"])
        self.assertEqual(mine["erased_in_your_read_set"], sum(full["counts_by_class"]["base"].values()))
        self.assertNotIn("counts_by_class", mine)
        self.assertNotIn("tenantwide", str(mine))
        none = requester_copy(full, ["otherclass"])
        self.assertEqual(none["erased_in_your_read_set"], 0, "a requester sees a count outside its read set")

    def test_broken_requester_copy_with_per_class_counts_is_red(self):
        rc, full = self.receipt()
        leaked = requester_copy(full, ["base"], broken_per_class=True)
        with self.assertRaises(AssertionError):
            self.assertNotIn("counts_by_class", leaked, "the requester's copy carries per-class counts (review 3, F19b)")
        self.assertNotIn("counts_by_class", requester_copy(full, ["base"]))

    # ---- the verifier as a separate process with its own key ------------------------------------------------------------------
    def cli(self, phase, canaries_file, *extra, key="verify"):
        from tests.security.vworld import CA, URL
        cmd = [sys.executable, "-m", "living_context.verifier", phase, "--url", URL, "--ca", CA,
               "--key-file", str(self.root / "keys" / self.T / f"{key}.key"), "--tenant", self.T, "--canaries", str(canaries_file),
               "--log", str(self.logf), *extra]
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=120, cwd=str(Path(__file__).resolve().parents[2]),
                           env={"PATH": os.environ.get("PATH", ""), "PYTHONPATH": str(Path(__file__).resolve().parents[2])})
        return p.returncode, (json.loads(p.stdout.strip().splitlines()[-1]) if p.stdout.strip() else {})

    def canaries_file(self):
        c = self.canaries
        f = self.dir / "canaries.json"
        f.write_text(json.dumps({"short": c.short, "long": c.long, "vector": list(c.vector), "subject_token": c.subject_token,
                                 "leaf_ids": list(c.leaf_ids), "principal": c.principal, "log_marker": c.log_marker}))
        return f

    def test_the_verifier_runs_as_its_own_process_with_its_own_read_only_key_and_reports_residue_by_exit_status(self):
        self.plant()
        cf, ctl = self.canaries_file(), self.dir / "control.json"
        rc, rep = self.cli("control", cf, "--out", str(ctl))
        self.assertEqual(rc, 0, rep)
        self.assertTrue(all(found > 0 for n, p, found in rep["steps"] if p and n != "logs"), rep)
        rc, rep = self.cli("residue", cf, "--control", str(ctl))
        self.assertEqual(rc, 1, "the residue phase before any erasure must find the planted canaries")
        self.executor().erase_subject(self.subject, apply_guard=lambda: self.guard.erase_identifier(self.email))
        rc, rep = self.cli("residue", cf, "--control", str(ctl))
        self.assertEqual((rc, rep["total"]), (0, 0), rep)
        self.assertEqual(rep["probes"], 8)

    def test_the_verifier_key_can_neither_write_nor_delete(self):
        from living_context.gateway.es import EsClient
        from tests.security.vworld import CA, URL
        vk = EsClient(URL, CA, str(self.root / "keys" / self.T / "verify.key"), scheme="apikey")
        st, _ = vk.request("POST", f"/lc-{self.T}-*/_search?ignore_unavailable=true", {"size": 1})
        self.assertEqual(st, 200, "the verifier key cannot read")
        st, _ = vk.request("PUT", f"/lc-{self.T}-base-leaf/_doc/x1", {"tenant": self.T})
        self.assertEqual(st, 403, "the verifier key can write")
        st, _ = vk.request("POST", f"/lc-{self.T}-*/_delete_by_query", {"query": {"match_all": {}}})
        self.assertEqual(st, 403, "the verifier key can delete")

    def test_the_verifier_process_with_a_key_that_cannot_read_is_an_error_not_a_clean_report(self):
        self.plant()
        cf = self.canaries_file()
        rc, rep = self.cli("control", cf, key="log")   # the log role cannot read the leaf indices
        self.assertEqual(rc, 2, rep)
        self.assertIn("error", rep)

    def test_a_receipt_with_a_log_row_that_still_names_the_subject_is_not_done(self):
        rc, full = self.receipt(stray_log_row=True)
        self.assertFalse(full["done"])
        self.assertEqual(full["residue"], 1)

    def test_broken_receipt_without_a_recount_says_done_while_a_log_row_remains_is_red(self):
        rc, full = self.receipt(stray_log_row=True, trust_closure=True)
        with self.assertRaises(AssertionError):
            self.assertFalse(full["done"], "the receipt says done while a log row still names the subject")

    # ---- restore: an older snapshot with an older keystore backup -------------------------------------------------------------
    def dump(self):
        self.admin.request("POST", f"/lc-{self.T}-*/_refresh")
        docs = []
        st, r = self.admin.request("POST", f"/lc-{self.T}-*/_search?ignore_unavailable=true", {"size": 5000, "query": {"match_all": {}},
                                                                                                 "sort": [{"_doc": "asc"}]})
        for h in r["hits"]["hits"]:
            docs.append((h["_index"], h["_id"], h["_source"]))
        return docs

    def check_restore(self, replay=True, snapshot_journal=False, **kw):
        self.plant()
        v = self.verifier()
        before = v.positive_control(self.canaries)
        snap = self.dump()
        journal_then = TombstoneJournal(self.dir / "jthen" / "t.log", self.dir / "rthen" / "t.log")   # the journal as the snapshot held it
        keystore_copy = self.dir / "ks-older.db"
        for suffix in ("", "-wal", "-shm", "-journal"):
            src = Path(str(self.root / "ks.db") + suffix)
            if src.exists():
                shutil.copy(src, Path(str(keystore_copy) + suffix))
        self.executor().erase_subject(self.subject, apply_guard=lambda: self.guard.erase_identifier(self.email))
        self.assertEqual(v.residue(self.canaries, before).total, 0, "the premise: the erasure itself left nothing")
        # the restore: the older snapshot's documents come back, and the older keystore backup replaces the keystore
        for idx, doc_id, src in snap:
            st, r = self.admin.request("PUT", f"/{idx}/_doc/{doc_id}?refresh=true", src)
            self.assertIn(st, (200, 201), r)
        old_ks = Keystore(keystore_copy, self.T)
        self.addCleanup(old_ks.close)
        self.assertGreater(v.probe(self.canaries).total, 0, "the premise: the older snapshot resurrected the content")
        self.assertTrue(any(old_ks.has_key(lid, 1) for lid in self.canaries.leaf_ids), "the premise: the older keystore holds the keys")
        ex = self.executor(ks=old_ks, **kw)
        res = restore_replay(ex, journal_then if snapshot_journal else self.journal, lambda: v.residue(self.canaries, before), replay=replay)
        self.assertTrue(res.enabled, f"the tenant was not enabled: {res.reason}")
        for lid in self.canaries.leaf_ids:
            self.assertFalse(old_ks.has_key(lid, 1), "a key named by a tombstone survived the restore")
        return res

    def test_restoring_an_older_snapshot_and_keystore_replays_the_closure_and_enables_only_at_zero_residue(self):
        res = self.check_restore()
        self.assertEqual((res.replayed, res.residue), (1, 0))
        print(f"restore replay: {res.replayed} tombstone, residue {res.residue}, {res.seconds:.1f} s, steps {[s[0] for s in res.steps]}")

    def test_broken_restore_that_serves_without_re_applying_the_journal_is_red(self):
        with self.assertRaises(AssertionError) as cm:
            self.check_restore(replay=False)
        self.assertIn("the tenant was not enabled", str(cm.exception))

    def test_broken_replay_that_does_not_cover_the_authz_index_is_red(self):
        with self.assertRaises(AssertionError) as cm:
            self.check_restore(broken_skip_authz=True)
        self.assertIn("the tenant was not enabled", str(cm.exception))

    def test_broken_tombstones_kept_only_inside_the_snapshot_resurrect_the_content(self):
        # the journal is the newest copy and lives outside the snapshot; one restored with the snapshot knows no erasure
        with self.assertRaises(AssertionError) as cm:
            self.check_restore(snapshot_journal=True)
        self.assertIn("the tenant was not enabled", str(cm.exception))

    def test_broken_keystore_restore_that_keeps_the_keys_named_by_newer_tombstones_is_red(self):
        with self.assertRaises(AssertionError) as cm:
            self.check_restore(broken_replay_keeps_keys=True)
        self.assertIn("a key named by a tombstone survived the restore", str(cm.exception))


if __name__ == "__main__":
    unittest.main()
