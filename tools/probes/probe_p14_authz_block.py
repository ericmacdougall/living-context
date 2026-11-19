"""Probe P14: the Elasticsearch side of "revoke must not depend on the authz write".

DESIGN 4.4: revocation takes effect from the gateway's own table and the
Elasticsearch persist is retried. What this probe establishes is the premise
and nothing more: when `lc-<t>-authz` is blocked, writes to it are refused
while reads still work, both for a manual `read_only_allow_delete` block on
the secured test cluster and for the block Elasticsearch applies itself at
the flood-stage disk watermark on a scratch volume (a second, throw-away,
security-off node whose data directory is a 160 MB tmpfs; the watermarks are
set in absolute bytes, the monitor interval at its 10 s minimum, and the volume is filled until the node blocks the
index). That the gateway's revoke is effective at once under these conditions
is tested at Step 15 (revocation tests with the authz index blocked and with
the flood-stage fill); this probe does not claim it.

Checks (N=3): manual block refuses writes; reads work under the block;
flood-stage block applied by the node refuses writes while reads work.
Environment: LC_TEST_* for the secured cluster; LC_SCRATCH_URL (http) and
LC_SCRATCH_CONTAINER for the scratch node. Without them the flood check is
not run, N is short, and the result is ERROR.
"""
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from tools.probes import pcommon  # noqa: E402

pcommon.start()
es = pcommon.admin_client()
SCRATCH = os.environ.get("LC_SCRATCH_URL", "")
SC_NAME = os.environ.get("LC_SCRATCH_CONTAINER", "")


def sreq(method, path, body=None):
    data = None if body is None else json.dumps(body).encode()
    r = urllib.request.Request(SCRATCH.rstrip("/") + path, data=data, method=method,
                               headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(r, timeout=30) as resp:
            return resp.status, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def manual_block():
    ix = f"lc-tenp-authz-{uuid.uuid4().hex[:6]}"
    pcommon.create(es, ix, {"p": {"type": "keyword"}})
    pcommon.bulk_index(es, ix, [("rec", {"p": "reader1"})])
    es.request("PUT", f"/{ix}/_settings", {"index.blocks.read_only_allow_delete": "true"})
    st_w, _ = es.request("PUT", f"/{ix}/_doc/rec2", {"p": "reader2"})
    st_r, r = es.request("GET", f"/{ix}/_doc/rec")
    st_s, s = es.request("POST", f"/{ix}/_search", {"query": {"match_all": {}}})
    es.request("PUT", f"/{ix}/_settings", {"index.blocks.read_only_allow_delete": None})
    st_w2, _ = es.request("PUT", f"/{ix}/_doc/rec3", {"p": "reader3"})
    pcommon.drop(es, ix)
    return st_w, st_r == 200 and st_s == 200 and s["hits"]["total"]["value"] == 1, st_w2


def flood():
    if not (SCRATCH and SC_NAME):
        return None
    st_set, _ = sreq("PUT", "/_cluster/settings", {"persistent": {
        "cluster.info.update.interval": "10s", "cluster.routing.allocation.disk.watermark.low": "70mb",
        "cluster.routing.allocation.disk.watermark.high": "60mb",
        "cluster.routing.allocation.disk.watermark.flood_stage": "50mb"}})
    assert st_set == 200, "watermark settings refused"
    ix = f"lc-tenp-authz-{uuid.uuid4().hex[:6]}"
    sreq("PUT", f"/{ix}", {"settings": {"number_of_replicas": 0}})
    sreq("PUT", f"/{ix}/_doc/rec?refresh=true", {"p": "reader1"})
    dd = subprocess.run(["docker", "exec", SC_NAME, "sh", "-c",
                         "dd if=/dev/zero of=/usr/share/elasticsearch/data/fill.bin bs=1M count=120 2>&1 | tail -1"],
                        capture_output=True, text=True, timeout=120)
    st_w, blocked, t0 = None, False, time.time()
    while time.time() - t0 < 120:
        st_w, _ = sreq("PUT", f"/{ix}/_doc/rec2", {"p": "reader2"})
        if st_w in (403, 429):
            blocked = True
            break
        time.sleep(1)
    st_r, r = sreq("GET", f"/{ix}/_doc/rec")
    reads = st_r == 200
    elapsed = round(time.time() - t0, 1)
    subprocess.run(["docker", "exec", SC_NAME, "rm", "-f", "/usr/share/elasticsearch/data/fill.bin"], timeout=60)
    sreq("DELETE", f"/{ix}")
    return blocked, reads, st_w, elapsed, dd.returncode


def main():
    st_w, reads_ok, st_w2 = manual_block()
    c1 = st_w in (403, 429)
    c2 = reads_ok and st_w2 in (200, 201)
    f = flood()
    if f is None:
        return pcommon.finish("P14", __file__, "authz writes refused and reads work under both blocks", "flood check not run",
                              False, 2, {"manual_write_status": st_w}, verdict="LIMITED")
    blocked, reads, st_fw, elapsed, dd_rc = f
    c3 = blocked and reads
    ok = c1 and c2 and c3
    return pcommon.finish(
        "P14", __file__, "authz writes refused and reads work under a manual block and the flood-stage block",
        f"manual write {st_w}, reads {reads_ok}, unblock write {st_w2}; flood write {st_fw}, reads {reads}, blocked after {elapsed} s",
        ok, 3, {"scope": "Elasticsearch premise only; the gateway revoke under these blocks is tested at Step 15",
                "scratch": "throw-away node, 160 MB tmpfs data directory, absolute watermarks 70/60/50 MB"})


if __name__ == "__main__":
    sys.exit(main())
