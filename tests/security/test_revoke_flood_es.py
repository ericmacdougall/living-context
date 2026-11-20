"""BUILD-PLAN Step 15, probe P14 closed: the gateway's revoke is effective at once while the node itself has applied the disk
flood-stage block to the authz index (DESIGN 4.4; SEC-R3). Probe P14 established only the premise (writes refused, reads work).

The module starts its own throw-away security-off Elasticsearch node (named by LC_SCRATCH_CONTAINER, published at LC_SCRATCH_URL,
image taken from the container named by LC_ES_CONTAINER) whose data directory is a small tmpfs, sets absolute watermarks, fills the
volume until the node blocks the index, runs the checks, and removes the node. It FAILS (never skips) when the environment does not
name them. Broken variant: a revoke that needs the Elasticsearch write first is not effective under the block."""
import json
import os
import subprocess
import tempfile
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from living_context.gateway import epoch_journal
from living_context.gateway.fence import SendFence
from living_context.gateway.revocation import OPEN, RevocationService
from living_context.gateway.sessions import EpochTable

URL = os.environ.get("LC_SCRATCH_URL", "")
NAME = os.environ.get("LC_SCRATCH_CONTAINER", "")
MAIN = os.environ.get("LC_ES_CONTAINER", "")
T = "tenp"
DATA = "/usr/share/elasticsearch/data"


def sreq(method, path, body=None, timeout=30):
    data = None if body is None else json.dumps(body).encode()
    r = urllib.request.Request(URL.rstrip("/") + path, data=data, method=method, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(r, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")
    except (urllib.error.URLError, OSError):
        return 0, {}


def docker(*args, timeout=120):
    return subprocess.run(["docker", *args], capture_output=True, text=True, timeout=timeout)


class RevokeUnderNodeAppliedBlock(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not (URL and NAME and MAIN):
            raise RuntimeError("LC_SCRATCH_URL, LC_SCRATCH_CONTAINER and LC_ES_CONTAINER must be set")
        port = urllib.parse.urlparse(URL).port
        image = docker("inspect", "--format", "{{.Config.Image}}", MAIN).stdout.strip()
        if not image or docker("ps", "-aq", "--filter", f"name=^{NAME}$").stdout.strip():
            raise RuntimeError("no image to copy, or the scratch container name is already taken")
        cls.addClassCleanup(lambda: docker("rm", "-f", NAME))
        r = docker("run", "-d", "--name", NAME, "-p", f"127.0.0.1:{port}:9200", "--memory", "1500m",
                   "-e", "discovery.type=single-node", "-e", "xpack.security.enabled=false", "-e", "ES_JAVA_OPTS=-Xms512m -Xmx512m",
                   "--mount", f"type=tmpfs,destination={DATA},tmpfs-size=160m", image)
        if r.returncode:
            raise RuntimeError("scratch node did not start")
        t0 = time.time()
        while time.time() - t0 < 180 and sreq("GET", "/_cluster/health?wait_for_status=yellow&timeout=5s")[0] != 200:
            time.sleep(2)
        st, _ = sreq("PUT", "/_cluster/settings", {"persistent": {
            "cluster.info.update.interval": "10s", "cluster.routing.allocation.disk.watermark.low": "70mb",
            "cluster.routing.allocation.disk.watermark.high": "60mb", "cluster.routing.allocation.disk.watermark.flood_stage": "50mb"}})
        assert st == 200, "watermark settings refused"
        ix = f"lc-{T}-authz"
        sreq("PUT", f"/{ix}", {"settings": {"number_of_shards": 1, "number_of_replicas": 0}})
        st, _ = sreq("PUT", f"/{ix}/_doc/before?refresh=true", {"principal": "before"})
        assert st in (200, 201), "the authz index is not writable before the fill"
        docker("exec", NAME, "sh", "-c", f"dd if=/dev/zero of={DATA}/fill.bin bs=1M count=120 2>&1 | tail -1")
        t0, cls.blocked_after = time.time(), None
        while time.time() - t0 < 150:
            st, _ = sreq("PUT", f"/{ix}/_doc/probe", {"principal": "probe"})
            if st in (403, 429):
                cls.blocked_after = round(time.time() - t0, 1)
                break
            time.sleep(1)
        if cls.blocked_after is None:
            raise RuntimeError("the node never applied the flood-stage block")

    def persist(self, principal, epoch):
        st, _ = sreq("PUT", f"/lc-{T}-authz/_doc/ep-{principal}", {"principal": principal, "epoch": epoch})
        if st not in (200, 201):
            raise OSError(f"authz persist refused ({st})")

    def check_revoke(self, **kw):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        d = Path(tmp.name) / T
        epoch_journal.provision(d, 16384)
        ep = EpochTable(lambda t: SendFence())
        svc = RevocationService(T, ep, d, persist=self.persist, **kw)
        self.addCleanup(lambda: svc._j and svc._j.close())
        self.assertEqual(svc.start(), OPEN)
        self.assertEqual(sreq("PUT", f"/lc-{T}-authz/_doc/x", {"principal": "x"})[0] in (403, 429), True, "premise: the block is in force")
        t0, rc = time.time(), None
        try:
            rc = svc.revoke("victim")
        except Exception:
            pass
        took = time.time() - t0
        self.assertTrue(ep.is_revoked(T, "victim"), "the revoke was not effective while the authz index refused writes")
        self.assertFalse(ep.current(T, "victim", 0))
        self.assertLess(took, 2.0, "the revoke was slower than its 2 s bound under the block")
        self.assertIsNotNone(rc.durable_at, "the journal did not hold the revoke")
        self.assertIs(rc.persisted, False)
        self.assertEqual(svc.persist_failures, 1)
        d2 = EpochTable(lambda t: SendFence())   # a restart: the table rebuilt from the journal alone holds the revoke
        s2 = RevocationService(T, d2, d)
        svc.clean_stop()
        self.addCleanup(lambda: s2._j and s2._j.close())
        self.assertEqual(s2.start(), OPEN)
        self.assertTrue(d2.is_revoked(T, "victim"))

    def test_the_gateways_revoke_is_effective_durable_and_fast_while_the_node_blocks_the_authz_index(self):
        self.check_revoke()
        print(f"P14: the node applied the block {self.blocked_after} s after the fill; the revoke was effective under it")

    def test_broken_revoke_that_needs_the_elasticsearch_write_first_is_red(self):
        with self.assertRaises(AssertionError) as cm:
            self.check_revoke(broken_require_persist=True)
        self.assertIn("not effective while the authz index refused writes", str(cm.exception))


if __name__ == "__main__":
    unittest.main()
