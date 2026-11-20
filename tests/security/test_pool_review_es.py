"""Fixes for the independent review of the pools foundation (findings M1 and following), each shown red first on the unfixed code and
then green. Needs LC_TEST_ES_URL, LC_TEST_CA and LC_TEST_PW; without them the module FAILS (it never skips)."""
import base64
import threading
import time
import unittest

from living_context.gateway import pool_boot
from living_context.gateway.indices import class_index, pool_index
from living_context.gateway.pool_tools import PoolTools
from living_context.ingest.writer import Writer
from living_context.ingest.subjects import Guard, TokenSource
from tests.security.test_pools_es import Base, FailingModel, T


class Repair(Base):
    def test_m1_repair_reprojects_a_pool_leaf_into_its_own_index_only(self):
        a, b = self.pool("alpha"), self.pool("beta")
        tok = self.mint("alice", {a: {"query", "save"}, b: {"query", "save"}})
        ptrs = {}
        for pid in (a, b):
            r = self.tools.save(tok, {"pool": pid, "text": f"repair canary {pid[:4]} secret", "key": f"k{pid}"})
            self.assertTrue(r["ok"], r)
            ptrs[pid] = r
        self.refresh()
        # a crash between source and projection: the pool leaf document of A is gone, its source stays
        st, hits = self.admin.request("POST", f"/{pool_index(T, 'base', a, 'leaf')}/_search", {"query": {"match_all": {}}, "_source": False})
        for h in hits["hits"]["hits"]:
            self.admin.request("DELETE", f"/{pool_index(T, 'base', a, 'leaf')}/_doc/{h['_id']}?refresh=true")
        out = self.writer.repair(["base"])
        self.refresh()
        # nothing of any pool is in a class-level index or in the other pool's index
        for idx in (class_index(T, "base", "leaf"), class_index(T, "base", "ctl"), pool_index(T, "base", b, "leaf")):
            st, r = self.admin.request("POST", f"/{idx}/_search", {"size": 50, "query": {"match_all": {}}})
            blob = str(r)
            self.assertNotIn("repair canary " + a[:4], blob, idx)
            if idx != pool_index(T, "base", b, "leaf"):
                self.assertNotIn("repair canary " + b[:4], blob, idx)
        st, r = self.admin.request("POST", f"/{class_index(T, 'base', 'ctl')}/_search", {"size": 50, "query": {"term": {"kind": "dirty"}}})
        self.assertEqual(r["hits"]["total"]["value"], 0)                      # no placement job for a pool leaf
        st, r = self.admin.request("POST", f"/{pool_index(T, 'base', a, 'leaf')}/_search", {"size": 5, "query": {"match_all": {}}})
        self.assertEqual(r["hits"]["total"]["value"], 1)                      # the repair put it back where it belongs
        self.assertEqual(r["hits"]["hits"][0]["_source"]["pool"], a)
        self.assertGreaterEqual(out["reprojected"], 1)
        f = self.tools.fetch(tok, {"pointer": ptrs[a]["pointer"]})
        self.assertEqual(f["status"], "ok")


