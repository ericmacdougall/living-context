"""F4: an ordinary error (HTTP 503, or a transport exception) in the middle of a claimed leaf change never discards the durable claim
after the change's source landed.

Review round 3 F4: a correction's source append succeeds and its projection write answers 503; the old `finally` abandoned the claim, so
the next correction (another text) reused version cur + 1 and failed on "version already written with another text", and repair skipped
the source. The sweep makes the k-th ingest request of a correction, a supersede and a retract fail, for every k, both as an HTTP error
and as an exception, then checks that the leaf is usable and consistent: no claim is left on the head once the store answers again, the
next change of the leaf succeeds (after at most one version conflict that names the current version), and the head's version has a
source and a projection with the same text. The pool tools run over a document store with ES write semantics (tests/unit/fake_es.py).
"""
import base64
import tempfile
import unittest
from pathlib import Path

from living_context.config import Constants
from living_context.gateway import pointers
from living_context.gateway.class_reader import ClassReader
from living_context.gateway.es import EsError
from living_context.gateway.fence import SendFence
from living_context.gateway.indices import pool_index, tenant_index
from living_context.gateway.pool_tools import PoolTools
from living_context.gateway.revocation import EpochTable
from living_context.gateway.admin import GrantStore
from living_context.gateway.sessions import Sessions
from living_context.ingest.subjects import Guard, TokenSource
from living_context.ingest.writer import Writer
from living_context.keystore import Keystore
from living_context.policy import ClassLattice, Principal, Role
from living_context.policy import pools as pl
from tests.unit.fake_es import DocStore

T = "tena"
LAT = {T: ClassLattice(["base"], [])}
OPS = {"query", "save", "update", "retract"}


class Adapter:
    human_presence = True

    def verify(self, tenant, proof):
        return proof.decode() if proof.startswith(b"user") else None


class NoModel:
    def __getattr__(self, name):
        raise AssertionError("no model at stage 0")


class Failing:
    """The writer's client: the k-th request (counted while `armed`) fails once, as an HTTP 503 or as a transport exception."""

    def __init__(self, inner):
        self.inner, self.armed, self.k, self.n, self.mode, self.fired = inner, False, 0, 0, "503", False

    def request(self, method, path, body=None, **kw):
        if self.armed:
            self.n += 1
            if self.n == self.k:
                self.fired = True
                if self.mode == "503":
                    return 503, {"error": {"type": "unavailable_shards_exception"}}
                raise EsError("TimeoutError")
        return self.inner.request(method, path, body, **kw)


class World:
    def __init__(self, tmp: Path):
        self.es = DocStore()
        self.ks = Keystore(tmp / "ks.db", T)
        self.dir = pl.PoolDirectory()
        store = GrantStore()
        store.restore(Principal(T, "pt-user-alice", frozenset({Role.READER, Role.WRITER}), frozenset({"base"}), frozenset({"base"})))
        fences = {}
        self.sessions = Sessions({T: Adapter()}, EpochTable(lambda t: fences.setdefault(t, SendFence())), lambda t, w: f"pt-{w}",
                                 lambda t, k: store.get(t, k), consts=Constants(), directory=self.dir)
        self.wclient = Failing(self.es)
        self.reader = ClassReader(lambda cls: self.es, T)
        self.writer = Writer(lambda: self.wclient, self.ks, T, Guard(TokenSource(self.ks)), reader=self.reader)
        self.tools = PoolTools(T, self.dir, self.sessions, LAT, self.writer, self.reader, lambda cls: self.es,
                               pointers.PointerKeys(tmp / "pk"), consts=Constants(), model=NoModel())
        self.pool = self.dir.create(T, "base", "team", "alpha").id
        self.dir.grant(T, "pt-user-alice", self.pool, OPS)
        self.tok = self.sessions.mint_scoped(T, b"user-alice", conversation="c1", scope={self.pool: frozenset(OPS)})

    def head(self, leaf):
        got = self.es.docs.get((f"lc-{T}-base-ctl", f"lh:{self.pool}:{leaf}"))
        return None if got is None else got[0]

    def texts(self, leaf, lv):
        s = self.es.docs.get((tenant_index(T, "src"), f"{leaf}:{lv}"))
        p = self.es.docs.get((pool_index(T, "base", self.pool, "leaf"), f"{leaf}:{lv}"))
        src = None if s is None else self.ks.decrypt(leaf, lv, base64.b64decode(s[0]["ct"]), base64.b64decode(s[0]["nonce"])).decode()
        return src, None if p is None else p[0]["text"]


