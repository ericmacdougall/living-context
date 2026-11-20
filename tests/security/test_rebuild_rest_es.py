"""BUILD-PLAN Step 13 remainder on a real Elasticsearch (stub model only; the module FAILS without LC_TEST_ES_URL, LC_TEST_CA,
LC_TEST_PW).

Each mechanism has a check and a broken variant that turns it red for its stated reason:
  * the tombstone check before the head flip (broken: the flip without the check publishes a generation holding an erased leaf);
  * a pending-new level-1 node in a tree taller than one level (built: the leaf is in the tree once the batch ran);
  * merge of an under-full node (broken: the merge forgets the absorbed node's leaves), collapse of a single-child chain;
  * the evaluation-only pin (broken: the pin ignored, a planted leaf is re-clustered away from its donor);
  * member cleanup at retention expiry (broken: no cleanup; broken: members deleted by the generation that wrote them);
  * V7, a placement member written while a candidate is open (100 cycles; broken: the member stamped with the candidate's number).
"""
import collections
import tempfile
import unittest
from pathlib import Path

from living_context.erasure.hold import HoldRegistry
from living_context.config import Constants
from living_context.erasure.executor import Executor, State
from living_context.gateway.fence import SendFence
from living_context.gateway.gate import WriteGate
from living_context.gateway.journal import TombstoneJournal
from living_context.ingest import title
from living_context.ids import decode_pointer
from living_context.recall.fanout import Recall
from living_context.recall.view import EsView
from living_context.structure.placement import Placer
from living_context.structure.publish import Publisher
from living_context.structure.retention import Retention
from tests.security.vworld import C, VersionsBase, leaf_text

NOVEL = "zqxv wjkp qqzx xwvq jjkz kkqz vvxq"


