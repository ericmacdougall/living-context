"""BUILD-PLAN Step 8: the stub model port, budget counter, cache and fair queue (AC-7, AC-17). No Elasticsearch needed."""
import tempfile
import threading
import time
import unittest
from pathlib import Path

from living_context.model.budget import Budget, BudgetExceeded
from living_context.model.cache import DictStore
from living_context.model.fairq import FairLimiter, FifoLimiter
from living_context.model.port import CallContext, ModelPort, PortRefused, Task, schema_ok
from living_context.model.stub import StubBackend
from living_context.policy import ClassLattice

LAT = ClassLattice(["base", "restricted"], [("restricted", "base")])
KIDS = [{"label": "c1", "title": "tomato garden", "gist": "Water tomatoes every morning in the garden.", "entities": ["tomatoes"]},
        {"label": "c2", "title": "tax filing", "gist": "The accountant files the quarterly tax return.", "entities": ["tax"]},
        {"label": "c3", "title": "piano lessons", "gist": "Practice scales on the piano daily.", "entities": ["piano"]}]
QUERY = "how often should the tomatoes in the garden be watered"


def make(tmp, cap=100, **kw):
    stub = kw.pop("stub", None) or StubBackend()
    port = ModelPort(stub, Budget(Path(tmp) / "budget", cap), LAT, kw.pop("store", None) or DictStore(), **kw)
    return port, stub


def ctx(cls="base", principal="px", inputs=("base",)):
    return CallContext("tena", cls, principal, tuple(inputs))


class LabelOrder(StubBackend):
    """Broken stub: selects by label order and never reads content."""

    def _route(self, i):
        sel = [k["label"] for k in i["children"][:1]]
        return {"selected": sel, "reasons": {s: "first" for s in sel}}


class FlatStore(DictStore):
    """Broken store: forgets the class partition."""

    def get(self, cls, key):
        return self.data.get(("-", key))

    def put(self, cls, key, value, subjects):
        self.data[("-", key)] = value


class Stub(unittest.TestCase):
    def selected(self, stub):
        return stub.run("route", {"query": QUERY, "children": KIDS})["selected"]

    def shuffled_selected(self, stub):
        contents = [{k: v for k, v in c.items() if k != "label"} for c in KIDS]
        rotated = contents[1:] + contents[:1]
        kids = [dict(c, label=f"c{n}") for n, c in enumerate(rotated, 1)]
        return stub.run("route", {"query": QUERY, "children": kids})["selected"]

    def check_depends_on_content(self, stub):
        a, b = self.selected(stub), self.shuffled_selected(stub)
        self.assertEqual(a, ["c1"])
        self.assertNotEqual(a, b, "the router chose the same children after the entries were shuffled")

    def test_route_depends_on_content(self):
        self.check_depends_on_content(StubBackend())

    def test_broken_label_order_stub_is_red(self):
        with self.assertRaises(AssertionError) as cm:
            self.check_depends_on_content(LabelOrder())
        self.assertIn("same children after the entries were shuffled", str(cm.exception))

    def test_reasons_are_short_and_from_matched_terms(self):
        r = StubBackend().run("route", {"query": QUERY, "children": KIDS})
        self.assertLessEqual(len(r["reasons"]["c1"].split()), 12)
        self.assertIn("tomatoes", r["reasons"]["c1"])

    def test_every_task_is_deterministic_and_passes_its_schema(self):
        inputs = {
            "build_l1": {"leaves": [{"title": "t", "text": "Alpha beta gamma. Beta beta delta. Gamma epsilon."}]},
            "build_upper": {"children": [{"title": "t", "summary": "alpha beta", "facts": ["beta delta"]}]},
            "route": {"query": QUERY, "children": KIDS},
            "organise": {"pointers": [{"path": "p1", "pointer": "x1", "score": 2}, {"path": "p1", "pointer": "x2", "score": 3}]},
            "type_edges": {"items": [{"label": "a", "text": "saves 5 dollars", "entities": ["bob"]},
                                     {"label": "b", "text": "no savings 7", "entities": ["bob"]}]},
            "judge_relevance": {"query": QUERY, "text": KIDS[0]["gist"]},
            "query_variants": {"query": QUERY}, "synthetic_queries": {"text": "alpha beta beta gamma gamma gamma delta"},
            "ask": {"question": QUERY, "context": [KIDS[0]["gist"]]},
            "reader_answer": {"question": QUERY, "context": [KIDS[0]["gist"]]},
            "answer_judge": {"gold": "water tomatoes every morning", "answer": "Water tomatoes every morning."}}
        s1, s2 = StubBackend(), StubBackend()
        for t in Task:
            a, b = s1.run(t.value, inputs[t.value]), s2.run(t.value, inputs[t.value])
            self.assertEqual(a, b, t)
            self.assertTrue(schema_ok(t.value, a), t)
        self.assertNotIn("title", s1.run("build_l1", inputs["build_l1"])["entries"][0])  # titles are code-built
        self.assertTrue(s1.run("judge_relevance", inputs["judge_relevance"])["relevant"])


