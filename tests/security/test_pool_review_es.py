"""Fixes for the independent review of the pools foundation (findings M1 and following), each shown red first on the unfixed code and
then green. Needs LC_TEST_ES_URL, LC_TEST_CA and LC_TEST_PW; without them the module FAILS (it never skips)."""
import base64
import threading
import time
import unittest

from living_context.gateway.indices import class_index, pool_index
from living_context.gateway.pool_tools import PoolTools
from living_context.ingest.writer import Writer
from living_context.ingest.subjects import Guard, TokenSource
from tests.security.test_pools_es import Base, FailingModel, T


class Repair(Base):
    def test_m1_repair_reprojects_a_pool_leaf_into_its_own_index_only(self):
        a, b = self.pool("alpha"), self.pool("beta")
        tok = self.mint("alice", {a: {"query", "save"}, b: {"query", "save"}})
        ptrs = {}
        for pid in (a, b):
            r = self.tools.save(tok, {"pool": pid, "text": f"repair canary {pid[:4]} secret", "key": f"k{pid}"})
            self.assertTrue(r["ok"], r)
            ptrs[pid] = r
        self.refresh()
        # a crash between source and projection: the pool leaf document of A is gone, its source stays
        st, hits = self.admin.request("POST", f"/{pool_index(T, 'base', a, 'leaf')}/_search", {"query": {"match_all": {}}, "_source": False})
        for h in hits["hits"]["hits"]:
            self.admin.request("DELETE", f"/{pool_index(T, 'base', a, 'leaf')}/_doc/{h['_id']}?refresh=true")
        out = self.writer.repair(["base"])
        self.refresh()
        # nothing of any pool is in a class-level index or in the other pool's index
        for idx in (class_index(T, "base", "leaf"), class_index(T, "base", "ctl"), pool_index(T, "base", b, "leaf")):
            st, r = self.admin.request("POST", f"/{idx}/_search", {"size": 50, "query": {"match_all": {}}})
            blob = str(r)
            self.assertNotIn("repair canary " + a[:4], blob, idx)
            if idx != pool_index(T, "base", b, "leaf"):
                self.assertNotIn("repair canary " + b[:4], blob, idx)
        st, r = self.admin.request("POST", f"/{class_index(T, 'base', 'ctl')}/_search", {"size": 50, "query": {"term": {"kind": "dirty"}}})
        self.assertEqual(r["hits"]["total"]["value"], 0)                      # no placement job for a pool leaf
        st, r = self.admin.request("POST", f"/{pool_index(T, 'base', a, 'leaf')}/_search", {"size": 5, "query": {"match_all": {}}})
        self.assertEqual(r["hits"]["total"]["value"], 1)                      # the repair put it back where it belongs
        self.assertEqual(r["hits"]["hits"][0]["_source"]["pool"], a)
        self.assertGreaterEqual(out["reprojected"], 1)
        f = self.tools.fetch(tok, {"pointer": ptrs[a]["pointer"]})
        self.assertEqual(f["status"], "ok")


class Update(Base):
    def paused_tools(self, step):
        """A writer that holds the first thread that reaches `step` for a moment, so a second caller overlaps it."""
        seen = threading.Event()

        def pause(name):
            if name == step and not seen.is_set():
                seen.set()
                time.sleep(0.6)
        self.writer = Writer(lambda: self.cc("ingest"), self.ks, T, Guard(TokenSource(self.ks)), reader=self.reader, gate=self.wgate, pause=pause)
        self.tools = PoolTools(T, self.dir, self.sessions, __import__("tests.security.test_pools_es", fromlist=["x"]).LAT, self.writer, self.reader,
                               lambda cls: self.cc(f"read-{cls}"), self.keys, consts=self.consts, model=FailingModel())

    def test_m4_two_concurrent_corrects_one_wins_one_conflicts_and_source_equals_projection(self):
        a = self.pool("alpha", ops={"query", "save", "update"})
        tok = self.mint("alice", {a: {"query", "save", "update"}})
        self.paused_tools("source")
        first = self.tools.save(tok, {"pool": a, "text": "original text", "key": "k0"})
        self.assertTrue(first["ok"], first)
        self.paused_tools("source")                                    # re-arm the pause for the two updates
        out = []

        def go(text, key):
            out.append(self.tools.update(tok, {"pointer": first["pointer"], "mode": "correct", "text": text, "key": key}))
        ts = [threading.Thread(target=go, args=(f"edit number {i}", f"u{i}")) for i in range(2)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        self.assertEqual(sorted(bool(o.get("ok")) for o in out), [False, True], out)
        self.assertEqual([o.get("error") for o in out if not o.get("ok")], ["version_conflict"])
        self.refresh()
        won = next(o for o in out if o.get("ok"))
        f = self.tools.fetch(tok, {"pointer": won["pointer"]})
        self.assertEqual(f["status"], "ok")
        st, r = self.admin.request("POST", f"/lc-{T}-src/_search", {"size": 10, "query": {"term": {"pool": a}}})
        rows = [h["_source"] for h in r["hits"]["hits"] if int(h["_source"]["lv"]) == 2]
        self.assertEqual(len(rows), 1)
        leaf_id = rows[0]["leaf_id"]
        text = self.ks.decrypt(leaf_id, 2, base64.b64decode(rows[0]["ct"]), base64.b64decode(rows[0]["nonce"])).decode()
        self.assertIn(text, f["text"])                                  # the stored source and the projection are the same text

    def test_m4_a_supersede_that_loses_the_head_leaves_no_live_duplicate(self):
        a = self.pool("alpha", ops={"query", "save", "update"})
        tok = self.mint("alice", {a: {"query", "save", "update"}})
        first = self.tools.save(tok, {"pool": a, "text": "plan alpha stays", "key": "k0"})
        real = self.writer.update_head
        calls = []

        def lose_once(cls, pool, leaf, expect, **kw):
            calls.append(kw.get("status"))
            if kw.get("status") == "superseded":
                return False                                           # the head moved under the caller
            return real(cls, pool, leaf, expect, **kw)
        self.writer.update_head = lose_once
        r = self.tools.update(tok, {"pointer": first["pointer"], "mode": "supersede", "text": "plan beta replaces", "key": "u1"})
        self.assertEqual(r.get("error"), "version_conflict")
        self.refresh()
        q = self.tools.query(tok, {"query": "plan beta replaces"})
        self.assertNotIn("beta", str(q["items"]))                       # the new leaf is not retrievable
        self.assertIn("aborted", calls)


if __name__ == "__main__":
    unittest.main()
