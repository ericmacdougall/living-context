"""The contract every answer judge of gateway/reads.py keeps (review round 6, class C7): a malformed answer is never accepted.

The judges are enumerated from reads.py by AST, not listed by hand: every function whose first two parameters are `(status, body)` is a
validator and must be in `reads.VALIDATORS` (which tests/unit/test_read_paths.py uses to decide which call sites are routed), and every
function that sends a request (`<client>.request(...)`, or a call of another such function) is a request-owning helper. Each one must
refuse every answer of MALFORMED with `IncompleteRead`, and accept one well-formed answer (the control, so a judge that refuses
everything cannot pass). A helper given a permanently malformed answer must refuse at its first request (it never spins)."""
import ast
import copy
import unittest
from pathlib import Path

from living_context.gateway import reads
from tests.unit.fake_es import SHARDS_ONE, doc_answer

READS = Path(reads.__file__)
IX, DID = "lc-tena-base-ctl", "d1"
SEARCH = {"timed_out": False, "_shards": SHARDS_ONE, "hits": {"hits": []}}

MALFORMED = {
    "200 with an empty body": (200, {}),
    "201 with an empty body": (201, {}),
    "200 with a list body": (200, []),
    "200 with a text body": (200, "ok"),
    "200 with no body": (200, None),
    "404 with an empty body": (404, {}),
    "409 with an empty body": (409, {}),
    "500 with an error": (500, {"error": {}}),
    "503 with an empty body": (503, {}),
}

# validator -> (the arguments after (status, body), one well-formed answer it accepts)
VALIDATOR_CASES = {
    "complete_hits": ((), (200, SEARCH)),
    "complete_search": ((), (200, SEARCH)),
    "complete_count": ((), (200, {"count": 0, "_shards": SHARDS_ONE})),
    "broadcast": ((), (200, {"_shards": SHARDS_ONE})),
    "alias_answer": ((IX,), (200, {IX + "-p0a0b0c0d": {"aliases": {IX: {}}}})),
    "pit_answer": ((IX, True), (200, {"id": "pit1", "_shards": SHARDS_ONE})),
    "doc_read": ((IX, DID, True), doc_answer("GET", IX, DID, True, {})),
    "doc_written": ((IX, DID, True), doc_answer("PUT", IX, DID, True)),
    "doc_deleted": ((IX, DID, True), doc_answer("DELETE", IX, DID, True)),
    "mget_items": (([(IX, DID)], True), (200, {"docs": [{"_index": IX, "_id": DID, "found": False}]})),
    "acknowledged": ((), (200, {"acknowledged": True})),
    "index_created": ((IX,), (200, {"acknowledged": True, "index": IX})),
    "setting_value": ((IX, "index.k", True), (200, {IX: {"settings": {"index.k": "0"}}})),
    "deleted_docs": ((IX, True), (200, {"_shards": SHARDS_ONE, "_all": {"primaries": {"docs": {"deleted": 0}}},
                                        "indices": {IX: {"primaries": {"docs": {"deleted": 0}}}}})),
    "api_keys_active": (("lc-",), (200, {"api_keys": []})),
    "api_keys_invalidated": ((), (200, {"error_count": 0, "invalidated_api_keys": [], "previously_invalidated_api_keys": []})),
    "api_key_created": (("lc-tena-reader",), (200, {"id": "k1", "name": "lc-tena-reader", "encoded": "eA=="})),
}

# request-owning helper -> (how it is called on a client, the well-formed answers it is given in order)
HELPER_CASES = {
    "refresh": (lambda c: reads.refresh(c, IX), [(200, {"_shards": SHARDS_ONE})]),
    "aliases": (lambda c: reads.aliases(c, IX), [(200, {IX + "-p0a0b0c0d": {"aliases": {IX: {}}}})]),
    "scan": (lambda c: reads.scan(c, IX, {"query": {"match_all": {}}}, missing_ok=True),
             [(200, {"id": "pit1", "_shards": SHARDS_ONE}), (200, SEARCH), (200, {"succeeded": True, "num_freed": 1})]),
    "read_doc": (lambda c: reads.read_doc(c, IX, DID, alias=True), [doc_answer("GET", IX, DID, True, {})]),
    "put_doc": (lambda c: reads.put_doc(c, IX, DID, {}, alias=True), [doc_answer("PUT", IX, DID, True)]),
    "put_doc_landed": (lambda c: reads.put_doc_landed(c, IX, DID, {}, alias=True), [doc_answer("PUT", IX, DID, True)]),
    "delete_doc": (lambda c: reads.delete_doc(c, IX, DID, alias=True), [doc_answer("DELETE", IX, DID, True)]),
    "mget_docs": (lambda c: reads.mget_docs(c, [(IX, DID)], alias=True), [(200, {"docs": [{"_index": IX, "_id": DID, "found": False}]})]),
}


