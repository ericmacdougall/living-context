"""Review (declared open item): one process serves a tenant's epoch journal. A second process that finds it locked refuses to start."""
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

from living_context.gateway import epoch_journal
from living_context.gateway.fence import SendFence
from living_context.gateway.revocation import OPEN, RevocationService
from living_context.gateway.sessions import EpochTable

CHILD = textwrap.dedent("""
    import sys
    from pathlib import Path
    from living_context.gateway.fence import SendFence
    from living_context.gateway.revocation import RevocationService
    from living_context.gateway.sessions import EpochTable
    r = RevocationService("tenaaa", EpochTable(lambda t: SendFence()), Path(sys.argv[1]))
    print(r.start(), r.fault or "")
""")


class JournalLock(unittest.TestCase):
    def child(self, d):
        return subprocess.run([sys.executable, "-c", CHILD, str(d)], capture_output=True, text=True, timeout=60).stdout.strip()

    def test_a_second_process_does_not_start_while_the_first_holds_the_journal(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp) / "j"
            epoch_journal.provision(d, 65536)
            a = RevocationService("tenaaa", EpochTable(lambda t: SendFence()), d)
            self.assertEqual(a.start(), OPEN)
            out = self.child(d)
            self.assertTrue(out.startswith("fault_closed"), out)     # refused: it would serve stale grants, revocations and taint
            self.assertIn("another process", out)
            a.clean_stop()
            self.assertTrue(self.child(d).startswith("open"))        # the lock went with the clean stop


if __name__ == "__main__":
    unittest.main()
