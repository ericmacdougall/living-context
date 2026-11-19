"""Every probe that talks to a cluster says "security off: P2 not established" against an open one."""
import http.server
import os
import subprocess
import sys
import threading
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SCRIPTS = ["probe_p1_licence.py", "probe_p4_percolate_knn.py", "probe_refresh.py", "probe_p7_knn_filter.py",
           "probe_p8_alias_swap.py", "probe_p9_p10_expunge.py", "probe_p11_seqno.py", "probe_p12_msearch.py",
           "probe_p13_terms.py", "probe_p14_authz_block.py", "probe_p15_key_owner.py", "probe_p16_slowlogs.py"]


class Open(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b'{"tagline":"open"}')

    def log_message(self, *a):
        pass


class SecurityOffTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Open)
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.url = f"http://127.0.0.1:{cls.srv.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    def run_script(self, name, *args):
        env = dict(os.environ, LC_TEST_ES_URL=self.url, LC_TEST_CA="", LC_TEST_PW="", PYTHONPATH=str(REPO))
        return subprocess.run([sys.executable, str(REPO / "tools" / "probes" / name), *args], env=env,
                              capture_output=True, text=True, timeout=60, cwd=str(REPO))

    def test_each_probe_refuses_an_open_cluster(self):
        for name in SCRIPTS:
            r = self.run_script(name)
            self.assertEqual(r.returncode, 2, (name, r.stderr[-300:]))
            self.assertIn("security off: P2 not established", r.stdout, name)
            self.assertNotIn('"verdict": "PASS"', r.stdout, name)

    def test_isolation_probe_refuses_an_open_cluster(self):
        r = self.run_script("probe_isolation.py", self.url, "", "/nonexistent")
        self.assertEqual(r.returncode, 2, r.stderr[-300:])
        self.assertIn("security off: P2 not established", r.stdout)


if __name__ == "__main__":
    unittest.main()
