"""Probe P16: slow logs are off and nothing request- or document-shaped reaches the logs.

Checks (N=7), on the secured test cluster whose container name is
LC_ES_CONTAINER (default lc-es-test): (1) search slow-log thresholds of -1
are accepted and read back on every index kind; (2) the same for the indexing
slow log; (3) with default logger levels, a marker in a document and a search
body appears nowhere in the container logs or log files; (4) nor does a
marker in a request that fails; (5) positive control: a scratch index with the
search slow-log threshold at 0 shows the marker in the logs; (6) positive
control for the indexing slow log with `source` enabled; (7) the control
indices set back to -1 stop logging a fresh marker. Counts only; markers are
random per run.
"""
import os
import secrets
import subprocess
import sys
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from living_context.gateway import indices  # noqa: E402
from tools.probes import pcommon  # noqa: E402

pcommon.start()
es = pcommon.admin_client()
NAME = os.environ.get("LC_ES_CONTAINER", "lc-es-test")


def log_text() -> bytes:
    out = subprocess.run(["docker", "logs", NAME], capture_output=True, timeout=120)
    files = subprocess.run(["docker", "exec", NAME, "sh", "-c", "cat /usr/share/elasticsearch/logs/* 2>/dev/null"],
                           capture_output=True, timeout=120)
    return out.stdout + out.stderr + files.stdout


def count(marker: str) -> int:
    return log_text().count(marker.encode())


def wait_for(marker: str, secs: float = 20) -> int:
    t0 = time.time()
    n = 0
    while time.time() - t0 < secs:
        n = count(marker)
        if n:
            return n
        time.sleep(1)
    return n


def main():
    run = uuid.uuid4().hex[:6]
    kinds = list(indices.CLASS_KINDS) + list(indices.TENANT_KINDS)
    off = {f"index.search.slowlog.threshold.{k}.{lv}": "-1" for k in ("query", "fetch")
           for lv in ("warn", "info", "debug", "trace")}
    off_idx = {f"index.indexing.slowlog.threshold.index.{lv}": "-1" for lv in ("warn", "info", "debug", "trace")}
    names = [f"lcp16-{run}-{k}" for k in kinds]
    for n in names:
        pcommon.create(es, n, {"t": {"type": "text"}}, settings=dict(off, **off_idx))
    ok_search = ok_index = True
    for n in names:
        st, r = es.request("GET", f"/{n}/_settings?flat_settings=true")
        s = r[n]["settings"] if st == 200 else {}
        ok_search &= all(s.get(k) == "-1" for k in off)
        ok_index &= all(s.get(k) == "-1" for k in off_idx)
        st, _ = es.request("PUT", f"/{n}/_settings", dict(off, **off_idx))
        ok_search &= st == 200
    m1, m2 = "mk" + secrets.token_hex(8), "mk" + secrets.token_hex(8)
    for n in names:
        es.request("PUT", f"/{n}/_doc/1?refresh=true", {"t": m1})
        es.request("POST", f"/{n}/_search", {"query": {"match": {"t": m1}}})
        es.request("POST", f"/{n}/_search", {"query": {"nonexistent_query_kind": {"t": m2}}})
        es.request("PUT", f"/{n}/_doc/2", {"t": {"nested": m2}})  # mapping conflict
    time.sleep(6)
    c3, c4 = count(m1), count(m2)
    ctl = f"lcp16c-{run}"
    pcommon.create(es, ctl, {"t": {"type": "text"}}, settings={
        "index.search.slowlog.threshold.query.warn": "0ms", "index.search.slowlog.threshold.fetch.warn": "0ms",
        "index.indexing.slowlog.threshold.index.warn": "0ms", "index.indexing.slowlog.source": "1000"})
    m3, m4 = "mk" + secrets.token_hex(8), "mk" + secrets.token_hex(8)
    es.request("PUT", f"/{ctl}/_doc/1?refresh=true", {"t": m3})
    es.request("POST", f"/{ctl}/_search", {"query": {"match": {"t": m4}}})
    c5, c6 = wait_for(m4), wait_for(m3)
    es.request("PUT", f"/{ctl}/_settings", dict(off, **off_idx))
    m5 = "mk" + secrets.token_hex(8)
    es.request("PUT", f"/{ctl}/_doc/9?refresh=true", {"t": m5})
    es.request("POST", f"/{ctl}/_search", {"query": {"match": {"t": m5}}})
    time.sleep(6)
    c7 = count(m5)
    pcommon.drop(es, ctl, *names)
    res = [ok_search, ok_index, c3 == 0, c4 == 0, c5 > 0, c6 > 0, c7 == 0]
    return pcommon.finish(
        "P16", __file__, "slow logs off on every kind; no request or document field in the logs; positive controls visible",
        "checks 1-7: " + ", ".join(str(int(x)) for x in res), all(res), 7,
        {"index_kinds": len(kinds), "marker_counts": {"default_doc_and_search": c3, "default_failing_request": c4,
                                                    "control_search": c5, "control_index": c6, "control_after_off": c7}})


if __name__ == "__main__":
    sys.exit(main())