# `(status, body)` functions that only answer a yes/no question inside a validator and accept nothing by themselves. They are not in
# VALIDATORS, so test_read_paths.py does not count a call site that uses one alone as routed (it fails the guard).
PREDICATES = {"version_conflict": "the typed 409 test of `doc_written`: a bool, never an accepted answer"}


# Every judge looks inside the body, so a malformed answer can also be a well-formed one with one field wrong. Each case of FIELD_CASES
# starts from the answer the cluster gives (tests/unit/fake_es.py and tests/security/test_pools_rescue_es.py pin these shapes) and
# breaks one field at a time, at any depth: the field dropped (a dictionary key; dropping a list item can leave a valid answer), or set
# to each wrong value listed for it. Every such answer must be refused, and so must the unbroken body under any other status.
STATUSES = (200, 201, 404, 409, 500, 503)
PHYS = IX + "-p0a0b0c0d"
OTHER = "lc-tenb-base-ctl"
SH = {"total": 1, "successful": 1, "failed": 0}
# a complete `_shards` section (searches, counts, points in time): every shard successful, none failed, integers only
COMPLETE = [(("_shards",), ("x", None)), (("_shards", "failed"), (1, "0", False, None)), (("_shards", "total"), ("1", True, None)),
            (("_shards", "successful"), (0, "1", True, None))]
# a broadcast (refresh, flush, force merge, stats): no failed shard, integer counts (an unassigned copy is not counted as failed)
BROADCAST = [(("_shards",), ("x", None)), (("_shards", "failed"), (1, "0", False, None)), (("_shards", "total"), ("1", True, None)),
             (("_shards", "successful"), ("1", True, None))]
# a write or delete acknowledgement: the primary applied it
LANDED = [(("_shards",), ("x", None)), (("_shards", "successful"), (0, "1", True, None)), (("_shards", "failed"), ("0", True, None))]
SEQ = [(("_seq_no",), ("1", -1, True, None)), (("_primary_term",), (0, "1", True, None))]
ABOUT = [(("_index",), (OTHER, IX + "-pnothex00", 7)), (("_id",), ("d2", 7))]


def at(prefix, fields):
    return [(prefix + path, wrong) for path, wrong in fields]


FOUND, NOT_FOUND = doc_answer("GET", IX, DID, True, {})[1], doc_answer("GET", IX, DID, False)[1]
WROTE, DELETED, NOT_THERE = doc_answer("PUT", IX, DID, True)[1], doc_answer("DELETE", IX, DID, True)[1], doc_answer("DELETE", IX, DID, False)[1]
HITS = {"timed_out": False, "_shards": SH, "hits": {"hits": [{"_index": IX, "_id": DID}]}}
HITS_OFF = [(("timed_out",), (True, "false", None)), (("terminated_early",), (True,)), *COMPLETE, (("hits",), ("x", None, [])),
            (("hits", "hits"), ("x", None, {})), (("hits", "hits", 0), ("x", None))]
STATS = {"_shards": SH, "_all": {"primaries": {"docs": {"deleted": 2}}}, "indices": {IX: {"primaries": {"docs": {"deleted": 2}}}}}