class Update(Base):
    def paused_tools(self, step):
        """A writer that holds the first thread that reaches `step` for a moment, so a second caller overlaps it."""
        seen = threading.Event()

        def pause(name):
            if name == step and not seen.is_set():
                seen.set()
                time.sleep(0.6)
        self.writer = Writer(lambda: self.cc("ingest"), self.ks, T, Guard(TokenSource(self.ks)), reader=self.reader, gate=self.wgate, pause=pause)
        self.tools = PoolTools(T, self.dir, self.sessions, __import__("tests.security.test_pools_es", fromlist=["x"]).LAT, self.writer, self.reader,
                               lambda cls: self.cc(f"read-{cls}"), self.keys, consts=self.consts, model=FailingModel())

    def test_m4_two_concurrent_corrects_one_wins_one_conflicts_and_source_equals_projection(self):
        a = self.pool("alpha", ops={"query", "save", "update"})
        tok = self.mint("alice", {a: {"query", "save", "update"}})
        self.paused_tools("source")
        first = self.tools.save(tok, {"pool": a, "text": "original text", "key": "k0"})
        self.assertTrue(first["ok"], first)
        self.paused_tools("source")                                    # re-arm the pause for the two updates
        out = []

        def go(text, key):
            out.append(self.tools.update(tok, {"pointer": first["pointer"], "mode": "correct", "text": text, "key": key}))
        ts = [threading.Thread(target=go, args=(f"edit number {i}", f"u{i}")) for i in range(2)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        self.assertEqual(sorted(bool(o.get("ok")) for o in out), [False, True], out)
        self.assertEqual([o.get("error") for o in out if not o.get("ok")], ["version_conflict"])
        self.refresh()
        won = next(o for o in out if o.get("ok"))
        f = self.tools.fetch(tok, {"pointer": won["pointer"]})
        self.assertEqual(f["status"], "ok")
        st, r = self.admin.request("POST", f"/lc-{T}-src/_search", {"size": 10, "query": {"term": {"pool": a}}})
        rows = [h["_source"] for h in r["hits"]["hits"] if int(h["_source"]["lv"]) == 2]
        self.assertEqual(len(rows), 1)
        leaf_id = rows[0]["leaf_id"]
        text = self.ks.decrypt(leaf_id, 2, base64.b64decode(rows[0]["ct"]), base64.b64decode(rows[0]["nonce"])).decode()
        self.assertIn(text, f["text"])                                  # the stored source and the projection are the same text

    def test_l4_a_retried_correct_after_a_crash_keeps_the_key_of_the_live_version(self):
        a = self.pool("alpha", ops={"query", "save", "update"})
        tok = self.mint("alice", {a: {"query", "save", "update"}})
        n = [0]

        def pause(name):
            if name == "key_made":
                n[0] += 1
                if n[0] == 2:
                    raise RuntimeError("crash after the key of version 2 was made")
        self.writer = Writer(lambda: self.cc("ingest"), self.ks, T, Guard(TokenSource(self.ks)), reader=self.reader, gate=self.wgate, pause=pause)
        self.tools = PoolTools(T, self.dir, self.sessions, __import__("tests.security.test_pools_es", fromlist=["x"]).LAT, self.writer, self.reader,
                               lambda cls: self.cc(f"read-{cls}"), self.keys, consts=self.consts, model=FailingModel())
        first = self.tools.save(tok, {"pool": a, "text": "live version one text", "key": "k0"})
        with self.assertRaises(RuntimeError):
            self.tools.update(tok, {"pointer": first["pointer"], "mode": "correct", "text": "second text", "key": "u1"})
        r = self.tools.update(tok, {"pointer": first["pointer"], "mode": "correct", "text": "second text", "key": "u2"})
        self.assertTrue(r["ok"], r)
        self.refresh()
        st, hits = self.admin.request("POST", f"/lc-{T}-src/_search", {"size": 10, "query": {"term": {"pool": a}}})
        rows = {int(h["_source"]["lv"]): h["_source"] for h in hits["hits"]["hits"]}
        for lv in (1, 2):                                                  # every version still opens under its own key
            text = self.ks.decrypt(rows[lv]["leaf_id"], lv, base64.b64decode(rows[lv]["ct"]), base64.b64decode(rows[lv]["nonce"])).decode()
            self.assertIn("text", text)

    def test_m4_a_supersede_that_loses_the_head_leaves_no_live_duplicate(self):
        a = self.pool("alpha", ops={"query", "save", "update"})
        tok = self.mint("alice", {a: {"query", "save", "update"}})
        first = self.tools.save(tok, {"pool": a, "text": "plan alpha stays", "key": "k0"})
        real_commit, real_ht = self.writer.leaf_commit, self.writer.head_transition
        calls = []

        def lose(cls, pool, leaf, opid, changes):
            if changes.get("status") == "superseded":
                return False                                           # the head moved under the caller
            return real_commit(cls, pool, leaf, opid, changes)

        def transition(cls, pool, leaf, expect, changes, drop=()):
            calls.append(changes.get("status"))
            return real_ht(cls, pool, leaf, expect, changes, drop)
        self.writer.leaf_commit, self.writer.head_transition = lose, transition
        r = self.tools.update(tok, {"pointer": first["pointer"], "mode": "supersede", "text": "plan beta replaces", "key": "u1"})
        self.assertEqual(r.get("error"), "version_conflict")
        self.refresh()
        q = self.tools.query(tok, {"query": "plan beta replaces"})
        self.assertNotIn("beta", str(q["items"]))                       # the new leaf is not retrievable
        self.assertIn("aborted", calls)


class FailingLeafRead:
    """A client whose reads of a pool leaf document answer 503 (a node under load)."""

    def __init__(self, inner):
        self.inner = inner

    def request(self, method, path, body=None, **kw):
        if method == "GET" and "-leaf" in path and "/_doc/" in path:
            return 503, {}
        return self.inner.request(method, path, body, **kw)


class Retraction(Base):
    def setUp(self):
        super().setUp()
        self.consts = __import__("living_context.config", fromlist=["x"]).Constants(tok_live_max=100, mint_rate_per_h=1000)
        self.build()

    def test_m9_a_failed_leaf_read_never_lets_an_agent_retract_a_human_memory_directly(self):
        a = self.pool("alpha", ops={"query", "save", "retract"})
        human = self.mint("alice", {a: {"query", "save", "retract"}}, human=True)
        agent = self.mint("alice", {a: {"query", "save", "retract"}})
        h = self.tools.save(human, {"pool": a, "text": "a human wrote this tapir fact", "key": "h1"})["pointer"]
        good = self.tools.es_for
        self.tools.es_for = lambda cls: FailingLeafRead(good(cls))
        r = self.tools.retract(agent, {"pointers": [h], "reason": "wrong", "key": "k1"})
        self.assertNotEqual(r.get("results"), ["retracted"], r)
        self.tools.es_for = good
        self.refresh()
        self.assertEqual([i["pointer"] for i in self.tools.query(agent, {"query": "tapir fact"})["items"]], [h])   # still live

    def test_m6_subject_erasure_after_a_retraction_completes_and_leaves_no_retraction_row(self):
        from living_context.erasure.executor import Executor
        from living_context.erasure.hold import HoldRegistry
        from living_context.gateway.fence import SendFence
        a = self.pool("alpha", ops={"query", "save", "retract"})
        t = self.mint("alice", {a: {"query", "save", "retract"}})
        email = f"yan{Base.n}@example.com"
        p = self.tools.save(t, {"pool": a, "text": f"met {email} about the zinfandel plan", "key": "e1"})["pointer"]
        self.assertTrue(self.tools.retract(t, {"pointers": [p], "reason": "wrong", "key": "k1"})["ok"])
        self.refresh()
        self.assertEqual(len(self.reader.search("base", "ctl", a, [{"term": {"kind": "retraction"}}])), 1)
        ex = Executor(lambda: self.gate.client(T, "erase"), lambda: self.gate.client(T, "structure"), T, self.ks, self.journal,
                      self.wgate, SendFence(), self.state, holds=HoldRegistry(self.root / f"h{Base.n}"), purge_deadline_s=30,
                      purge_interval_s=1, es_log=lambda: self.gate.client(T, "log"))
        sub = self.ks.subject_token(email)
        r = ex.erase_subject(sub, identifiers=[email])
        self.refresh()
        self.assertEqual(self.reader.count("base", "ctl", a, [{"term": {"kind": "retraction"}}]), 0)
        from living_context.erasure.receipt import full_receipt, recount
        from living_context.gateway import pointers
        leaf = pointers.open_(self.keys, T, p).id
        residue = recount(self.admin, T, sub, [leaf])
        self.assertEqual(residue, 0)                                    # no retraction row names the actor or the leaf any more
        self.assertTrue(full_receipt(r, residue=residue, verifier_steps=[("probe", 1)])["done"])


class DeleteRace(Base):
    def setUp(self):
        super().setUp()
        self.consts = __import__("living_context.config", fromlist=["x"]).Constants(tok_live_max=100, mint_rate_per_h=1000)
        self.build()
        from living_context.policy import Principal, Role
        self.store.restore(Principal(T, "pt-user-admin2", frozenset({Role.TENANT_ADMIN}), frozenset({"base"}), frozenset({"base"})))

    def admins(self):
        out = []
        for who in ("admin", "admin2"):
            rec = self.sessions.resolve(self.sessions.mint(T, b"user-" + who.encode()))
            out.append((self.sessions.principal(rec), rec))
        return out

    def delete(self, pool):
        (p1, r1), (p2, r2) = self.admins()
        self.svc.handle("pool_delete", p1, r1, {"pool": pool})
        return self.svc.handle("pool_delete", p2, r2, {"pool": pool})

    def test_m5_a_save_authorized_before_the_delete_cannot_commit_after_it(self):
        from living_context.verifier.check import pool_residue
        a = self.pool("alpha")
        tok = self.mint("alice", {a: {"query", "save"}})
        real = self.writer.remember
        started, release = threading.Event(), threading.Event()

        def slow(*args, **kw):
            started.set()
            release.wait(10)
            return real(*args, **kw)
        self.writer.remember = slow
        res = []
        th = threading.Thread(target=lambda: res.append(self.tools.save(tok, {"pool": a, "text": "racing canary fjordlight", "key": "k"})))
        th.start()
        self.assertTrue(started.wait(10))                                # the save is authorized and waits just before its commit
        out = self.delete(a)
        self.assertTrue(out["ok"], out)
        release.set()
        th.join()
        self.assertFalse(res[0].get("ok"), res)                          # the commit saw the pool going away and refused
        st, r = self.admin.request("POST", f"/lc-{T}-src/_search", {"query": {"term": {"pool": a}}})
        self.assertEqual(r["hits"]["total"]["value"], 0)                 # no source document names the pool
        v = self.gate.client(T, "verify")
        r = pool_residue(v, T, a, [], texts=["fjordlight"])
        self.assertEqual((r["indices"], r["docs"], r["text"]), (0, 0, 0), r)

    def test_m5_a_hold_or_retraction_after_the_delete_leaves_nothing(self):
        a = self.pool("alpha", ops={"query", "save", "retract"})
        tok = self.mint("alice", {a: {"query", "save", "retract"}})
        ptr = self.tools.save(tok, {"pool": a, "text": "doomed note one", "key": "k"})["pointer"]
        pool = self.dir.get(T, a)
        self.assertTrue(self.delete(a)["ok"])
        from living_context.ingest.writer import WriterError
        with self.assertRaises(WriterError):
            self.writer.record_retraction("base", a, "x" * 32, 1, "pt-user-alice", "agent", "wrong", "done")
        with self.assertRaises(WriterError):
            self.writer.hold_tainted("pt-user-alice", "base", a, [a], "late held text", {"mode": "save"})
        self.assertFalse(self.writer.update_head("base", a, "x" * 32, 1, status="retracted"))
        self.refresh()
        st, r = self.admin.request("POST", f"/lc-{T}-*/_search?ignore_unavailable=true", {"query": {"bool": {"should": [
            {"term": {"pool": a}}, {"term": {"lin_pools": a}}]}}})
        self.assertEqual(r["hits"]["total"]["value"], 0, r)

    def test_m5_a_crash_at_each_step_of_a_delete_is_resumable(self):
        from living_context.verifier.check import pool_residue
        for step in ("closure", "boundary", "docs", "keys", "indices"):
            a = self.pool("alpha" + step)
            tok = self.mint("alice", {a: {"query", "save"}})
            r = self.tools.save(tok, {"pool": a, "text": f"crash canary {step} quillmoss", "key": "k"})
            leaf = __import__("living_context.gateway.pointers", fromlist=["x"]).open_(self.keys, T, r["pointer"]).id

            def boom(name, step=step):
                if name == step:
                    raise RuntimeError("crash at " + step)
            self.svc._eraser = lambda cls, pool, boom=boom: self.eraser(pause=boom).delete(cls, pool)
            with self.assertRaises(RuntimeError):
                self.delete(a)
            st, _ = self.admin.request("GET", f"/lc-{T}-authz/_doc/pp:{a}")
            self.assertEqual(st, 200, step)                              # the pool's own record is still there to resume from
            self.assertEqual(self.dir.get(T, a).state, "deleting")
            self.svc._eraser = lambda cls, pool: self.eraser().delete(cls, pool)
            out = self.delete(a)
            self.assertTrue(out["ok"], (step, out))
            v = self.gate.client(T, "verify")
            rr = pool_residue(v, T, a, [leaf], texts=["quillmoss"])
            self.assertEqual((rr["indices"], rr["docs"], rr["text"]), (0, 0, 0), (step, rr))
            self.assertFalse(self.ks.has_key(leaf, 1), step)
            st, _ = self.admin.request("GET", f"/lc-{T}-authz/_doc/pp:{a}")
            self.assertEqual(st, 404, step)


class Boot(Base):
    def authz(self):
        return self.gate.client(T, "authz")

    def plant_tokens(self, n, exp):
        import json
        lines = []
        for i in range(n):
            h = f"{i:064x}"
            rec = {"tenant": T, "principal": f"pt-user-plant{i % 7}", "actor": "agent", "epoch": 0, "exp": exp, "scope": {}, "conversation": "c"}
            lines += [{"index": {"_index": f"lc-{T}-authz", "_id": f"tk:{h}"}},
                      {"tenant": T, "cls": "tenantwide", "kind": "token", "lin_leaves": [], "lin_leaf_ids": [], "lin_nodes": [],
                                  "lin_subjects": [rec["principal"]], "lin_queries": [], "derivation": "token", "erasure_rewrite": 0,
                                  "schema_v": 1, "created_at": "1970-01-01T00:00:00+00:00", "hash": h, "exp": exp, "rec_json": json.dumps(rec)}]
        for k in range(0, len(lines), 2000):
            st, r = self.admin.request("POST", "/_bulk?refresh=true", lines[k:k + 2000], ndjson=True)
            self.assertEqual(st, 200)
            self.assertFalse(r.get("errors"), str(r)[:300])

    def tearDown(self):
        self.admin.request("POST", f"/lc-{T}-authz/_delete_by_query?refresh=true", {"query": {"term": {"kind": "token"}}})

    def test_m7_the_start_up_read_is_paged_complete_and_a_partial_read_fails(self):
        import time
        self.plant_tokens(10001, time.time() + 3600)
        a = self.pool("alpha")                                           # written after the tokens: the last documents of the index
        self.refresh()
        self.build()                                                     # a fresh gateway state: empty directory and token table
        out = pool_boot.load(self.authz, T, self.dir, self.sessions, self.svc)
        self.assertIn(("pt-user-alice", a), out["persisted"])            # the pool and its grant are not lost behind 10,000 token documents
        self.assertTrue(out["complete"])
        self.assertEqual(out["tokens"], 10001)
        self.assertIsNotNone(self.dir.get(T, a))

        class Flaky:
            n = 0

            def request(inner, method, path, body=None, **kw):
                if method == "POST" and path.endswith("/_search"):
                    Flaky.n += 1
                    if Flaky.n == 2:
                        return 429, {}
                return self.authz().request(method, path, body, **kw)
        self.build()
        with self.assertRaises(RuntimeError):                            # a failed page: the start-up fails, nothing is confirmed from it
            pool_boot.load(lambda: Flaky(), T, self.dir, self.sessions, self.svc, page=3000)

    def test_m7_an_expired_token_document_is_deleted_and_scope_is_not_a_mapping_of_pool_ids(self):
        import time
        self.plant_tokens(5, time.time() - 10)
        a = self.pool("alpha")
        persist = pool_boot.token_persister(self.authz, T)
        persist("f" * 64, {"tenant": T, "principal": "pt-user-alice", "exp": time.time() + 99, "epoch": 0, "scope": {a: ["query"]}})
        self.refresh()
        st, m = self.admin.request("GET", f"/lc-{T}-authz/_mapping")
        self.assertNotIn(a, str(m))                                      # no field per pool id: the scope is one stored string
        self.build()
        out = pool_boot.load(self.authz, T, self.dir, self.sessions, self.svc)
        self.assertEqual((out["expired_deleted"], out["tokens"]), (5, 1), out)
        self.refresh()
        self.assertEqual(self.admin.request("GET", f"/lc-{T}-authz/_count?q=kind:token")[1]["count"], 1)

    def test_m7_a_partial_read_confirms_no_removal(self):
        a = self.pool("alpha")
        self.svc._unconfirmed[("pt-user-alice", a, 1)] = {"t": "grant_removed", "principal": "pt-user-alice", "pool": a, "ops": ["query"], "counter": 1}
        self.svc.confirm_against_authz({}, complete=False)
        self.assertEqual(len(self.svc._unconfirmed), 1)
        self.svc.confirm_against_authz({}, complete=True)
        self.assertEqual(len(self.svc._unconfirmed), 0)


class OldReaders(Base):
    def test_l2_the_class_level_lookup_and_the_quarantine_list_do_not_see_pool_rows(self):
        from living_context.gateway import pointers
        from living_context.recall.lookup import EsLeafStore
        a = self.pool("alpha")
        tok = self.mint("alice", {a: {"query", "save"}})
        r = self.tools.save(tok, {"pool": a, "text": "pool only text lookup canary", "key": "k"})
        leaf = pointers.open_(self.keys, T, r["pointer"]).id
        self.refresh()
        store = EsLeafStore(self.cc("ingest"), T, self.cc("structure"))
        self.assertIsNone(store.leaf(leaf, 1))                          # an unsealed pointer of a pool leaf does not resolve at class level
        self.assertIsNone(store.current_lv(leaf))
        self.writer.hold_tainted("pt-user-alice", "base", a, [a], "held pool text", {"mode": "save"})
        self.refresh()
        self.assertEqual(self.writer.quarantine_list("base"), [])       # a pool's hold is not in the class's quarantine list


class CountingModel:
    """A model port that counts every touch and fails it (the stage 0 contract: no model call of any kind)."""

    def __init__(self):
        self.touched = []

    def call(self, task, inputs, *, ctx):
        self.touched.append(task)
        raise AssertionError(f"the model port was used ({task})")


class StageZero(Base):
    def tools_at(self, stage, model):
        return PoolTools(T, self.dir, self.sessions, __import__("tests.security.test_pools_es", fromlist=["x"]).LAT, self.writer, self.reader,
                         lambda cls: self.cc(f"read-{cls}"), self.keys, consts=self.consts, model=model, stage_of=lambda pid: stage)

    def test_item0b_every_stage_0_operation_runs_with_a_port_that_raises_and_never_touches_it(self):
        a = self.pool("alpha", ops={"query", "save", "update", "retract"})
        model = CountingModel()
        tools = self.tools_at(0, model)                                    # the port is in the object under test
        tok = self.mint("alice", {a: {"query", "save", "update", "retract"}})
        s = tools.save(tok, {"pool": a, "text": "stage zero fact about herons", "key": "k1"})
        self.assertTrue(s["ok"], s)
        self.refresh()
        self.assertEqual(len(tools.query(tok, {"query": "herons"})["items"]), 1)
        self.assertEqual(tools.fetch(tok, {"pointer": s["pointer"]})["status"], "ok")
        u = tools.update(tok, {"pointer": s["pointer"], "mode": "correct", "text": "stage zero fact about egrets", "key": "u1"})
        self.assertTrue(u["ok"], u)
        self.assertTrue(tools.retract(tok, {"pointers": [u["pointer"]], "reason": "wrong", "key": "r1"})["ok"])
        self.assertEqual(model.touched, [])
        # control: the same code with the pool at stage 1 DOES reach the port (so the zero above is a result, not an artefact)
        hot = self.tools_at(1, model)
        for op in (lambda: hot.query(tok, {"query": "herons"}), lambda: hot.save(tok, {"pool": a, "text": "stage one fact", "key": "k2"})):
            with self.assertRaises(AssertionError):
                op()
        self.assertEqual(sorted(model.touched), ["route", "write"])


if __name__ == "__main__":
    unittest.main()
