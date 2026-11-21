"""Pools delta review 1 (goodqa#16441, finding F4, class C3): every mutation of a pool leaf's head (correct, supersede, retract) is one
expected-version-and-status claim on the head, so a retract never loses to a paused correct, a supersede that crashes never leaves two
live leaves, and an edit after a crashed one never pairs one text's source with another's projection. Paused senders are events; a crash
is `os._exit` in a second process. Needs LC_TEST_ES_URL, LC_TEST_CA and LC_TEST_PW."""
import base64
import threading
import unittest

from living_context.gateway import pointers
from living_context.gateway.indices import pool_index
from living_context.gateway.pool_tools import PoolTools
from living_context.ingest.subjects import Guard, TokenSource
from living_context.ingest.writer import Writer
from tests.security.test_pools_delta_es import Child
from tests.security.test_pools_es import LAT, Base, FailingModel, T

OPS = {"query", "save", "update", "retract"}


class Leaf(Base):
    def paused(self, step):
        """A second request handler (its own PoolTools) whose writer stops at `step` until released."""
        at, go, fired = threading.Event(), threading.Event(), []

        def pause(name):
            if name == step and not fired:
                fired.append(name)
                at.set()
                go.wait(30)
        w = Writer(lambda: self.cc("ingest"), self.ks, T, Guard(TokenSource(self.ks)), reader=self.reader, gate=self.wgate, pause=pause)
        tools = PoolTools(T, self.dir, self.sessions, LAT, w, self.reader, lambda cls: self.cc(f"read-{cls}"), self.keys, consts=self.consts,
                          model=FailingModel())
        return tools, at, go

    def child(self, a, request, crash):
        pool = self.dir.get(T, a)
        return Child(self).run(op="pool_update", pool={"id": a, "cls": "base", "label": pool.label}, who="alice", scope=sorted(OPS),
                               principals=[["user-alice", ["base"], ["base"]]], grants=[["user-alice", sorted(OPS)]], request=request,
                               crash=crash)

    def source_and_projection(self, a, leaf):
        head = self.reader.get("base", "ctl", f"lh:{a}:{leaf}", a)
        lv = int(head.source["current_lv"])
        st, src = self.admin.request("GET", f"/lc-{T}-src/_doc/{leaf}:{lv}")
        s = src["_source"]
        text = self.ks.decrypt(leaf, lv, base64.b64decode(s["ct"]), base64.b64decode(s["nonce"])).decode()
        st, proj = self.admin.request("GET", f"/{pool_index(T, 'base', a, 'leaf')}/_doc/{leaf}:{lv}")
        return lv, text, proj["_source"]["text"] if st == 200 else None

    def test_f4_a_retract_during_a_paused_correct_never_leaves_the_retracted_item_live(self):
        a = self.pool("alpha", ops=OPS)
        tok = self.mint("alice", {a: OPS})
        s = self.tools.save(tok, {"pool": a, "text": "the gannet roost is on the stack", "key": "k"})
        self.assertTrue(s["ok"], s)
        other, at, go = self.paused("source")
        out = []
        th = threading.Thread(target=lambda: out.append(other.update(tok, {"pointer": s["pointer"], "mode": "correct",
                                                                           "text": "the gannet roost is on the arch", "key": "u"})))
        th.start()
        self.assertTrue(at.wait(30))                                       # the correct read the live head and is writing version 2
        r = self.tools.retract(tok, {"pointers": [s["pointer"]], "reason": "wrong", "key": "r"})   # another handler, the same leaf
        go.set()
        th.join(60)
        self.assertFalse(th.is_alive())
        u = out[0]
        retracted = r.get("results") == ["retracted"]
        self.assertFalse(retracted and u.get("ok"), ("both the retract and the correct answered success", r, u))
        if retracted:
            self.assertEqual(self.tools.fetch(tok, {"pointer": s["pointer"]})["status"], "retracted")
            self.refresh()
            self.assertEqual(self.tools.query(tok, {"query": "gannet roost"})["items"], [])
        if u.get("ok"):
            self.assertEqual(self.tools.fetch(tok, {"pointer": u["pointer"]})["status"], "ok")
        lv, src, proj = self.source_and_projection(a, pointers.open_(self.keys, T, s["pointer"]).id)
        self.assertEqual(src, proj)

    def test_f4_a_supersede_that_crashes_in_another_process_never_leaves_two_live_leaves(self):
        a = self.pool("alpha", ops=OPS)
        tok = self.mint("alice", {a: OPS})
        s = self.tools.save(tok, {"pool": a, "text": "tern colony on the east cliff", "key": "k"})
        self.assertTrue(s["ok"], s)
        self.child(a, {"pointer": s["pointer"], "mode": "supersede", "text": "tern colony on the west cliff", "key": "u"}, crash="queued")
        self.refresh()
        live = self.tools.query(tok, {"query": "tern colony cliff"})["items"]
        self.assertLessEqual(len(live), 1, ("an unacknowledged supersede left two live leaves", live))
        self.writer.repair(["base"])                                       # the restart's recovery finishes or undoes it
        self.refresh()
        live = self.tools.query(tok, {"query": "tern colony cliff"})["items"]
        self.assertEqual(len(live), 1, live)

    def test_f4_an_edit_after_a_correct_crashed_past_its_source_never_pairs_two_texts(self):
        a = self.pool("alpha", ops=OPS)
        tok = self.mint("alice", {a: OPS})
        s = self.tools.save(tok, {"pool": a, "text": "puffin burrow count is twelve", "key": "k"})
        self.assertTrue(s["ok"], s)
        self.child(a, {"pointer": s["pointer"], "mode": "correct", "text": "puffin burrow count is thirteen", "key": "u1"}, crash="source")
        u = self.tools.update(tok, {"pointer": s["pointer"], "mode": "correct", "text": "puffin burrow count is fourteen", "key": "u2"})
        self.refresh()
        lv, src, proj = self.source_and_projection(a, pointers.open_(self.keys, T, s["pointer"]).id)
        self.assertEqual(src, proj, ("the source and the projection of the live version hold different texts", u))
        if u.get("ok"):
            self.assertEqual(src, "puffin burrow count is fourteen")
        else:
            self.assertEqual(u.get("error"), "version_conflict", u)


if __name__ == "__main__":
    unittest.main()