# case -> (validator, the arguments after (status, body), the statuses it accepts this body under, the well-formed body,
#          [(path into the body, the wrong values for that field)])
FIELD_CASES = {
    "complete_hits": ("complete_hits", (), (200,), HITS, HITS_OFF),
    "complete_search": ("complete_search", (), (200,), HITS, HITS_OFF),
    "complete_count": ("complete_count", (), (200,), {"count": 3, "_shards": SH}, [(("count",), ("3", -1, True, 3.0, None)), *COMPLETE]),
    "broadcast": ("broadcast", (), (200,), {"_shards": SH}, BROADCAST),
    "alias_answer": ("alias_answer", (IX,), (200,), {PHYS: {"aliases": {IX: {}}}},
                     [((PHYS,), ("x", None, {})), ((PHYS, "aliases"), ("x", None, [], {})), ((OTHER + "-p0a0b0c0d",), ({"aliases": {}},))]),
    "pit_answer": ("pit_answer", (IX, True), (200,), {"id": "pit1", "_shards": SH}, [(("id",), ("", 7, None)), *COMPLETE]),
    "doc_read found": ("doc_read", (IX, DID, True), (200,), FOUND,
                       [*ABOUT, (("found",), ("true", 1, None)), *SEQ, (("_source",), ("x", [], None))]),
    "doc_read absent": ("doc_read", (IX, DID, True), (404,), NOT_FOUND, [*ABOUT, (("found",), ("false", 0, None))]),
    "doc_written": ("doc_written", (IX, DID, True), (200, 201), WROTE, [*ABOUT, (("result",), ("noop", None)), *SEQ, *LANDED]),
    "doc_deleted": ("doc_deleted", (IX, DID, True), (200,), DELETED, [*ABOUT, (("result",), ("noop", None)), *SEQ, *LANDED]),
    "doc_deleted not there": ("doc_deleted", (IX, DID, True), (404,), NOT_THERE, [*ABOUT, (("result",), ("noop", None)), *SEQ]),
    "mget_items found": ("mget_items", ([(IX, DID)], True), (200,), {"docs": [FOUND]},
                         [(("docs",), ("x", None, [], {})), *at(("docs", 0), [*ABOUT, (("found",), ("true", 1, None)), *SEQ,
                                                                           (("_source",), ("x", [], None)), (("error",), ({},))])]),
    "mget_items absent": ("mget_items", ([(IX, DID)], True), (200,), {"docs": [NOT_FOUND]},
                          at(("docs", 0), [*ABOUT, (("found",), ("false", 0, None)), (("error",), ({"type": "index_not_found_exception"},))])),
    "acknowledged": ("acknowledged", (), (200,), {"acknowledged": True}, [(("acknowledged",), (False, "true", 1, None))]),
    "index_created": ("index_created", (IX,), (200,), {"acknowledged": True, "shards_acknowledged": True, "index": IX},
                      [(("acknowledged",), (False, "true", 1, None)), (("index",), (OTHER, PHYS, None))]),
    "setting_value": ("setting_value", (IX, "index.k", True), (200,), {IX: {"settings": {"index.k": "0"}}},
                      [((IX,), ("x", None, {})), ((IX, "settings"), ("x", None, {})), ((OTHER,), ({"settings": {"index.k": "0"}},))]),
    "deleted_docs": ("deleted_docs", (IX, True), (200,), STATS,
                     [*BROADCAST, (("_all",), ("x", None)), (("_all", "primaries", "docs", "deleted"), ("2", 3, None)),
                      (("indices",), ("x", None, {})), (("indices", IX), ("x", None)), (("indices", IX, "primaries"), ("x", None)),
                      (("indices", IX, "primaries", "docs"), ("x", None)), (("indices", IX, "primaries", "docs", "deleted"), ("2", -1, True, 3, None)),
                      (("indices", OTHER), ({"primaries": {"docs": {"deleted": 2}}},))]),
    "api_keys_active": ("api_keys_active", ("lc-",), (200,), {"api_keys": [{"id": "k1", "name": "lc-tena-reader", "invalidated": False}]},
                        [(("api_keys",), ("x", None, {})), (("api_keys", 0), ("x", None)), (("api_keys", 0, "id"), ("", 7, None)),
                         (("api_keys", 0, "name"), ("other-reader", 7, None)), (("api_keys", 0, "invalidated"), ("false", 0, None))]),
    "api_keys_invalidated": ("api_keys_invalidated", (), (200,),
                             {"error_count": 0, "invalidated_api_keys": ["k1"], "previously_invalidated_api_keys": []},
                             [(("error_count",), (1, "0", False, None)), (("invalidated_api_keys",), ("k1", None, {})),
                              (("invalidated_api_keys", 0), (7, None)), (("previously_invalidated_api_keys",), ("x", None))]),
    "api_key_created": ("api_key_created", ("lc-tena-reader",), (200,), {"id": "k1", "name": "lc-tena-reader", "encoded": "eA=="},
                        [(("id",), ("", 7, None)), (("name",), ("lc-tena-writer", None)), (("encoded",), ("", 7, None))]),
}


def _put(body, path, value, drop=False):
    """A deep copy of `body` with the field at `path` set to `value` (or, `drop`, removed)."""
    out = copy.deepcopy(body)
    x = out
    for k in path[:-1]:
        x = x[k]
    if drop:
        del x[path[-1]]
    else:
        x[path[-1]] = value
    return out


def one_field_off(good, fields):
    """(what, body) for every body that differs from `good` by one field: a present dictionary key dropped, or a field set to each of
    its wrong values."""
    for path, wrong in fields:
        name = ".".join(map(str, path))
        parent = good
        for k in path[:-1]:
            parent = parent[k]
        if isinstance(parent, dict) and path[-1] in parent:
            yield f"{name} dropped", _put(good, path, None, drop=True)
        for v in wrong:
            yield f"{name}={v!r}", _put(good, path, v)


class Client:
    """Answers its requests from `answers` in order; the last answer repeats for ever (a permanently malformed server)."""

    def __init__(self, answers):
        self.answers, self.sent = list(answers), []

    def request(self, method, path, body=None, **kw):
        self.sent.append((method, path))
        return self.answers[min(len(self.sent), len(self.answers)) - 1]


