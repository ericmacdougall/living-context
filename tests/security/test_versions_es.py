"""BUILD-PLAN Step 13 on a real Elasticsearch: incremental insert, rebuild, versions (AC-8; SEC-V1, V2, V6, V7).

Needs LC_TEST_ES_URL, LC_TEST_CA, LC_TEST_PW; without them the module FAILS. Every check drives the real writer, placement, the
write-path watermark and the batch rebuilder with the stub model. Broken variants, each red for its stated reason: node ids
reused for different content (V1); ancestry truncated to retention across a real expiry (V2); edges visible at write (V6); a rebuild
that publishes W past a failed node (V7); a head flipped before the refresh (publication barrier).
"""
import unittest

from living_context.config import Constants
from living_context.graph import expand as ex
from living_context.graph.edges import EdgeStore, Leaf
from living_context.ids import KIND_LEAF, KIND_NODE, Pointer, encode_pointer
from living_context.ingest import title
from living_context.policy import ClassLattice
from living_context.recall.fanout import Recall
from living_context.recall.lookup import EsLeafStore, Resolver
from living_context.recall.view import EsView
from living_context.structure.publish import Publisher
from living_context.structure.retention import Retention
from tests.security.vworld import C, VersionsBase


class Versions(VersionsBase):
    T = "vers"

    # ---- V1: pointers held from every generation ----------------------------------------------------------------------
    def tree_refs(self):
        head = self.head()
        mf = self.doc("ctl", f"mf:topic:{head['gen']}")
        seen, frontier = {}, list(mf["top"])
        while frontier:
            ref = frontier.pop()
            if ref in seen:
                continue
            d = self.doc("node", ref)
            seen[ref] = d
            frontier += [f"{ch['child_id']}:{ch['child_version']}" for ch in d["children"] if not ch["is_leaf"]]
        return seen

    def hold(self, held):
        gen = self.head()["gen"]
        for ref in self.tree_refs():
            nid, nv = ref.split(":")
            held.append((KIND_NODE, gen, nid, int(nv), self.leaves_under(ref), None))
        st, r = self.admin.request("POST", f"/lc-{self.T}-base-leaf/_search", {"size": 1000, "_source": ["text"], "query": {"match_all": {}}})
        for h in r["hits"]["hits"]:
            lid, lv = h["_id"].rsplit(":", 1)
            held.append((KIND_LEAF, gen, lid, int(lv), None, h["_source"]["text"]))

    def follow(self, ids, cur, depth=0):
        """The leaves behind forwarding ids: a current node's leaves, or, when that node was itself replaced, its own forwarding."""
        got = set()
        for fid in ids:
            if fid in cur:
                got |= self.leaves_under(cur[fid])
                continue
            st, r = self.admin.request("POST", f"/lc-{self.T}-base-ctl/_search", {"size": 1, "sort": [{"gen": "asc"}], "query": {"bool": {"filter": [
                {"term": {"old_id": fid}}, {"term": {"forward": True}}]}}})
            hits = r["hits"]["hits"] if st == 200 else []
            self.assertTrue(hits and depth < 20, f"a forwarding id {fid} resolves nowhere")
            got |= self.follow(hits[0]["_source"]["new_ids"], cur, depth + 1)
        return got

    def check_pointers(self, held, expect_expired_ok):
        store = EsLeafStore(self.ingest()(), self.T, node_es=self.structure()())
        res = Resolver(store, ["base"], lambda i: False, lambda d: None)
        cur = {ref.split(":")[0]: ref for ref in self.tree_refs()}
        n = 0
        for kind, gen, pid, ver, truth, text in held:
            out = res.resolve(encode_pointer(Pointer(kind, gen, pid, ver)))
            n += 1
            st = out["status"]
            self.assertIn(st, ("ok", "superseded", "moved", "expired"), f"pointer of generation {gen} resolved to {out}")
            if kind == KIND_LEAF:
                self.assertEqual((st, out.get("text")), ("ok", text), "a leaf pointer resolved to another leaf")
            elif st == "ok":
                self.assertEqual(self.leaves_under(f"{pid}:{ver}"), truth, "a node pointer resolved to wrong content (node id reused)")
            elif st == "moved":
                got = self.follow(out["forward"], cur)
                self.assertTrue(truth <= got, "forwarding does not cover the old node's leaves")
            else:
                self.assertTrue(expect_expired_ok, "a retained generation's node pointer expired")
        return n

    def check_v1(self, **kw):
        w = self.world(consts=Constants(f_target=4, f_max=6, f_min=2, expand_hops=1, r_gen_count=1, r_gen_days=0))
        w.bootstrap(topics=8, per=4)
        held, total = [], 0
        self.hold(held)
        rounds = 6
        for t in range(rounds):
            w.add(t, 4, place=True)
            r = w.rebuilder(**kw).run()
            self.assertEqual(r.status, "published", r)
            self.hold(held)
            total += self.check_pointers(held, expect_expired_ok=False)
        Retention(w.structure(), self.T, "base", consts=w.consts).expire()
        total += self.check_pointers(held, expect_expired_ok=True)
        return total

    def test_every_pointer_held_from_an_earlier_generation_resolves_to_the_right_thing(self):
        n = self.check_v1()
        print(f"V1: {n} pointer resolutions, all ok, superseded, moved or expired")
        self.assertGreaterEqual(n, 1000)

    def test_broken_node_ids_reused_for_different_content_are_red(self):
        with self.assertRaises(AssertionError) as cm:
            self.check_v1(reuse_node_ids=True)
        self.assertIn("node id reused", str(cm.exception))

    # ---- V2: pinned consistency across a real retention expiry (R_GEN shortened), tree and edges ------------------------
    def check_expiry(self, cycles, truncate):
        w = self.world(consts=Constants(f_target=4, f_max=6, f_min=2, expand_hops=1, expand_neighbours=500, expand_leaves=500,
                                        r_gen_count=1, r_gen_days=0))
        w.bootstrap(topics=2, per=4)
        st, r = self.admin.request("POST", f"/lc-{self.T}-base-leaf/_search", {"size": 1, "_source": ["text"], "query": {"match_all": {}}})
        hub = r["hits"]["hits"][0]["_id"].rsplit(":", 1)
        hub = Leaf(hub[0], int(hub[1]), "base")
        store = EdgeStore(w.structure(), self.T, ClassLattice(["base"], []))
        expander = ex.Expander(lambda c: self.gate.client(self.T, "read-base"), self.T, consts=w.consts)
        edge_index = f"lc-{self.T}-base-edge"
        for n in range(cycles):
            adopted = w.add(n % 2, 1, place=True)[0]
            g = w.rebuilder().run()
            self.assertEqual(g.status, "published", g)
            ad = store.link(hub, Leaf(adopted.leaf_id, adopted.lv, "base"), "continues", gen_from=g.gen)      # adopted at gen g
            late = w.add(n % 2, 1, place=True)[0]
            ret = store.link(hub, Leaf(late.leaf_id, late.lv, "base"), "continues", src_seq=late.src_seq)    # write-path edge
            w.wp.advance()
            g2 = w.rebuilder().run()
            self.assertEqual(g2.status, "published", g2)
            self.assertTrue(store.retire(edge_index, ret[0], g2.gen))                                         # retired by gen g+1
            for _ in range(2):          # two more adopted generations push g and g+1 out of R_GEN
                w.add(n % 2, 1, place=True)
                self.assertEqual(w.rebuilder().run().status, "published")
            self.admin.request("POST", f"/{edge_index}/_refresh")
            expired = Retention(w.structure(), self.T, "base", consts=w.consts, ancestry_truncate=truncate).expire()
            self.assertIn(g.gen, expired, "the generation did not expire")
            head = self.head()
            pin = ex.Pin(w.wp.read_p(), tuple(head["ancestry"]), head["gen"])
            seen = [l.leaf_id for l in expander.expand([hub.leaf_id], ["base"], pin)]
            self.assertIn(adopted.leaf_id, seen, f"expiry {n}: the adopted edge vanished after its generation expired")
            self.assertNotIn(late.leaf_id, seen, f"expiry {n}: the retired edge came back after its retiring generation expired")

    def test_adopted_edges_stay_and_retired_edges_stay_hidden_across_20_real_expiries(self):
        self.check_expiry(20, truncate=False)

    def test_broken_ancestry_truncated_to_retention_is_red(self):
        with self.assertRaises(AssertionError) as cm:
            self.check_expiry(20, truncate=True)
        self.assertRegex(str(cm.exception), "adopted edge vanished|retired edge came back")

    # ---- V6: candidate isolation -----------------------------------------------------------------------------------------
    def check_candidate(self, ignore_pin):
        w = self.world()
        w.bootstrap(topics=2, per=4)
        st, r = self.admin.request("POST", f"/lc-{self.T}-base-leaf/_search", {"size": 2, "_source": ["text"], "query": {"match_all": {}}})
        (a, av), (b, bv) = [h["_id"].rsplit(":", 1) for h in r["hits"]["hits"]]
        pub = Publisher(w.structure(), self.T, "base")
        store = EdgeStore(w.structure(), self.T, ClassLattice(["base"], []))
        e = ex.Expander(lambda c: self.gate.client(self.T, "read-base"), self.T, consts=w.consts, ignore_pin=ignore_pin)
        head = pub.head()
        cand = pub.next_gen()
        store.link(Leaf(a, int(av), "base"), Leaf(b, int(bv), "base"), "continues", gen_from=cand)
        self.admin.request("POST", f"/lc-{self.T}-*/_refresh")
        before = ex.Pin(w.wp.read_p(), tuple(head["ancestry"]), head["gen"])
        self.assertEqual(e.expand([a], ["base"], before), [], "a candidate's edge is visible before the gate adopts it")
        # adoption: the head flips to a generation whose ancestry holds the candidate
        hd = pub._base("head", lin_nodes=["none"], family="topic", gen=cand, ancestry=sorted({*head["ancestry"], cand}))
        st, _ = pub._put("ctl", "hd:topic", hd, cas=(head["_seq_no"], head["_primary_term"]))
        self.assertEqual(st, 200)
        self.admin.request("POST", f"/lc-{self.T}-base-ctl/_refresh")
        new = pub.head()
        after = ex.Pin(w.wp.read_p(), tuple(new["ancestry"]), new["gen"])
        self.assertEqual([l.leaf_id for l in e.expand([a], ["base"], after)], [b])
        self.assertEqual(e.expand([a], ["base"], before), [], "an adopted candidate changed an earlier task's view")
        # rejection: a second candidate's edge and members disappear
        cand2 = pub.next_gen()
        store.link(Leaf(b, int(bv), "base"), Leaf(a, int(av), "base"), "same_entity", gen_from=cand2)
        Retention(w.structure(), self.T, "base", consts=w.consts).discard_generation(cand2)
        self.admin.request("POST", f"/lc-{self.T}-*/_refresh")
        st, r = self.admin.request("POST", f"/lc-{self.T}-base-edge/_count", {"query": {"term": {"gen_from": cand2}}})
        self.assertEqual(r["count"], 0, "a rejected candidate's edge survived")

    def test_candidate_edges_are_invisible_until_adopted_and_gone_when_rejected(self):
        self.check_candidate(ignore_pin=False)

    def test_broken_edges_visible_at_write_are_red(self):
        with self.assertRaises(AssertionError) as cm:
            self.check_candidate(ignore_pin=True)
        self.assertIn("before the gate adopts it", str(cm.exception))

    # ---- V7: a failed build call publishes nothing and the pending leaf stays reachable -----------------------------------
    def tree_only_ids(self, text):
        view = EsView(self.gate.client(self.T, "read-base"), self.T, "base", "topic")
        res = Recall(self.port(), self.T, "base", "px", consts=C).recall(title.build_title(text), view)
        from living_context.ids import decode_pointer
        return [decode_pointer(p["pointer"]).id for p in res.digest["sources"]["tree"]], res

    def check_failed_call(self, cycles, **kw):
        from tests.security.vworld import FailBackend
        be = FailBackend()
        w = self.world(backend=be)
        w.bootstrap(topics=3, per=4)
        for n in range(cycles):
            be.fail = True
            leaf = w.add(n % 3, 1, extra=" zzfailzz", place=True)[0]
            before = self.head()["gen"]
            r = w.rebuilder(**kw).run()
            if not kw:
                self.assertEqual((r.status, self.head()["gen"]), ("aborted", before), "a generation was published past a failed call")
            st, hit = self.admin.request("GET", f"/lc-{self.T}-base-leaf/_doc/{leaf.leaf_id}:{leaf.lv}")
            ids, _ = self.tree_only_ids(hit["_source"]["text"])
            self.assertIn(leaf.leaf_id, ids, f"cycle {n}: the pending leaf dropped out of tree-only recall after a failed rebuild")
            be.fail = False
            self.assertEqual(w.rebuilder().run().status, "published")   # the next batch summarises it

    def test_a_failed_build_call_aborts_the_generation_and_the_leaf_stays_pending_100_cycles(self):
        self.check_failed_call(100)

    def test_broken_rebuild_publishing_w_past_a_failed_node_is_red(self):
        with self.assertRaises(AssertionError) as cm:
            self.check_failed_call(100, publish_w_past_failed=True)
        self.assertIn("dropped out of tree-only recall", str(cm.exception))

    # ---- a leaf written during a rebuild -----------------------------------------------------------------------------------
    def test_a_leaf_is_found_directly_at_once_pending_in_the_tree_once_p_covers_it_and_with_a_gist_after_the_batch(self):
        w = self.world()
        w.bootstrap(topics=3, per=4)
        leaf = w.add(1, 1)[0]
        text = self.admin.request("GET", f"/lc-{self.T}-base-leaf/_doc/{leaf.leaf_id}:{leaf.lv}")[1]["_source"]["text"]
        self.admin.request("POST", f"/lc-{self.T}-base-leaf/_refresh")
        st, r = self.gate.client(self.T, "read-base").request("POST", f"/lc-{self.T}-base-leaf/_search", {"query": {"match": {"text": text}}, "size": 5})
        self.assertIn(leaf.leaf_id, [h["_id"].rsplit(":", 1)[0] for h in r["hits"]["hits"]], "the direct search does not find a fresh leaf")
        ids, _ = self.tree_only_ids(text)
        self.assertNotIn(leaf.leaf_id, ids, "the tree shows a leaf before P covers it")
        w.settle()
        ids, res = self.tree_only_ids(text)
        self.assertIn(leaf.leaf_id, ids, "the pending member is not reached once P covers it")
        self.assertEqual(w.rebuilder().run().status, "published")
        view = EsView(self.gate.client(self.T, "read-base"), self.T, "base", "topic")
        entries = [e for ref in view.top() for e in self.walk(view, ref) if e["child_id"] == leaf.leaf_id]
        self.assertTrue(entries and entries[0]["gist"] and not entries[0].get("pending"), "after the batch the leaf has no gist")
        self.assertEqual(entries[0]["title"], title.build_title(text), "the title is not the code-built title")

    def walk(self, view, ref):
        for e in view.entries(ref):
            if e["is_leaf"]:
                yield e
            else:
                yield from self.walk(view, f"{e['child_id']}:{e['child_version']}")

    # ---- the publication barrier for a rebuild's head flip -----------------------------------------------------------------
    def check_flip_barrier(self, **kw):
        w = self.world()
        w.bootstrap(topics=2, per=4)
        w.add(0, 1, place=True)
        seen = []

        def pause(step):
            if step == "refreshed":
                st, r = self.admin.request("POST", f"/lc-{self.T}-base-node/_count", {"query": {"match_all": {}}})
                seen.append(r["count"])

        before = self.admin.request("POST", f"/lc-{self.T}-base-node/_count", {"query": {"match_all": {}}})[1]["count"]
        self.admin.request("PUT", f"/lc-{self.T}-base-node/_settings", {"index": {"refresh_interval": "-1"}})
        try:
            w.rebuilder(pause=pause, **kw).run()
        finally:
            self.admin.request("PUT", f"/lc-{self.T}-base-node/_settings", {"index": {"refresh_interval": "1s"}})
        self.assertGreater(seen[0], before, "the head flipped before the new node versions were searchable")

    def test_the_head_flips_only_after_the_refresh_barrier(self):
        self.check_flip_barrier()

    def test_broken_flip_before_the_refresh_is_red(self):
        with self.assertRaises(AssertionError) as cm:
            self.check_flip_barrier(refresh_before_flip=False)
        self.assertIn("before the new node versions were searchable", str(cm.exception))


if __name__ == "__main__":
    unittest.main()
