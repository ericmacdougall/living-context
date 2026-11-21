"""Writer.repair takes a destructive decision (delete a projection whose source is missing) from its reads, so those reads must be
complete (review C1, the repair sibling of F13): a per-document `_mget` error, or a partial or failed scan page, is never "the source
is missing". A scripted client answers each request; the repair must raise and send no DELETE."""
import unittest

from living_context.ingest.writer import Writer, WriterError
from tests.unit.fake_es import behind, doc_answer, unconfirmed_deletes

T = "tena"
OK = {"total": 1, "successful": 1, "skipped": 0, "failed": 0}


def page(hits):
    return 200, {"timed_out": False, "_shards": OK, "hits": {"hits": hits}}


class Scripted:
    def __init__(self, mget_doc, leaf_page=None, src_page=None):
        self.mget_doc, self.sent, self.leaf_pages = mget_doc, [], 0
        self.leaf_page = leaf_page or page([{"_id": "L1:1", "sort": [5]}])
        self.src_page = src_page or page([])

    def request(self, method, path, body=None, **kw):
        self.sent.append((method, path.split("?")[0]))
        if path.split("?")[0].endswith("/_refresh"):
            return 200, {"_shards": OK}
        if method == "POST" and f"lc-{T}-src/_search" in path:
            return self.src_page
        if method == "POST" and f"lc-{T}-base-leaf/_search" in path:
            self.leaf_pages += 1
            return self.leaf_page if self.leaf_pages == 1 else page([])
        if path.startswith("/_mget"):
            return 200, {"docs": [self.mget_doc]}
        if method == "DELETE":   # the answer Elasticsearch gives a delete through the class alias (the physical index named)
            return doc_answer("DELETE", behind(f"lc-{T}-base-leaf"), path.split("/_doc/")[1].split("?")[0], True)
        raise AssertionError(path)


def repair(es):
    return Writer(lambda: es, None, T, None).repair(["base"])


class RepairReads(unittest.TestCase):
    def test_a_missing_source_still_deletes_its_projection(self):   # the control: a verified absence is acted on
        es = Scripted({"_index": f"lc-{T}-src", "_id": "L1:1", "found": False})
        self.assertEqual(repair(es)["projections_deleted"], 1)
        self.assertIn(("DELETE", f"/lc-{T}-base-leaf/_doc/L1:1"), es.sent)

    def test_a_per_document_mget_error_deletes_nothing(self):
        es = Scripted({"_index": f"lc-{T}-src", "_id": "L1:1", "error": {"type": "unavailable_shards_exception"}})
        with self.assertRaises(WriterError):
            repair(es)
        self.assertNotIn("DELETE", [m for m, _ in es.sent])

    def test_a_partial_scan_page_is_not_the_end_of_the_scan(self):
        partial = (200, {"timed_out": False, "_shards": {"total": 2, "successful": 1, "skipped": 0, "failed": 1}, "hits": {"hits": []}})
        for name, kw in (("source scan", {"src_page": partial}), ("projection scan", {"leaf_page": partial})):
            with self.subTest(name):
                es = Scripted({"_index": f"lc-{T}-src", "_id": "L1:1", "found": False}, **kw)
                with self.assertRaises(WriterError):
                    repair(es)

    def test_a_delete_answer_that_confirms_nothing_is_an_error(self):   # round 5: at f16ea94 any 404 or 200 counted as deleted
        for name, answer in unconfirmed_deletes(f"lc-{T}-base-leaf", "L1:1", strict=False).items():
            with self.subTest(name):
                es = Scripted({"_index": f"lc-{T}-src", "_id": "L1:1", "found": False})
                real = es.request
                es.request = lambda m, p, b=None, real=real, answer=answer, **kw: answer if m == "DELETE" else real(m, p, b, **kw)
                with self.assertRaises(WriterError):
                    repair(es)

    def test_a_verified_absence_through_the_alias_counts_as_before(self):
        es = Scripted({"_index": f"lc-{T}-src", "_id": "L1:1", "found": False})
        real = es.request
        gone = doc_answer("DELETE", behind(f"lc-{T}-base-leaf"), "L1:1", False)   # a concurrent repair deleted it first
        es.request = lambda m, p, b=None, **kw: gone if m == "DELETE" else real(m, p, b, **kw)
        self.assertEqual(repair(es)["projections_deleted"], 1)

    def test_a_failed_delete_is_an_error(self):
        es = Scripted({"_index": f"lc-{T}-src", "_id": "L1:1", "found": False})
        real = es.request
        es.request = lambda m, p, b=None, **kw: (503, {}) if m == "DELETE" else real(m, p, b, **kw)
        with self.assertRaises(WriterError):
            repair(es)


if __name__ == "__main__":
    unittest.main()
