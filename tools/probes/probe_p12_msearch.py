"""Probe P12: `_msearch` with one failing sub-search: the others succeed, the failure is per item.

50 trials of a three-part `_msearch` (valid, failing on a missing index, valid).
HTTP status must be 200, items 0 and 2 carry hits, item 1 carries an error.
"""
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from tools.probes import pcommon  # noqa: E402

pcommon.start()
es = pcommon.admin_client()


def main():
    ix = f"lcp12-{uuid.uuid4().hex[:6]}"
    pcommon.create(es, ix, {"k": {"type": "keyword"}})
    pcommon.bulk_index(es, ix, [("1", {"k": "a"})])
    bad = 0
    for _ in range(50):
        st, r = es.request("POST", "/_msearch", [
            {"index": ix}, {"query": {"match_all": {}}},
            {"index": f"{ix}-missing"}, {"query": {"match_all": {}}},
            {"index": ix}, {"query": {"term": {"k": "a"}}}], ndjson=True)
        rs = r.get("responses", []) if st == 200 else []
        good = (len(rs) == 3 and rs[0].get("hits", {}).get("total", {}).get("value") == 1
                and "error" in rs[1] and rs[2].get("hits", {}).get("total", {}).get("value") == 1)
        bad += not good
    pcommon.drop(es, ix)
    return pcommon.finish("P12", __file__, "one failing sub-search leaves the others intact",
                          "50 of 50 as expected" if bad == 0 else f"{bad} of 50 differ", bad == 0, 50, {})


if __name__ == "__main__":
    sys.exit(main())
