"""A second process that rebuilds one tenant's pool runtime over the parent test's cluster, keystore and files, runs ONE operation and
dies with `os._exit` at a named step (a crash: no `finally`, no in-memory state survives). The parent then restarts from what is
durable and checks the invariant. Used by tests/security/test_pools_delta_es.py (classes C3 and C4 of the pools delta review).

Usage: python -m tests.faults.pools_child <config.json>. The config names the cluster key files, keystore, journals, pointer keys, the
pool and grant rows, the operation and the step to crash at. The cluster's URL, CA and password come from the same environment as the
parent's (LC_TEST_ES_URL, LC_TEST_CA, LC_TEST_PW)."""
import json
import os
import sys
from pathlib import Path

CRASHED = 77


def _crash_at(step):
    def pause(name):
        if name == step:
            sys.stdout.flush()
            os._exit(CRASHED)
    return pause


def main(path: str) -> int:
    cfg = json.loads(Path(path).read_text())
    from living_context.config import Constants
    from living_context.erasure.pool_scope import PoolEraser
    from living_context.gateway import pointers
    from living_context.gateway.admin import GrantStore
    from living_context.gateway.class_reader import ClassReader
    from living_context.gateway.es import EsClient
    from living_context.gateway.fence import SendFence
    from living_context.gateway.gate import WriteGate
    from living_context.gateway.handler import EsGate
    from living_context.gateway.journal import TombstoneJournal
    from living_context.gateway.pool_tools import PoolTools
    from living_context.gateway.sessions import EpochTable, Sessions
    from living_context.ingest.subjects import Guard, TokenSource
    from living_context.ingest.writer import Writer
    from living_context.keystore import Keystore
    from living_context.policy import ClassLattice, Principal, Role
    from living_context.policy import pools as pl

    url, ca, pw = (os.environ[k] for k in ("LC_TEST_ES_URL", "LC_TEST_CA", "LC_TEST_PW"))
    t = cfg["tenant"]
    gate = EsGate(url, ca, {t: cfg["keys"]})
    ks = Keystore(cfg["keystore"], t)
    wgate = WriteGate()
    crash = _crash_at(cfg["crash"])
    if cfg["op"] == "erase":
        admin = EsClient(url, ca, pw, scheme="basic", user="elastic")
        er = PoolEraser(lambda: gate.client(t, "erase"), lambda name: admin.request("DELETE", f"/{name}"), t, ks,
                        TombstoneJournal(Path(cfg["tj"][0]), Path(cfg["tj"][1])), wgate, purge_deadline_s=5, purge_interval_s=1,
                        pause=crash)
        er.delete(cfg["cls"], cfg["pool"]["id"])
        return 0
    lat = {t: ClassLattice(["base", "restricted"], [("restricted", "base")])}
    directory = pl.PoolDirectory()
    p = cfg["pool"]
    directory.restore(pl.Pool(p["id"], t, p["cls"], p.get("kind", "team"), p["label"], "", bool(p.get("private")), pl.ACTIVE, 0))
    store = GrantStore()
    for who, read, write in cfg["principals"]:
        store.restore(Principal(t, f"pt-{who}", frozenset({Role.READER, Role.WRITER}), frozenset(read), frozenset(write)))
    for who, ops in cfg["grants"]:
        directory.grant(t, f"pt-{who}", p["id"], frozenset(ops))

    class Adapter:
        human_presence = True

        def verify(self, tenant, proof):
            return proof.decode() if proof.startswith(b"user") else None

    epochs = EpochTable(lambda _t: SendFence())
    sessions = Sessions({t: Adapter()}, epochs, lambda _t, w: f"pt-{w}", store.get, consts=Constants(tok_live_max=100, mint_rate_per_h=1000),
                        directory=directory)
    reader = ClassReader(lambda c: gate.client(t, f"read-{c}"), t)
    writer = Writer(lambda: gate.client(t, "ingest"), ks, t, Guard(TokenSource(ks)), reader=reader, gate=wgate, pause=crash)
    tools = PoolTools(t, directory, sessions, lat, writer, reader, lambda c: gate.client(t, f"read-{c}"),
                      pointers.PointerKeys(Path(cfg["pointer_keys"])), consts=Constants(tok_live_max=100, mint_rate_per_h=1000))
    tok = sessions.mint_scoped(t, f"user-{cfg['who']}".encode(), conversation=cfg.get("conversation", "child"),
                               scope={p["id"]: frozenset(cfg["scope"])}, human=bool(cfg.get("human")))
    out = tools.handle(cfg["op"], None, None, cfg["request"], token=tok)
    print(json.dumps(out, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))
