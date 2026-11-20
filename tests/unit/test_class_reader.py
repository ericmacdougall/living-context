"""NEG-16 and AC-3: the class-level reader (POOLS 1.2, 10). Unit level on an in-memory stand-in; the same reader is driven against
a real Elasticsearch in tests/security/test_pools_es.py. Each check runs against the real reader and against the broken variant
named in its comment, and the check must go red on the broken one."""
import unittest

from living_context.gateway.class_reader import READ_PATHS, ClassReader, ReaderRefused
from tests.unit.fake_es import FakeEs

T, C = "ta01", "base"
A, B = "a" * 16, "b" * 16
IDX = f"lc-{T}-{C}-ctl"


def world():
    es = FakeEs()
    for pool in (A, B):
        for n in range(3):
            es.put(IDX, f"lh:{pool}:{n:032x}", {"pool": pool, "kind": "head", "status": "live", "current_lv": 1})
        es.put(IDX, f"pm:{pool}", {"pool": pool, "kind": "pool"})
    return es


def reader(es, **kw):
    return ClassReader(lambda cls: es, T, **kw)


def call(r, path, pool, **kw):
    """Drive one read path of the reader against pool `pool`; returns the ids it returned."""
    if path == "get":
        d = r.get(C, "ctl", kw.get("id", f"lh:{A}:{0:032x}"), pool)
        return [d.id] if d else []
    if path == "mget":
        return [i for i, d in r.mget(C, "ctl", kw.get("ids", [f"lh:{A}:{n:032x}" for n in range(3)]), pool).items() if d]
    if path == "search":
        return [d.id for d in r.search(C, "ctl", pool, kw.get("filters", [{"term": {"kind": "head"}}]))]
    if path == "count":
        return [r.count(C, "ctl", pool, kw.get("filters", []))]
    if path == "aggregate":
        return list(r.aggregate(C, "ctl", pool, {"k": {"terms": {"field": "kind"}}}).get("k", {}).get("buckets", []))
    raise AssertionError(path)


