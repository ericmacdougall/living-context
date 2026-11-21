"""F13 (verifier side): the independent verifier refuses an incomplete read instead of counting it as zero residue.

Every read of the verifier, its probe searches, its refresh, and the pool residue check's index listing, residue count and text
searches, is answered by a scripted fake with one failure at a time: HTTP 200 with a failed shard or `timed_out`, an HTTP error, or a
total that is only a lower bound. Each must raise `VerifierError`; none may return a report or a zero.
"""
import unittest

from living_context.verifier.check import Verifier, VerifierError, pool_residue

T = "tena"
OK_SHARDS = {"total": 3, "successful": 3, "skipped": 0, "failed": 0}
PARTIAL = (200, {"timed_out": False, "_shards": {"total": 3, "successful": 2, "skipped": 0, "failed": 1},
                 "hits": {"total": {"value": 0, "relation": "eq"}, "hits": []}, "aggregations": {"by": {"buckets": []}}})
TIMED_OUT = (200, {"timed_out": True, "_shards": OK_SHARDS, "hits": {"total": {"value": 0, "relation": "eq"}, "hits": []},
                   "aggregations": {"by": {"buckets": []}}})
LOWER_BOUND = (200, {"timed_out": False, "_shards": OK_SHARDS, "hits": {"total": {"value": 0, "relation": "gte"}, "hits": []},
                     "aggregations": {"by": {"buckets": []}}})
ERROR = (503, {"error": {"type": "unavailable_shards_exception"}})


def good_search(n=0):
    return 200, {"timed_out": False, "_shards": OK_SHARDS, "hits": {"total": {"value": n, "relation": "eq"}, "hits": []},
                 "aggregations": {"by": {"buckets": []}}}


class Scripted:
    """Answers by request kind; `fail` replaces the answer of the n-th request of a kind (`(kind, n)`)."""

    def __init__(self, fail=None):
        self.fail, self.seen = fail, {}

    def request(self, method, path, body=None, **kw):
        kind = "refresh" if "/_refresh" in path else "resolve" if path.startswith("/_resolve") else "search" if "/_search" in path else "other"
        self.seen[kind] = self.seen.get(kind, 0) + 1
        if self.fail and self.fail[0] == (kind, self.seen[kind]):
            return self.fail[1]
        if kind == "refresh":
            return 200, {"_shards": OK_SHARDS}
        if kind == "resolve":
            return 200, {"indices": [{"name": f"lc-{T}-src"}], "aliases": [], "data_streams": []}
        return good_search()


class VerifierComplete(unittest.TestCase):
    def test_pool_residue_refuses_every_incomplete_read(self):
        cases = [(("refresh", 1), (200, {"_shards": {"total": 3, "successful": 2, "failed": 1}})), (("refresh", 1), ERROR),
                 (("resolve", 1), ERROR), (("resolve", 1), (200, {"unexpected": True})),
                 (("search", 1), PARTIAL), (("search", 1), TIMED_OUT), (("search", 1), LOWER_BOUND), (("search", 1), ERROR),
                 (("search", 2), PARTIAL), (("search", 2), ERROR), (("search", 2), LOWER_BOUND)]
        self.assertEqual(pool_residue(Scripted(), T, "pzzzzzzzzzzzzzzz", texts=["canary"]),
                         {"indices": 0, "docs": 0, "by_index": {}, "text": 0})   # the control: complete reads give a report
        for fail in cases:
            with self.subTest(fail=fail):
                with self.assertRaises(VerifierError):
                    pool_residue(Scripted(fail), T, "pzzzzzzzzzzzzzzz", texts=["canary"])

    def test_probe_searches_refuse_a_partial_answer(self):
        for answer in (PARTIAL, TIMED_OUT, ERROR):
            with self.subTest(answer=answer[0]):
                with self.assertRaises(VerifierError):
                    Verifier(Scripted((("search", 1), answer)), T)._text("canary")

    def test_probe_refuses_a_refresh_with_a_failed_shard(self):
        v = Verifier(Scripted((("refresh", 1), (200, {"_shards": {"total": 3, "successful": 2, "failed": 1}}))), T)
        with self.assertRaises(VerifierError):
            v.probe(None)   # the refresh is the first step: the canaries are never read


if __name__ == "__main__":
    unittest.main()
