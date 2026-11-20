"""BUILD-PLAN Step 15, SEC-R6 and tenant-wide revocation, on a real Elasticsearch with security (needs LC_TEST_ES_URL, LC_TEST_CA,
LC_TEST_PW; the module FAILS without them).

The tenant key owner (a native user with `manage_own_api_key` and index privileges on `lc-<t>-*`) creates the tenant's keys at
provisioning, rotates them and invalidates them; the bootstrap credential is used only to create the owner. Broken variants:
  * a rotation that needs the superuser (it reads the users first; the owner is refused);
  * a gateway key with `manage_api_key` (it creates and invalidates keys, so the check that the gateway key cannot goes red);
  * a tenant-wide drop that forgets the clients and lets the next request build a new one from the old key files;
  * T7 (EXPERIMENT 9.1, N=100): every one of 100 invalidated keys fails within 5 s and stays failed, and the gateway's own path
    refuses at once (broken: a gateway that caches an authenticated client keeps handing it out)."""
import concurrent.futures as cf
import os
import stat
import tempfile
import time
import unittest
import uuid
from pathlib import Path

from living_context.gateway.es import EsClient, EsError
from living_context.gateway.handler import EsGate
from living_context.gateway.keyowner import RotationError, invalidate_all, provision_owner, rotate
from living_context.gateway.provision import provision_tenant

URL, CA, PW = (os.environ.get(k) for k in ("LC_TEST_ES_URL", "LC_TEST_CA", "LC_TEST_PW"))
T = "krot"
CLASSES = ["base"]


