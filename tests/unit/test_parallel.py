"""Concurrent model calls (the registered model-port concurrency, DESIGN 13): ordered commits, one charge per call, journal keys that do
not depend on worker interleaving, one shared back-off and stop gate, a resume that never charges twice, and stage outputs (tree and
ledger) byte-identical between one worker and several. Fake runners and the stub model only; no real model, no dataset."""
import hashlib
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from evalkit import claude_cli as cc, parallel as par, run_s6h1 as rs
from evalkit.s2_ports import Journal, ResilientPort, ScopeRequired, StagePaused, StageStopped
from living_context.config import Constants
from living_context.ids import IdGenerator
from living_context.model.budget import Budget, BudgetExceeded
from living_context.model.port import CallResult, Task
from living_context.model.cache import DictStore
from living_context.model.port import ModelPort
from living_context.model.stub import StubBackend
from living_context.policy import ClassLattice
from living_context.structure import build, cluster
from tests.security.test_run_s2_es import Runner
from tests.unit import test_run_s6h1 as t6


def jitter(prompt: str, scale_ms: float = 12.0) -> float:
    """A deterministic per-prompt delay, so calls (and whole questions) finish in an order unrelated to the order they began."""
    return (int(hashlib.sha256(prompt.encode()).hexdigest()[:4], 16) % 8) * scale_ms / 8000.0


class SafeRunner(Runner):
    """The stage test runner, safe under threads, with a per-prompt delay outside the lock and an in-flight high-water mark."""
    def __init__(self, *a, scale_ms=12.0, **kw):
        super().__init__(*a, **kw)
        self.lock, self.inflight, self.max_inflight, self.scale_ms = threading.Lock(), 0, 0, scale_ms
        self.after_stop = 0

    def __call__(self, prompt):
        with self.lock:
            if self.stop_file is not None and Path(self.stop_file).exists():
                self.after_stop += 1             # a call that began after the stop file was written
            ro = super().__call__(prompt)
            self.inflight += 1
            self.max_inflight = max(self.max_inflight, self.inflight)
        time.sleep(jitter(prompt, self.scale_ms))
        with self.lock:
            self.inflight -= 1
        return ro


def capped(tmp, runner, *, cap=10_000, concurrency=4, sleep=lambda s: None, **kw):
    tmp = Path(tmp)
    b = Budget(tmp / "counter", cap)
    inner = cc.CappedPort(runner, b, tmp / "calls.jsonl", model_id="m")
    port = ResilientPort(inner, b, Journal(tmp / "journal.jsonl"), tmp / "STOP", sleep=sleep, base_delay_s=0.01,
                         concurrency=concurrency, **kw)
    return b, inner, port


def route_inputs(i):
    return {"query": "question %d" % i, "children": [{"label": "c1", "title": "t%d" % i, "gist": "g", "entities": [], "facts": [],
                                                     "size_tokens": 1, "kind": "leaf"}]}


