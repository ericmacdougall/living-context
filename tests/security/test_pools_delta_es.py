"""Pools delta review 1 (goodqa#16441, findings F1 to F5 and F9) on a real Elasticsearch: each case reproduces the finding against
e979747 (red) and holds on the repair. Paused senders are events, not sleeps; a crash is `os._exit` in a second process
(tests/faults/pools_child.py), never an in-process retry. Needs LC_TEST_ES_URL, LC_TEST_CA and LC_TEST_PW; without them the module
FAILS (it never skips)."""
import base64
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path

from living_context.config import Constants
from living_context.gateway import boot, epoch_journal, pointers, pool_boot
from living_context.gateway.class_reader import ClassReader
from living_context.gateway.handler import Gateway
from living_context.gateway.indices import class_index, pool_index
from living_context.gateway.journal import TombstoneJournal
from living_context.gateway.pool_root import start_pools
from living_context.gateway.revocation import RevocationService
from living_context.gateway.sessions import Sessions
from living_context.ingest.subjects import Guard, TokenSource
from living_context.ingest.writer import Writer
from living_context.policy import Principal, Role
from living_context.policy import pools as pl
from living_context.verifier.check import pool_residue
from tests.security.test_pools_es import LAT, Adapter, Base, FailingModel, T, UNAV

REPO = Path(__file__).resolve().parents[2]


class Partial:
    """The real authz/erase client, except that a search answers HTTP 200 with a PARTIAL result: `timed_out` with no hits, or one failed
    shard (the shapes Elasticsearch returns when a search times out or a shard is unavailable and partial results are allowed)."""

    def __init__(self, inner, mode="timed_out"):
        self.inner, self.mode, self.partials = inner, mode, 0

    def request(self, method, path, body=None, **kw):
        if method == "POST" and "/_search" in path:
            self.partials += 1
            if self.mode == "timed_out":
                return 200, {"timed_out": True, "_shards": {"total": 1, "successful": 1, "skipped": 0, "failed": 0},
                             "hits": {"total": {"value": 0, "relation": "eq"}, "hits": []}}
            st, r = self.inner.request(method, path, body, **kw)
            if st == 200 and isinstance(r, dict):
                r = dict(r, _shards={"total": 2, "successful": 1, "skipped": 0, "failed": 1}, hits=dict(r["hits"], hits=[]))
            return st, r
        return self.inner.request(method, path, body, **kw)


class PartialGate:
    def __init__(self, gate, roles=("authz",), mode="timed_out"):
        self.gate, self.roles, self.mode = gate, roles, mode

    def client(self, tenant, role):
        c = self.gate.client(tenant, role)
        return Partial(c, self.mode) if role in self.roles else c


