"""Probe P7: a restrictive kNN `filter` still returns k hits (the filter is applied during the search).

5,000 documents with 8-dim vectors; 20 of them carry the rare class value.
40 queries ask k=10, num_candidates=50 with that filter: each must return 10
hits, all of the rare class. Control (not counted in N): the same request
with the restriction as `post_filter` returns fewer than k hits at least once,
so the check can fail.
"""
import random
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from tools.probes import pcommon  # noqa: E402

pcommon.start()
es = pcommon.admin_client()


def main():
    rnd = random.Random(7)
    ix = f"lcp7-{uuid.uuid4().hex[:6]}"
    pcommon.create(es, ix, {"cls": {"type": "keyword"}, "vec": {"type": "dense_vector", "dims": 8, "similarity": "cosine"}})
    docs = [(str(i), {"cls": "rare" if i % 250 == 0 else "common", "vec": [rnd.uniform(-1, 1) for _ in range(8)]})
            for i in range(5000)]
    assert pcommon.bulk_index(es, ix, docs) == 0
    short, wrong_class, ctl_short = 0, 0, 0
    for t in range(40):
        q = [rnd.uniform(-1, 1) for _ in range(8)]
        st, b = es.request("POST", f"/{ix}/_search", {"size": 10, "_source": ["cls"], "knn": {
            "field": "vec", "query_vector": q, "k": 10, "num_candidates": 50, "filter": {"term": {"cls": "rare"}}}})
        hits = b["hits"]["hits"] if st == 200 else []
        short += len(hits) != 10
        wrong_class += sum(1 for h in hits if h["_source"]["cls"] != "rare")
        st, b = es.request("POST", f"/{ix}/_search", {"size": 10, "_source": ["cls"], "knn": {
            "field": "vec", "query_vector": q, "k": 10, "num_candidates": 50}, "post_filter": {"term": {"cls": "rare"}}})
        ctl_short += (len(b["hits"]["hits"]) if st == 200 else 0) < 10
    pcommon.drop(es, ix)
    ok = short == 0 and wrong_class == 0 and ctl_short > 0
    return pcommon.finish("P7", __file__, "kNN filter with a restrictive filter returns k hits",
                          "40 of 40 queries returned 10 filtered hits" if ok else
                          f"short {short}, wrong class {wrong_class}, control short {ctl_short}",
                          ok, 40, {"control_post_filter_short_of_k": ctl_short, "restriction": "20 of 5000 documents"})


if __name__ == "__main__":
    sys.exit(main())
