"""The purge must not ignore a refused settings change (the expunge threshold), and its `ok` (the deleted-docs count is 0) is taken only
from well-formed answers about the index purged (review round 6). Pure test with a scripted client."""
import unittest

from living_context.erasure import purge

KEY = "index.merge.policy.expunge_deletes_allowed"
SHARDS = {"total": 1, "successful": 1, "failed": 0}


def stats(index, deleted):
    return 200, {"_shards": SHARDS, "_all": {"primaries": {"docs": {"count": 3, "deleted": deleted}}},
                 "indices": {index: {"uuid": "u", "primaries": {"docs": {"count": 3, "deleted": deleted}}}}}


class Es:
    def __init__(self, put_answer, value, *, settings_index="ix", merge=(200, {"_shards": SHARDS}), stats_answer=None):
        self.put_answer, self.value, self.calls = put_answer, value, []
        self.settings_index, self.merge, self.stats_answer = settings_index, merge, stats_answer or stats("ix", 0)

    def request(self, method, path, body=None, **kw):
        self.calls.append((method, path))
        if method == "PUT":
            return self.put_answer
        if method == "GET" and "_settings" in path:
            return 200, {self.settings_index: {"settings": {KEY: self.value} if self.value is not None else {}}}
        if method == "GET" and "_stats" in path:
            return self.stats_answer
        if "_forcemerge" in path:
            return self.merge
        return 200, {"_shards": SHARDS}


ACK = (200, {"acknowledged": True})
REFUSED = (403, {"error": {"type": "security_exception"}, "status": 403})


class Threshold(unittest.TestCase):
    def test_accepted_change_needs_no_lookup(self):
        es = Es(ACK, None)
        purge.ensure_expunge_threshold(es, "ix")
        self.assertEqual([m for m, _ in es.calls], ["PUT"])

    def test_refused_change_is_fine_when_the_index_already_carries_zero(self):
        purge.ensure_expunge_threshold(Es(REFUSED, "0"), "ix")

    def test_refused_change_on_an_index_without_the_setting_is_an_error_not_a_skip(self):
        with self.assertRaises(purge.PurgeError):
            purge.ensure_expunge_threshold(Es(REFUSED, None), "ix")
        with self.assertRaises(purge.PurgeError):
            purge.ensure_expunge_threshold(Es(REFUSED, "10%"), "ix")

    def test_an_unacknowledged_change_is_not_a_change(self):
        # round 6: at 4e7ee85 any HTTP 200 to the settings PUT was the change, an empty body included
        for answer in ((200, {}), (200, {"acknowledged": False}), (200, [])):
            with self.subTest(answer):
                with self.assertRaises(purge.PurgeError):
                    purge.ensure_expunge_threshold(Es(answer, "10%"), "ix")

    def test_a_readback_about_another_index_proves_nothing(self):
        # round 6: at 4e7ee85 the first index of the answer was read, whichever it was
        with self.assertRaises(purge.PurgeError):
            purge.ensure_expunge_threshold(Es(REFUSED, "0", settings_index="lc-t-other"), "ix")


class DeletedCount(unittest.TestCase):
    def purge(self, **kw):
        return purge.purge_index(Es(ACK, "0", **kw), "ix", deadline_s=0, interval_s=0, sleep=lambda s: None)

    def test_control_a_purge_whose_count_reads_zero_is_ok(self):
        self.assertTrue(self.purge().ok)

    def test_a_malformed_or_foreign_count_is_never_zero(self):
        # round 6: at 4e7ee85 the count was `_all.primaries.docs.deleted` of whatever came back with HTTP 200
        for name, answer in {
            "200 with an empty body": (200, {}),
            "a count about another index": stats("lc-t-other", 0),
            "a count with a failed shard": (200, dict(stats("ix", 0)[1], _shards={"total": 1, "successful": 0, "failed": 1})),
            "a count that is not an integer": stats("ix", "0"),
            "503": (503, {"error": {"type": "unavailable"}}),
        }.items():
            with self.subTest(name):
                with self.assertRaises(purge.PurgeError):
                    self.purge(stats_answer=answer)

    def test_a_force_merge_that_did_not_run_is_an_error(self):
        for name, answer in {"200 with an empty body": (200, {}), "a failed shard": (200, {"_shards": dict(SHARDS, failed=1)})}.items():
            with self.subTest(name):
                with self.assertRaises(purge.PurgeError):
                    self.purge(merge=answer)


if __name__ == "__main__":
    unittest.main()
