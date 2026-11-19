"""BUILD-PLAN Step 12 on a real Elasticsearch: edges, bounded expansion, the write-path watermark (AC-8; SEC-A2, SEC-V2, SEC-V7).

Needs LC_TEST_ES_URL, LC_TEST_CA, LC_TEST_PW; without them the module FAILS. Every check drives the real writer, the real link
job and the real expander. Broken variants (each must go red for the stated reason): RANDOM-EDGES; the edge stored in the
source leaf's class (SEC-A2); expansion over the latest edges (SEC-V2); a publish of P or a head before the refresh (an edge
appears mid-task); the candidate generation number stamped on a write-path edge (SEC-V7, 100 cycles); P advanced past a late
job without joining it.
"""
import os
import random
import tempfile
import unittest
from pathlib import Path

from living_context.config import Constants
from living_context.gateway.es import EsClient
from living_context.gateway.handler import EsGate
from living_context.gateway.indices import class_index
from living_context.gateway.provision import provision_tenant
from living_context.graph import expand as ex
from living_context.graph.edges import EdgeStore, Leaf, LinkJob
from living_context.ingest.subjects import Guard, TokenSource
from living_context.ingest.writer import Writer
from living_context.keystore import Keystore
from living_context.model.budget import Budget
from living_context.model.cache import DictStore
from living_context.model.port import ModelPort
from living_context.model.stub import StubBackend
from living_context.policy import ClassLattice, Principal, Role
from living_context.recall.direct import DirectSearch
from living_context.structure.publish import Publisher
from living_context.structure.watermark import WritePath

URL, CA, PW = (os.environ.get(k) for k in ("LC_TEST_ES_URL", "LC_TEST_CA", "LC_TEST_PW"))
T = "edgz"
LAT = ClassLattice(["base", "restricted"], [("restricted", "base")])
CLASSES = ["base", "restricted"]
PAIRS, FILLERS = 16, 110


def reset(admin):
    st, rows = admin.request("GET", "/_cat/indices/lc-*?format=json&h=index")
    for row in rows if st == 200 and isinstance(rows, list) else []:
        if row["index"].startswith(f"lc-{T}-") or row["index"] == "lc-sys-registry":
            admin.request("DELETE", "/" + row["index"])


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