class Root(Base):
    """The tenant wired by the composition root (`start_pools`), as the e2e module does."""

    def setUp(self):
        self.jdir = Path(tempfile.mkdtemp(dir=self.root)) / "j"
        epoch_journal.provision(self.jdir, 65536)
        self.pkeys = self.root / f"d-pk{Base.n + 1}"
        super().setUp()

    def build(self, gate=None, **kw):
        self.consts = Constants(tok_live_max=100, mint_rate_per_h=1000)
        self.dir = pl.PoolDirectory()
        self.sessions = Sessions({T: Adapter()}, self.epochs, lambda t, w: f"pt-{w}", lambda t, k: self.store.get(t, k), consts=self.consts,
                                 directory=self.dir, token_persist=pool_boot.token_persister(lambda: self.gate.client(T, "authz"), T))
        self.rev = RevocationService(T, self.epochs, self.jdir)
        wreader = ClassReader(lambda cls: self.cc(f"read-{cls}"), T)
        self.writer = Writer(lambda: self.cc("ingest"), self.ks, T, Guard(TokenSource(self.ks)), reader=wreader, gate=self.wgate)
        self.rt = start_pools(T, gate=gate or self.gate, sessions=self.sessions, directory=self.dir, lattice=LAT[T], revocation=self.rev,
                              writer=self.writer, pointer_key_path=self.pkeys, create_index=self._create,
                              grants=lambda t, k: self.store.get(t, k), principal_token=lambda t, w: f"pt-{w}",
                              eraser=lambda cls, pool: self.eraser().delete(cls, pool), consts=self.consts)
        self.gw = Gateway(self.gate, self.sessions, LAT, writers=lambda t: self.writer, pools=lambda t: self.rt.router)

    def adm(self, **req):
        return self.gw.handle(self.sessions.mint(T, b"user-admin"), req)

    def block_authz(self, on):
        st, r = self.admin.request("PUT", f"/lc-{T}-authz/_settings", {"index.blocks.write": True if on else None})
        self.assertEqual(st, 200, r)

    labels = 0

    def removed_while_authz_blocked(self):
        """A pool and a grant of alice, then the grant removed while the authz copy cannot be written: the journal carries the removal,
        the authz copy still holds the stale grant."""
        Root.labels += 1
        a = self.adm(op="pool_create", cls="base", kind="team", label=f"stale crew {Root.labels}")["pool"]
        self.assertTrue(self.adm(op="pool_grant", who="user-alice", pool=a)["ok"])
        self.block_authz(True)
        try:
            self.assertTrue(self.adm(op="pool_ungrant", who="user-alice", pool=a)["ok"])
        finally:
            self.block_authz(False)
        st, doc = self.admin.request("GET", f"/lc-{T}-authz/_doc/pg:pt-user-alice:{a}")
        self.assertEqual(st, 200)                                          # the stale grant is still in the copy
        return a


class StartAdmission(Root):
    def test_f1_a_journal_that_does_not_open_gives_no_pool_runtime(self):
        a = self.removed_while_authz_blocked()
        # an unclean stop (no shutdown record), then a restart: the journal refuses to open before it replays the removal
        refused = False
        try:
            self.build()
        except Exception:
            refused = True
        if not refused:
            self.assertNotEqual(self.rev.state, "open")
            r = self.gw.mint(T, b"user-alice", {"conversation": "c9", "scope": {a: ["query"]}})
            self.assertEqual(r, UNAV, "a runtime over a journal that did not open minted a token from the stale grant")
        self.assertTrue(refused, "start_pools returned a runtime although the journal did not open")


class CompleteReads(Root):
    def test_f5_an_http_200_partial_authz_read_never_confirms_a_removal(self):
        for mode in ("timed_out", "failed_shard"):
            with self.subTest(mode=mode):
                a = self.removed_while_authz_blocked()
                self.rev.clean_stop()
                refused = False
                try:
                    self.build(gate=PartialGate(self.gate, mode=mode))
                except Exception:
                    refused = True
                if not refused:
                    # the partial read was taken as complete: the removal is confirmed, compaction drops its record, and a later complete
                    # read of the stale copy restores the removed grant
                    self.rev.compact()
                    self.rev.clean_stop()
                    self.build()
                    self.assertEqual(self.dir.live_ops(T, "pt-user-alice", a), frozenset(), "the removed grant came back")
                self.assertTrue(refused, "a partial HTTP 200 authz read was accepted at start-up")
                self.rev.clean_stop() if self.rev.state == "open" else None
                self.jdir = Path(tempfile.mkdtemp(dir=self.root)) / "j"
                epoch_journal.provision(self.jdir, 65536)
                self.build()

    def test_f5_every_authz_and_erasure_scan_refuses_a_partial_200(self):
        from living_context.erasure.pool_scope import PoolEraseError, PoolEraser
        a = self.adm(op="pool_create", cls="base", kind="team", label="scan crew")["pool"]
        self.refresh()
        for mode in ("timed_out", "failed_shard"):
            with self.subTest(mode=mode):
                with self.assertRaises(Exception):
                    boot.load_grants(PartialGate(self.gate, mode=mode), T)
                er = PoolEraser(lambda: Partial(self.gate.client(T, "erase"), mode), lambda n: None, T, self.ks, self.journal, self.wgate)
                with self.assertRaises(PoolEraseError):
                    er._docs(a)
                with self.assertRaises(PoolEraseError):
                    er._leaf_ids("base", a)


