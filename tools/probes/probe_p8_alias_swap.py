"""Probe P8: one `_aliases` request with add and remove actions is atomic under concurrent search.

Two indices hold one document each (marker A, marker B). 400 swaps of the
alias between them, each a single `_aliases` request, run while 4 readers
search the alias without pause. A reader must always see exactly one hit;
a missing alias (404) or two hits is an anomaly. Control (not counted):
the non-atomic variant (remove, then add, two requests) shows at least one
anomaly, so the readers can see a gap.
"""
import sys
import threading
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from tools.probes import pcommon  # noqa: E402

pcommon.start()
es = pcommon.admin_client()


def swaps(alias, a, b, n, atomic):
    bad, reads, stop = [0], [0], threading.Event()

    def reader():
        c = pcommon.admin_client()
        while not stop.is_set():
            st, r = c.request("GET", f"/{alias}/_search")
            reads[0] += 1
            if st != 200 or r["hits"]["total"]["value"] != 1:
                bad[0] += 1

    ts = [threading.Thread(target=reader) for _ in range(4)]
    for t in ts:
        t.start()
    cur, other = a, b
    for _ in range(n):
        if atomic:
            st, _ = es.request("POST", "/_aliases", {"actions": [{"remove": {"index": cur, "alias": alias}},
                                                                  {"add": {"index": other, "alias": alias}}]})
            assert st == 200
        else:
            es.request("POST", "/_aliases", {"actions": [{"remove": {"index": cur, "alias": alias}}]})
            es.request("POST", "/_aliases", {"actions": [{"add": {"index": other, "alias": alias}}]})
        cur, other = other, cur
    stop.set()
    for t in ts:
        t.join()
    return bad[0], reads[0]


def main():
    run = uuid.uuid4().hex[:6]
    a, b, alias = f"lcp8-{run}-a", f"lcp8-{run}-b", f"lcp8-{run}"
    pcommon.create(es, a, {"m": {"type": "keyword"}}, aliases={alias: {}})
    pcommon.create(es, b, {"m": {"type": "keyword"}})
    pcommon.bulk_index(es, a, [("1", {"m": "A"})])
    pcommon.bulk_index(es, b, [("1", {"m": "B"})])
    bad, reads = swaps(alias, a, b, 400, True)
    ctl_bad, ctl_reads = swaps(alias, a if 400 % 2 == 0 else b, b if 400 % 2 == 0 else a, 200, False)
    pcommon.drop(es, a, b)
    ok = bad == 0 and ctl_bad > 0
    return pcommon.finish("P8", __file__, "one _aliases request with add and remove is atomic under concurrent search",
                          f"0 anomalies in {reads} reads" if bad == 0 else f"{bad} anomalies in {reads} reads", ok, 400,
                          {"reads": reads, "anomalies": bad, "control_two_request_anomalies": ctl_bad,
                           "control_reads": ctl_reads})


if __name__ == "__main__":
    sys.exit(main())
