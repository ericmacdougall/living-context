"""Review finding M2: two parallel raises of one conversation's taint both land (the stored value is joined inside a compare-and-set loop),
so the taint never falls and the write floor holds. The broken control is a durable store that ignores the stored value (last writer wins)."""
import threading
import unittest
from types import SimpleNamespace

from living_context.gateway.session_state import SessionStates, Taint
from living_context.policy import ClassLattice

T = "tenaaa"
LAT = {T: ClassLattice(["base", "restricted"], [("restricted", "base")])}
REC = SimpleNamespace(tenant=T, principal_token="pt-alice", conversation="c1", hash="h1")
R = SimpleNamespace(id="r" * 16, cls="restricted", private=False)
P = SimpleNamespace(id="p" * 16, cls="base", private=True)


class Store:
    """A durable copy with a real compare-and-set; `gate` holds the first two writers between their read and their write."""

    def __init__(self, *, blind=False):
        self.v, self.seq, self.lock, self.blind = None, 0, threading.Lock(), blind
        self.gate = threading.Barrier(2, timeout=5)
        self.first = threading.local()

    def persist(self, key, merge):
        n = 0
        while True:
            with self.lock:
                stored, seq = self.v, self.seq
            if not getattr(self.first, "done", False):
                self.first.done = True
                try:
                    self.gate.wait()     # both raisers have read the same stored value
                except threading.BrokenBarrierError:
                    pass
            t = merge(None if self.blind else stored)
            with self.lock:
                if self.seq == seq or self.blind:
                    self.v, self.seq = t, self.seq + 1
                    return t
            n += 1

    def load(self, key):
        return self.v


class Race(unittest.TestCase):
    def run_race(self, store):
        st = SessionStates(LAT, store.persist, store.load)
        base = st.get(REC)
        news = [st.raised_by(REC, [R]), st.raised_by(REC, [P])]      # both computed from the floor, before either is stored
        ts = [threading.Thread(target=st.raise_to, args=(REC, n)) for n in news]
        [t.start() for t in ts]
        [t.join() for t in ts]
        return st, base

    def test_parallel_raises_both_land_and_the_write_floor_holds(self):
        st, base = self.run_race(Store())
        got = st.get(REC)
        self.assertEqual((got.cls, set(got.private)), ("restricted", {P.id}))
        self.assertTrue(st.blocks_write(REC, P))                      # content read from R cannot be written down into P
        st.forget_memory()
        again = st.get(REC)                                           # and the durable copy says the same
        self.assertEqual((again.cls, set(again.private)), ("restricted", {P.id}))

    def test_broken_a_store_that_ignores_the_stored_value_lets_the_taint_fall(self):
        st, _ = self.run_race(Store(blind=True))
        st.forget_memory()
        got = st.get(REC)
        self.assertNotEqual((got.cls, set(got.private)), ("restricted", {P.id}))   # last writer won: the defect is observable

    def test_a_raise_never_lowers(self):
        store = Store()
        store.first.done = True
        st = SessionStates(LAT, store.persist, store.load)
        st.raise_to(REC, Taint("restricted", frozenset({P.id})))
        st.raise_to(REC, Taint("base", frozenset()))
        got = st.get(REC)
        self.assertEqual((got.cls, set(got.private)), ("restricted", {P.id}))


if __name__ == "__main__":
    unittest.main()