class Provenance(Base):
    def drop_projection(self, pool):
        idx = pool_index(T, "base", pool, "leaf")
        self.refresh()
        st, hits = self.admin.request("POST", f"/{idx}/_search", {"query": {"match_all": {}}, "_source": False})
        for h in hits["hits"]["hits"]:
            self.admin.request("DELETE", f"/{idx}/_doc/{h['_id']}?refresh=true")

    def test_f3_repair_keeps_human_origin_and_an_agent_still_cannot_retract_or_correct_it(self):
        ops = {"query", "save", "update", "retract"}
        a = self.pool("alpha", ops=ops)
        human = self.mint("alice", {a: ops}, human=True)
        agent = self.mint("alice", {a: ops})
        s = self.tools.save(human, {"pool": a, "text": "the human wrote this heron note", "key": "kh"})
        self.assertTrue(s["ok"], s)
        self.assertEqual(self.tools.fetch(human, {"pointer": s["pointer"]})["origin"], "human")
        self.drop_projection(a)                                            # an interrupted projection, or a rebuild
        self.writer.repair(["base"])
        self.refresh()
        f = self.tools.fetch(human, {"pointer": s["pointer"]})
        self.assertEqual((f["status"], f["origin"]), ("ok", "human"), f)
        self.assertEqual(self.tools.update(agent, {"pointer": s["pointer"], "mode": "correct", "text": "agent edit", "key": "u"}),
                         {"ok": False, "error": "invalid"})
        r = self.tools.retract(agent, {"pointers": [s["pointer"]], "reason": "wrong", "key": "r"})
        self.assertEqual(r["results"], ["held"], r)
        self.assertEqual(self.tools.fetch(human, {"pointer": s["pointer"]})["status"], "ok")

    def test_f3_a_source_without_provenance_is_repaired_with_the_strict_origin(self):
        ops = {"query", "save", "update", "retract"}
        a = self.pool("alpha", ops=ops)
        agent = self.mint("alice", {a: ops})
        s = self.tools.save(agent, {"pool": a, "text": "an old note from before provenance was kept", "key": "k"})
        self.refresh()
        leaf = pointers.open_(self.keys, T, s["pointer"]).id
        st, src = self.admin.request("GET", f"/lc-{T}-src/_doc/{leaf}:1")
        legacy = {k: v for k, v in src["_source"].items() if k not in ("prov", "prov_json")}
        self.assertEqual(self.admin.request("PUT", f"/lc-{T}-src/_doc/{leaf}:1?refresh=true", legacy)[0], 200)
        self.drop_projection(a)
        self.writer.repair(["base"])
        self.refresh()
        f = self.tools.fetch(agent, {"pointer": s["pointer"]})
        self.assertNotEqual(f.get("origin"), "agent", f)                  # unknown provenance is never the most permissive origin
        r = self.tools.retract(agent, {"pointers": [s["pointer"]], "reason": "wrong", "key": "r"})
        self.assertEqual(r["results"], ["held"], r)


class Child:
    """Runs tests/faults/pools_child.py in a second process and expects it to die at the step."""

    def __init__(self, test):
        self.t = test

    def cfg(self, **kw):
        t = self.t
        base = {"tenant": T, "keys": dict(t.gate._keys[T]), "keystore": str(t.root / "ks" / f"{T}.db"),
                "tj": [str(t.journal._p), str(t.journal._r)], "pointer_keys": str(t.keys._path)}
        base.update(kw)
        return base

    def run(self, expect_crash=True, **kw):
        path = Path(tempfile.mkdtemp(dir=self.t.root)) / "child.json"
        path.write_text(json.dumps(self.cfg(**kw)))
        p = subprocess.run([sys.executable, "-m", "tests.faults.pools_child", str(path)], cwd=REPO, capture_output=True, text=True,
                           timeout=300)
        if expect_crash:
            self.t.assertEqual(p.returncode, 77, (p.stdout[-2000:], p.stderr[-2000:]))
        return p


