"""BUILD-PLAN Step 13 on a real Elasticsearch: concurrent generations and rollback (AC-8; SEC-V4).

Needs LC_TEST_ES_URL, LC_TEST_CA, LC_TEST_PW; without them the module FAILS. Two builders whose lease looks expired to the second
race to flip the head (both pause after the refresh barrier, so both have read the same head): one manifest becomes current, the
other is discarded with its nodes and members, no leaf is lost, in 100 of 100 rounds. Rollback restores the previous tree and
edges exactly, also after a retention expiry removed the generations the surviving edges were adopted in. Broken variants: a
flip without compare-and-set; a rollback that keeps the candidate in the ancestry (its edges stay visible).
"""
import threading
import time

from living_context.config import Constants
from living_context.graph import expand as ex
from living_context.graph.edges import EdgeStore, Leaf
from living_context.policy import ClassLattice
from living_context.structure.retention import Retention
from tests.security.vworld import VersionsBase

ROUNDS = 100


class Candidates(VersionsBase):
    T = "cand"

    def all_nodes(self):
        self.admin.request("POST", f"/lc-{self.T}-base-node/_refresh")
        st, r = self.admin.request("POST", f"/lc-{self.T}-base-node/_search", {"size": 5000, "_source": False, "query": {"match_all": {}}})
        return {h["_id"] for h in r["hits"]["hits"]}

    def manifests(self):
        self.admin.request("POST", f"/lc-{self.T}-base-ctl/_refresh")
        st, r = self.admin.request("POST", f"/lc-{self.T}-base-ctl/_search", {"size": 1000, "query": {"bool": {"filter": [
            {"term": {"kind": "manifest"}}, {"exists": {"field": "created_nodes"}}]}}})
        return [h["_source"] for h in r["hits"]["hits"]]

    def check_race(self, rounds, use_cas):
        w = self.world()
        w.bootstrap(topics=3, per=4)
        written = [r.leaf_id for r, _ in []]
        for n in range(rounds):
            written += [r.leaf_id for r in w.add(n % 3, 1, place=True)]
            barrier = threading.Barrier(2)
            out = {}

            def meet(step):
                if step == "refreshed":
                    try:
                        barrier.wait(2)   # both have read the same head and written; a builder that lost on a node id never comes
                    except threading.BrokenBarrierError:
                        pass

            def go(owner, shift):
                out[owner] = w.rebuilder(owner=owner, rebase_tries=1, use_cas=use_cas, clock=lambda: time.time() + shift, pause=meet).run()

            ts = [threading.Thread(target=go, args=("b1", 0)), threading.Thread(target=go, args=("b2", 5000))]
            ts[0].start()
            for _ in range(200):            # the first builder holds the lease before the second one starts
                if self.doc("ctl", "ls:topic") is not None:
                    break
                time.sleep(0.05)
            ts[1].start()
            [t.join() for t in ts]
            self.assertEqual(sorted(r.status for r in out.values()), ["lost", "published"],
                             f"round {n}: {[(k, v.status) for k, v in out.items()]} (both builders flipped the same head)")
            win = next(r for r in out.values() if r.status == "published")
            self.assertEqual(self.head()["gen"], win.gen)
            created = {ref for mf in self.manifests() for ref in mf["created_nodes"]}
            self.assertEqual(self.all_nodes(), created, f"round {n}: the discarded generation left node documents behind")
            st, r = self.admin.request("POST", f"/lc-{self.T}-base-member/_count", {"query": {"range": {"gen_from": {"gt": win.gen}}}})
            self.assertEqual(r["count"], 0, f"round {n}: the discarded generation left members behind")
            if n % 10 == 9 or n == rounds - 1:   # the full walk of the tree is quadratic over the rounds: every tenth round
                mf = self.doc("ctl", f"mf:topic:{win.gen}")
                under = set()
                for ref in mf["top"]:
                    under |= self.leaves_under(ref)
                self.assertTrue(set(written) <= under, f"round {n}: a leaf is missing from the published tree")

    def test_two_concurrent_builders_one_generation_wins_and_the_other_is_discarded_100_rounds(self):
        t0 = time.time()
        self.check_race(ROUNDS, use_cas=True)
        print(f"V4 race: {ROUNDS} rounds in {time.time() - t0:.0f} s")

    def test_broken_flip_without_compare_and_set_is_red(self):
        with self.assertRaises(AssertionError) as cm:
            self.check_race(ROUNDS, use_cas=False)
        self.assertIn("both builders flipped", str(cm.exception))

    # ---- rollback ----------------------------------------------------------------------------------------------------------
    def check_rollback(self, keep_ancestry):
        w = self.world(consts=Constants(f_target=4, f_max=6, f_min=2, expand_hops=1, r_gen_count=2, r_gen_days=0))
        w.bootstrap(topics=2, per=4)
        st, r = self.admin.request("POST", f"/lc-{self.T}-base-leaf/_search", {"size": 1, "_source": False, "query": {"match_all": {}}})
        hub_id, hub_lv = r["hits"]["hits"][0]["_id"].rsplit(":", 1)
        hub = Leaf(hub_id, int(hub_lv), "base")
        store = EdgeStore(w.structure(), self.T, ClassLattice(["base"], []))
        e = ex.Expander(lambda c: self.gate.client(self.T, "read-base"), self.T, consts=w.consts)
        spokes = []
        for n in range(4):                      # generations 2 to 5, an adopted edge in each
            s = w.add(n % 2, 1, place=True)[0]
            g = w.rebuilder().run()
            self.assertEqual(g.status, "published", g)
            store.link(hub, Leaf(s.leaf_id, s.lv, "base"), "continues", gen_from=g.gen)
            spokes.append(s.leaf_id)
        rt = Retention(w.structure(), self.T, "base", consts=w.consts)
        self.assertTrue(rt.expire(), "no generation expired")   # the early generations the surviving edges were adopted in
        self.admin.request("POST", f"/lc-{self.T}-*/_refresh")

        def view():
            h = self.head()
            return h["gen"], sorted(l.leaf_id for l in e.expand([hub.leaf_id], ["base"], ex.Pin(w.wp.read_p(), tuple(h["ancestry"]))))

        before_gen, before = view()
        tops = self.doc("ctl", f"mf:topic:{before_gen}")["top"]
        # a candidate generation with its own adopted edge, adopted by the head
        s = w.add(0, 1, place=True)[0]
        cand = w.rebuilder().run()
        self.assertEqual(cand.status, "published", cand)
        store.link(hub, Leaf(s.leaf_id, s.lv, "base"), "continues", gen_from=cand.gen)
        self.admin.request("POST", f"/lc-{self.T}-*/_refresh")
        self.assertIn(s.leaf_id, view()[1])
        self.assertTrue(Retention(w.structure(), self.T, "base", consts=w.consts).rollback(before_gen, keep_ancestry=keep_ancestry))
        self.admin.request("POST", f"/lc-{self.T}-*/_refresh")
        gen, after = view()
        self.assertEqual(gen, before_gen)
        self.assertEqual(self.doc("ctl", f"mf:topic:{gen}")["top"], tops, "the previous tree was not restored")
        self.assertEqual(after, before, "the candidate's edge is still visible after the rollback")

    def test_rollback_restores_the_previous_tree_and_edges_after_a_retention_expiry(self):
        self.check_rollback(keep_ancestry=False)

    def test_broken_rollback_that_leaves_the_candidates_edges_visible_is_red(self):
        with self.assertRaises(AssertionError) as cm:
            self.check_rollback(keep_ancestry=True)
        self.assertIn("still visible after the rollback", str(cm.exception))
