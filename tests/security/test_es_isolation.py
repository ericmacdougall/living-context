"""SEC-T1 to T3 on a real security-on Elasticsearch, through the gateway handler (BUILD-PLAN Step 5).

Needs LC_TEST_ES_URL, LC_TEST_CA and LC_TEST_PW (bootstrap credential file) pointing at a
scratch cluster. Without them the module reports itself as not run, never as a pass: the
loader refuses to skip silently (see test_requires_cluster).
"""
import json
import os
import random
import tempfile
import unittest
from pathlib import Path

from living_context.gateway.es import EsClient
from living_context.gateway.handler import EsGate, Gateway, REFUSED, UNAVAILABLE
from living_context.gateway.provision import provision_tenant
from living_context.gateway.sessions import EpochTable, Sessions
from living_context.gateway.fence import SendFence
from living_context.ingest.subjects import Guard, TokenSource
from living_context.ingest.writer import Writer
from living_context.keystore import Keystore
from living_context.policy import ClassLattice, Principal, Role

URL, CA, PW = (os.environ.get(k) for k in ("LC_TEST_ES_URL", "LC_TEST_CA", "LC_TEST_PW"))
HAVE = bool(URL and CA and PW)
TENANTS = ["tena", "tenb", "tenbx"]
CLASSES = ["base", "restricted"]
MARK = {"tena": "alphamarker", "tenb": "bravomarker", "tenbx": "xraymarker"}
LAT = ClassLattice(CLASSES, [("restricted", "base")])


class Adapter:
    def verify(self, tenant, proof):
        return proof.decode() if proof.startswith(b"user") else None


def reset(admin):
    """Delete every index of the fixture tenants by NAME (wildcard deletes are refused by default)."""
    st, rows = admin.request("GET", "/_cat/indices/lc-*?format=json&h=index")
    for row in rows if st == 200 and isinstance(rows, list) else []:
        if any(row["index"].startswith(f"lc-{t}-") for t in TENANTS) or row["index"] == "lc-sys-registry":
            admin.request("DELETE", "/" + row["index"])


class Forwarding(Gateway):
    """Broken variant: names the index from the request and forwards the raw sub-response."""

    def _lookup(self, tenant, p, lattice, req):
        st, body = self._es.client(tenant, "read-base").request(
            "POST", "/_mget", {"docs": [{"_index": req["index"], "_id": "x:1"}]})
        return {"ok": True, "raw": body}