class Cache(unittest.TestCase):
    def test_a_different_class_misses(self):
        with tempfile.TemporaryDirectory() as d:
            port, stub = make(d)
            inp = {"children": KIDS, "query": QUERY}
            port.call(Task.ROUTE, inp, ctx=ctx("base"))
            self.assertTrue(port.call(Task.ROUTE, inp, ctx=ctx("base")).cached)
            self.assertFalse(port.call(Task.ROUTE, inp, ctx=ctx("restricted")).cached, "a different class hit the cache")

    def test_broken_key_without_class_hits_and_is_caught(self):
        with tempfile.TemporaryDirectory() as d:
            port, _ = make(d, store=FlatStore(), class_in_cache=False)
            inp = {"children": KIDS, "query": QUERY}
            port.call(Task.ROUTE, inp, ctx=ctx("base"))
            with self.assertRaises(AssertionError) as cm:
                self.assertFalse(port.call(Task.ROUTE, inp, ctx=ctx("restricted")).cached, "a different class hit the cache")
            self.assertIn("different class hit the cache", str(cm.exception))

    def check_restricted_route_over_base_node_is_stored_restricted(self, port, store):
        port.call(Task.ROUTE, {"children": KIDS, "query": QUERY}, ctx=ctx("restricted", inputs=("base",)))
        classes = {c for c, _ in store.data}
        self.assertEqual(classes, {"restricted"}, "the entry was stored outside the restricted cache")

    def test_route_over_a_base_node_from_a_restricted_channel_is_stored_restricted(self):
        with tempfile.TemporaryDirectory() as d:
            store = DictStore()
            port, _ = make(d, store=store)
            self.check_restricted_route_over_base_node_is_stored_restricted(port, store)

    def test_broken_cache_class_equal_to_node_class_is_caught(self):
        with tempfile.TemporaryDirectory() as d:
            store = DictStore()
            port, _ = make(d, store=store, join_class=False)
            with self.assertRaises(AssertionError) as cm:
                self.check_restricted_route_over_base_node_is_stored_restricted(port, store)
            self.assertIn("outside the restricted cache", str(cm.exception))

    def check_principal_scope(self, port, stub):
        inp = {"children": KIDS, "query": QUERY}
        port.call(Task.ROUTE, inp, ctx=ctx(principal="px"))
        res = port.call(Task.ROUTE, inp, ctx=ctx(principal="py"))
        self.assertFalse(res.cached, "a route cached for one principal answered another")
        self.assertTrue(port.call(Task.ROUTE, inp, ctx=ctx(principal="px")).cached)

    def test_prompt_bearing_cache_is_per_principal(self):
        with tempfile.TemporaryDirectory() as d:
            port, stub = make(d)
            self.check_principal_scope(port, stub)
            leaves = {"leaves": [{"title": "t", "text": "One sentence here."}]}  # prompt-free: shared within the class
            port.call(Task.BUILD_L1, leaves, ctx=ctx(principal="px"))
            self.assertTrue(port.call(Task.BUILD_L1, leaves, ctx=ctx(principal="py")).cached)

    def test_broken_shared_prompt_cache_is_caught(self):
        with tempfile.TemporaryDirectory() as d:
            port, stub = make(d, principal_in_scope=False)
            with self.assertRaises(AssertionError) as cm:
                self.check_principal_scope(port, stub)
            self.assertIn("answered another", str(cm.exception))


