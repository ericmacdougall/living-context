"""Probes P9 and P10: expunge-deletes purge and the time to `docs.deleted == 0`.

P9 (N=3): `index.merge.policy.expunge_deletes_allowed: 0` is accepted; after
deleting half of 2,000 documents `docs.deleted` is above 0 (control: the
purge has something to do); `only_expunge_deletes` brings it to 0.
P10 (N=3): three trials of the same, measuring seconds from the end of the
delete (after refresh) to the first `docs.deleted == 0` with the expunge
merge repeated every second (soft-delete tombstones wait for a retention-lease
sync, 30 s by default, so one merge right after the delete leaves them). The measured figure is the bound a receipt may state
(single node, no replicas, no retention leases beyond the default).
"""
import sys
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from tools.probes import pcommon  # noqa: E402

pcommon.start()
es = pcommon.admin_client()


def deleted(ix):
    st, r = es.request("GET", f"/{ix}/_stats/docs")
    return r["_all"]["primaries"]["docs"]["deleted"] if st == 200 else -1


def trial(ix_base):
    ix = f"{ix_base}-{uuid.uuid4().hex[:4]}"
    pcommon.create(es, ix, {"k": {"type": "keyword"}})
    st, r = es.request("PUT", f"/{ix}/_settings", {"index.merge.policy.expunge_deletes_allowed": "0"})
    accepted = st == 200 and r.get("acknowledged") is True
    for blk in range(4):
        pcommon.bulk_index(es, ix, [(f"{blk}-{i}", {"k": "x"}) for i in range(500)])
    lines = []
    for blk in range(2):
        for i in range(500):
            lines += [{"delete": {"_index": ix, "_id": f"{blk}-{i}"}}]
    es.request("POST", "/_bulk", lines, ndjson=True)
    es.request("POST", f"/{ix}/_refresh")
    before = deleted(ix)
    t0 = time.time()
    # Deletes are soft: the tombstones are merged away only after the retention lease advances (a
    # periodic sync, 30 s by default), so the expunge merge is repeated until `deleted` reads 0.
    t_to_zero = None
    while time.time() - t0 < 120:
        es.request("POST", f"/{ix}/_forcemerge?only_expunge_deletes=true")
        if deleted(ix) == 0:
            t_to_zero = round(time.time() - t0, 3)
            break
        time.sleep(1)
    st, c = es.request("GET", f"/{ix}/_count")
    live = c.get("count") if st == 200 else None
    pcommon.drop(es, ix)
    return accepted, before, t_to_zero, live


def main():
    a, before, t, live = trial("lcp9")
    ok9 = a and before > 0 and t is not None and live == 1000
    rc = pcommon.finish("P9", __file__, "expunge_deletes_allowed 0 accepted; only_expunge_deletes brings docs.deleted to 0",
                        f"accepted={a}, deleted before {before}, zero after {t} s, live {live}", ok9, 3,
                        {"deleted_before": before, "live_documents_after": live})
    times, ok10 = [], True
    for _ in range(3):
        a, before, t, live = trial("lcp10")
        times.append(t)
        ok10 = ok10 and t is not None and before > 0 and live == 1000
    rc10 = pcommon.finish("P10", __file__, "time from delete to docs.deleted == 0 is measured",
                          f"{times} s (max {max(x for x in times if x is not None) if any(x is not None for x in times) else None})",
                          ok10, 3, {"seconds_per_trial": times, "scope": "single node, 2,000 documents, explicit expunge merge"})
    return rc or rc10


if __name__ == "__main__":
    sys.exit(main())