class TwoAdmins(Base):
    def setUp(self):
        super().setUp()
        self.consts = Constants(tok_live_max=100, mint_rate_per_h=1000)
        self.build()
        self.store.restore(Principal(T, "pt-user-admin2", frozenset({Role.TENANT_ADMIN}), frozenset({"base"}), frozenset({"base"})))

    def delete(self, pool):
        out = []
        for who in ("admin", "admin2"):
            rec = self.sessions.resolve(self.sessions.mint(T, b"user-" + who.encode()))
            out.append(self.svc.handle("pool_delete", self.sessions.principal(rec), rec, {"pool": pool}))
        return out[-1]


class DeleteKeys(TwoAdmins):
    def plant(self, a, tag):
        """In pool `a`: a complete save, a taint hold (its own key, `hid`), and a save interrupted after its source append (a key and a
        source document, no projection). Returns the key ids."""
        tok = self.mint("alice", {a: {"query", "save"}})
        done = self.tools.save(tok, {"pool": a, "text": f"complete {tag} lanternfish", "key": "k1"})
        self.assertTrue(done["ok"], done)
        hid = self.writer.hold_tainted("pt-user-alice", "base", a, [a], f"held {tag} lanternfish", {"mode": "save", "origin": "agent"})

        def crash(name):
            if name == "source":
                raise RuntimeError("crash after the source append")
        w2 = Writer(lambda: self.cc("ingest"), self.ks, T, Guard(TokenSource(self.ks)), reader=self.reader, gate=self.wgate, pause=crash)
        t2 = type(self.tools)(T, self.dir, self.sessions, LAT, w2, self.reader, lambda cls: self.cc(f"read-{cls}"), self.keys,
                              consts=self.consts, model=FailingModel())
        with self.assertRaises(RuntimeError):
            t2.save(tok, {"pool": a, "text": f"interrupted {tag} lanternfish", "key": "k2"})
        self.refresh()
        st, r = self.admin.request("POST", f"/lc-{T}-src/_search", {"size": 10, "query": {"term": {"pool": a}}, "_source": ["leaf_id"]})
        leaves = {h["_source"]["leaf_id"] for h in r["hits"]["hits"]}
        self.assertEqual(len(leaves), 2, r)
        keys = [(x, 1) for x in sorted(leaves)] + [(hid, 1)]
        for k in keys:
            self.assertTrue(self.ks.has_key(*k), k)
        return keys

    def test_f2_pool_delete_destroys_the_keys_of_holds_and_of_writes_that_never_projected(self):
        a = self.pool("alpha")
        keys = self.plant(a, "plain")
        out = self.delete(a)
        self.assertTrue(out["ok"], out)
        self.assertEqual([k for k in keys if self.ks.has_key(*k)], [], "a key of the deleted pool survived")
        rr = pool_residue(self.gate.client(T, "verify"), T, a, [k[0] for k in keys], texts=["lanternfish"])
        self.assertEqual((rr["indices"], rr["docs"], rr["text"]), (0, 0, 0), rr)

    def test_f2_a_delete_that_crashes_in_another_process_at_any_step_is_finished_with_no_key_left(self):
        for step in ("closure", "boundary", "keys", "docs", "indices"):
            with self.subTest(step=step):
                a = self.pool("crash" + step)
                keys = self.plant(a, step)
                self.svc.set_state(a, pl.DELETING)                         # the first half of the delete: no write lands any more
                Child(self).run(op="erase", cls="base", pool={"id": a}, crash=step)
                self.journal = TombstoneJournal(self.journal._p, self.journal._r)   # the restart reads the journal the child appended to
                self.eraser().delete("base", a)                            # a fresh eraser (a restart) finishes it
                self.assertEqual([k for k in keys if self.ks.has_key(*k)], [], step)
                rr = pool_residue(self.gate.client(T, "verify"), T, a, [k[0] for k in keys], texts=["lanternfish"])
                self.assertEqual((rr["indices"], rr["docs"], rr["text"]), (0, 0, 0), (step, rr))