class KeyOwner(unittest.TestCase):
    @classmethod
    def reset(cls):
        st, rows = cls.admin.request("GET", "/_cat/indices/lc-*?format=json&h=index")
        for row in rows if st == 200 and isinstance(rows, list) else []:
            if row["index"].startswith(f"lc-{T}-") or row["index"] == "lc-sys-registry":
                cls.admin.request("DELETE", "/" + row["index"])
        cls.admin.request("DELETE", "/_security/api_key", {"name": f"lc-{T}-*"})
        cls.admin.request("DELETE", "/_security/api_key", {"name": f"lcbroken-{T}"})

    @classmethod
    def setUpClass(cls):
        if not (URL and CA and PW):
            raise RuntimeError("LC_TEST_ES_URL, LC_TEST_CA and LC_TEST_PW must point at a scratch cluster")
        cls.tmp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.tmp.name)
        cls.admin = EsClient(URL, CA, PW, scheme="basic", user="elastic")
        cls.reset()
        cls.user = provision_owner(cls.admin, T, cls.root / "owner.pw")
        cls.owner = EsClient(URL, CA, str(cls.root / "owner.pw"), scheme="basic", user=cls.user)
        r = provision_tenant(cls.admin, T, CLASSES, key_dir=cls.root / "keys", data_root=cls.root / "data", journal_size=65536,
                             key_owner=cls.owner)
        cls.keys = r["keys"]
        cls.gate = EsGate(URL, CA, {T: r["keys"]})

    @classmethod
    def tearDownClass(cls):
        cls.reset()
        cls.admin.request("DELETE", f"/_security/user/{cls.user}")
        cls.admin.request("DELETE", f"/_security/role/{cls.user}_role")
        cls.tmp.cleanup()

    def setUp(self):
        res = rotate(self.owner, T, CLASSES, self.root / "keys")   # a fresh set of live keys for every test
        self.gate.reload(T, res["keys"])
        self.keys = res["keys"]

    def search(self, client):
        st, _ = client.request("POST", f"/lc-{T}-base-leaf/_search", {"query": {"match_all": {}}})
        return st

    def gone(self, client, tries=50):
        """T7: an invalidated key fails within the fixed bound of 5 s (EXPERIMENT 9.1)."""
        t0 = time.time()
        for _ in range(tries):
            if client.request("GET", "/_security/_authenticate")[0] == 401:   # an invalidated key fails authentication
                self.latest = max(getattr(self, "latest", 0.0), time.time() - t0)
                return True
            time.sleep(0.1)
        return False

    # ---- R6: the owner rotates and invalidates without the bootstrap credential --------------------------------------
    def check_rotation(self, **kw):
        old = {role: EsClient(URL, CA, path) for role, path in self.keys.items()}
        self.assertEqual(self.search(old["read-base"]), 200, "premise: the old key works")
        self.assertTrue(all(c.request("GET", "/_security/_authenticate")[0] == 200 for c in old.values()))
        self.assertEqual(self.owner.request("GET", "/_security/user")[0], 403, "premise: the owner is not a superuser")
        try:
            res = rotate(self.owner, T, CLASSES, self.root / "keys", **kw)
        except RotationError as e:
            self.fail(f"rotation needed more than the key owner's own privileges: {e}")
        self.gate.reload(T, res["keys"])
        self.assertEqual(self.search(self.gate.client(T, "read-base")), 200, "the new key does not work")
        self.assertTrue(all(self.gone(c) for c in old.values()), "an old key still works after the rotation")
        self.assertEqual(len(res["invalidated"]), len(old))
        for p in res["keys"].values():
            self.assertEqual(stat.S_IMODE(os.stat(p).st_mode), 0o600)

    def test_the_key_owner_rotates_and_invalidates_the_tenants_keys_without_the_bootstrap_credential(self):
        self.check_rotation()

    def test_broken_rotation_that_needs_the_superuser_is_red(self):
        with self.assertRaises(AssertionError) as cm:
            self.check_rotation(broken_needs_superuser=True)
        self.assertIn("needed more than the key owner's own privileges", str(cm.exception))

    def test_a_failed_rotation_leaves_the_old_keys_and_files_in_place(self):
        before = {r: Path(p).read_bytes() for r, p in self.keys.items()}

        class Flaky:   # the owner's client, failing the third key creation
            n = 0

            def request(inner, method, path, body=None, **kw):
                if method == "POST" and path == "/_security/api_key":
                    inner.n += 1
                    if inner.n == 3:
                        return 500, {}
                return self.owner.request(method, path, body, **kw)

        with self.assertRaises(RotationError):
            rotate(Flaky(), T, CLASSES, self.root / "keys")
        self.assertEqual(sorted(p.name for p in (self.root / "keys" / T).iterdir()), sorted(Path(p).name for p in self.keys.values()),
                         "a half-written key file was left behind")
        self.assertEqual(before, {r: Path(p).read_bytes() for r, p in self.keys.items()})
        self.assertEqual(self.search(EsClient(URL, CA, self.keys["read-base"])), 200)

    # ---- the gateway's keys carry no cluster privilege -----------------------------------------------------------------------
    def check_cannot_manage_keys(self, clients):
        for name, c in clients.items():
            st, _ = c.request("POST", "/_security/api_key", {"name": f"x-{uuid.uuid4().hex[:6]}"})
            self.assertIn(st, (401, 403), f"the gateway key {name} created an API key")
            st, _ = c.request("DELETE", "/_security/api_key", {"name": f"lc-{T}-*"})
            self.assertIn(st, (401, 403), f"the gateway key {name} invalidated API keys")

    def test_no_gateway_key_can_create_or_invalidate_keys(self):
        self.check_cannot_manage_keys({role: EsClient(URL, CA, path) for role, path in self.keys.items()})

    def test_broken_gateway_key_with_manage_api_key_is_red(self):
        st, k = self.admin.request("POST", "/_security/api_key", {"name": f"lcbroken-{T}", "expiration": "1h", "role_descriptors": {
            "r": {"cluster": ["manage_api_key"], "indices": [{"names": [f"lc-{T}-*"], "privileges": ["read"]}]}}})
        self.assertEqual(st, 200)
        p = self.root / "broken.key"
        p.write_text(k["encoded"])
        os.chmod(p, 0o600)
        with self.assertRaises(AssertionError) as cm:
            self.check_cannot_manage_keys({"broken": EsClient(URL, CA, str(p))})
        self.assertIn("created an API key", str(cm.exception))
        self.admin.request("DELETE", "/_security/api_key", {"ids": [k["id"]]})

    # ---- tenant-wide revocation -----------------------------------------------------------------------------------------------
    def check_drop(self, gate):
        gate.reload(T, self.keys)
        self.assertEqual(self.search(gate.client(T, "read-base")), 200)
        gate.drop_tenant(T)
        try:
            gate.client(T, "read-base")
            reached = True
        except EsError:
            reached = False
        self.assertFalse(reached, "the gateway still reaches the tenant after the drop")
        old = {role: EsClient(URL, CA, path) for role, path in self.keys.items()}
        self.assertEqual(len(invalidate_all(self.owner, T)), len(old))
        self.assertTrue(all(self.gone(c) for c in old.values()), "a key of the offboarded tenant still works")

    def test_a_tenant_wide_drop_stops_every_gateway_path_and_the_owner_invalidates_the_keys(self):
        self.check_drop(self.gate)

    def test_broken_drop_that_only_forgets_the_clients_is_red(self):
        class Lazy(EsGate):
            def drop_tenant(self, tenant):   # the earlier behaviour: forget the cached clients, rebuild on the next request
                for k in [k for k in self._clients if k[0] == tenant]:
                    del self._clients[k]

        with self.assertRaises(AssertionError) as cm:
            self.check_drop(Lazy(URL, CA, {T: self.keys}))
        self.assertIn("still reaches the tenant", str(cm.exception))

    # ---- T7: invalidation within the fixed bound of 5 s, N = 100 keys (EXPERIMENT 9.1) -------------------------------------------
    T7_BOUND_S, T7_N = 5.0, 100

    def check_t7(self, gate):
        """100 keys are created by the tenant's key owner and every one authenticates; the owner invalidates them all, and each key
        is polled (concurrently) from the moment the invalidation returned: the time to its first 401 must be at most 5 s, and it
        must still be refused after the bound. The gateway's own path (`drop_tenant`) refuses at once."""
        paths = []
        for i in range(self.T7_N):
            st, body = self.owner.request("POST", "/_security/api_key", {
                "name": f"lc-{T}-t7-{i}", "expiration": "1h",
                "role_descriptors": {"r": {"indices": [{"names": [f"lc-{T}-*"], "privileges": ["read"]}]}}})
            self.assertEqual(st, 200)
            f = self.root / f"t7-{i}.key"
            f.write_text(body["encoded"])
            os.chmod(f, 0o600)
            paths.append(str(f))
        clients = [EsClient(URL, CA, f) for f in paths]
        with cf.ThreadPoolExecutor(16) as ex:
            self.assertTrue(all(ex.map(lambda c: c.request("GET", "/_security/_authenticate")[0] == 200, clients)),
                            "premise: every key authenticates before the invalidation")
        gate.reload(T, self.keys)
        self.assertEqual(self.search(gate.client(T, "read-base")), 200)
        self.assertGreaterEqual(len(invalidate_all(self.owner, T)), self.T7_N)
        t_inv = time.time()
        gate.drop_tenant(T)
        try:
            gate.client(T, "read-base")
            self.fail("the gateway's own path still hands out a client after the invalidation")
        except EsError:
            pass
        self.assertLess(time.time() - t_inv, 0.5, "the gateway's own path did not refuse at once")

        def time_to_refusal(c):
            while time.time() - t_inv < self.T7_BOUND_S + 1.0:
                if c.request("GET", "/_security/_authenticate")[0] == 401:
                    return time.time() - t_inv
                time.sleep(0.05)
            return float("inf")

        with cf.ThreadPoolExecutor(16) as ex:
            lat = list(ex.map(time_to_refusal, clients))
        self.t7_max = max(lat)
        self.assertLessEqual(self.t7_max, self.T7_BOUND_S, f"a key still worked {self.t7_max:.2f} s after its invalidation (bound 5 s)")
        with cf.ThreadPoolExecutor(16) as ex:
            late = list(ex.map(lambda c: c.request("GET", "/_security/_authenticate")[0], clients))
        self.assertEqual([x for x in late if x != 401], [], "a key worked again after the bound")

    def test_t7_every_invalidated_key_fails_within_five_seconds_and_the_gateway_refuses_at_once(self):
        self.check_t7(self.gate)
        print(f"T7 N={self.T7_N}: slowest key refused after {self.t7_max:.2f} s", flush=True)

    def test_broken_t7_gateway_that_caches_an_authenticated_client_is_red(self):
        class Caching(EsGate):
            def drop_tenant(self, tenant):   # keeps serving the client it already authenticated
                pass

        with self.assertRaises(AssertionError) as cm:
            self.check_t7(Caching(URL, CA, {T: self.keys}))
        self.assertIn("still hands out a client", str(cm.exception))


if __name__ == "__main__":
    unittest.main()