def call(fn, *a):
    try:
        return fn(*a)
    except Exception as e:   # an error the tool did not turn into an answer: the caller sees a failure either way
        return {"ok": False, "error": type(e).__name__}


def change(w, mode, pointer, text, key):
    if mode == "retract":
        return call(w.tools.retract, w.tok, {"pointers": [pointer], "reason": "other", "key": key})
    return call(w.tools.update, w.tok, {"pointer": pointer, "mode": mode, "text": text, "key": key})


class ClaimErrors(unittest.TestCase):
    def sweep(self, mode, fail_mode):
        bad = []
        for k in range(1, 40):
            with tempfile.TemporaryDirectory() as d:
                w = World(Path(d))
                saved = w.tools.save(w.tok, {"pool": w.pool, "text": "the gannet roost is on the stack", "key": "s"})
                self.assertTrue(saved["ok"], saved)
                leaf = pointers.open_(w.tools.keys, T, saved["pointer"]).id
                w.wclient.armed, w.wclient.k, w.wclient.mode = True, k, fail_mode
                change(w, mode, saved["pointer"], "the gannet roost is on the arch", "u1")
                w.wclient.armed = False
                if not w.wclient.fired:
                    return bad, k   # k is past the last request of a clean change: the sweep is complete
                # the store answers again: the next change of the live leaf (the newer one, if the supersede landed) must succeed and
                # leave it consistent; a fresh pointer is taken from the head, as a client re-reading the item would
                live = leaf
                h0 = w.head(leaf)
                if h0 is not None and h0.get("status") == "superseded" and h0.get("superseded_by"):
                    live = h0["superseded_by"]
                hl = w.head(live)
                problems = []
                if h0 is None or h0.get("pending_op"):
                    problems.append(f"claim left on the head: {h0 and h0.get('pending_kind')}")
                if hl is None or hl.get("status") not in ("live", "retracted"):
                    problems.append(f"the live leaf's head is {hl and hl.get('status')}")
                elif hl.get("status") == "live":
                    nxt = "retract" if mode == "retract" else "correct"
                    ptr = w.tools._seal(w.pool, live, int(hl["current_lv"]))
                    ans = change(w, nxt, ptr, "the gannet roost is on the skerry", "u2")
                    if not ans.get("ok"):
                        problems.append(f"next change failed: {ans}")
                    hl = w.head(live)
                    if hl is None or hl.get("pending_op"):
                        problems.append("claim left after the next change")
                    elif hl.get("status") == "live":
                        src, proj = w.texts(live, int(hl["current_lv"]))
                        if src is None or src != proj:
                            problems.append(f"version {hl['current_lv']}: source and projection differ")
                try:
                    w.writer.repair(["base"])
                except Exception as e:
                    problems.append(f"repair failed: {type(e).__name__}")
                if problems:
                    bad.append((k, problems))
                w.ks.close()
        self.fail("the sweep did not reach a clean change")

    def test_correct_supersede_and_retract_survive_an_error_at_every_request(self):
        for mode in ("correct", "supersede", "retract"):
            for fail_mode in ("503", "exception"):
                with self.subTest(mode=mode, fail=fail_mode):
                    bad, n = self.sweep(mode, fail_mode)
                    self.assertGreater(n, 2)   # the change made requests, so the sweep tested something
                    self.assertEqual(bad, [])


if __name__ == "__main__":
    unittest.main()
