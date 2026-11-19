"""One evaluation tenant on a real Elasticsearch: the data path of the H1 slice (EXPERIMENT 3.1; BUILD-PLAN Steps 6, 7, 9).

Sessions are segmented by the real segmenter and written leaf by leaf through the real `Writer` (code-built titles, stub
embedding, source log, heads). The write path receives, per leaf, the text and ONE opaque session reference; nothing else of the
dataset record exists on this side (`dataset.Question` carries no other field). The harness keeps the leaf to session-content-hash
map itself and never sends it to a model. The tree is built by the real builder, published as a generation, and read through
the pinned `EsView`; the direct list is the real `DirectSearch` over the leaf index.

Credentials are passed by PATH only (the files are read by the Elasticsearch client, mode 0600 enforced). Nothing is printed."""
from __future__ import annotations

from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from living_context.config import Constants
from living_context.embed import stub as embed_stub
from living_context.gateway.es import EsClient
from living_context.gateway.handler import EsGate
from living_context.gateway.provision import provision_tenant
from living_context.ingest import segment, title
from living_context.ingest.subjects import Guard, TokenSource
from living_context.ingest.writer import Writer
from living_context.keystore import Keystore
from living_context.policy import ClassLattice, Principal, Role
from living_context.recall.direct import DirectSearch
from living_context.recall.view import EsView
from living_context.structure import build
from living_context.structure.build import LeafRec
from living_context.structure.publish import Publisher

from .arms import World
from .corpus import session_leaf_texts
from .dataset import opaque_ref

CLS = "base"
LATTICE = ClassLattice([CLS], [])


def reset_tenant(admin: EsClient, tenant: str) -> None:
    st, rows = admin.request("GET", "/_cat/indices/lc-*?format=json&h=index")
    for row in rows if st == 200 and isinstance(rows, list) else []:
        if row["index"].startswith(f"lc-{tenant}-") or row["index"] == "lc-sys-registry":
            admin.request("DELETE", "/" + row["index"])


class EvalTenant:
    def __init__(self, es_url: str, ca_path: str, admin_credential_path: str, tenant: str, root: Path, *,
                 consts: Optional[Constants] = None, embed: Callable[[str], list] = embed_stub.embed, reset: bool = True):
        self.tenant, self.root, self.consts, self._embed = tenant, Path(root), consts or Constants(), embed
        self.admin = EsClient(es_url, ca_path, admin_credential_path, scheme="basic", user="elastic")
        if reset:
            reset_tenant(self.admin, tenant)
        keys = provision_tenant(self.admin, tenant, [CLS], key_dir=self.root / "keys", data_root=self.root / "data",
                                journal_size=65536)["keys"]
        self.gate = EsGate(es_url, ca_path, {tenant: keys})
        self.ks = Keystore(self.root / "ks" / f"{tenant}.db", tenant)
        self.writer = Writer(lambda: self.gate.client(tenant, "ingest"), self.ks, tenant, Guard(TokenSource(self.ks)),
                             consts=self.consts, embed=embed)
        self.leaves: List[LeafRec] = []
        self.leaf_session: Dict[str, str] = {}
        self.session_tokens: Dict[str, int] = {}
        self._seen: set = set()
        self.max_seq = 0
        self.principal = Principal(tenant, "p-eval", frozenset([Role.READER]), frozenset([CLS]), frozenset())

    # ---- ingest -------------------------------------------------------------------------------------------------
    def ingest(self, sessions: Sequence[Tuple[str, Sequence[Tuple[str, str]]]]) -> int:
        """(content hash, turns) per session; a content already in the tenant is skipped. Returns leaves written."""
        wrote = 0
        for h, turns in sessions:
            if h in self._seen:
                continue
            self._seen.add(h)
            ref = {"session": opaque_ref(h)}
            for text in session_leaf_texts(turns, self.consts.leaf_cap):
                res = self.writer.remember("eval", CLS, text, source_ref=ref, refresh=False)
                if not res.ok:
                    raise RuntimeError("the write path refused a leaf: %s" % res.status)
                size = segment.tokens_of(text, self.consts.chars_per_token_bound)
                self.leaves.append(LeafRec(res.leaf_id, res.lv, text, title.build_title(text, n=self.consts.title_terms),
                                           self._embed(text), size, (), res.src_seq))
                self.leaf_session[res.leaf_id] = h
                self.session_tokens[h] = self.session_tokens.get(h, 0) + size
                self.max_seq = max(self.max_seq, res.src_seq)
                wrote += 1
        self.admin.request("POST", f"/lc-{self.tenant}-{CLS}-leaf/_refresh")
        return wrote

    @property
    def world(self) -> World:
        return World(dict(self.leaf_session), dict(self.session_tokens))

    def leaf_count_in_es(self) -> int:
        st, r = self.admin.request("POST", f"/lc-{self.tenant}-{CLS}-leaf/_count", {"query": {"match_all": {}}})
        return int(r["count"]) if st == 200 else -1

    # ---- structure ----------------------------------------------------------------------------------------------
    def build_and_publish(self, port) -> build.Tree:
        tree = build.build_tree(self.leaves, port, self.tenant, CLS, consts=self.consts)
        pub = Publisher(lambda: self.gate.client(self.tenant, "structure"), self.tenant, CLS)
        gen = pub.publish(tree, watermark=self.max_seq)
        if gen is None:
            raise RuntimeError("publication lost its compare-and-set")
        self.admin.request("POST", f"/lc-{self.tenant}-{CLS}-*/_refresh")
        return tree

    def view(self) -> EsView:
        return EsView(self.gate.client(self.tenant, f"read-{CLS}"), self.tenant, CLS, "topic")

    # ---- the direct list (A0) -------------------------------------------------------------------------------------
    def direct(self, window: int = 50) -> Callable[[str], List[Tuple[str, float]]]:
        ds = DirectSearch(lambda c: self.gate.client(self.tenant, f"read-{c}"), self.tenant, embed=self._embed)

        def run(query: str) -> List[Tuple[str, float]]:
            hits = ds.search(self.principal, LATTICE, query, size=window, classes=[CLS])
            return [(h.doc_id.rsplit(":", 1)[0], h.score) for h in hits]
        return run

    def close(self) -> None:
        self.ks.close()