class ReleaseEvent(TwoAdmins):
    def held(self, a, text):
        """A conversation that read restricted content saves into base pool `a`: the save is held at the taint class."""
        from living_context.gateway.session_state import SessionStates, Taint
        hid = self.writer.hold_tainted("pt-user-alice", "base", a, [a], text, {"mode": "save", "origin": "agent"})
        return hid

    def human_token(self, a):
        return self.mint("alice", {a: {"query", "save", "review"}}, human=True)

    def events(self, a):
        self.refresh()
        st, r = self.admin.request("POST", f"/lc-{T}-*/_search?ignore_unavailable=true", {"size": 50, "query": {"bool": {"filter": [
            {"term": {"kind": "event"}}, {"term": {"pool": a}}]}}})
        return [h["_source"] for h in r["hits"]["hits"]] if st == 200 else []

    def leaves(self, a):
        self.refresh()
        st, r = self.admin.request("POST", f"/{pool_index(T, 'base', a, 'leaf')}/_search?ignore_unavailable=true",
                                   {"size": 50, "query": {"match_all": {}}, "_source": ["text"]})
        return [h["_source"]["text"] for h in r["hits"]["hits"]] if st == 200 else []

    def test_f9_a_release_paused_before_its_event_cannot_write_an_event_after_the_pool_is_deleted(self):
        a = self.pool("alpha", ops={"query", "save", "review"})
        hid = self.held(a, "held vellum canary")
        tok = self.human_token(a)
        shown = self.tools.hold_show(tok, {"pool": a, "hold": hid})
        self.assertTrue(shown["ok"], shown)
        real = self.writer.record_event
        at, go = threading.Event(), threading.Event()

        def paused(cls, pool, name, *args, **kw):
            if name.startswith("hold_admit"):
                at.set()
                go.wait(30)
            return real(cls, pool, name, *args, **kw)
        self.writer.record_event = paused
        res = []
        th = threading.Thread(target=lambda: res.append(self.tools.release(tok, {"pool": a, "hold": hid, "decision": "admit",
                                                                              "content_hash": shown["content_hash"]})))
        th.start()
        self.assertTrue(at.wait(30))
        self.assertTrue(self.delete(a)["ok"])
        go.set()
        th.join(60)
        self.assertFalse(th.is_alive())
        rr = pool_residue(self.gate.client(T, "verify"), T, a, [hid], texts=["vellum"])
        self.assertEqual((rr["indices"], rr["docs"], rr["text"]), (0, 0, 0), (res, rr))

    def test_f9_a_release_that_crashes_in_another_process_leaves_no_declassification_without_its_event(self):
        a = self.pool("alpha", ops={"query", "save", "review"})
        hid = self.held(a, "held quire canary")
        tok = self.human_token(a)
        shown = self.tools.hold_show(tok, {"pool": a, "hold": hid})
        req = {"pool": a, "hold": hid, "decision": "admit", "content_hash": shown["content_hash"]}
        pool = self.dir.get(T, a)
        Child(self).run(op="pool_release", pool={"id": a, "cls": "base", "label": pool.label}, who="alice", human=True,
                        scope=["query", "save", "review"], principals=[["user-alice", ["base"], ["base"]]],
                        grants=[["user-alice", ["query", "save", "review"]]], request=req, crash="queued")
        admits = [e for e in self.events(a) if e.get("event") == "hold_admit"]
        if self.leaves(a):                                                  # the release wrote the leaf before the crash
            self.assertEqual(len(admits), 1, "a declassified leaf exists with no approval event")
        out = self.tools.release(tok, req)                                  # the retry finishes it
        self.assertTrue(out.get("ok"), out)
        self.assertEqual(self.leaves(a), ["held quire canary"])
        self.assertEqual(len([e for e in self.events(a) if e.get("event") == "hold_admit"]), 1)


if __name__ == "__main__":
    unittest.main()
