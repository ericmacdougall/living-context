"""Probe P13: `terms` on `src_id` with 10,000 ids; `index.max_terms_count` default 65,536.

Checks (N=4): 10,000 ids in one terms query return all 10,000 documents; the
default `index.max_terms_count` reads 65536; 65,536 ids are accepted; 65,537
are refused (so a gateway must batch above the cap).
"""
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from tools.probes import pcommon  # noqa: E402

pcommon.start()
es = pcommon.admin_client()


def main():
    ix = f"lcp13-{uuid.uuid4().hex[:6]}"
    pcommon.create(es, ix, {"src_id": {"type": "keyword"}})
    pcommon.bulk_index(es, ix, [(str(i), {"src_id": f"s{i}"}) for i in range(10000)])
    q = lambda n: {"size": 0, "track_total_hits": True, "query": {"terms": {"src_id": [f"s{i}" for i in range(n)]}}}
    st, r = es.request("POST", f"/{ix}/_search", q(10000))
    c1 = st == 200 and r["hits"]["total"]["value"] == 10000
    st, r = es.request("GET", f"/{ix}/_settings?include_defaults=true&flat_settings=true")
    cap = None
    if st == 200:
        flat = {**r[ix].get("defaults", {}), **r[ix].get("settings", {})}
        cap = flat.get("index.max_terms_count")
    c2 = cap == "65536"
    st, r = es.request("POST", f"/{ix}/_search", q(65536))
    c3 = st == 200
    st4, r4 = es.request("POST", f"/{ix}/_search", q(65537))
    c4 = st4 == 400
    pcommon.drop(es, ix)
    ok = c1 and c2 and c3 and c4
    return pcommon.finish("P13", __file__, "10,000 ids in one terms query work; the default cap is 65,536",
                          f"10k ok={c1}, cap={cap}, 65536 ok={c3}, 65537 refused={c4}", ok, 4, {"cap_setting": cap})


if __name__ == "__main__":
    sys.exit(main())
