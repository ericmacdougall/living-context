"""BUILD-PLAN Step 15, the composition root (`gateway/boot.py`): what a gateway restart brings back, on a real Elasticsearch with
security (needs LC_TEST_ES_URL, LC_TEST_CA, LC_TEST_PW; the module FAILS without them).

A revoke (epoch journal), a grant (authz index) and an erased identifier (tombstone journal, E3) are made, the gateway stops
cleanly, and a second start-up from the same directories must hold all three. Broken variant: a start-up that reads nothing back
(`broken_skip_load`) loses the grant and lets an erased identifier be ingested again."""
import os
import tempfile
import unittest
from pathlib import Path

from living_context.gateway import epoch_journal
from living_context.gateway.boot import offboard_tenant, start_tenant
from living_context.gateway.es import EsClient, EsError
from living_context.gateway.fence import SendFence
from living_context.gateway.handler import EsGate
from living_context.gateway.provision import provision_tenant
from living_context.gateway.revocation import OPEN
from living_context.gateway.sessions import EpochTable
from living_context.keystore.store import Keystore
from living_context.policy import Principal, Role

URL, CA, PW = (os.environ.get(k) for k in ("LC_TEST_ES_URL", "LC_TEST_CA", "LC_TEST_PW"))
T = "bootr"


def reset(admin):
    st, rows = admin.request("GET", "/_cat/indices/lc-*?format=json&h=index")
    for row in rows if st == 200 and isinstance(rows, list) else []:
        if row["index"].startswith(f"lc-{T}-") or row["index"] == "lc-sys-registry":
            admin.request("DELETE", "/" + row["index"])


class Boot(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not (URL and CA and PW):
            raise RuntimeError("LC_TEST_ES_URL, LC_TEST_CA and LC_TEST_PW must point at a scratch cluster")
        cls.tmp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.tmp.name)
        cls.admin = EsClient(URL, CA, PW, scheme="basic", user="elastic")
        reset(cls.admin)
        r = provision_tenant(cls.admin, T, ["base"], key_dir=cls.root / "keys", data_root=cls.root / "data", journal_size=65536)
        cls.keys = r["keys"]
        cls.ks = Keystore(cls.root / "ks.db", T)

    @classmethod
    def tearDownClass(cls):
        reset(cls.admin)
        cls.tmp.cleanup()

    def setUp(self):
        d = Path(tempfile.mkdtemp(dir=self.root))
        self.dirs = dict(epoch_dir=d / "epochs", tombstone_paths=(d / "tomb" / "t.log", d / "tomb-replica" / "t.log"),
                         holds_path=d / "holds" / "h.json")
        epoch_journal.provision(self.dirs["epoch_dir"], 16384)

    def start(self, **kw):
        self.epochs = EpochTable(lambda t: SendFence())
        self.gate = EsGate(URL, CA, {T: self.keys})
        rt = start_tenant(T, gate=self.gate, epochs=self.epochs, keystore=self.ks, **self.dirs, **kw)
        self.addCleanup(lambda: rt.revocation._j and rt.revocation._j.close())
        return rt

    def check_restart(self, **kw):
        rt = self.start()
        self.assertEqual(rt.revocation.state, OPEN)
        reader = Principal(T, "tok-reader", frozenset({Role.READER}), frozenset({"base"}), frozenset(), None)
        rt.grants.put(reader)
        self.assertEqual(rt.grants.persist_failures, 0, "the grant was not persisted to the authz index")
        rt.revocation.revoke("tok-gone")
        email = "restart.person@example.com"
        subject = rt.guard._t.identifier(email)
        rt.tombstones.append({"subject": subject, "leaf_ids": [], "redact": [], "erased_ids": [subject], "erased_bare": []})
        rt.revocation.clean_stop()
        # the gateway restarts: new memory, the same directories and the same index
        rt2 = self.start(**kw)
        self.assertEqual(rt2.revocation.state, OPEN)
        self.assertTrue(self.epochs.is_revoked(T, "tok-gone"), "a revoke did not survive the restart")
        self.assertEqual(rt2.grants.get(T, "tok-reader"), reader, "a grant did not survive the restart")
        self.assertTrue(rt2.guard.token_erased(subject), "an erased identifier is accepted again after the restart")

    def test_a_revoke_a_grant_and_an_erased_identifier_survive_a_gateway_restart(self):
        self.check_restart()

    def test_broken_start_up_that_reads_nothing_back_is_red(self):
        with self.assertRaises(AssertionError) as cm:
            self.check_restart(broken_skip_load=True)
        self.assertRegex(str(cm.exception), "grant did not survive|accepted again")

    def test_offboarding_drops_every_gateway_client_of_the_tenant(self):
        gate = EsGate(URL, CA, {T: self.keys})
        st, _ = gate.client(T, "authz").request("GET", f"/lc-{T}-authz/_count")
        self.assertEqual(st, 200)
        self.assertEqual(offboard_tenant(gate, T), 0)
        with self.assertRaises(EsError):
            gate.client(T, "authz")


if __name__ == "__main__":
    unittest.main()