class Port(unittest.TestCase):
    def test_a_non_stub_backend_is_refused_and_counts_agree(self):
        class Real:
            name = "real"

            def run(self, task, inputs):
                raise AssertionError("must never be reached")

        with tempfile.TemporaryDirectory() as d:
            bad, _ = make(d, stub=Real())
            with self.assertRaises(PortRefused):
                bad.call(Task.JUDGE_RELEVANCE, {"query": "a", "text": "b"}, ctx=ctx())
        with tempfile.TemporaryDirectory() as d:
            port, stub = make(d)
            for i in range(5):
                port.call(Task.JUDGE_RELEVANCE, {"query": f"q{i}", "text": "text"}, ctx=ctx())
            port.call(Task.JUDGE_RELEVANCE, {"query": "q0", "text": "text"}, ctx=ctx())  # cache hit: a port call, no stub call
            self.assertEqual((port.port_calls, stub.invocations, port._budget.count()), (6, 5, 5))
        with tempfile.TemporaryDirectory() as d:
            wrong, _ = make(d)
            with self.assertRaises(PortRefused):
                wrong.call(Task.ROUTE, {"children": KIDS, "query": QUERY},
                           ctx=CallContext("tena", "base", "px", ("base",), model_id="some-real-model-id"))

    def test_schema_failure_is_a_typed_failure_and_counts(self):
        class Bad(StubBackend):
            def _judge_relevance(self, i):
                return {"score": "high"}

        with tempfile.TemporaryDirectory() as d:
            port, _ = make(d, stub=Bad())
            r = port.call(Task.JUDGE_RELEVANCE, {"query": "a", "text": "b"}, ctx=ctx())
            self.assertEqual((r.ok, r.error, port._budget.count()), (False, "schema", 1))


class BudgetTests(unittest.TestCase):
    def test_refuses_at_the_cap_and_survives_a_restart(self):
        with tempfile.TemporaryDirectory() as d:
            port, _ = make(d, cap=3)
            for i in range(3):
                self.assertTrue(port.call(Task.JUDGE_RELEVANCE, {"query": f"q{i}", "text": "t"}, ctx=ctx()).ok)
            with self.assertRaises(BudgetExceeded):
                port.call(Task.JUDGE_RELEVANCE, {"query": "q9", "text": "t"}, ctx=ctx())
            self.assertEqual(Budget(Path(d) / "budget", 3).count(), 3)  # persisted, not in memory
            with self.assertRaises(BudgetExceeded):
                Budget(Path(d) / "budget", 3).charge()

    def check_failed_call_counts(self, **kw):
        class Boom(StubBackend):
            def _judge_relevance(self, i):
                raise RuntimeError("provider failure")

        with tempfile.TemporaryDirectory() as d:
            port, _ = make(d, stub=Boom(), **kw)
            r = port.call(Task.JUDGE_RELEVANCE, {"query": "a", "text": "b"}, ctx=ctx())
            self.assertFalse(r.ok)
            self.assertEqual(port._budget.count(), 1, "a failed call was not counted")

    def test_a_failed_call_is_counted(self):
        self.check_failed_call_counts()

    def test_broken_count_after_success_is_caught(self):
        with self.assertRaises(AssertionError) as cm:
            self.check_failed_call_counts(charge_before=False)
        self.assertIn("failed call was not counted", str(cm.exception))


class Fair(unittest.TestCase):
    def wait_of_victim(self, limiter):
        started = []

        def work(tag):
            started.append((tag, time.time()))
            time.sleep(0.03)

        ts = [threading.Thread(target=limiter.run, args=("flood", lambda: work("flood"))) for _ in range(30)]
        for t in ts:
            t.start()
        time.sleep(0.05)
        t0 = time.time()
        v = threading.Thread(target=limiter.run, args=("victim", lambda: work("victim")))
        v.start()
        v.join(30)
        [t.join(30) for t in ts]
        return [at for tag, at in started if tag == "victim"][0] - t0

    def test_a_flooding_tenant_does_not_delay_another_beyond_its_fair_share(self):
        self.assertLess(self.wait_of_victim(FairLimiter(2)), 0.15)

    def test_broken_single_fifo_queue_is_caught(self):
        self.assertGreater(self.wait_of_victim(FifoLimiter(2)), 0.25)


if __name__ == "__main__":
    unittest.main()