def judges():
    """(validators, request-owning helpers) of reads.py, found by AST."""
    tree = ast.parse(READS.read_text())
    funcs = {n.name: n for n in tree.body if isinstance(n, ast.FunctionDef)}
    validators = {name for name, f in funcs.items() if [a.arg for a in f.args.args[:2]] == ["status", "body"]}

    def calls(f):
        for n in ast.walk(f):
            if isinstance(n, ast.Call):
                if isinstance(n.func, ast.Attribute) and n.func.attr == "request":
                    yield "<request>"
                elif isinstance(n.func, ast.Name):
                    yield n.func.id
    helpers = {name for name, f in funcs.items() if "<request>" in set(calls(f))}
    while True:
        more = {name for name, f in funcs.items() if name not in helpers and helpers & set(calls(f))}
        if not more:
            return validators, helpers
        helpers |= more


class Contract(unittest.TestCase):
    def test_every_judge_of_reads_py_is_enumerated_and_covered_here(self):
        validators, helpers = judges()
        self.assertTrue(set(PREDICATES) <= validators and not set(PREDICATES) & set(reads.VALIDATORS))
        validators -= set(PREDICATES)
        self.assertEqual(validators, set(reads.VALIDATORS), "a validator of reads.py is missing from VALIDATORS (or a stale entry)")
        self.assertEqual(validators, set(VALIDATOR_CASES), "a validator of reads.py has no contract case here")
        self.assertEqual(helpers, set(HELPER_CASES), "a request-owning helper of reads.py has no contract case here")

    def test_every_validator_refuses_every_malformed_answer(self):
        for name, (args, _) in VALIDATOR_CASES.items():
            for what, (st, body) in MALFORMED.items():
                with self.subTest(validator=name, answer=what):
                    with self.assertRaises(reads.IncompleteRead):
                        getattr(reads, name)(st, body, *args)

    def test_control_every_validator_accepts_its_well_formed_answer(self):
        for name, (args, (st, body)) in VALIDATOR_CASES.items():
            with self.subTest(name):
                getattr(reads, name)(st, body, *args)

    def test_every_helper_refuses_a_permanently_malformed_server_at_its_first_request(self):
        for name, (call, _) in HELPER_CASES.items():
            for what, answer in MALFORMED.items():
                with self.subTest(helper=name, answer=what):
                    c = Client([answer])
                    with self.assertRaises(reads.IncompleteRead):
                        call(c)
                    self.assertEqual(len(c.sent), 1, "the helper asked again after a malformed answer")

    def test_control_every_helper_accepts_well_formed_answers(self):
        for name, (call, answers) in HELPER_CASES.items():
            with self.subTest(name):
                c = Client(answers)
                call(c)
                self.assertEqual(len(c.sent), len(answers))


class FieldShapes(unittest.TestCase):
    def test_every_validator_has_field_cases(self):
        # VALIDATORS is itself checked against the AST enumeration of reads.py (Contract above), so a new judge fails here until it
        # has cases of its own
        self.assertEqual({v[0] for v in FIELD_CASES.values()}, set(reads.VALIDATORS))

    def test_control_each_judge_accepts_the_answer_of_the_cluster(self):
        for case, (name, args, ok, good, _) in FIELD_CASES.items():
            for st in ok:
                with self.subTest(case, status=st):
                    getattr(reads, name)(st, copy.deepcopy(good), *args)

    def test_one_field_off_is_refused(self):
        for case, (name, args, ok, good, fields) in FIELD_CASES.items():
            for what, body in one_field_off(good, fields):
                for st in ok:
                    with self.subTest(case, field=what, status=st):
                        with self.assertRaises(reads.IncompleteRead):
                            getattr(reads, name)(st, body, *args)

    def test_the_right_body_under_another_status_is_refused(self):
        for case, (name, args, ok, good, _) in FIELD_CASES.items():
            for st in set(STATUSES) - set(ok):
                with self.subTest(case, status=st):
                    with self.assertRaises(reads.IncompleteRead):
                        getattr(reads, name)(st, copy.deepcopy(good), *args)

    def test_a_multi_get_item_with_an_error_is_not_an_absence(self):
        item = {"_index": IX, "_id": DID, "error": {"type": "index_not_found_exception", "index": IX}}
        for what, d in (("error only", item), ("error and found false", dict(item, found=False))):
            with self.subTest(what):
                with self.assertRaises(reads.IncompleteRead):
                    reads.mget_items(200, {"docs": [d]}, [(IX, DID)], True)


if __name__ == "__main__":
    unittest.main()