class ReaderTests(unittest.TestCase):
    def test_every_read_path_is_enumerated_and_covered(self):
        """The public read methods of the reader are exactly READ_PATHS, and each is exercised below."""
        public = {n for n in dir(ClassReader) if not n.startswith("_") and callable(getattr(ClassReader, n))}
        self.assertEqual(public, set(READ_PATHS))

    def test_own_pool_reads_work_on_every_path(self):
        r = reader(world())
        self.assertEqual(len(call(r, "get", A)), 1)
        self.assertEqual(len(call(r, "mget", A)), 3)
        self.assertEqual(len(call(r, "search", A)), 3)
        self.assertEqual(call(r, "count", A), [4])
        self.assertEqual(sum(b["doc_count"] for b in call(r, "aggregate", A)), 4)

    def test_no_path_returns_another_pools_document(self):
        es = world()
        r = reader(es)
        # the reader is asked for pool B but handed ids and filters of pool A: nothing of A comes back, or the call is refused
        for path, kw in (("get", {"id": f"lh:{A}:{0:032x}"}), ("mget", {})):
            with self.assertRaises(ReaderRefused, msg=path):
                call(r, path, B, **kw)
        self.assertTrue(all(i.split(":")[1] == B for i in call(r, "search", B)))
        self.assertEqual(call(r, "count", B), [4])
        self.assertEqual(sum(b["doc_count"] for b in call(r, "aggregate", B)), 4)

    def test_one_foreign_id_refuses_a_whole_mget(self):
        r = reader(world())
        ids = [f"lh:{A}:{0:032x}", f"lh:{B}:{0:032x}"]
        with self.assertRaises(ReaderRefused):
            r.mget(C, "ctl", ids, A)
        self.assertEqual(r.plan, [])   # nothing was sent

    def test_every_search_request_carries_the_pool_term(self):
        es = world()
        r = reader(es)
        for p in ("search", "count", "aggregate"):
            call(r, p, A)
        bodies = [b for (m, path, b) in es.requests if path.endswith(("_search", "_count"))]
        self.assertEqual(len(bodies), 3)
        for b in bodies:
            self.assertIn({"term": {"pool": A}}, b["query"]["bool"]["filter"])

    def test_scored_and_unsafe_requests_are_refused(self):
        r = reader(world())
        for bad in ({"match": {"text": "x"}}, {"multi_match": {"query": "x", "fields": ["t"]}}, {"script_score": {}},
                    {"bool": {"filter": [{"match": {"t": "x"}}]}}, {"knn": {}}, {"query_string": {"query": "x"}}, {"wildcard": {"a": "*"}}):
            with self.assertRaises(ReaderRefused, msg=str(bad)):
                r.search(C, "ctl", A, [bad])

    def test_a_term_in_should_or_must_not_does_not_count_as_the_pool_term(self):
        from living_context.gateway.class_reader import check_request
        for q in ({"bool": {"should": [{"term": {"pool": A}}]}}, {"bool": {"must_not": [{"term": {"pool": B}}]}},
                  {"bool": {"filter": [{"term": {"kind": "head"}}]}}, {"term": {"kind": "head"}}):
            with self.assertRaises(ReaderRefused, msg=str(q)):
                check_request({"query": q}, A)
        check_request({"query": {"bool": {"filter": [{"term": {"pool": A}}]}}}, A)

    def test_top_level_knn_and_unknown_sections_are_refused(self):
        from living_context.gateway.class_reader import check_request
        ok = {"query": {"bool": {"filter": [{"term": {"pool": A}}]}}}
        for extra in ({"knn": {"field": "e", "query_vector": [0.1], "k": 1}}, {"rank": {}}, {"script_fields": {}}, {"rescore": {}}):
            with self.assertRaises(ReaderRefused, msg=str(extra)):
                check_request({**ok, **extra}, A)

    def test_unreadable_kind_and_path_injection_are_refused(self):
        r = reader(world())
        with self.assertRaises(ReaderRefused):
            r.get(C, "leaf", f"lh:{A}:x", A)   # a scored kind is not readable here
        for bad in (f"lh:{A}:../../_search", f"lh:{A}:x?pretty", f"lh:{A}:x/y"):
            with self.assertRaises(ReaderRefused, msg=bad):
                r.get(C, "ctl", bad, A)

    # ---- the broken variants: each must be caught by a check above ----------------------------------------------------------
    def test_broken_reader_without_pool_term_leaks_across_pools(self):
        r = reader(world(), broken_no_pool_term=True, broken_no_leak_guard=True)
        for path in ("search", "count", "aggregate"):
            out = call(r, path, B)
            if path == "search":
                self.assertTrue(any(i.split(":")[1] == A for i in out), "the broken variant returns pool A's heads for pool B")
            if path == "count":
                self.assertEqual(out, [8])   # both pools: 8, not B's 4
        self.assertTrue(any(i.split(":")[1] == A for i in call(r, "search", B)))

    def test_broken_reader_accepts_scored_requests(self):
        r = reader(world(), broken_allow_scored=True)
        try:
            r.search(C, "ctl", A, [{"match": {"t": "x"}}])
        except AssertionError:
            pass   # the fake cannot evaluate `match`: reaching it at all is the defect (the real reader refuses before sending)
        else:
            self.fail("expected the broken reader to send the scored request")

    def test_broken_reader_accepts_a_foreign_id(self):
        r = reader(world(), broken_id_unchecked=True, broken_no_leak_guard=True)
        self.assertEqual(len(r.mget(C, "ctl", [f"lh:{A}:{0:032x}"], B)), 1)
        self.assertEqual(r.get(C, "ctl", f"lh:{A}:{0:032x}", B).id, f"lh:{A}:{0:032x}")

    def test_leak_guard_refuses_a_foreign_document_returned_behind_the_term(self):
        es = world()
        es.put(IDX, f"lh:{B}:{9:032x}", {"pool": A, "kind": "head"})   # a mislabelled document sits under pool B's id
        r = reader(es)
        with self.assertRaises(ReaderRefused):
            r.mget(C, "ctl", [f"lh:{B}:{9:032x}"], B)
        self.assertEqual(len(reader(es, broken_no_leak_guard=True).mget(C, "ctl", [f"lh:{B}:{9:032x}"], B)), 1)


if __name__ == "__main__":
    unittest.main()