class RebuildRest(VersionsBase):
    T = "vrst"
    _n = 0

    def setUp(self):
        RebuildRest._n += 1
        self.wgate, self.fence, self.state = WriteGate(), SendFence(), State()
        d = Path(tempfile.mkdtemp(dir=self.root))
        self.journal = TombstoneJournal(d / "j" / "t.log", d / "r" / "t.log")
        self.email = f"rrest{RebuildRest._n}x{id(self) % 9973}@example.com"

    def executor(self):
        return Executor(lambda: self.gate.client(self.T, "erase"), self.structure(), self.T, self.ks, self.journal, self.wgate,
                        self.fence, self.state, holds=HoldRegistry(), purge_deadline_s=2, purge_interval_s=1, es_log=lambda: self.gate.client(self.T, "log"))

    # ---- helpers ------------------------------------------------------------------------------------------------------
    def top_refs(self, gen=None):
        gen = gen or self.head()["gen"]
        return list(self.doc("ctl", f"mf:topic:{gen}")["top"])

    def l1_homes(self, gen=None):
        """Leaf id -> how many level-1 nodes of the generation's tree hold it; and the node ids of every level-1 node."""
        homes, l1, frontier, seen = collections.Counter(), {}, self.top_refs(gen), set()
        while frontier:
            ref = frontier.pop()
            if ref in seen:
                continue
            seen.add(ref)
            d = self.doc("node", ref)
            if d["level"] == 1:
                l1[ref] = [ch["child_id"] for ch in d["children"]]
                homes.update(l1[ref])
            else:
                frontier += [f"{ch['child_id']}:{ch['child_version']}" for ch in d["children"]]
        return homes, l1

    def add_text(self, w, text, place=True):
        # a placer whose thresholds nothing clears: the leaf starts a pending-new node whatever the embedding says
        w.placer = Placer(w.structure(), self.T, "base", w.wp, consts=w.consts, theta=(1.1,))
        res = w.writer.remember("tok", "base", text)
        self.assertTrue(res.ok, res.status)
        w.new.append((res, text))
        w.count += 1
        if place:
            w.settle([(res, text)])
        w.placer = Placer(w.structure(), self.T, "base", w.wp, consts=w.consts)
        return res

    def tree_only_ids(self, text):
        view = EsView(self.gate.client(self.T, "read-base"), self.T, "base", "topic")
        res = Recall(self.port(), self.T, "base", "px", consts=C).recall(title.build_title(text), view)
        return [decode_pointer(p["pointer"]).id for p in res.digest["sources"]["tree"]]

    def members_of(self, parent_ids):
        self.admin.request("POST", f"/lc-{self.T}-base-member/_refresh")
        st, r = self.admin.request("POST", f"/lc-{self.T}-base-member/_count", {"query": {"terms": {"parent_id": sorted(parent_ids)}}})
        return r["count"]

    # ---- the tombstone check before the head flip ---------------------------------------------------------------------
    def check_tombstone(self, **kw):
        w = self.world()
        subject = self.ks.subject_token(self.email)
        w.bootstrap(topics=6, per=4)
        gone = {r.leaf_id for r in w.add(0, 2, extra=f" {self.email}", place=True)}
        w.add(3, 1, place=True)

        def pause(step):
            if step == "planned":   # the erasure lands after the plan (which holds the leaves' text) and before the commit
                self.executor().erase_subject(subject, apply_guard=lambda: self.guard.erase_identifier(self.email))

        before = self.head()["gen"]
        r = w.rebuilder(gate=self.wgate, state=self.state, pause=pause, **kw).run()
        self.assertEqual(self.state.seq, 1, "the erasure did not run")
        self.admin.request("POST", f"/lc-{self.T}-*/_refresh")
        if self.head()["gen"] != before:
            homes, _ = self.l1_homes()
            self.assertFalse(gone & set(homes), "a generation containing an erased item was flipped")
        st, c = self.admin.request("POST", f"/lc-{self.T}-base-node/_count", {"query": {"terms": {"lin_leaf_ids": sorted(gone)}}})
        self.assertEqual(c["count"], 0, "a node version holding an erased leaf was left in the index")
        return r, before

    def test_a_tombstone_published_after_the_plan_aborts_the_rebuild_before_the_flip(self):
        r, before = self.check_tombstone()
        self.assertEqual((r.status, self.head()["gen"]), ("aborted", before))
        self.assertIn("tombstone", r.reason)

    def test_broken_flip_without_the_tombstone_check_is_red(self):
        with self.assertRaises(AssertionError) as cm:
            self.check_tombstone(check_tombstones=False)
        self.assertRegex(str(cm.exception), "erased item was flipped|erased leaf was left")

    def test_the_next_rebuild_after_an_aborted_one_publishes_without_the_erased_leaves(self):
        w = self.world()
        subject = self.ks.subject_token(self.email)
        w.bootstrap(topics=4, per=4)
        keep = w.add(1, 1, place=True)[0]
        gone = {r.leaf_id for r in w.add(0, 2, extra=f" {self.email}", place=True)}
        done = []
        w.rebuilder(gate=self.wgate, state=self.state, pause=lambda s: done.append(self.executor().erase_subject(
            subject, apply_guard=lambda: self.guard.erase_identifier(self.email))) if s == "planned" and not done else None).run()
        r = w.rebuilder(gate=self.wgate, state=self.state).run()
        self.assertEqual(r.status, "published", r)
        homes, _ = self.l1_homes()
        self.assertIn(keep.leaf_id, homes)
        self.assertFalse(gone & set(homes))

    # ---- a pending-new node in a taller tree --------------------------------------------------------------------------
    def check_tall_tree_pending_new(self, **kw):
        w = self.world()
        w.bootstrap(topics=8, per=4)
        self.assertGreaterEqual(max(self.doc("node", r)["level"] for r in self.top_refs()), 2, "the premise: a tall tree")
        before, _ = self.l1_homes()
        res = self.add_text(w, NOVEL)
        st, r = self.admin.request("POST", f"/lc-{self.T}-base-member/_search", {"query": {"term": {"child_id": f"{res.leaf_id}:{res.lv}"}}})
        self.assertEqual([h["_source"]["parent_id"] for h in r["hits"]["hits"]], ["pendingnew"], "the premise: it cleared no node")
        rb = w.rebuilder(**kw)
        out = rb.run()
        self.assertEqual(out.status, "published", out)
        roots = {self.doc("node", r)["level"] for r in self.top_refs()}
        self.assertTrue(min(roots) >= 2, f"a new level-1 node was left beside the upper nodes (root levels {sorted(roots)}): it was not adopted")
        homes, _ = self.l1_homes()
        self.assertEqual(homes[res.leaf_id], 1, "the new leaf is not in the tree once")
        self.assertEqual({k for k in before}, {k for k in homes} - {res.leaf_id}, "a leaf was lost or duplicated")
        self.assertTrue(all(v == 1 for v in homes.values()))
        self.assertIn(res.leaf_id, self.tree_only_ids(NOVEL), "tree-only recall does not reach the adopted leaf")

    def test_a_leaf_that_cleared_no_node_joins_a_tree_taller_than_one_level(self):
        self.check_tall_tree_pending_new()

    def test_broken_rebuild_without_adoption_leaves_a_level_one_root_beside_the_upper_nodes(self):
        with self.assertRaises(AssertionError) as cm:
            self.check_tall_tree_pending_new(no_adoption=True)
        self.assertIn("was not adopted", str(cm.exception))

    # ---- merge of an under-full node, collapse of a single-child chain ---------------------------------------------------
    def check_merge(self, **kw):
        loose = Constants(f_target=4, f_max=6, f_min=1, expand_hops=1)
        w = self.world(consts=loose)
        w.bootstrap(topics=4, per=4)
        res = self.add_text(w, NOVEL)
        self.assertEqual(w.rebuilder().run().status, "published")
        homes0, l1 = self.l1_homes()
        small = [ref for ref, ids in l1.items() if len(ids) == 1]
        self.assertEqual(len(small), 1, "the premise: one under-full node")
        gen0 = self.head()["gen"]
        w.consts = Constants(f_target=4, f_max=6, f_min=2, expand_hops=1)
        w.add(0, 1, place=True)
        rb = w.rebuilder(**kw)
        out = rb.run()
        self.assertEqual(out.status, "published", out)
        self.assertEqual(rb.merges, 1)
        homes, l1n = self.l1_homes()
        for lid in homes0:
            self.assertEqual(homes.get(lid), 1, "a leaf was lost or duplicated by the merge")
        self.assertNotIn(small[0].split(":")[0], {r.split(":")[0] for r in l1n}, "the under-full node is still in the tree")
        self.assertTrue(all(len(v) >= 2 for v in l1n.values()))
        # the earlier generation is untouched, and the old id forwards
        self.assertEqual(self.l1_homes(gen0)[0], homes0, "the merge changed an earlier generation")
        fw = self.doc("ctl", f"fw:topic:{self.head()['gen']}:{small[0].split(':')[0]}")
        self.assertTrue(fw and fw["new_ids"], "no forwarding record for the merged node")
        return res

    def test_an_under_full_node_merges_into_its_most_similar_sibling(self):
        self.check_merge()

    def test_broken_merge_that_forgets_the_absorbed_leaves_is_red(self):
        with self.assertRaises(AssertionError) as cm:
            self.check_merge(merge_drops_members=True)
        self.assertIn("lost or duplicated by the merge", str(cm.exception))

    def check_collapse(self, **kw):
        c2 = Constants(f_target=2, f_max=3, f_min=2, expand_hops=1)
        w = self.world(consts=c2)
        w.bootstrap(topics=5, per=2)
        tops = {r: self.doc("node", r) for r in self.top_refs()}
        single = [r for r, d in tops.items() if d["level"] == 2 and len(d["children"]) == 1]
        self.assertEqual(len(single), 1, "the premise: a one-child upper node in the first tree")
        gen0 = self.head()["gen"]
        homes0, _ = self.l1_homes()
        child = tops[single[0]]["children"][0]
        leaf = self.doc("node", f"{child['child_id']}:{child['child_version']}")["children"][0]["child_id"]
        text = self.admin.request("GET", f"/lc-{self.T}-base-leaf/_doc/{leaf}:1")[1]["_source"]["text"]
        topic = "abcdefghijklmnopqrstuvwxyz".index(text.split("topic")[-1][0])
        w.add(topic, 1, place=True)
        rb = w.rebuilder(**kw)
        out = rb.run()
        self.assertEqual(out.status, "published", out)
        homes, _ = self.l1_homes()
        self.assertEqual(set(homes) - set(homes0) and len(set(homes) - set(homes0)), 1)
        self.assertTrue(all(v == 1 for v in homes.values()))
        new_top = {r: self.doc("node", r) for r in self.top_refs()}
        self.assertNotIn(single[0], new_top)
        self.assertTrue(any(d["level"] == 1 for d in new_top.values()), "the lone child did not take the node's place")
        self.assertEqual(rb.collapses, 1)
        self.assertEqual(self.l1_homes(gen0)[0], homes0, "the collapse changed an earlier generation")
        fw = self.doc("ctl", f"fw:topic:{self.head()['gen']}:{single[0].split(':')[0]}")
        self.assertTrue(fw and fw["new_ids"], "no forwarding record for the collapsed node")
        # the mixed-level frontier still places and rebuilds
        again = w.add(topic, 1, place=True)[0]
        self.assertEqual(w.rebuilder().run().status, "published")
        self.assertEqual(self.l1_homes()[0][again.leaf_id], 1)

    def test_a_single_child_chain_collapses_and_the_tree_stays_placeable(self):
        self.check_collapse()

    def test_broken_rebuild_that_keeps_a_single_child_chain_is_red(self):
        with self.assertRaises(AssertionError) as cm:
            self.check_collapse(no_collapse=True)
        self.assertIn("did not take the node's place", str(cm.exception))

    # ---- the evaluation-only pin ------------------------------------------------------------------------------------------
    def overflow_world(self, pins_by_text=None, **kw):
        """Topic 1's node gets 3 more leaves (7 members, over F_MAX): the rebuild splits it. Returns {text: new level-1 node}."""
        w = self.world()
        w.bootstrap(topics=4, per=4)
        ress = w.add(1, 3, place=True)
        texts = {r.leaf_id: self.admin.request("GET", f"/lc-{self.T}-base-leaf/_doc/{r.leaf_id}:{r.lv}")[1]["_source"]["text"] for r in ress}
        pins = {}
        if pins_by_text is not None:
            by_text = {t: i for i, t in texts.items()}
            for a, b in pins_by_text:
                pins[by_text[a]] = by_text[b]
        rb = w.rebuilder(pins=pins, **kw)
        self.assertEqual(rb.run().status, "published")
        _, l1 = self.l1_homes()
        node_of = {lid: ref for ref, ids in l1.items() for lid in ids}
        return {texts[lid]: node_of[lid] for lid in texts}, texts

    def test_a_pinned_leaf_is_not_reclustered_away_from_its_donor_by_a_split(self):
        placed, _ = self.overflow_world()
        groups = collections.defaultdict(list)
        for t, n in placed.items():
            groups[n].append(t)
        self.assertGreaterEqual(len(groups), 2, "the premise: the split separated the new leaves")
        (ga, gb) = list(groups.values())[:2]
        plant, donor = ga[0], gb[0]
        pinned, _ = self.overflow_world(pins_by_text=[(plant, donor)])
        self.assertEqual(pinned[plant], pinned[donor], "a pinned leaf was moved away from its donor")

    def test_broken_pin_ignored_is_red(self):
        placed, _ = self.overflow_world()
        groups = collections.defaultdict(list)
        for t, n in placed.items():
            groups[n].append(t)
        (ga, gb) = list(groups.values())[:2]
        plant, donor = ga[0], gb[0]
        with self.assertRaises(AssertionError) as cm:
            pinned, _ = self.overflow_world(pins_by_text=[(plant, donor)], ignore_pins=True)
            self.assertEqual(pinned[plant], pinned[donor], "a pinned leaf was moved away from its donor")
        self.assertIn("moved away from its donor", str(cm.exception))

    # ---- member cleanup at retention expiry ---------------------------------------------------------------------------------
    def check_cleanup(self, **kw):
        w = self.world(consts=Constants(f_target=4, f_max=6, f_min=2, expand_hops=1, r_gen_count=1, r_gen_days=0))
        w.bootstrap(topics=4, per=4)
        w.add(1, 3, place=True)
        before = self.head()["gen"]
        old_nodes = set(self.doc("ctl", f"mf:topic:{before}")["top"])
        self.assertEqual(w.rebuilder().run().status, "published")
        top = self.top_refs()
        live_ids = {r.split(":")[0] for r in top}
        gone_ids = {r.split(":")[0] for r in old_nodes} - live_ids
        self.assertTrue(gone_ids, "the premise: the split replaced a node")
        dead = self.members_of(gone_ids)
        live = self.members_of(live_ids)
        self.assertGreater(dead, 0, "the premise: the replaced node has members")
        Retention(w.structure(), self.T, "base", consts=w.consts, **kw).expire()
        self.assertEqual(self.members_of(live_ids), live, "members of a live node were deleted")
        self.assertEqual(self.members_of(gone_ids), 0, "the members of a node removed at expiry remain")
        self.assertEqual(self.l1_homes()[0], self.l1_homes(self.head()["gen"])[0])

    def test_members_of_nodes_removed_at_retention_expiry_are_removed(self):
        self.check_cleanup()

    def test_broken_no_member_cleanup_is_red(self):
        with self.assertRaises(AssertionError) as cm:
            self.check_cleanup(keep_dead_members=True)
        self.assertIn("members of a node removed at expiry remain", str(cm.exception))

    def test_broken_members_deleted_by_their_generation_are_red(self):
        with self.assertRaises(AssertionError) as cm:
            self.check_cleanup(members_by_gen_from=True)
        self.assertIn("members of a live node were deleted", str(cm.exception))

    # ---- V7: a placement member written while a candidate is open -----------------------------------------------------
    def check_v7(self, cycles, stamp=False):
        w = self.world()
        w.bootstrap(topics=3, per=4)
        pub = Publisher(w.structure(), self.T, "base")
        for n in range(cycles):
            adopt = n % 2 == 0
            seen = {}

            def pause(step, n=n, seen=seen):
                if step == "candidate" and "id" not in seen:   # the candidate is written and not yet flipped
                    cand = int(self.doc("ctl", "gc:topic")["value"])
                    if stamp:
                        w.placer = Placer(w.structure(), self.T, "base", w.wp, consts=w.consts, stamp_gen=cand)
                    res = self.similar_leaf(w, n % 3, f"hk{n}", place=False)
                    seen["id"] = res.leaf_id
                    seen["cand"] = cand
                    w.settle()
                    seen["parents"] = [h["_source"]["parent_id"] for h in self.admin.request("POST", f"/lc-{self.T}-base-member/_search", {
                        "query": {"term": {"child_id": f"{res.leaf_id}:{res.lv}"}}})[1]["hits"]["hits"]]

            self.similar_leaf(w, n % 3, f"pre{n}", place=True)
            rb = w.rebuilder(pause=pause, abort_if=(None if adopt else (lambda: True)))
            out = rb.run()
            self.assertEqual(out.status, "published" if adopt else "aborted", out)
            if not adopt:   # the gate rejects the candidate: what it owns goes, by its number
                Retention(w.structure(), self.T, "base", consts=w.consts).discard_generation(seen["cand"])
            self.admin.request("POST", f"/lc-{self.T}-*/_refresh")
            view = EsView(self.gate.client(self.T, "read-base"), self.T, "base", "topic")
            pend = {e["child_id"] for ref in view.top() for e in self.pend_under(view, ref)}
            self.assertIn(seen["id"], pend | set(self.l1_homes()[0]),
                          f"cycle {n}: a placement member written while a candidate was open is stranded (adopt={adopt}, parents={seen.get('parents')}, {out.status})")
        return cycles

    def similar_leaf(self, w, topic, tag, place):
        """A leaf whose text is a bootstrapped leaf's plus a tag word, so placement always finds a level-1 node for it."""
        text = leaf_text(topic, 0) + f" {tag}"
        res = w.writer.remember("tok", "base", text)
        self.assertTrue(res.ok, res.status)
        w.new.append((res, text))
        w.count += 1
        if place:
            w.settle([(res, text)])
        return res

    def pend_under(self, view, ref):
        for e in view.entries(ref):
            if e["is_leaf"]:
                yield e
            else:
                yield from self.pend_under(view, f"{e['child_id']}:{e['child_version']}")

    def test_v7_a_placement_member_written_while_a_candidate_is_open_stays_visible_100_cycles(self):
        self.assertEqual(self.check_v7(100), 100)

    def test_broken_v7_member_stamped_with_the_candidates_number_is_red(self):
        with self.assertRaises(AssertionError) as cm:
            self.check_v7(6, stamp=True)
        self.assertIn("stranded", str(cm.exception))


if __name__ == "__main__":
    unittest.main()
