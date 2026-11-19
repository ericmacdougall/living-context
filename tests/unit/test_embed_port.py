"""The embedding port waits in the per-tenant fair queue (BUILD-PLAN Step 8; AC-17)."""
import threading
import time
import unittest

from living_context.embed import stub
from living_context.embed.port import EmbedPort
from living_context.model.fairq import FairLimiter, FifoLimiter


def victim_wait(limiter):
    def slow(text):
        time.sleep(0.03)
        return stub.embed(text)

    port = EmbedPort(limiter, slow)
    flood = [threading.Thread(target=port.embed, args=("flood", f"w{i}")) for i in range(30)]
    for t in flood:
        t.start()
    time.sleep(0.05)
    t0, done = time.time(), []
    v = threading.Thread(target=lambda: (port.embed("victim", "hello world"), done.append(time.time())))
    v.start()
    v.join(30)
    [t.join(30) for t in flood]
    return done[0] - t0


class EmbedFair(unittest.TestCase):
    def test_port_returns_the_stub_vector_and_counts(self):
        port = EmbedPort()
        self.assertEqual(port.for_tenant("tena")("garden tomato"), stub.embed("garden tomato"))
        self.assertEqual(port.calls, 1)

    def test_a_flooding_tenant_does_not_delay_another(self):
        self.assertLess(victim_wait(FairLimiter(2)), 0.15)

    def test_broken_single_fifo_queue_is_caught(self):
        self.assertGreater(victim_wait(FifoLimiter(2)), 0.25)


if __name__ == "__main__":
    unittest.main()
