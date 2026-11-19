"""BUILD-PLAN Step 15, probe P14 follow-up: a revoke is effective in memory and durable in the journal while the tenant's authz
index refuses writes (`read_only_allow_delete`, the block a full disk puts on it: writes answer 429, reads work). Needs
LC_TEST_ES_URL, LC_TEST_CA, LC_TEST_PW. Broken control: a revoke that needs the Elasticsearch write first is not effective.

Scope: the manual block (same refusal as the node's own flood-stage block, probe P14); the watermark-induced block is not
re-run here."""
import os
import tempfile
import unittest
from pathlib import Path

from living_context.gateway import epoch_journal
from living_context.gateway.es import EsClient
from living_context.gateway.fence import SendFence
from living_context.gateway.handler import EsGate
from living_context.gateway.provision import provision_tenant
from living_context.gateway.revocation import RevocationService
from living_context.gateway.sessions import EpochTable

URL, CA, PW = (os.environ.get(k) for k in ("LC_TEST_ES_URL", "LC_TEST_CA", "LC_TEST_PW"))
T = "rvbl"


def reset(admin):
    st, rows = admin.request("GET", "/_cat/indices/lc-*?format=json&h=index")
    for row in rows if st == 200 and isinstance(rows, list) else []:
        if row["index"].startswith(f"lc-{T}-") or row["index"] == "lc-sys-registry":
            admin.request("DELETE", "/" + row["index"])


class RevokeUnderWriteBlock(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not (URL and CA and PW):
            raise RuntimeError("LC_TEST_ES_URL, LC_TEST_CA and LC_TEST_PW must point at a scratch cluster")
        cls.tmp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.tmp.name)
        cls.admin = EsClient(URL, CA, PW, scheme="basic", user="elastic")
        reset(cls.admin)
        r = provision_tenant(cls.admin, T, ["base"], key_dir=cls.root / "keys", data_root=cls.root / "data", journal_size=65536)
        cls.gate = EsGate(URL, CA, {T: r["keys"]})
        cls.jdir = cls.root / "data" / T if (cls.root / "data" / T).exists() else cls.root / "data"

    @classmethod
    def tearDownClass(cls):
        reset(cls.admin)
        cls.tmp.cleanup()

    def block(self, on: bool):
        st, r = self.admin.request("PUT", f"/lc-{T}-authz/_settings", {"index.blocks.read_only_allow_delete": on or None})
        self.assertEqual(st, 200, r)

    def persist(self, principal, epoch):
        st, r = self.gate.client(T, "authz").request("PUT", f"/lc-{T}-authz/_doc/ep:{principal}",
                                                     {"tenant": T, "cls": "tenantwide", "kind": "counter", "epoch": epoch})
        if st not in (200, 201):
            raise RuntimeError(f"authz write refused ({st})")

    def service(self, **kw):
        d = Path(tempfile.mkdtemp(dir=self.root))
        epoch_journal.provision(d, 16384)
        self.epochs = EpochTable(lambda t: SendFence())
        s = RevocationService(T, self.epochs, d, persist=self.persist, **kw)
        s.start()
        self.addCleanup(lambda: s._j and s._j.close())
        return s

    def check_effective(self, s):
        self.block(True)
        try:
            st, _ = self.gate.client(T, "authz").request("PUT", f"/lc-{T}-authz/_doc/probe", {"x": 1})
            self.assertEqual(st, 429, "the block is not in force")
            try:
                rc = s.revoke("p1")
            except RuntimeError:
                self.fail("the revoke needed the Elasticsearch write and was not effective under the block")
        finally:
            self.block(False)
        self.assertTrue(self.epochs.is_revoked(T, "p1"))
        self.assertIsNotNone(rc.durable_at)
        self.assertIs(rc.persisted, False)
        self.assertEqual(s.persist_failures, 1)
        self.persist("p1", rc.epoch)   # after the block lifts the best-effort write succeeds

    def test_revoke_is_effective_and_durable_while_the_authz_index_refuses_writes(self):
        self.check_effective(self.service())

    def test_broken_revoke_that_needs_the_elasticsearch_write_is_red(self):
        with self.assertRaises(AssertionError) as cm:
            self.check_effective(self.service(broken_require_persist=True))
        self.assertIn("needed the Elasticsearch write", str(cm.exception))


if __name__ == "__main__":
    unittest.main()
