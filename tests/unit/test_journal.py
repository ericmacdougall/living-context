"""BUILD-PLAN Step 14: the tombstone journal (hash chain, fsync, replica; DESIGN 14.3, 14.6)."""
import json
import os
import tempfile
import unittest
from pathlib import Path

from living_context.gateway.journal import JournalCorrupt, TombstoneJournal


class Journal(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)

    def j(self, **kw):
        return TombstoneJournal(self.root / "a" / "tomb.log", self.root / "b" / "tomb.log", **kw)

    def test_append_fsyncs_both_copies_and_the_directory_on_creation(self):
        calls = []
        j = self.j(fsync=lambda fd: calls.append(fd))
        j.append({"subject": "s1"})
        self.assertGreaterEqual(len(calls), 4)  # two files and two directories
        n = len(calls)
        j.append({"subject": "s2"})
        self.assertEqual(len(calls) - n, 2)       # files only once the directories exist
        self.assertEqual((self.root / "a" / "tomb.log").read_text(), (self.root / "b" / "tomb.log").read_text())
        self.assertEqual(j.last_seq, 2)

    def test_reopen_returns_the_records_and_a_lost_tail_is_repaired_from_the_replica(self):
        j = self.j()
        for s in ("s1", "s2", "s3"):
            j.append({"subject": s})
        prim = self.root / "a" / "tomb.log"
        lines = prim.read_text().splitlines()
        prim.write_text("\n".join(lines[:2]) + "\n")  # the primary lost its last record
        j2 = self.j()
        self.assertEqual([r["scope"]["subject"] for r in j2.records()], ["s1", "s2", "s3"])
        self.assertEqual(prim.read_text().count("\n"), 3)

    def tamper(self, **kw):
        j = self.j(**kw)
        for s in ("s1", "s2", "s3"):
            j.append({"subject": s})
        for side in ("a", "b"):
            p = self.root / side / "tomb.log"
            rows = [json.loads(x) for x in p.read_text().splitlines()]
            rows[1]["scope"]["subject"] = "other"   # rewrite history in both copies
            p.write_text("".join(json.dumps(r, sort_keys=True) + "\n" for r in rows))
        self.j(**kw)  # opening must refuse

    def test_a_rewritten_record_fails_closed(self):
        with self.assertRaises(JournalCorrupt):
            self.tamper()

    def test_broken_journal_without_a_chain_accepts_rewritten_history(self):
        self.tamper(chain=False)  # no exception: the tamper went unnoticed (what the chain prevents)

    def test_primary_and_replica_that_disagree_fail_closed(self):
        j = self.j()
        j.append({"subject": "s1"})
        p = self.root / "b" / "tomb.log"
        r = json.loads(p.read_text())
        r["scope"]["subject"] = "x"
        p.write_text(json.dumps(r, sort_keys=True) + "\n")
        with self.assertRaises(JournalCorrupt):
            self.j()


if __name__ == "__main__":
    unittest.main()
