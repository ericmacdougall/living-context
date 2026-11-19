"""Probe P4: a stored `knn` query in a percolator field.

Expected (BUILD-PLAN): fails or unsupported; the design is already lexical-only
for percolation. The verdict is relative to that: a refusal records PASS, a
success records FAIL (the design would then be re-checked). Three checks: a
stored top-level knn query, a knn query inside a bool filter, and the control
that a stored match query is accepted (so a refusal is about knn, not about
the percolator mapping).
"""
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from tools.probes import pcommon  # noqa: E402

pcommon.start()
es = pcommon.admin_client()


def main():
    ix = f"lcp4-{uuid.uuid4().hex[:6]}"
    pcommon.create(es, ix, {"q": {"type": "percolator"}, "text": {"type": "text"},
                            "vec": {"type": "dense_vector", "dims": 3, "similarity": "cosine"}})
    knn = {"knn": {"field": "vec", "query_vector": [1, 0, 0], "k": 3, "num_candidates": 10}}
    st_a, b_a = es.request("PUT", f"/{ix}/_doc/a?refresh=true", {"q": knn})
    st_b, b_b = es.request("PUT", f"/{ix}/_doc/b?refresh=true", {"q": {"bool": {"filter": [knn]}}})
    st_c, _ = es.request("PUT", f"/{ix}/_doc/c?refresh=true", {"q": {"match": {"text": "x"}}})
    succeeded = [n for n, st in (("top-level", st_a), ("in bool filter", st_b)) if st in (200, 201)]
    pcommon.drop(es, ix)
    ok = pcommon.p4_verdict(st_a, st_b, st_c) == "PASS"
    return pcommon.finish(
        "P4", __file__, "storing a knn query in a percolator field fails (unsupported)",
        "unsupported (stored knn refused in both forms)" if ok else f"accepted: {succeeded}; control status {st_c}",
        ok, 3, {"top_level_status": st_a, "in_bool_status": st_b, "control_match_status": st_c,
                "reason_excerpt": str(b_a)[:160]})


if __name__ == "__main__":
    sys.exit(main())
