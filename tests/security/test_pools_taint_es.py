"""POOLS 2.8 (NEG-10, NEG-13, AC-8) on a real Elasticsearch: the taint of a conversation, held saves, release by a human. Each negative
case runs against the real code and a broken variant that must turn it red."""
import unittest

from living_context.config import Constants

from living_context.gateway import pointers
from living_context.gateway.pool_tools import PoolTools
from living_context.gateway.session_state import SessionStates, es_load, es_persist
from living_context.policy import Principal, Role
from tests.security.test_pools_es import LAT, T, UNAV, Base, FailingModel, pool_index


class Taint(Base):
    def setUp(self):
        super().setUp()
        self.consts = Constants(tok_live_max=100, mint_rate_per_h=1000)
        self.build()
        both = frozenset({"base", "restricted"})
        self.store.restore(Principal(T, "pt-user-carol", frozenset({Role.READER, Role.WRITER}), both, both))
        self.store.restore(Principal(T, "pt-user-dana", frozenset({Role.READER, Role.WRITER}), frozenset({"base"}), frozenset({"base"})))
        self.st = self.new_states()
        self.tt = self.tools_with(self.st)

    def new_states(self, **kw):
        return SessionStates(LAT, es_persist(lambda: self.gate.client(T, "ingest"), T), es_load(lambda: self.gate.client(T, "ingest"), T), **kw)

    def tools_with(self, states, **kw):
        return PoolTools(T, self.dir, self.sessions, LAT, self.writer, self.reader, lambda cls: self.cc(f"read-{cls}"), self.keys,
                         model=FailingModel(), states=states, **kw)

    def holds(self, pool, cls):
        self.refresh()
        return self.reader.search(cls, "ctl", pool, [{"term": {"kind": "quarantine"}}])

    def leaves(self, pool, cls="base"):
        self.refresh()
        st, r = self.admin.request("GET", f"/{pool_index(T, cls, pool, 'leaf')}/_count")
        return r["count"]

    def world(self):
        r_pool = self.pool("restr", cls="restricted", grant_to=("carol", "dana"), ops={"query", "save"})
        b_pool = self.pool("base", grant_to=("carol", "dana"), ops={"query", "save", "review"})
        return r_pool, b_pool

    def test_write_down_after_reading_a_restricted_pool_is_held_at_the_restricted_class(self):
        r, b = self.world()
        t = self.mint("carol", {r: {"query", "save"}, b: {"query", "save"}})
        seed = self.tt.save(t, {"pool": r, "text": "restricted plan ZQ", "key": "s1"})
        self.assertTrue(seed["ok"])
        self.assertTrue(self.tt.save(t, {"pool": b, "text": "base note before reading", "key": "s2"})["ok"])   # ordinary work is not blocked
        f = self.tt.fetch(t, {"pointer": seed["pointer"]})
        self.assertEqual(f["taint_raised"], [r])
        n = self.leaves(b)
        h = self.tt.save(t, {"pool": b, "text": "copy of the restricted plan", "key": "s3"})
        self.assertEqual(h, {"ok": False, "error": "held", "reason": "taint"})
        self.assertEqual(self.leaves(b), n)                           # nothing was written to the base pool's indices
        self.assertEqual((len(self.holds(b, "restricted")), len(self.holds(b, "base"))), (1, 0))   # stored at the taint class
        self.assertTrue(self.tt.save(t, {"pool": r, "text": "more restricted text", "key": "s4"})["ok"])   # into the same or a higher class is fine
        # BROKEN: no taint rule writes it; a hold stored with the target's class is visible at the lower class
        bad = self.tools_with(self.st, broken_taint_ignored=True)
        self.assertTrue(bad.save(t, {"pool": b, "text": "leaked copy", "key": "s5"})["ok"])
        bad2 = self.tools_with(self.st, broken_hold_in_target=True)
        bad2.save(t, {"pool": b, "text": "held in the wrong place", "key": "s6"})
        self.assertEqual(len(self.holds(b, "base")), 1)

    def test_the_taint_follows_the_conversation_across_remint_child_and_restart(self):
        r, b = self.world()
        t = self.mint("carol", {r: {"query", "save"}, b: {"query", "save"}}, conv="cv-1")
        ptr = self.tt.save(t, {"pool": r, "text": "restricted again", "key": "k1"})["pointer"]
        self.tt.fetch(t, {"pointer": ptr})
        again = self.mint("carol", {b: {"query", "save"}}, conv="cv-1")                  # a re-mint for the same conversation
        child = self.sessions.delegate(t, {b: {"save"}}, 60)
        for tok in (again, child):
            self.assertEqual(self.tt.save(tok, {"pool": b, "text": "should be held " + tok[-6:], "key": "m" + tok[-4:]})["error"], "held")
        self.st.forget_memory()                                                           # a gateway restart: only the durable value remains
        self.assertEqual(self.tt.save(again, {"pool": b, "text": "held after restart", "key": "m9"})["error"], "held")
        other = self.mint("carol", {b: {"query", "save"}}, conv="cv-2")                  # another conversation is clean
        self.assertTrue(self.tt.save(other, {"pool": b, "text": "clean conversation", "key": "m10"})["ok"])
        # BROKEN: keyed by token, a fresh token is clean; held in memory only, a restart forgets
        keyed = self.tools_with(self.new_states(broken_keyed_by_token=True))
        t2 = self.mint("carol", {r: {"query", "save"}, b: {"query", "save"}}, conv="cv-3")
        keyed.fetch(t2, {"pointer": keyed.save(t2, {"pool": r, "text": "r text", "key": "q1"})["pointer"]})
        t3 = self.mint("carol", {b: {"query", "save"}}, conv="cv-3")
        self.assertTrue(keyed.save(t3, {"pool": b, "text": "not held by token keyed state", "key": "q2"})["ok"])
        mem = self.new_states(broken_memory_only=True)
        memt = self.tools_with(mem)
        t4 = self.mint("carol", {r: {"query", "save"}, b: {"query", "save"}}, conv="cv-4")
        memt.fetch(t4, {"pointer": memt.save(t4, {"pool": r, "text": "r text 4", "key": "q3"})["pointer"]})
        mem.forget_memory()
        self.assertTrue(memt.save(t4, {"pool": b, "text": "forgotten taint", "key": "q4"})["ok"])

    def test_a_private_pool_read_taints_writes_into_a_team_pool_but_not_into_itself(self):
        p = self.pool("mine", grant_to=("carol",), ops={"query", "save"})
        self.svc.dir.get(T, p)   # the pool exists
        arec = self.sessions.resolve(self.sessions.mint(T, b"user-admin"))
        pr = self.svc.handle("pool_create", self.sessions.principal(arec), arec, {"cls": "base", "kind": "person", "label": "mine2"})
        priv = pr["pool"]
        self.svc.handle("pool_grant", self.sessions.principal(arec), arec, {"who": "user-carol", "pool": priv, "ops": ["query", "save"]})
        team = self.pool("team", grant_to=("carol",), ops={"query", "save"})
        t = self.mint("carol", {priv: {"query", "save"}, team: {"query", "save"}}, conv="cv-p")
        self.assertTrue(self.tt.save(t, {"pool": priv, "text": "my private zebra note", "key": "a1"})["ok"])
        self.assertTrue(self.tt.save(t, {"pool": team, "text": "team zebra note", "key": "a2"})["ok"])
        # no hint: the private pool is not queried; the team pool is; nothing is raised and saves into the team stay direct
        n0 = len(self.log)
        q = self.tt.query(t, {"query": "zebra"})
        self.assertEqual((q["pools"], q["taint_raised"]), ([team], []))
        self.assertFalse(any(pool_index(T, "base", priv, "leaf") in pth for _, pth in self.log[n0:]))
        self.assertTrue(self.tt.save(t, {"pool": team, "text": "still direct", "key": "a3"})["ok"])
        # naming it in the hint raises the taint and says so; the next save into the team pool is held, into the private pool it is not
        q2 = self.tt.query(t, {"query": "zebra", "pools": [priv]})
        self.assertEqual(q2["taint_raised"], [priv])
        self.assertEqual(self.tt.save(t, {"pool": team, "text": "after reading mine", "key": "a4"})["error"], "held")
        self.assertTrue(self.tt.save(t, {"pool": priv, "text": "into the private pool itself", "key": "a5"})["ok"])
        # BROKEN: default selection over the whole grant set queries the private pool
        bad = self.tools_with(self.new_states(), broken_default_selection_over_g=True)
        t5 = self.mint("carol", {priv: {"query", "save"}, team: {"query", "save"}}, conv="cv-q")
        self.assertEqual(set(bad.query(t5, {"query": "zebra"})["pools"]), {priv, team})

    def test_a_hold_is_released_only_by_a_human_with_review_query_and_the_taint_class(self):
        r, b = self.world()
        t = self.mint("carol", {r: {"query", "save"}, b: {"query", "save"}}, conv="cv-r")
        self.tt.fetch(t, {"pointer": self.tt.save(t, {"pool": r, "text": "secret plan", "key": "r1"})["pointer"]})
        self.assertEqual(self.tt.save(t, {"pool": b, "text": "needs a human decision", "key": "r2"})["error"], "held")
        agent = self.mint("carol", {b: {"query", "save", "review"}}, conv="cv-h")
        human = self.mint("carol", {b: {"query", "save", "review"}}, conv="cv-h2", human=True)
        weak = self.mint("dana", {b: {"query", "save", "review"}}, conv="cv-d", human=True)       # a human whose read set lacks the taint class
        hold = self.tt.holds(human, {"pool": b})["holds"]
        self.assertEqual(len(hold), 1)
        hid = hold[0]["hold"]
        self.assertEqual(self.tt.release(agent, {"pool": b, "hold": hid, "decision": "admit", "content_hash": "x"}), UNAV)    # an agent token of the same principal
        self.assertEqual(self.tt.release(weak, {"pool": b, "hold": hid, "decision": "admit", "content_hash": "x"}), UNAV)
        n = self.leaves(b)
        qonly = self.mint("carol", {b: {"query", "save"}}, conv="cv-h3", human=True)                      # human, no `review`
        self.assertEqual(self.tt.release(qonly, {"pool": b, "hold": hid, "decision": "admit", "content_hash": "x"}), UNAV)
        self.assertEqual(self.leaves(b), n)
        shown = self.tt.hold_show(human, {"pool": b, "hold": hid})
        self.assertEqual(self.tt.hold_show(agent, {"pool": b, "hold": hid}), UNAV)               # showing needs the same human checks
        res = self.tt.release(human, {"pool": b, "hold": hid, "decision": "admit", "content_hash": shown["content_hash"]})
        self.assertTrue(res["ok"], res)
        self.assertEqual(self.leaves(b), n + 1)
        self.assertEqual(self.tt.fetch(human, {"pointer": res["pointer"]})["origin"], "agent")     # a release does not change origin
        self.assertEqual(self.tt.release(human, {"pool": b, "hold": hid, "decision": "admit", "content_hash": shown["content_hash"]}), UNAV)   # once
        # discard
        self.assertEqual(self.tt.save(t, {"pool": b, "text": "to be discarded", "key": "r3"})["error"], "held")
        h2 = [x for x in self.tt.holds(human, {"pool": b})["holds"] if x["state"] == "held"][0]["hold"]
        self.assertEqual(self.tt.release(human, {"pool": b, "hold": h2, "decision": "discard"}), {"ok": True, "status": "discarded"})
        # BROKEN: release by `review` on the target alone lets an agent token (and a human without the taint class) approve
        bad = self.tools_with(self.st, broken_release_by_review_only=True)
        self.assertEqual(self.tt.save(t, {"pool": b, "text": "third held", "key": "r4"})["error"], "held")
        h3 = [x for x in self.tt.holds(human, {"pool": b})["holds"] if x["state"] == "held"][0]["hold"]
        h3hash = bad.hold_show(agent, {"pool": b, "hold": h3})["content_hash"]
        self.assertTrue(bad.release(agent, {"pool": b, "hold": h3, "decision": "admit", "content_hash": h3hash})["ok"])

    def held(self, b, r, text, key, conv):
        t = self.mint("carol", {r: {"query", "save"}, b: {"query", "save"}}, conv=conv)
        self.tt.fetch(t, {"pointer": self.tt.save(t, {"pool": r, "text": "secret plan " + key, "key": "r" + key})["pointer"]})
        self.assertEqual(self.tt.save(t, {"pool": b, "text": text, "key": key})["error"], "held")
        human = self.mint("carol", {b: {"query", "save", "review"}}, conv=conv + "h", human=True)
        hid = [x for x in self.tt.holds(human, {"pool": b})["holds"] if x["state"] == "held"][-1]["hold"]
        return human, hid

    def test_m8_the_release_binds_the_shown_content_is_logged_and_is_idempotent(self):
        import threading
        r, b = self.world()
        human, hid = self.held(b, r, "release me carefully", "m8a", "cv-m8")
        shown = self.tt.hold_show(human, {"pool": b, "hold": hid})
        self.assertIn("release me carefully", shown["text"])
        self.assertTrue(shown["text"].startswith("<memory "))                                     # model-visible only inside the envelope
        n = self.leaves(b)
        for bad_hash in (None, "", "0" * 64, shown["content_hash"][:-1] + ("1" if shown["content_hash"][-1] != "1" else "2")):                  # a release of other content, or of none
            self.assertEqual(self.tt.release(human, {"pool": b, "hold": hid, "decision": "admit", "content_hash": bad_hash}), UNAV)
        self.assertEqual(self.leaves(b), n)
        out = []
        ts = [threading.Thread(target=lambda: out.append(self.tt.release(human, {"pool": b, "hold": hid, "decision": "admit",
                                                                                 "content_hash": shown["content_hash"]}))) for _ in range(2)]
        [x.start() for x in ts]
        [x.join() for x in ts]
        self.assertEqual(self.leaves(b), n + 1)                                                   # two concurrent admits write one leaf
        self.refresh()
        st, ev = self.admin.request("POST", f"/lc-{T}-restricted-ctl/_search", {"query": {"bool": {"filter": [{"term": {"kind": "event"}}, {"term": {"pool": b}}]}}})
        names = sorted(h["_source"]["event"] for h in ev["hits"]["hits"])
        self.assertIn("hold_show", names)
        self.assertEqual(names.count("hold_admit"), 1)
        self.assertNotIn("release me carefully", str(ev))                                         # hashes and ids only, never the text

if __name__ == "__main__":
    unittest.main()
