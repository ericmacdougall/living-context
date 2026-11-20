"""POOLS 3.5 (NEG-15 part, AC-10) on a real Elasticsearch: pool_retract hides an item reversibly, agents cannot hide human memories,
and the call is rate limited. Release to erasure, restore and the review queue of retraction requests are not built yet."""
import unittest

from living_context.config import Constants
from living_context.gateway.pool_tools import PoolTools
from tests.security.test_pools_es import LAT, T, UNAV, Base, FailingModel


class Retract(Base):
    def setUp(self):
        super().setUp()
        self.consts = Constants(tok_live_max=100, mint_rate_per_h=1000, retract_rate_per_h=3)
        self.build()

    def tools_with(self, **kw):
        return PoolTools(T, self.dir, self.sessions, LAT, self.writer, self.reader, lambda cls: self.cc(f"read-{cls}"), self.keys,
                         consts=self.consts, model=FailingModel(), **kw)

    def ptr(self, tok, pool, text, key):
        r = self.tools.save(tok, {"pool": pool, "text": text, "key": key})
        self.assertTrue(r["ok"], r)
        return r["pointer"]

    def test_an_agent_retracts_its_own_item_and_it_leaves_results_but_resolves_retracted(self):
        a = self.pool("alpha", ops={"query", "save", "retract"})
        t = self.mint("alice", {a: {"query", "save", "retract"}})
        p = self.ptr(t, a, "the wrong wombat fact", "r1")
        keep = self.ptr(t, a, "the right wombat fact", "r2")
        r = self.tools.retract(t, {"pointers": [p], "reason": "wrong", "key": "k1"})
        self.assertEqual(r, {"ok": True, "accepted": True, "results": ["retracted"]})
        got = [i["pointer"] for i in self.tools.query(t, {"query": "wombat fact"})["items"]]
        self.assertEqual(got, [keep])
        self.assertEqual(self.tools.fetch(t, {"pointer": p}), {"ok": True, "status": "retracted", "reason": "wrong"})
        outsider = self.mint("bob", {self.pool("beta", grant_to=("bob",)): {"query"}})
        self.assertEqual(self.tools.fetch(outsider, {"pointer": p}), UNAV)               # a token that could not read it: unavailable
        self.refresh()
        docs = self.reader.search("base", "ctl", a, [{"term": {"kind": "retraction"}}])
        self.assertEqual([(d.source["state"], d.source["actor"]) for d in docs], [("done", "agent")])

    def test_an_agent_cannot_hide_a_human_memory_and_a_human_can(self):
        a = self.pool("alpha", ops={"query", "save", "retract"})
        agent = self.mint("alice", {a: {"query", "save", "retract"}})
        human = self.mint("alice", {a: {"query", "save", "retract"}}, human=True)
        h = self.ptr(human, a, "a human wrote this emu fact", "h1")
        r = self.tools.retract(agent, {"pointers": [h], "reason": "outdated", "key": "k2"})
        self.assertEqual(r, {"ok": True, "accepted": True, "results": ["held"]})            # a request, not a retraction
        self.assertEqual([i["pointer"] for i in self.tools.query(agent, {"query": "emu fact"})["items"]], [h])
        self.refresh()
        self.assertEqual([d.source["state"] for d in self.reader.search("base", "ctl", a, [{"term": {"kind": "retraction"}}])], ["requested"])
        r2 = self.tools.retract(human, {"pointers": [h], "reason": "outdated", "key": "k3"})
        self.assertEqual(r2["results"], ["retracted"])
        self.assertEqual(self.tools.query(human, {"query": "emu fact"})["items"], [])
        # BROKEN: an agent retracts a human leaf directly
        bad = self.tools_with(broken_agent_retracts_human=True)
        h2 = self.ptr(human, a, "second human kiwi fact", "h2")
        self.assertEqual(bad.retract(agent, {"pointers": [h2], "reason": "wrong", "key": "k4"})["results"], ["retracted"])

    def test_operations_pools_and_enum_are_checked_and_the_call_is_whole(self):
        a, b = self.pool("alpha", ops={"query", "save", "retract"}), self.pool("beta", ops={"query", "save"})
        t = self.mint("alice", {a: {"query", "save", "retract"}, b: {"query", "save"}})
        pa, pb = self.ptr(t, a, "alpha quokka fact", "x1"), self.ptr(t, b, "beta quokka fact", "x2")
        qonly = self.mint("alice", {a: {"query"}}, conv="qq")
        self.assertEqual(self.tools.retract(qonly, {"pointers": [pa], "reason": "wrong", "key": "k"}), UNAV)
        self.assertEqual(self.tools.retract(t, {"pointers": [pa, pb], "reason": "wrong", "key": "k"}), UNAV)   # b has no retract: the whole call fails
        self.assertEqual(len(self.tools.query(t, {"query": "quokka fact"})["items"]), 2)                       # nothing was retracted
        self.assertEqual(self.tools.retract(t, {"pointers": [pa], "reason": "because I said so", "key": "k"})["error"], "invalid")
        self.assertEqual(self.tools.retract(t, {"pointers": ["nope"], "reason": "wrong", "key": "k"}), UNAV)
        self.assertEqual(self.tools.retract(t, {"pointers": [pa] * 21, "reason": "wrong", "key": "k"})["error"], "invalid")

    def test_the_retract_rate_is_limited_and_a_repeat_looks_the_same(self):
        a = self.pool("alpha", ops={"query", "save", "retract"})
        t = self.mint("alice", {a: {"query", "save", "retract"}})
        ptrs = [self.ptr(t, a, f"rate fact number{i}", f"q{i}") for i in range(5)]
        for p in ptrs[:3]:
            self.assertTrue(self.tools.retract(t, {"pointers": [p], "reason": "wrong", "key": "k"})["ok"])
        self.assertEqual(self.tools.retract(t, {"pointers": [ptrs[3]], "reason": "wrong", "key": "k"}), {"ok": False, "error": "rate_limited"})
        bad = self.tools_with(broken_no_retract_rate=True)
        self.assertTrue(bad.retract(t, {"pointers": [ptrs[3]], "reason": "wrong", "key": "k"})["ok"])      # BROKEN: no rate bound
        self.assertEqual(bad.retract(t, {"pointers": [ptrs[3]], "reason": "wrong", "key": "k"})["results"], ["retracted"])   # already retracted: same answer


if __name__ == "__main__":
    unittest.main()
