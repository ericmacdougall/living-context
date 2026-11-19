"""Probe P1: what the Basic licence allows (BUILD-PLAN Step 0).

Expected: licence errors for the first five (rrf, linear, DLS, FLS,
semantic_text); the `bbq_disk` mapping, lexical percolate, int8_hnsw kNN at 384
dims, aliases and force merge work. A surprise is recorded and the design
re-checked.
Run against the secured test cluster (LC_TEST_ES_URL, LC_TEST_CA, LC_TEST_PW).
"""
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from tools.probes import pcommon  # noqa: E402

pcommon.start()
es = pcommon.admin_client()
run = uuid.uuid4().hex[:6]
ix = f"lcp1-{run}"
R = {}


def main():
    vec = [0.1] * 384
    pcommon.create(es, ix, {"text": {"type": "text"}, "q": {"type": "percolator"},
                            "vec": {"type": "dense_vector", "dims": 384, "similarity": "cosine",
                                    "index_options": {"type": "int8_hnsw"}}})
    pcommon.bulk_index(es, ix, [("1", {"text": "alpha", "vec": vec})])
    st, b = es.request("POST", f"/{ix}/_search", {"retriever": {"rrf": {"retrievers": [
        {"standard": {"query": {"match": {"text": "alpha"}}}},
        {"knn": {"field": "vec", "query_vector": vec, "k": 1, "num_candidates": 5}}]}}})
    R["rrf"] = ("licence_error" if pcommon.licence_error(st, b) else f"status {st}", pcommon.licence_error(st, b))
    st, b = es.request("POST", f"/{ix}/_search", {"retriever": {"linear": {"retrievers": [
        {"retriever": {"standard": {"query": {"match": {"text": "alpha"}}}}, "weight": 1.0},
        {"retriever": {"knn": {"field": "vec", "query_vector": vec, "k": 1, "num_candidates": 5}}, "weight": 1.0}]}}})
    R["linear"] = ("licence_error" if pcommon.licence_error(st, b) else f"status {st}", pcommon.licence_error(st, b))
    for name, idx_part in (("dls", {"query": {"match_all": {}}}), ("fls", {"field_security": {"grant": ["text"]}})):
        role = dict({"names": [ix], "privileges": ["read"]}, **idx_part)
        st, b = es.request("PUT", f"/_security/role/lcp1_{name}_{run}", {"indices": [role]})
        refused = pcommon.licence_error(st, b)
        if not refused and st == 200:  # role stored; is it usable?
            st2, b2 = es.request("POST", "/_security/api_key", {"name": f"lcp1-{name}-{run}", "expiration": "1h",
                                                               "role_descriptors": {"r": {"indices": [role]}}})
            refused = pcommon.licence_error(st2, b2)
            R[name] = ("licence_error_on_key_creation" if refused else f"role stored, key status {st2}", refused)
            if st2 == 200:
                es.request("DELETE", "/_security/api_key", {"ids": [b2["id"]]})
        else:
            R[name] = ("licence_error" if refused else f"status {st}", refused)
        es.request("DELETE", f"/_security/role/lcp1_{name}_{run}")
    st, b = es.request("PUT", f"/lcp1s-{run}", {"mappings": {"properties": {"t": {"type": "semantic_text"}}}})
    sem_gated = st >= 400
    if st == 200:
        st2, _ = es.request("PUT", f"/lcp1s-{run}/_doc/1?refresh=true", {"t": "hello"})
        sem_gated = st2 >= 400
        R["semantic_text"] = (f"mapping accepted, indexing status {st2}", sem_gated)
    else:
        R["semantic_text"] = (f"mapping refused, status {st}", True)
    pcommon.drop(es, f"lcp1s-{run}")
    st, b = es.request("PUT", f"/lcp1b-{run}", {"mappings": {"properties": {"v": {
        "type": "dense_vector", "dims": 384, "similarity": "cosine", "index_options": {"type": "bbq_disk"}}}}})
    R["bbq_disk"] = (f"status {st}" + ("" if st == 200 else " " + str(b)[:200]), st == 200)
    pcommon.drop(es, f"lcp1b-{run}")
    es.request("PUT", f"/{ix}/_doc/q1?refresh=true", {"q": {"match": {"text": "alpha"}}})
    st, b = es.request("POST", f"/{ix}/_search", {"query": {"percolate": {"field": "q", "document": {"text": "alpha beta"}}}})
    R["percolate"] = (f"status {st}, hits {b['hits']['total']['value'] if st == 200 else '-'}",
                      st == 200 and b["hits"]["total"]["value"] >= 1)
    st, b = es.request("POST", f"/{ix}/_search", {"knn": {"field": "vec", "query_vector": vec, "k": 1, "num_candidates": 5}})
    R["knn_int8_hnsw_384"] = (f"status {st}", st == 200 and len(b["hits"]["hits"]) == 1)
    st, b = es.request("POST", "/_aliases", {"actions": [{"add": {"index": ix, "alias": f"lcp1a-{run}"}}]})
    R["alias"] = (f"status {st}", st == 200)
    st, b = es.request("POST", f"/{ix}/_forcemerge?max_num_segments=1")
    R["force_merge"] = (f"status {st}", st == 200)
    pcommon.drop(es, ix)
    gated = ["rrf", "linear", "dls", "fls", "semantic_text"]
    works = ["bbq_disk", "percolate", "knn_int8_hnsw_384", "alias", "force_merge"]
    ok = all(R[k][1] for k in gated + works)
    return pcommon.finish(
        "P1", __file__, "licence errors for rrf, linear, DLS, FLS, semantic_text; the rest work",
        "; ".join(f"{k}: {v[0]}" for k, v in R.items()), ok, len(R),
        {"results": {k: v[0] for k, v in R.items()}})


if __name__ == "__main__":
    sys.exit(main())
