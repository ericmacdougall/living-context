"""Probe P11: an index request with a stale `if_seq_no` returns 409 and writes nothing.

100 trials: index a document, read its seq_no and primary term, overwrite it
once (the legitimate update), then index again with the stale values. The
stale write must return 409 and the stored source must stay the second
version. Control: the write with the current values succeeds (so a 409 is
about staleness).
"""
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from tools.probes import pcommon  # noqa: E402

pcommon.start()
es = pcommon.admin_client()


def main():
    ix = f"lcp11-{uuid.uuid4().hex[:6]}"
    pcommon.create(es, ix, {"v": {"type": "keyword"}})
    bad, ctl_bad = 0, 0
    for t in range(100):
        i = f"d{t}"
        st, r = es.request("PUT", f"/{ix}/_doc/{i}", {"v": "one"})
        seq, term = r["_seq_no"], r["_primary_term"]
        st, r2 = es.request("PUT", f"/{ix}/_doc/{i}?if_seq_no={seq}&if_primary_term={term}", {"v": "two"})
        ctl_bad += st != 200
        st, _ = es.request("PUT", f"/{ix}/_doc/{i}?if_seq_no={seq}&if_primary_term={term}", {"v": "three"})
        st_get, g = es.request("GET", f"/{ix}/_doc/{i}")
        if st != 409 or g["_source"]["v"] != "two":
            bad += 1
    pcommon.drop(es, ix)
    ok = bad == 0 and ctl_bad == 0
    return pcommon.finish("P11", __file__, "a stale if_seq_no returns 409 and writes nothing",
                          "100 of 100 stale writes refused with 409, source unchanged" if ok else f"{bad} bad, control failures {ctl_bad}",
                          ok, 100, {"control_current_value_write_failures": ctl_bad})


if __name__ == "__main__":
    sys.exit(main())
