"""The purge must not ignore a refused settings change (the expunge threshold). Pure test with a scripted client."""
import unittest

from living_context.erasure import purge

KEY = "index.merge.policy.expunge_deletes_allowed"


class Es:
    def __init__(self, put_status, value):
        self.put_status, self.value, self.calls = put_status, value, []

    def request(self, method, path, body=None, **kw):
        self.calls.append((method, path))
        if method == "PUT":
            return self.put_status, {}
        if method == "GET" and "_settings" in path:
            return 200, {"ix": {"settings": {KEY: self.value} if self.value is not None else {}}}
        return 200, {}


class Threshold(unittest.TestCase):
    def test_accepted_change_needs_no_lookup(self):
        es = Es(200, None)
        purge.ensure_expunge_threshold(es, "ix")
        self.assertEqual([m for m, _ in es.calls], ["PUT"])

    def test_refused_change_is_fine_when_the_index_already_carries_zero(self):
        purge.ensure_expunge_threshold(Es(403, "0"), "ix")

    def test_refused_change_on_an_index_without_the_setting_is_an_error_not_a_skip(self):
        with self.assertRaises(purge.PurgeError):
            purge.ensure_expunge_threshold(Es(403, None), "ix")
        with self.assertRaises(purge.PurgeError):
            purge.ensure_expunge_threshold(Es(403, "10%"), "ix")


if __name__ == "__main__":
    unittest.main()
