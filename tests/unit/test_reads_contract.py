"""The contract every answer judge of gateway/reads.py keeps (review round 6, class C7): a malformed answer is never accepted.

The judges are enumerated from reads.py by AST, not listed by hand: every function whose first two parameters are `(status, body)` is a
validator and must be in `reads.VALIDATORS` (which tests/unit/test_read_paths.py uses to decide which call sites are routed), and every
function that sends a request (`<client>.request(...)`, or a call of another such function) is a request-owning helper. Each one must
refuse every answer of MALFORMED with `IncompleteRead`, and accept one well-formed answer (the control, so a judge that refuses
everything cannot pass). A helper given a permanently malformed answer must refuse at its first request (it never spins)."""
import ast
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


if __name__ == "__main__":
    unittest.main()