def in_threads(fns):
    errs = [None] * len(fns)

    def wrap(k, f):
        try:
            f()
        except BaseException as e:          # collected and inspected by the test
            errs[k] = e
    ts = [threading.Thread(target=wrap, args=(k, f)) for k, f in enumerate(fns)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    return errs


class LimitAt(SafeRunner):
    """Allowance refusals (the CLI's headless envelope) at the given call numbers; records (in flight, limit) at every call."""
    def __init__(self, at=(), slow_refusal=0.0, **kw):
        super().__init__(**kw)
        self.at, self.port, self.seen, self.slow = set(at), None, [], slow_refusal

    def __call__(self, prompt):
        with self.lock:
            k = self.n + 1
            if self.port is not None:
                self.seen.append((self.inflight + 1, self.port.slots.limit))
        if k in self.at:
            with self.lock:
                self.n += 1
            time.sleep(self.slow)
            return cc.RunOut(1, json.dumps({"type": "result", "is_error": True, "result": "You've hit your session limit"}), "", 0.01)
        return super().__call__(prompt)


class BurstThrottle(SafeRunner):
    """Throttles (HTTP 429 text) the first `burst` calls, each held `hold` seconds so they are in flight together."""
    def __init__(self, burst, hold=0.05, **kw):
        super().__init__(**kw)
        self.burst, self.hold, self.thrown = burst, hold, 0

    def __call__(self, prompt):
        with self.lock:
            mine = self.thrown < self.burst
            if mine:
                self.thrown += 1
        if mine:
            time.sleep(self.hold)
            return cc.RunOut(1, "", "rate limit exceeded", 0.01)
        return super().__call__(prompt)


class RefuseOne(SafeRunner):
    """An allowance refusal for every call whose prompt holds `needle`; every other call is answered."""
    def __init__(self, needle, slow_refusal=0.0, **kw):
        super().__init__(**kw)
        self.needle, self.slow, self.refused = needle, slow_refusal, 0

    def __call__(self, prompt):
        if self.needle in prompt:
            with self.lock:
                self.refused += 1
            time.sleep(self.slow)
            return cc.RunOut(1, json.dumps({"type": "result", "is_error": True, "result": "You've hit your session limit"}), "", 0.01)
        return super().__call__(prompt)


def adaptive_port(tmp, runner, ceiling=8, **kw):
    b, inner, port = capped(tmp, runner, concurrency=ceiling, adaptive=True, start_concurrency=2, **kw)
    runner.port = port
    return b, inner, port


def run_workers(port, workers=12, per=10):
    return in_threads([lambda w=w: [port.scoped("q%d" % w).call(Task.ROUTE, route_inputs(w * 100 + i)) for i in range(per)]
                       for w in range(workers)])


class Adaptive(unittest.TestCase):
    def test_it_ramps_from_two_to_the_ceiling_and_never_exceeds_its_limit(self):
        with tempfile.TemporaryDirectory() as tmp:
            r = LimitAt(scale_ms=40)
            b, inner, port = adaptive_port(tmp, r)
            self.assertEqual(run_workers(port), [None] * 12)
            self.assertEqual(port.slots.changes[:2], [(2, 4, "clean window"), (4, 8, "clean window")])
            self.assertEqual(port.slots.limit, 8)
            self.assertTrue(all(inflight <= limit for inflight, limit in r.seen), max(r.seen))
            rows = [json.loads(l) for l in (Path(tmp) / "calls.jsonl").read_text().splitlines()]
            self.assertEqual({x["concurrency"] for x in rows}, {2, 4, 8})          # every call row records the effective concurrency
            self.assertGreater(r.max_inflight, 4)

    def test_the_ramp_stops_at_a_ceiling_that_is_not_a_power_of_two(self):
        with tempfile.TemporaryDirectory() as tmp:
            r = LimitAt(scale_ms=8)
            b, inner, port = adaptive_port(tmp, r, ceiling=3)
            self.assertEqual(run_workers(port), [None] * 12)
            self.assertEqual(port.slots.changes, [(2, 3, "clean window")])
            self.assertTrue(all(inflight <= 3 for inflight, _limit in r.seen), max(r.seen))

    def test_a_throttle_halves_it_and_a_clean_window_brings_it_back(self):
        with tempfile.TemporaryDirectory() as tmp:
            r = LimitAt(scale_ms=8, throttle=(40,))
            b, inner, port = adaptive_port(tmp, r)
            self.assertEqual(run_workers(port), [None] * 12)
            i = port.slots.changes.index((8, 4, "throttle"))
            self.assertIn((4, 8, "clean window"), port.slots.changes[i:])
            self.assertEqual(b.count(), 120 + 1)                                   # the throttled call charged once, then asked again

    def test_an_allowance_refusal_halves_and_backs_off_a_bounded_number_of_times(self):
        with tempfile.TemporaryDirectory() as tmp:
            r = LimitAt(at=(30,), scale_ms=8)
            b, inner, port = adaptive_port(tmp, r)
            self.assertEqual(run_workers(port), [None] * 12)
            self.assertEqual(port.limited, 1)
            self.assertTrue(any(c[2] == "allowance refusal" for c in port.slots.changes))
            self.assertEqual(b.count(), 120 + 1)
        with tempfile.TemporaryDirectory() as tmp:                                # refusals that persist stop the stage
            r = LimitAt(at=range(30, 400), scale_ms=8)
            b, inner, port = adaptive_port(tmp, r)
            errs = run_workers(port)
            self.assertTrue(all(isinstance(e, cc.StageLimited) for e in errs if e is not None))
            self.assertTrue(any(e is not None for e in errs))
            self.assertLessEqual(port.limited, 1 + 3 + 8)          # the retries of one call, plus calls already in flight

    def test_refusals_of_calls_in_flight_together_do_not_multiply_the_retries(self):
        # 8 calls in flight are refused together: the stage halts after max_retries refusals counted across workers (plus the
        # calls already in flight), not after max_retries per worker (a per-call count gave about 25 here)
        with tempfile.TemporaryDirectory() as tmp:
            r = LimitAt(at=range(1, 1000), slow_refusal=0.05)
            b, inner, port = capped(tmp, r, concurrency=8, adaptive=True, start_concurrency=8, max_retries=3)
            r.port = port
            errs = in_threads([lambda w=w: port.scoped("q%d" % w).call(Task.ROUTE, route_inputs(w)) for w in range(8)])
            self.assertTrue(all(isinstance(e, cc.StageLimited) for e in errs), errs)
            # MEASURED locally, 8 runs each: shared count 5 to 8 refused charges; a per-call count 11 to 16
            self.assertLessEqual(port.limited, 8)
            self.assertLessEqual(b.count(), 8)

    def test_a_burst_of_throttles_in_flight_together_halves_and_goes_on(self):
        # 4 calls in flight are throttled together, more than max_retries: each backs off once and is asked again (a count shared
        # across workers halted the stage at the 4th, where 14.4 says a throttle halves the limit and the stage goes on). Five
        # non-zero exits in a row still stop it in the CLI port (MAX_CLI_FAILURE_STREAK), throttles included.
        with tempfile.TemporaryDirectory() as tmp:
            r = BurstThrottle(4, scale_ms=8)
            b, inner, port = capped(tmp, r, concurrency=4, adaptive=True, start_concurrency=4, max_retries=3)
            errs = in_threads([lambda w=w: port.scoped("q%d" % w).call(Task.ROUTE, route_inputs(w)) for w in range(4)])
            self.assertEqual(errs, [None] * 4)                                   # every call in flight was throttled, none answered
            self.assertEqual(port.throttled, 4)
            self.assertEqual(b.count(), 4 + 4)                                   # each throttled call charged once, then asked again
            self.assertEqual(port.slots.changes[0], (4, 2, "throttle"))

    def test_one_call_refused_again_and_again_stops_at_its_own_retries_while_others_answer(self):
        with tempfile.TemporaryDirectory() as tmp:
            r = RefuseOne("Prompt: question 5000\n", scale_ms=40, slow_refusal=0.03)
            b, inner, port = capped(tmp, r, concurrency=4, adaptive=True, start_concurrency=4, max_retries=3)
            fns = [lambda: port.scoped("x").call(Task.ROUTE, route_inputs(5000))]
            fns += [lambda w=w: [port.scoped("q%d" % w).call(Task.ROUTE, route_inputs(w * 100 + i)) for i in range(20)]
                    for w in range(1, 4)]
            errs = in_threads(fns)
            self.assertIsInstance(errs[0], cc.AllowanceRefused)
            self.assertEqual(r.refused, 1 + 3)                                   # the first refusal and max_retries retries

    def test_throttles_and_refusals_separated_by_answers_never_add_up_to_a_halt(self):
        with tempfile.TemporaryDirectory() as tmp:
            r = LimitAt(at=(20, 40, 60, 80, 100), scale_ms=8, throttle=(10, 50, 90, 110))
            b, inner, port = adaptive_port(tmp, r)
            self.assertEqual(run_workers(port), [None] * 12)
            self.assertEqual((port.limited, port.throttled), (5, 4))           # more refusals than max_retries, each one answered after

    def test_a_fixed_concurrency_stops_at_the_first_allowance_refusal_and_a_failing_cli_always_stops(self):
        with tempfile.TemporaryDirectory() as tmp:
            r = LimitAt(at=(10,), scale_ms=8)
            b, inner, port = capped(tmp, r, concurrency=4)
            errs = run_workers(port, workers=4)
            self.assertTrue(any(isinstance(e, cc.AllowanceRefused) for e in errs))
            self.assertEqual(port.limited, 1)
        with tempfile.TemporaryDirectory() as tmp:
            r = SafeRunner(crash_from=5)
            b, inner, port = adaptive_port(tmp, r)
            errs = run_workers(port, workers=4)
            self.assertTrue(any(isinstance(e, cc.StageLimited) and not isinstance(e, cc.AllowanceRefused) for e in errs))


# ---- ordered commits -----------------------------------------------------------------------------------------------------------------
class Ordered(unittest.TestCase):
    def test_commits_follow_item_order_whatever_finishes_first(self):
        done_order, committed = [], []

        def work(i):
            time.sleep((10 - i) * 0.004)        # later items finish first
            done_order.append(i)
            return i * i
        self.assertTrue(par.run_ordered(list(range(10)), work, lambda k, x, r: committed.append((k, x, r)), workers=4))
        self.assertEqual(committed, [(i, i, i * i) for i in range(10)])
        self.assertNotEqual(done_order, sorted(done_order))      # the test is not vacuous: completion order differed

    def test_the_earliest_failure_is_raised_and_only_the_prefix_is_committed(self):
        committed = []

        def work(i):
            time.sleep((10 - i) * 0.003)
            if i in (3, 5):
                raise KeyError(i)
            return i
        with self.assertRaises(KeyError) as c:
            par.run_ordered(list(range(10)), work, lambda k, x, r: committed.append(x), workers=4)
        self.assertEqual(c.exception.args, (3,))
        self.assertEqual(committed, [0, 1, 2])

    def test_the_stop_check_ends_submission_and_commits_what_finished(self):
        started, committed = [], []
        flag = {"stop": False}

        def work(i):
            started.append(i)
            if i == 2:
                flag["stop"] = True
            time.sleep(0.01)
            return i
        with self.assertRaises(StageStopped):
            par.run_ordered(list(range(20)), work, lambda k, x, r: committed.append(x), workers=2, stop=lambda: flag["stop"],
                            stop_exc=lambda: StageStopped("stop"))
        self.assertLessEqual(max(started), 3)
        self.assertEqual(committed, sorted(started))

    def test_a_rejected_answer_is_committed_last(self):
        out = par.ordered_map(3)(lambda i: CallResult(i != 4, {"i": i}), list(range(12)))
        self.assertEqual([r.output["i"] for r in out], [0, 1, 2, 3, 4])
        self.assertFalse(out[-1].ok)

    def test_concurrency_above_the_registered_value_needs_a_reason(self):
        self.assertEqual(par.REGISTERED_CONCURRENCY, 2)
        self.assertEqual(par.check_concurrency(2, ""), [])
        self.assertTrue(par.check_concurrency(3, " "))
        self.assertEqual(par.check_concurrency(8, "probe evidence"), [])
        self.assertTrue(par.check_concurrency(0, "x"))


# ---- the port under several workers --------------------------------------------------------------------------------------------------
class FakeInner:
    """A charged inner port whose answer carries its charge number (a stochastic model answers differently each time)."""
    def __init__(self, budget, delay=0.0):
        self.b, self.delay, self.calls = budget, delay, 0

    def call(self, task, inputs, *, ctx=None):
        n = self.b.charge()
        self.calls += 1
        time.sleep(self.delay)
        return CallResult(True, {"n": n})


class Port(unittest.TestCase):
    def test_concurrent_calls_are_charged_once_each_and_run_at_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            r = SafeRunner()
            b, inner, port = capped(tmp, r, concurrency=4)
            fns = [lambda w=w: [port.scoped("q%d" % w).call(Task.ROUTE, route_inputs(w * 100 + i)) for i in range(6)] for w in range(8)]
            self.assertEqual(in_threads(fns), [None] * 8)
            rows = [json.loads(l) for l in (Path(tmp) / "calls.jsonl").read_text().splitlines()]
            self.assertEqual(b.count(), 48)
            self.assertEqual(r.n, 48)
            self.assertEqual(sorted(x["n"] for x in rows), list(range(1, 49)))       # every charge number once: no racing counter
            self.assertEqual(port.fresh, 48)
            self.assertGreater(r.max_inflight, 1)                                     # calls really overlapped
            self.assertLessEqual(r.max_inflight, 4)                                   # never more than the concurrency

    def test_an_unscoped_call_above_one_worker_is_refused_before_any_charge(self):
        with tempfile.TemporaryDirectory() as tmp:
            b, _, port = capped(tmp, SafeRunner(), concurrency=2)
            with self.assertRaises(ScopeRequired):
                port.call(Task.ROUTE, route_inputs(1))
            self.assertEqual(b.count(), 0)

    def test_replayed_answers_go_to_the_same_scope_whatever_the_interleaving(self):
        """Two questions ask the same prompt three times each. Run 1: A first, then B. Resume (run 2): B first, then A. Each question
        must get back exactly its own answers; with one shared occurrence key A would get B's."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            b = Budget(tmp / "counter", 100)
            got = {}

            def ask(port, scope):
                got[scope] = [port.scoped(scope).call(Task.ROUTE, route_inputs(7)).output["n"] for _ in range(3)]
            p1 = ResilientPort(FakeInner(b), b, Journal(tmp / "j.jsonl"), tmp / "STOP", concurrency=2)
            ask(p1, "qa")
            ask(p1, "qb")
            first = dict(got)
            p2 = ResilientPort(FakeInner(b), b, Journal(tmp / "j.jsonl"), tmp / "STOP", concurrency=2)
            ask(p2, "qb")
            ask(p2, "qa")
            self.assertEqual(got, first)
            self.assertEqual(b.count(), 6)                    # the resume charged nothing
            self.assertEqual(p2.replayed, 6)

    def test_a_throttle_makes_every_worker_wait_and_each_call_is_charged_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            seen = []
            holder = {}

            def sleep(d):                       # while one worker backs off, no other worker may charge
                c0 = holder["b"].count()
                time.sleep(0.08)
                seen.append((c0, holder["b"].count()))
            r = SafeRunner(throttle=(3,), scale_ms=20)
            b, inner, port = capped(tmp, r, concurrency=4, sleep=sleep)
            holder["b"] = b
            fns = [lambda w=w: [port.scoped("q%d" % w).call(Task.ROUTE, route_inputs(w * 100 + i)) for i in range(5)] for w in range(4)]
            self.assertEqual(in_threads(fns), [None] * 4)
            self.assertEqual(port.throttled, 1)
            self.assertEqual(len(seen), 1)
            # during the back-off only calls already past the gate when it began may still be charged (at most one per other worker);
            # without the shared gate the other three workers keep charging all through the 80 ms sleep
            self.assertLessEqual(seen[0][1] - seen[0][0], 3)
            rows = (Path(tmp) / "calls.jsonl").read_text().splitlines()
            self.assertEqual(b.count(), r.n)
            self.assertEqual(b.count(), len(rows))
            self.assertEqual(b.count(), 20 + 1)               # 20 answered calls and the one throttled call, charged once

    def test_the_stop_file_stops_every_worker_before_its_next_charge(self):
        with tempfile.TemporaryDirectory() as tmp:
            stop = Path(tmp) / "STOP"
            r = SafeRunner(stop_at=6, stop_file=stop)
            b, _, port = capped(tmp, r, concurrency=4)
            fns = [lambda w=w: [port.scoped("q%d" % w).call(Task.ROUTE, route_inputs(w * 100 + i)) for i in range(20)] for w in range(4)]
            errs = in_threads(fns)
            self.assertTrue(all(isinstance(e, StageStopped) for e in errs), errs)
            self.assertLessEqual(r.after_stop, 3)             # at most the calls already past the gate when the file appeared
            self.assertLess(b.count(), 6 + 4)
            self.assertEqual(b.count(), r.n)

    def test_the_cap_halts_every_worker(self):
        with tempfile.TemporaryDirectory() as tmp:
            r = SafeRunner()
            b, _, port = capped(tmp, r, cap=10, concurrency=4)
            fns = [lambda w=w: [port.scoped("q%d" % w).call(Task.ROUTE, route_inputs(w * 100 + i)) for i in range(20)] for w in range(4)]
            errs = in_threads(fns)
            self.assertTrue(all(isinstance(e, BudgetExceeded) for e in errs), errs)
            self.assertEqual(b.count(), 10)
            self.assertEqual(r.n, 10)

    def test_a_resume_after_a_kill_with_calls_in_flight_never_charges_twice(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            b = Budget(tmp / "counter", 100)
            for _ in range(5):
                b.charge()
            rows = [{"t": "pending", "key": "q%d|k:0" % i, "task": "route", "count_before": 2 + i, "ts": 0} for i in range(3)]
            (tmp / "j.jsonl").write_text("".join(json.dumps(x) + "\n" for x in rows))
            p = ResilientPort(FakeInner(b), b, Journal(tmp / "j.jsonl"), tmp / "STOP", concurrency=3,
                              render=lambda name, i: "k")
            with mock.patch("evalkit.s2_ports.hashlib.sha256") as h:
                h.return_value.hexdigest.return_value = "k"
                outs = [p.scoped("q%d" % i).call(Task.ROUTE, {}) for i in range(3)]
            self.assertEqual([o.error for o in outs], ["lost_in_flight"] * 3)
            self.assertEqual(b.count(), 5)                    # none of the three was asked again
            self.assertEqual(p.lost, 3)

    def test_pending_calls_that_were_never_charged_are_asked_again(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            b = Budget(tmp / "counter", 100)
            for _ in range(2):
                b.charge()
            rows = [{"t": "pending", "key": "q%d|k:0" % i, "task": "route", "count_before": 2, "ts": 0} for i in range(2)]
            (tmp / "j.jsonl").write_text("".join(json.dumps(x) + "\n" for x in rows))
            p = ResilientPort(FakeInner(b), b, Journal(tmp / "j.jsonl"), tmp / "STOP", concurrency=2, render=lambda name, i: "k")
            with mock.patch("evalkit.s2_ports.hashlib.sha256") as h:
                h.return_value.hexdigest.return_value = "k"
                outs = [p.scoped("q%d" % i).call(Task.ROUTE, {}) for i in range(2)]
            self.assertTrue(all(o.ok for o in outs))
            self.assertEqual(b.count(), 4)


# ---- the build ----------------------------------------------------------------------------------------------------------------------
def canonical(tree: build.Tree) -> str:
    """The tree with node ids replaced by their creation index: equal trees have equal text whatever ids were drawn."""
    names = {}
    for ref in tree.nodes:
        names[ref.split(":")[0]] = "n%d" % len(names)
    nm = lambda i: names.get(i, i)
    nodes = []
    for ref, n in tree.nodes.items():
        nodes.append({"id": nm(n.node_id), "level": n.level, "summary": n.summary, "title": n.title, "total": n.total_tokens,
                      "children": [dict(c, child_id=nm(c["child_id"])) for c in n.children], "lin_leaf_ids": n.lin_leaf_ids,
                      "lin_nodes": [nm(x.split(":")[0]) for x in n.lin_nodes], "facts": n.facts})
    return json.dumps({"nodes": nodes, "top": [nm(r.split(":")[0]) for r in tree.top], "levels": tree.levels,
                       "members": [[nm(p.split(":")[0]), l, v, s] for p, l, v, s in tree.members]}, sort_keys=True)


class JitterStub:
    """The stub model with a per-call delay: answers by content, finishing order shuffled."""
    def __init__(self):
        self._tmp = tempfile.mkdtemp()
        self.inner = ModelPort(StubBackend(), Budget(Path(self._tmp) / "b", 10 ** 6), ClassLattice(["base"], []), DictStore())
        self.lock, self.calls = threading.Lock(), 0

    def call(self, task, inputs, *, ctx=None):
        time.sleep(jitter(json.dumps(inputs, sort_keys=True)[:400], 8))
        with self.lock:
            self.calls += 1
            return self.inner.call(task, inputs, ctx=ctx)


def seeded():
    state = {"i": 0}

    def src(n):
        state["i"] += 1
        return hashlib.sha256(b"seed%d" % state["i"]).digest()[:n]
    return IdGenerator(src)


class Build(unittest.TestCase):
    def leaves(self, n=300):
        from evalkit import corpus
        from tests.unit import test_s6h1_release as rel
        from evalkit import dataset as ds
        pool = []
        for q in rel.QS:
            for _, t in q.sessions:
                pool.append((ds.turns_hash(t), t))
        return corpus.MemCorpus(pool).leaves[:n]

    def test_a_parallel_build_is_the_serial_tree(self):
        lv = self.leaves()
        c = Constants(f_target=4, f_max=6)                          # small fan-out: several upper levels from the small pool
        serial = build.build_tree(lv, JitterStub(), "t", "base", consts=c, idgen=seeded())
        para = build.build_tree(lv, JitterStub(), "t", "base", consts=c, idgen=seeded(), pmap=par.ordered_map(4))
        self.assertGreater(len(serial.nodes), 10)
        self.assertGreaterEqual(serial.levels, 2)
        self.assertEqual(canonical(para), canonical(serial))
        self.assertEqual(list(para.nodes), list(serial.nodes))      # with a seeded id generator the ids themselves are equal
        self.assertEqual(para.calls, serial.calls)

    def test_each_group_gets_its_own_port_key(self):
        keys = []
        stub = JitterStub()

        def port_for(k):
            keys.append(k)
            return stub
        t = build.build_tree(self.leaves(), stub, "t", "base", pmap=par.ordered_map(3), port_for=port_for)
        self.assertEqual(len(keys), t.calls)
        self.assertEqual(len(set(keys)), len(keys))
        self.assertTrue(all(k.startswith(("l1:", "u")) for k in keys))

    def test_the_first_failed_group_in_order_is_reported_and_later_groups_are_not_asked(self):
        stub = JitterStub()
        asked = []

        class Failing:
            def __init__(self, k):
                self.k = k

            def call(self, task, inputs, *, ctx=None):
                asked.append(self.k)
                if self.k in ("l1:2", "l1:9"):
                    return CallResult(False, error="boom %s" % self.k)
                return stub.call(task, inputs, ctx=ctx)
        with self.assertRaises(build.BuildFailed) as c:
            build.build_tree(self.leaves(), stub, "t", "base", pmap=par.ordered_map(2), port_for=Failing)
        self.assertIn("l1:2", str(c.exception))
        self.assertLess(len(asked), 9)
        asked.clear()
        with self.assertRaises(build.BuildFailed):                 # one worker: nothing after the failed group is asked
            build.build_tree(self.leaves(), stub, "t", "base", port_for=Failing)
        self.assertEqual(asked, ["l1:0", "l1:1", "l1:2"])


# ---- the stage: concurrency 1 and N give the same bytes ---------------------------------------------------------------------------------
def seeded_node_ids():
    """Node ids drawn from a seed, in the order nodes are made. Node ids enter the recall's tie-breaks (candidate paths), so two runs
    are comparable byte for byte only with the same ids: with random ids even two one-worker runs differ."""
    gen = seeded()
    return mock.patch.object(build, "new_node_id", lambda g=None: gen.new_id())


@mock.patch.object(rs, "EXPECTED_LEAVES", 1)
class Stage(unittest.TestCase):
    def setUp(self):
        self._ids = seeded_node_ids()
        self._ids.start()
        self.addCleanup(self._ids.stop)

    def reseed(self):
        self._ids.stop()
        self._ids = seeded_node_ids()
        self._ids.start()

    def go(self, n, runner=None, **kw):
        self.reseed()
        base, s3, man, run = t6.setup_dirs()
        t6.proof(base / "mem.json")
        a = t6.args(base, s3, run, concurrency=n, concurrency_reason="test" if n > 2 else "", **kw)
        rc = rs.run(a, embedder=t6.RealEmbed(), runner=runner or SafeRunner(), loader=lambda _a: t6.rel.QS,
                    tenant_factory=t6.factory(run), registered_pool=t6.POOL, registered_fresh=t6.rel.REG, manifest_path=man)
        return rc, base, s3, man, run

    def outputs(self, run):
        res = run / "results"
        return {"ledger": (run / "done.jsonl").read_bytes(), "tree": canonical(t6._TENANTS[str(run)].tree),
                "decision": (res / "h1_decision.json").read_bytes(),
                "arms": {p.name: p.read_bytes() for p in sorted(res.glob("arm_*.json"))}}

    def test_one_worker_and_four_workers_give_byte_identical_outputs(self):
        rc1, *_, run1 = self.go(1)
        r4 = SafeRunner()
        real = cluster.balanced_groups
        with mock.patch.object(cluster, "balanced_groups", side_effect=lambda v, f, **kw: real(v, f, **kw)) as spy:
            rc4, *_, run4 = self.go(4, runner=r4, cluster_workers=3)
        self.assertEqual({c.kwargs.get("workers") for c in spy.call_args_list}, {3})     # every level clustered on three threads
        self.assertEqual(json.loads((run4 / "starts.jsonl").read_text().splitlines()[-1])["cluster_workers"], 3)
        self.assertEqual((rc1, rc4), (0, 0))
        o1, o4 = self.outputs(run1), self.outputs(run4)
        self.assertGreater(r4.max_inflight, 1)                     # the four-worker run really overlapped calls
        for k in o1:
            self.assertEqual(o4[k], o1[k], k)
        c1 = len((run1 / "calls.jsonl").read_text().splitlines())
        c4 = len((run4 / "calls.jsonl").read_text().splitlines())
        self.assertEqual(c4, c1)                                   # the same calls, each charged once
        self.assertEqual(json.loads((run4 / "STAMP.json").read_text())["concurrency"], 4)

    def test_a_four_worker_run_stopped_and_resumed_matches_one_worker_without_charging_twice(self):
        rc1, *_, run1 = self.go(1)
        n_ref = len((run1 / "calls.jsonl").read_text().splitlines())
        base, s3, man, run = t6.setup_dirs()
        t6.proof(base / "mem.json")
        a = t6.args(base, s3, run, concurrency=4, concurrency_reason="test")
        mk = lambda r: rs.run(a, embedder=t6.RealEmbed(), runner=r, loader=lambda _a: t6.rel.QS, tenant_factory=t6.factory(run),
                              registered_pool=t6.POOL, registered_fresh=t6.rel.REG, manifest_path=man)
        self.reseed()
        self.assertEqual(mk(SafeRunner(stop_at=n_ref // 2, stop_file=run / "STOP")), 3)
        (run / "STOP").unlink()
        a.resume = True
        self.assertEqual(mk(SafeRunner()), 0)
        self.assertEqual(self.outputs(run)["ledger"], self.outputs(run1)["ledger"])
        self.assertEqual(self.outputs(run)["decision"], self.outputs(run1)["decision"])
        rows = (run / "calls.jsonl").read_text().splitlines()
        self.assertEqual(int((run / "counter").read_text()), len(rows))
        st = json.loads((run / "STATE.json").read_text())
        # every charged call is either answered once or lost in flight; nothing was charged twice
        self.assertLessEqual(len(rows), n_ref + st["lost_in_flight"] + 4)


if __name__ == "__main__":
    unittest.main()