class EdgesOnElasticsearch(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not (URL and CA and PW):
            raise RuntimeError("LC_TEST_ES_URL, LC_TEST_CA and LC_TEST_PW must point at a scratch cluster")
        cls.tmp = tempfile.TemporaryDirectory()
        root = Path(cls.tmp.name)
        cls.root = root
        cls.admin = EsClient(URL, CA, PW, scheme="basic", user="elastic")
        reset(cls.admin)
        r = provision_tenant(cls.admin, T, CLASSES, key_dir=root / "keys", data_root=root / "data", journal_size=65536)
        cls.gate = EsGate(URL, CA, {T: r["keys"]})
        cls.ks = Keystore(root / "ks.db", T)
        cls.guard = Guard(TokenSource(cls.ks))
        cls.writer = Writer(lambda: cls.gate.client(T, "ingest"), cls.ks, T, cls.guard)
        cls.p_base = Principal(T, "p-base", frozenset([Role.READER]), frozenset(["base"]), frozenset())
        cls.n = 0

    @classmethod
    def tearDownClass(cls):
        cls.ks.close()
        reset(cls.admin)
        cls.tmp.cleanup()

    # ---- helpers ------------------------------------------------------------------------------------------
    def put(self, text, cls="base", writer=None):
        res = (writer or self.writer).remember("tok", cls, text)
        self.assertTrue(res.ok, res.status)
        return res

    def refresh(self):
        self.admin.request("POST", f"/lc-{T}-*/_refresh")

    def clear_edges(self):
        self.refresh()   # a delete-by-query sees only refreshed documents
        for c in CLASSES:
            self.admin.request("POST", f"/lc-{T}-{c}-edge/_delete_by_query?refresh=true&conflicts=proceed", {"query": {"match_all": {}}})
        self.admin.request("POST", f"/lc-{T}-ctl/_delete_by_query?refresh=true&conflicts=proceed", {"query": {"terms": {"_id": ["wp"]}}})
        self.admin.request("POST", f"/lc-{T}-ctl/_delete_by_query?refresh=true&conflicts=proceed", {"query": {"prefix": {"_id": "ec:"}}})

    def structure(self):
        return lambda: self.gate.client(T, "structure")

    def wpath(self, **kw):
        return WritePath(self.structure(), T, **kw)

    def store(self, wp=None, **kw):
        return EdgeStore(self.structure(), T, LAT, wp=wp, **kw)

    def port(self):
        self.n += 1
        return ModelPort(StubBackend(), Budget(self.root / f"b{self.n}", 1_000_000), LAT, DictStore())

    def linker(self, store):
        return LinkJob(store, lambda: self.gate.client(T, "ingest"), self.port(), T, CLASSES)

    def expander(self, classes=("base",), **kw):
        return ex.Expander(lambda c: self.gate.client(T, f"read-{c}"), T, **kw)

    def leaf(self, res, cls="base"):
        return Leaf(res.leaf_id, res.lv, cls)

    def edges_visible_to(self, key, cls, leaf_id):
        self.refresh()
        st, r = self.gate.client(T, key).request("POST", f"/lc-{T}-{cls}-edge/_search", {"size": 50, "query": {"term": {"lin_leaf_ids": leaf_id}}})
        return r["hits"]["total"]["value"] if st == 200 else -st

    # ---- A2 finds both and A1 does not; RANDOM-EDGES removes the gain --------------------------------------
    @classmethod
    def build_corpus(cls):
        if getattr(cls, "corpus", None):
            return cls.corpus
        rng = random.Random(11)
        pairs, fill = [], []
        letters = "bcdfghjklmnpqrstvwxz"

        def word(n):
            return "".join(rng.choice(letters) + rng.choice("aeiou") for _ in range(3)) + f"q{n}"

        for i in range(PAIRS):
            ent = f"Zorvak{chr(97 + i)}x"
            xw = [word(100 * i + k) for k in range(3)]
            yw = [word(100 * i + 50 + k) for k in range(3)]
            x = cls.writer.remember("tok", "base", f"Quarterly review {' '.join(xw)} common ledger. The {ent} account owner confirmed figures.")
            y = cls.writer.remember("tok", "base", f"Separately the {ent} account hosts the {' '.join(yw)} dossier archive.")
            assert x.ok and y.ok
            pairs.append((x, y, " ".join(xw) + " common ledger"))
        for i in range(FILLERS):
            f = cls.writer.remember("tok", "base", f"Quarterly review common ledger {word(900 + i)} {word(1900 + i)} entry {i}.")
            assert f.ok
            fill.append(f)
        cls.corpus = (pairs, fill)
        return cls.corpus

    def link_all(self, store, wp, leaves):
        lj = self.linker(store)
        for res in leaves:
            job = wp.begin(res.src_seq)
            lj.run(job, "base", res.leaf_id, res.lv, src_seq=res.src_seq)
            wp.finish(job)
        return wp.advance()

    def both_rates(self, pairs, pin, consts):
        ds = DirectSearch(lambda c: self.gate.client(T, f"read-{c}"), T)
        e = self.expander(consts=consts)
        a1 = a2 = 0
        for x, y, q in pairs:
            ids = [h.doc_id.split(":")[0] for h in ds.search(self.p_base, LAT, q, size=10)]
            linked = [l.leaf_id for l in e.expand(ids, ["base"], pin)]
            a1 += x.leaf_id in ids and y.leaf_id in ids
            a2 += x.leaf_id in ids + linked and y.leaf_id in ids + linked
        return a1 / len(pairs), a2 / len(pairs)

    def test_a2_finds_both_leaves_a1_does_not_and_random_edges_remove_the_gain(self):
        pairs, fill = self.build_corpus()
        everything = [r for x, y, _ in pairs for r in (x, y)] + fill
        consts = Constants(expand_hops=1)
        self.clear_edges()
        wp = self.wpath()
        store = self.store(wp)
        P = self.link_all(store, wp, everything)
        pin = ex.Pin(P, ())
        a1, a2 = self.both_rates(pairs, pin, consts)
        print(f"edges: A1 finds both {a1:.3f}, A2 finds both {a2:.3f}; written {store.stats.written}")
        self.assertLessEqual(a1, 0.1, "A1 (no expansion) already finds both leaves")
        self.assertGreaterEqual(a2, 0.9, "A2 (bounded expansion) does not find the second leaf through the shared entity")
        # RANDOM-EDGES: the same producer, the destination of every edge replaced by a random leaf
        self.clear_edges()
        wp2 = self.wpath()
        pool = [Leaf(r.leaf_id, r.lv, "base") for r in everything]
        store2 = self.store(wp2, random_edges=random.Random(5), leaf_pool=lambda: pool)
        P2 = self.link_all(store2, wp2, everything)
        r1, r2 = self.both_rates(pairs, ex.Pin(P2, ()), consts)
        print(f"edges: RANDOM-EDGES A1 {r1:.3f}, A2 {r2:.3f}")
        self.assertLessEqual(r2 - r1, 0.5 * (a2 - a1), "random edges kept A2's gain")
        self.clear_edges()

    # ---- SEC-A2: an edge between a base and a restricted leaf lives only in the restricted edge index --------
    def check_join_class(self, **kw):
        self.clear_edges()
        b = self.put("Notice that Hendrix Orbit audit notes remain open for base readers.")
        r = self.put("Restricted file: the Hendrix Orbit audit findings are sealed.", cls="restricted")
        wp = self.wpath()
        store = self.store(wp, **kw)
        job = wp.begin(r.src_seq)
        store.link(self.leaf(r, "restricted"), self.leaf(b), "same_entity", src_seq=r.src_seq, job=job)
        wp.finish(job)
        wp.advance()
        self.assertEqual(self.edges_visible_to("read-base", "base", r.leaf_id), 0,
                         "a document in a base-readable edge index names the restricted leaf")
        self.assertGreater(self.edges_visible_to("read-restricted", "restricted", r.leaf_id), 0)
        self.assertEqual(self.edges_visible_to("read-base", "restricted", r.leaf_id), -403,
                         "a base key can search the restricted edge index")

    def test_an_edge_across_classes_lives_only_in_the_join_class(self):
        self.check_join_class()

    def test_broken_edge_stored_in_the_source_class_is_red(self):
        with self.assertRaises(AssertionError) as cm:
            self.check_join_class(store_in_source_class=True)
        self.assertIn("names the restricted leaf", str(cm.exception))

    # ---- supersedes, degree cap, version counters ------------------------------------------------------------
    def test_a_supersedes_cycle_becomes_contradicts(self):
        self.clear_edges()
        a, b, c = (self.leaf(self.put(f"Plan {w} revision of the Hendrix schedule.")) for w in ("alpha", "bravo", "charlie"))
        store = self.store()
        store.link(a, b, "supersedes", src_seq=1)
        store.link(b, c, "supersedes", src_seq=2)
        out = store.link(c, a, "supersedes", src_seq=3)   # closes a -> b -> c -> a
        self.assertEqual(store.stats.cycles, 1)
        self.refresh()
        st, r = self.admin.request("POST", f"/lc-{T}-base-edge/_search", {"size": 20, "query": {"term": {"src_id": c.leaf_id}}})
        types = {h["_source"]["type"]: h["_source"]["cycle"] for h in r["hits"]["hits"]}
        self.assertEqual(types, {"contradicts": True}, "the closing edge is still a supersedes")
        self.assertEqual(len(out), 2)   # contradicts is undirected: two documents

    def test_the_degree_cap_refuses_and_counts(self):
        self.clear_edges()
        hub = self.leaf(self.put("The Hendrix hub leaf anchors many neighbours."))
        spokes = [self.leaf(self.put(f"Spoke number {i} mentions the Hendrix hub.")) for i in range(5)]
        store = self.store(degree_cap=3)
        for s in spokes:
            store.link(hub, s, "same_entity", src_seq=1)
        self.assertEqual((store.stats.written, store.stats.cap_refused), (8, 2), "the cap of 3 per type per leaf did not hold")
        self.assertEqual(store.degree(hub.leaf_id, "same_entity"), 3)

    def test_a_recreated_edge_starts_above_every_erased_version(self):
        self.clear_edges()
        a, b = (self.leaf(self.put(f"Item {w} about the Hendrix ledger.")) for w in ("one", "two"))
        store = self.store()
        ids = store.link(a, b, "continues", src_seq=1)
        self.assertTrue(ids[0].endswith(":1"))
        index = class_index(T, "base", "edge")
        self.admin.request("DELETE", f"/{index}/_doc/{ids[0]}?refresh=true")   # what an erasure does to the edge documents
        again = store.link(a, b, "continues", src_seq=2)
        self.assertEqual(again[0].split(":")[0], ids[0].split(":")[0])
        self.assertTrue(again[0].endswith(":2"), f"a re-created edge got {again[0]}, not a version above the erased one")

    # ---- caps of the expansion --------------------------------------------------------------------------------
    def test_expansion_respects_the_hop_neighbour_and_leaf_caps(self):
        self.clear_edges()
        leaves = [self.leaf(self.put(f"Node {w} of the Hendrix chain.")) for w in ("zero", "one", "two", "three", "four")]
        store = self.store()
        for i, (s, d) in enumerate(zip(leaves, leaves[1:]), 1):
            store.link(s, d, "continues", src_seq=i)
        for extra in range(4):
            store.link(leaves[0], self.leaf(self.put(f"Side {extra} of the Hendrix chain.")), "continues", src_seq=10 + extra)
        self.refresh()
        pin = ex.Pin(100, ())

        def got(consts):
            return self.expander(consts=consts).expand([leaves[0].leaf_id], ["base"], pin)

        two_hops = got(Constants(expand_hops=2, expand_neighbours=10, expand_leaves=50))
        self.assertEqual({l.hop for l in two_hops}, {1, 2})
        self.assertNotIn(leaves[3].leaf_id, [l.leaf_id for l in two_hops], "a third hop was taken")
        self.assertEqual(len([l for l in got(Constants(expand_hops=1, expand_neighbours=2, expand_leaves=50))]), 2, "more than 2 neighbours per node")
        self.assertEqual(len(got(Constants(expand_hops=2, expand_neighbours=10, expand_leaves=3))), 3, "more than the leaf cap")

    # ---- rebuild versions are bound by gen_from and retired by a gen_to set ------------------------------------
    def test_rebuild_edges_follow_the_ancestry_and_retirement_hides_them(self):
        self.clear_edges()
        a, b = (self.leaf(self.put(f"Gen leaf {w} about Hendrix.")) for w in ("one", "two"))
        store = self.store()
        ids = store.link(a, b, "continues", gen_from=2)
        self.assertEqual(len(ids), 1)
        self.refresh()
        e = self.expander()
        seen = lambda anc: [l.leaf_id for l in e.expand([a.leaf_id], ["base"], ex.Pin(0, anc))]
        self.assertEqual(seen((1,)), [], "a rebuild edge is visible to a pin whose ancestry lacks its generation")
        self.assertEqual(seen((1, 2)), [b.leaf_id])
        self.assertTrue(store.retire(class_index(T, "base", "edge"), ids[0], 3))
        self.refresh()
        self.assertEqual(seen((1, 2)), [b.leaf_id], "an older pin lost an edge retired by a later generation")
        self.assertEqual(seen((1, 2, 3)), [], "a retired edge is still visible to a pin that has the retiring generation")
        doc = self.admin.request("GET", f"/{class_index(T, 'base', 'edge')}/_doc/{ids[0]}")[1]["_source"]
        for pin in (ex.Pin(0, (1,)), ex.Pin(0, (1, 2)), ex.Pin(0, (1, 2, 3))):   # the query agrees with the reference reading
            self.assertEqual(bool(e.expand([a.leaf_id], ["base"], pin)), ex.visible(doc, pin))

    # ---- SEC-V2: visibility by the pinned watermark ----------------------------------------------------------
    def pinned_pair(self, wp):
        a = self.put("Pinned task leaf about Hendrix.", writer=Writer(lambda: self.gate.client(T, "ingest"), self.ks, T, self.guard, write_path=wp))
        wr = Writer(lambda: self.gate.client(T, "ingest"), self.ks, T, self.guard, write_path=wp)
        b = self.put("Second pinned leaf about Hendrix.", writer=wr)
        return a, b

    def check_pin(self, **kw):
        self.clear_edges()
        wp = self.wpath()
        a, b = self.pinned_pair(wp)
        store = self.store(wp)
        ja, jb = wp.begin(a.src_seq), wp.begin(b.src_seq)
        store.link(self.leaf(a), self.leaf(b), "same_entity", src_seq=b.src_seq, job=jb)
        wp.finish(ja)
        wp.finish(jb)
        before = ex.Pin(wp.read_p(), ())            # a task pinned before P covered the edge
        P = wp.advance()
        after = ex.Pin(P, ())
        e = self.expander(**kw)
        ids = lambda pin: [l.leaf_id for l in e.expand([a.leaf_id], ["base"], pin)]
        self.assertEqual(ids(before), [], "an edge written on insert is visible to a task pinned before P covered it")
        self.assertEqual(ids(after), [b.leaf_id], "a task pinned after P does not see the edge")
        self.assertEqual(ids(before), [], "the earlier task's view changed during its life")

    def test_an_insert_edge_is_invisible_to_an_earlier_pin_and_visible_to_a_later_one(self):
        self.check_pin()

    def test_broken_expansion_on_the_latest_edges_is_red(self):
        with self.assertRaises(AssertionError) as cm:
            self.check_pin(ignore_pin=True)
        self.assertIn("pinned before P", str(cm.exception))

    def check_refresh_barrier(self, **kw):
        """Index refresh is paused (interval -1) independently of write acknowledgement; tasks start without waiting."""
        self.clear_edges()
        idx = class_index(T, "base", "edge")
        self.admin.request("PUT", f"/{idx}/_settings", {"index": {"refresh_interval": "-1"}})
        try:
            wp = self.wpath(**kw)
            a, b = self.pinned_pair(wp)
            store = self.store(wp)
            ja, jb = wp.begin(a.src_seq), wp.begin(b.src_seq)
            store.link(self.leaf(a), self.leaf(b), "same_entity", src_seq=b.src_seq, job=jb)
            wp.finish(ja)
            wp.finish(jb)
            P = wp.advance()
            pin = ex.Pin(P, ())
            e = self.expander()
            first = [l.leaf_id for l in e.expand([a.leaf_id], ["base"], pin)]
            self.admin.request("POST", f"/{idx}/_refresh")   # a background refresh happens mid-task
            second = [l.leaf_id for l in e.expand([a.leaf_id], ["base"], pin)]
            self.assertEqual(first, second, "an edge appeared mid-task")
            self.assertEqual(second, [b.leaf_id])
        finally:
            self.admin.request("PUT", f"/{idx}/_settings", {"index": {"refresh_interval": "1s"}})

    def test_p_moves_only_after_the_refresh_so_a_pinned_task_sees_one_graph(self):
        self.check_refresh_barrier()

    def test_broken_p_published_on_acknowledgement_is_red(self):
        with self.assertRaises(AssertionError) as cm:
            self.check_refresh_barrier(refresh_before_advance=False)
        self.assertIn("appeared mid-task", str(cm.exception))

    # ---- SEC-V7: write-path edges survive a rejected candidate -----------------------------------------------
    def check_candidate_cycles(self, cycles, stamp_next_gen):
        self.clear_edges()
        wp = self.wpath()
        wr = Writer(lambda: self.gate.client(T, "ingest"), self.ks, T, self.guard, write_path=wp)
        hub = self.put("The Hendrix hub for candidate cycles.", writer=wr)
        pub = Publisher(self.structure(), T, "base")
        store = self.store(wp, degree_cap=1000)   # the default cap of 50 per type would refuse the hub's 51st edge
        ancestry = (0,)     # the live generation's ancestry; a rejected candidate is never added to it
        hj = wp.begin(hub.src_seq)
        wp.finish(hj)
        for n in range(cycles):
            cand = pub.next_gen()                      # an open reorganiser candidate takes the next number
            leaf = self.put(f"Cycle leaf {n} of the Hendrix hub.", writer=wr)
            job = wp.begin(leaf.src_seq)
            if stamp_next_gen:                         # BROKEN: the edge is bound to the candidate's number
                store.link(self.leaf(hub), self.leaf(leaf), "same_entity", gen_from=cand, job=job)
            else:
                store.link(self.leaf(hub), self.leaf(leaf), "same_entity", src_seq=leaf.src_seq, job=job)
            wp.finish(job)
            # the candidate is rejected here: its own unshared documents are deleted, the live ancestry is unchanged
            P = wp.advance()
            seen = [l.leaf_id for l in self.expander(consts=Constants(expand_hops=1, expand_neighbours=500, expand_leaves=500)).expand([hub.leaf_id], ["base"], ex.Pin(P, ancestry))]
            self.assertIn(leaf.leaf_id, seen, f"cycle {n}: the edge written during a rejected candidate is not visible")

    def test_write_path_edges_stay_visible_after_100_rejected_candidates(self):
        self.check_candidate_cycles(100, stamp_next_gen=False)

    def test_broken_edge_stamped_with_the_candidate_generation_is_red(self):
        with self.assertRaises(AssertionError) as cm:
            self.check_candidate_cycles(100, stamp_next_gen=True)
        self.assertIn("rejected candidate", str(cm.exception))

    # ---- a link job held past D_WP --------------------------------------------------------------------------
    def check_late_job(self, **kw):
        self.clear_edges()
        clock = Clock()
        wp = self.wpath(clock=clock, **kw)
        wr = Writer(lambda: self.gate.client(T, "ingest"), self.ks, T, self.guard, write_path=wp)
        a, b = self.put("Late job anchor leaf about Hendrix.", writer=wr), self.put("Late job partner leaf about Hendrix.", writer=wr)
        store = self.store(wp)
        ja, jb = wp.begin(a.src_seq), wp.begin(b.src_seq)
        wp.finish(ja)
        store.link(self.leaf(a), self.leaf(b), "same_entity", src_seq=b.src_seq, job=jb)     # written, job still open
        self.assertEqual(wp.advance(), a.src_seq, "P moved past an open job")
        clock.t += Constants().d_wp_s + 1
        P = wp.advance()                                    # the job is cancelled, its edges deleted, then P moves
        self.assertEqual(P, b.src_seq)
        self.refresh()
        pin = ex.Pin(P, ())
        e = self.expander()
        first = [l.leaf_id for l in e.expand([a.leaf_id], ["base"], pin)]
        store.link(self.leaf(a), self.leaf(b), "same_entity", src_seq=b.src_seq, job=jb)     # the late job writes again
        wp.advance()
        self.refresh()
        second = [l.leaf_id for l in e.expand([a.leaf_id], ["base"], pin)]
        self.assertEqual(first, [], "an edge of the cancelled job is still visible")
        self.assertEqual(second, first, "a late edge appeared under a pinned task")

    def test_a_job_held_past_d_wp_is_cancelled_deleted_and_writes_nothing_more(self):
        self.check_late_job()

    def test_broken_p_advanced_without_joining_the_job_is_red(self):
        with self.assertRaises(AssertionError) as cm:
            self.check_late_job(join_cancelled=False)
        self.assertRegex(str(cm.exception), "cancelled job is still visible|late edge appeared")


if __name__ == "__main__":
    unittest.main()
