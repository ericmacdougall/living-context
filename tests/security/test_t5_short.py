"""SEC-T5 short form (BUILD-PLAN Step 5): a smoke check that each broken control turns red.

Never reported as a T5 pass: the full measurement (3 runs of 10 minutes per flood kind with an
unloaded baseline) is the S0-load checkpoint. Here each flood runs a few seconds against a
latency-injecting dispatch (20 ms) and tenant B's p95 must stay under 3x its unloaded p95.
"""
import json
import socket
import threading
import time
import unittest
from unittest import mock

from living_context.gateway import admission, fence
from living_context.gateway.server import Server
from living_context.gateway.sessions import EpochTable, Sessions
from living_context.policy import Principal, Role


class Adapter:
    def verify(self, tenant, proof):
        return "u"


class SlowHandler:
    def handle(self, token, req):
        time.sleep(0.02)
        return {"ok": True}


def build(adm):
    fences = {}
    ff = lambda t: fences.setdefault(t, fence.SendFence())
    ep = EpochTable(ff)
    grants = {t: Principal(t, "tok", frozenset([Role.READER]), frozenset(["base"]), frozenset()) for t in ("tenaaa", "tenbbb")}
    s = Sessions({t: Adapter() for t in grants}, ep, lambda t, who: "tok", lambda t, p: grants.get(t))
    srv = Server(SlowHandler(), s, ep, ff, adm=adm, idle_s=1.0).start()
    return srv, {t: s.mint(t, b"x") for t in grants}


def call(addr, token, n=1, timeout=5):
    """n sequential requests on one connection; returns latencies (None on refusal)."""
    out = []
    c = socket.create_connection(addr, timeout=timeout)
    f = c.makefile("rb")
    try:
        for _ in range(n):
            t0 = time.time()
            c.sendall((json.dumps({"token": token, "request": {"op": "ping"}}) + "\n").encode())
            line = f.readline()
            ok = b'"ok": true' in line
            out.append(time.time() - t0 if ok else None)
    except OSError:
        out.append(None)
    finally:
        c.close()
    return out


def p95(xs):
    xs = sorted(x for x in xs if x is not None)
    return xs[int(0.95 * (len(xs) - 1))] if xs else float("inf")


class FloodSmoke(unittest.TestCase):
    def run_flood(self, adm, flood):
        srv, tok = build(adm)
        try:
            base = call(srv.address, tok["tenbbb"], 30)
            stop = threading.Event()
            threads = flood(srv, tok, stop)
            time.sleep(0.5)
            loaded = []
            for _ in range(3):
                loaded += call(srv.address, tok["tenbbb"], 15)
            stop.set()
            [t.join(5) for t in threads]
            return p95(base), loaded, srv
        finally:
            srv.stop()

    @staticmethod
    def concurrent_requests(srv, tok, stop):
        def worker():
            while not stop.is_set():
                call(srv.address, tok["tenaaa"], 3)
        threads = [threading.Thread(target=worker) for _ in range(60)]
        [t.start() for t in threads]
        return threads

    def test_concurrent_recall_flood_real_limits_keep_tenant_b_within_bound(self):
        adm = admission.Admission(slots=4, queue=8, connections=8)
        base, loaded, _ = self.run_flood(adm, self.concurrent_requests)
        served = [x for x in loaded if x is not None]
        self.assertEqual(len(served), len(loaded))  # every tenant B request served
        self.assertLess(p95(loaded), max(3 * base, 0.25))

    def test_broken_global_pool_starves_tenant_b(self):
        class Global(admission.Admission):
            @staticmethod
            def _t(tenant):
                return b"all"
        adm = Global(slots=4, queue=8, connections=1000)
        base, loaded, _ = self.run_flood(adm, self.concurrent_requests)
        refused = sum(1 for x in loaded if x is None)
        self.assertTrue(refused > 0 or p95(loaded) > max(3 * base, 0.25))

    def test_eight_idle_connections_do_not_block_tenant_b_and_are_closed_by_the_deadline(self):
        adm = admission.Admission(slots=4, queue=8, connections=8)

        def idle(srv, tok, stop):
            socks = [socket.create_connection(srv.address) for _ in range(8)]
            threading.Thread(target=lambda: (stop.wait(), [s.close() for s in socks])).start()
            return []
        base, loaded, srv = self.run_flood(adm, idle)
        self.assertEqual([x for x in loaded if x is None], [])

    def test_oversized_line_is_not_read_beyond_the_cap_and_broken_unbounded_read_is(self):
        big = b'{"token": "x", "request": "' + b"a" * (20 << 20) + b'"}\n'
        results = {}
        for broken in (False, True):
            srv, tok = build(admission.Admission())
            try:
                ctx = mock.patch.object(admission, "read_line", lambda stream, m: stream.readline()) if broken else mock.MagicMock()
                with ctx:
                    c = socket.create_connection(srv.address)
                    try:
                        c.sendall(big)
                    except OSError:
                        pass
                    time.sleep(1.0)
                    c.close()
                results[broken] = srv.bytes_read
                self.assertEqual(len(call(srv.address, tok["tenbbb"], 1)), 1)
            finally:
                srv.stop()
        self.assertEqual(results[False], 0)  # refused before any byte past the cap was counted or parsed
        self.assertGreater(results[True], 1 << 20)  # the broken variant read the whole line

    def test_connection_cap_and_oversized_line_leave_other_tenants_untouched(self):
        adm = admission.Admission(slots=4, queue=8, connections=2)
        srv, tok = build(adm)
        try:
            held = []
            for _ in range(2):
                c = socket.create_connection(srv.address)
                c.sendall((json.dumps({"token": tok["tenaaa"], "request": {"op": "ping"}}) + "\n").encode())
                c.makefile("rb").readline()
                held.append(c)
            c = socket.create_connection(srv.address)
            c.sendall((json.dumps({"token": tok["tenaaa"], "request": {"op": "ping"}}) + "\n").encode())
            self.assertIn(b"refused", c.makefile("rb").readline())
            self.assertEqual([x is not None for x in call(srv.address, tok["tenbbb"], 3)], [True] * 3)
            [h.close() for h in held]
        finally:
            srv.stop()


if __name__ == "__main__":
    unittest.main()