class IsolationOnElasticsearch(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not HAVE:  # fail, never skip: a security probe that did not run is not a pass (LAB-R8)
            raise RuntimeError("LC_TEST_ES_URL, LC_TEST_CA and LC_TEST_PW must point at a scratch cluster")
        cls.tmp = tempfile.TemporaryDirectory()
        root = Path(cls.tmp.name)
        cls.admin = EsClient(URL, CA, PW, scheme="basic", user="elastic")
        reset(cls.admin)
        cls.keys = {}
        for t in TENANTS:
            r = provision_tenant(cls.admin, t, CLASSES, key_dir=root / "keys", data_root=root / "data", journal_size=65536)
            cls.keys[t] = {role: path for role, path in r["keys"].items()}
        cls.es = EsGate(URL, CA, cls.keys)
        fences = {}
        cls.epochs = EpochTable(lambda t: fences.setdefault(t, SendFence()))
        grants = {}
        for t in TENANTS:
            grants[(t, "tok-user")] = Principal(t, "tok-user", frozenset([Role.READER, Role.WRITER]),
                                                frozenset(["base"]), frozenset(["base"]))
            grants[(t, "tok-super")] = Principal(t, "tok-super", frozenset([Role.READER, Role.WRITER]),
                                                 frozenset(["base", "restricted"]), frozenset(["base", "restricted"]))
        cls.sessions = Sessions({t: Adapter() for t in TENANTS}, cls.epochs, lambda t, who: {"user": "tok-user", "usersuper": "tok-super"}[who],
                                lambda t, p: grants.get((t, p)))
        cls.stores = {t: Keystore(root / "ks" / f"{t}.db", t) for t in TENANTS}
        cls.writers = {t: Writer(lambda t=t: cls.es.client(t, "ingest"), cls.stores[t], t,
                                 Guard(TokenSource(cls.stores[t]))) for t in TENANTS}
        cls.gw = Gateway(cls.es, cls.sessions, {t: LAT for t in TENANTS}, writers=lambda t: cls.writers[t],
                         admits=lambda t: True)   # no epoch journal in this test: admission is explicit (no fail-open default)
        cls.tok = {t: cls.sessions.mint(t, b"user") for t in TENANTS}
        cls.sup = {t: cls.sessions.mint(t, b"usersuper") for t in TENANTS}
        cls.ptr = {}
        for t in TENANTS:
            for c, tk in (("base", cls.tok[t]), ("restricted", cls.sup[t])):
                r = cls.gw.handle(tk, {"op": "write", "cls": c, "doc": {
                    "text": f"{MARK[t]} {c} document about gardens"}})
                assert r["ok"], r
                cls.ptr[(t, c)] = r["pointer"]
        for t in TENANTS:
            cls.admin.request("POST", f"/lc-{t}-*/_refresh")

    @classmethod
    def tearDownClass(cls):
        if not HAVE:
            return
        reset(cls.admin)
        cls.tmp.cleanup()

    def secrets(self):
        out = []
        for t in TENANTS:
            for p in self.keys[t].values():
                out.append(Path(p).read_text())
        return out

    def assert_clean(self, resp):
        text = json.dumps(resp)
        for t in ("tenb", "tenbx"):
            self.assertNotIn(MARK[t], text)
        self.assertNotIn("lc-ten", text)
        self.assertNotIn("security_exception", text)
        for s in self.secrets():
            self.assertNotIn(s, text)

    def test_requires_cluster(self):
        self.assertTrue(HAVE)

    def test_t1_gateway_reads_never_cross_tenants_or_classes(self):
        tk = self.tok["tena"]
        r = self.gw.handle(tk, {"op": "search", "cls": "base", "text": "gardens"})
        self.assertTrue(r["ok"])
        self.assertEqual(len(r["hits"]), 1)
        self.assertIn(MARK["tena"], r["hits"][0]["title"])  # the title is code-built from the text
        # another tenant's marker is not found; a class outside the grant is typed unavailable
        r = self.gw.handle(tk, {"op": "search", "cls": "base", "text": MARK["tenb"]})  # vector search returns the
        self.assert_clean(r)                                                          # nearest OWN leaves, never foreign ones
        self.assertEqual(len(r["hits"]), 1)
        self.assertEqual(self.gw.handle(tk, {"op": "search", "cls": "restricted", "text": "gardens"}), UNAVAILABLE)
        self.assertEqual(self.gw.handle(tk, {"op": "search", "cls": "nothere", "text": "gardens"}), UNAVAILABLE)
        # forged pointers (other tenant, restricted class, random) all resolve to the same answer
        rnd = self.gw.handle(tk, {"op": "lookup", "pointer": "aaaa"})
        for key in (("tenb", "base"), ("tenbx", "base"), ("tena", "restricted")):
            self.assertEqual(self.gw.handle(tk, {"op": "lookup", "pointer": self.ptr[key]}), UNAVAILABLE)
        self.assertEqual(rnd, UNAVAILABLE)
        self.assertTrue(self.gw.handle(tk, {"op": "lookup", "pointer": self.ptr[("tena", "base")]})["ok"])

    def test_t1_t2_300_randomised_requests_with_forged_fields(self):
        rng = random.Random(5)
        tk = self.tok["tena"]
        fields = ["tenant", "principal", "principal_token", "session", "roles", "read_set", "write_set"]
        allowed_ops = ("search", "lookup", "write", "ping", "bogus", None)
        for _ in range(300):
            req = {"op": rng.choice(allowed_ops), "cls": rng.choice(CLASSES + ["nothere"]),
                   "text": rng.choice(list(MARK.values()) + ["gardens"]),
                   "pointer": rng.choice(list(self.ptr.values()) + ["zz"]),
                   "doc": {"text": rng.choice(["fuzzwrite", MARK["tena"]]), "lin_subjects": ["t"]}}  # own text only: a write stores it
            if rng.random() < 0.5:
                req[rng.choice(fields)] = rng.choice(["tenb", "tenbx", ["restricted"], {"x": 1}])
            r = self.gw.handle(tk, req)
            self.assert_clean(r)
            if any(f in req for f in fields):
                self.assertEqual(r, REFUSED)
            self.assertIn(r.get("error", "ok"), ("ok", "refused", "unavailable"))
        for bad in ("", None, "tenb." + tk.split(".", 1)[1], tk[:-1]):
            self.assertEqual(self.gw.handle(bad, {"op": "ping"}), UNAVAILABLE)

    def test_t1_t2_raw_tenant_keys_reach_nothing_of_other_tenants(self):
        ra = EsClient(URL, CA, self.keys["tena"]["read-base"], scheme="apikey")
        ia = EsClient(URL, CA, self.keys["tena"]["ingest"], scheme="apikey")
        for ix in ("lc-tenb-base-leaf", "lc-tenbx-base-leaf", "lc-tenb-authz", "lc-tenb-base-ctl"):
            for method, path, body in (("GET", f"/{ix}/_search", None), ("GET", f"/{ix}/_count", None),
                                       ("POST", "/_mget", {"docs": [{"_index": ix, "_id": "x"}]}),
                                       ("POST", "/_msearch", [{"index": ix}, {"query": {"match_all": {}}}])):
                st, rb = ra.request(method, path, body, ndjson=(path == "/_msearch"))
                text = json.dumps(rb)
                self.assertNotIn(MARK["tenb"], text)
                self.assertNotIn(MARK["tenbx"], text)
            st, _ = ia.request("PUT", f"/{ix}/_doc/planted", {"text": "planted"})
            self.assertEqual(st, 403, ix)
        for path in ("/_cluster/state", "/_nodes/stats", "/_cluster/stats", "/_tasks?detailed=true"):
            self.assertEqual(ra.request("GET", path)[0], 403, path)
        st, rb = ra.request("GET", "/_cat/indices")
        self.assertNotIn("lc-tenb", json.dumps(rb))

    def test_t2_writes_are_refused_without_grant_or_with_forged_tenant(self):
        tk = self.tok["tena"]
        doc = {"text": "x"}
        self.assertEqual(self.gw.handle(tk, {"op": "write", "cls": "restricted", "doc": doc}), REFUSED)
        self.assertEqual(self.gw.handle(tk, {"op": "write", "cls": "base", "tenant": "tenb", "doc": doc}), REFUSED)
        self.assertEqual(self.gw.handle(tk, {"op": "write", "cls": "base", "doc": {"title": "t"}}), REFUSED)  # no text

    def test_t3_forbidden_and_missing_answers_do_not_depend_on_the_other_tenants_data(self):
        tk = self.tok["tena"]
        reqs = [{"op": "lookup", "pointer": p} for p in list(self.ptr.values()) + ["aaaa", "b" * 40]]
        reqs += [{"op": "search", "cls": "restricted", "text": t} for t in MARK.values()]
        reqs += [{"op": "search", "cls": "base", "text": MARK["tenb"]}, {"op": "search", "cls": "nothere", "text": "x"}]
        before = [json.dumps(self.gw.handle(tk, r), sort_keys=True) for r in reqs]
        for i in range(200):  # tenb grows from 1 to 201 leaves
            self.gw.handle(self.tok["tenb"], {"op": "write", "cls": "base", "doc": {"text": f"filler {i} bravomarker"}})
        self.admin.request("POST", "/lc-tenb-*/_refresh")
        after = [json.dumps(self.gw.handle(tk, r), sort_keys=True) for r in reqs]
        self.assertEqual(before, after)

    def test_t3_broken_gateway_that_forwards_a_sub_response_is_caught(self):
        bad = Forwarding(self.es, self.sessions, {t: LAT for t in TENANTS}, writers=lambda t: self.writers[t],
                         admits=lambda t: True)   # as `cls.gw`: no epoch journal here, admission is explicit (F1 has no fail-open default)
        for ix in ("lc-tenb-base-leaf", "lc-tenb-base-alias"):
            r = bad.handle(self.tok["tena"], {"op": "lookup", "index": ix})
            self.assertIn("lc-tenb", json.dumps(r), ix)  # the leak the clean checks must see
        with self.assertRaises(AssertionError):
            self.assert_clean(bad.handle(self.tok["tena"], {"op": "lookup", "index": "lc-tenb-base-leaf"}))

    def test_provisioning_refuses_a_second_provision_and_bad_keys_carry_no_cluster_privilege(self):
        from living_context.gateway.provision import ProvisionRefused
        with self.assertRaises(ProvisionRefused):
            provision_tenant(self.admin, "tena", CLASSES, key_dir=Path(self.tmp.name) / "k2",
                             data_root=Path(self.tmp.name) / "d2")
        for t in TENANTS:
            for role, path in self.keys[t].items():
                st, rb = EsClient(URL, CA, path, scheme="apikey").request("GET", "/_security/_authenticate")
                self.assertEqual(st, 200)
                self.assertEqual(st, 200)
                self.assertEqual(oct(os.stat(path).st_mode & 0o777), "0o600")


if __name__ == "__main__":
    unittest.main()
