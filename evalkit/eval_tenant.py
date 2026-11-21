"""One evaluation tenant on a real Elasticsearch: the data path of the H1 slice (EXPERIMENT 3.1; BUILD-PLAN Steps 6, 7, 9).

Sessions are segmented by the real segmenter and written leaf by leaf through the real `Writer` (code-built titles, stub
embedding, source log, heads). The write path receives, per leaf, the text and ONE opaque session reference; nothing else of the
dataset record exists on this side (`dataset.Question` carries no other field). The harness keeps the leaf to session-content-hash
map itself and never sends it to a model. The tree is built by the real builder, published as a generation, and read through
the pinned `EsView`; the direct list is `flat.FlatSearch` (the `DirectSearch` request shape; the BM25 field set is a parameter).

Credentials are passed by PATH only (the files are read by the Elasticsearch client, mode 0600 enforced). Nothing is printed."""
from __future__ import annotations

import threading
import time
from pathlib import Path
import numpy as np
from typing import Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from living_context.config import Constants
from living_context.embed import stub as embed_stub
from living_context.gateway.es import EsClient
from living_context.gateway.handler import EsGate
from living_context.gateway.indices import TENANT_CLS, tenant_index
from living_context.gateway.provision import provision_tenant
from living_context.ingest import segment, title
from living_context.ingest.subjects import Guard, TokenSource
from living_context.ingest.writer import CAS_TRIES, Writer, WriterError
from living_context.keystore import Keystore
from living_context.policy import ClassLattice, Principal, Role
from living_context.recall.view import EsView
from living_context.structure import build
from living_context.structure.build import LeafRec
from living_context.structure.publish import Publisher

from . import parallel as par
from .arms import World
from .flat import A0_FIELDS_DEFAULT, FlatSearch
from .corpus import session_leaf_texts
from .dataset import opaque_ref

CLS = "base"
LATTICE = ClassLattice([CLS], [])


class OrderedWriter(Writer):
    """The real `Writer` for a concurrent ingest: the tenant's `src_seq` range for the whole ingest is reserved by ONE compare-and-set
    of the same counter `next_seq` advances, and each leaf takes the sequence number of its position in the serial order, so the
    leaves carry exactly the numbers a one-at-a-time ingest would give them (the leaf order of `load_leaves` on a resume follows).
    Everything else is the inherited write path."""
    def __init__(self, *args, **kw):
        super().__init__(*args, **kw)
        self._local = threading.local()

    def reserve(self, n: int) -> int:
        """Advance the counter by `n`; returns the value before (the leaves get value+1 .. value+n)."""
        idx = tenant_index(self.tenant, "ctl")
        for _ in range(CAS_TRIES):
            cur = self._get(idx, "seq")
            if cur is None:
                st, _ = self._put(idx, "seq", self._base("counter", TENANT_CLS, value=n), cls=None, create=True)
                if st in (200, 201):
                    return 0
                continue
            v = int(cur["_source"]["value"])
            st, _ = self._put(idx, "seq", self._base("counter", TENANT_CLS, value=v + n), cls=None,
                              cas=(cur["_seq_no"], cur["_primary_term"]))
            if st in (200, 201):
                return v
            time.sleep(0.005)
        raise WriterError("sequence counter contended")

    def remember_at(self, seq: int, *args, **kw):
        self._local.seq = seq
        try:
            return self.remember(*args, **kw)
        finally:
            self._local.seq = None

    def next_seq(self) -> int:
        seq = getattr(self._local, "seq", None)
        if seq is None:
            raise WriterError("a concurrent ingest write without its reserved sequence number")
        self._local.seq = None          # one number per write
        return seq


def reset_tenant(admin: EsClient, tenant: str, *, registry_doc_only: bool = False) -> None:
    """Deletes the tenant's indices and the tenant registry. `registry_doc_only` (S6-H1, on an Elasticsearch shared with other stages'
    tenants) deletes only this tenant's registry document instead of the whole registry index, which holds the other tenants' rows."""
    st, rows = admin.request("GET", "/_cat/indices/lc-*?format=json&h=index")
    for row in rows if st == 200 and isinstance(rows, list) else []:
        if row["index"].startswith(f"lc-{tenant}-") or (row["index"] == "lc-sys-registry" and not registry_doc_only):
            admin.request("DELETE", "/" + row["index"])
    if registry_doc_only:
        admin.request("DELETE", f"/lc-sys-registry/_doc/{tenant}?refresh=true")


class EvalTenant:
    def __init__(self, es_url: str, ca_path: str, admin_credential_path: str, tenant: str, root: Path, *,
                 consts: Optional[Constants] = None, embed: Callable[[str], list] = embed_stub.embed, reset: bool = True,
                 query_embed: Optional[Callable[[str], list]] = None, a0_fields: Sequence[str] = A0_FIELDS_DEFAULT,
                 attach: bool = False, registry_doc_only: bool = False):
        """`embed` embeds leaf text (the passage side); `query_embed` embeds a query (the query side; the hashed stub has one
        function for both, so it defaults to `embed`). `a0_fields` is the field set of the flat BM25 list (flat.py). `attach` re-opens a
        tenant this process (or an earlier one, same root) already built: nothing is reset or provisioned, the key files and the
        keystore under `root` are reused and `load_state` restores the harness-side maps."""
        self.tenant, self.root, self.consts, self._embed = tenant, Path(root), consts or Constants(), embed
        self._query_embed, self.a0_fields = query_embed or embed, tuple(a0_fields)
        self._last_vec: Optional[Tuple[str, list]] = None
        self.admin = EsClient(es_url, ca_path, admin_credential_path, scheme="basic", user="elastic")
        if attach:
            keys = {p.stem: str(p) for p in sorted((self.root / "keys" / tenant).glob("*.key"))}
            if not keys:
                raise RuntimeError("nothing to attach to: no key files under the tenant root")
        else:
            if reset:
                reset_tenant(self.admin, tenant, registry_doc_only=registry_doc_only)
            keys = provision_tenant(self.admin, tenant, [CLS], key_dir=self.root / "keys", data_root=self.root / "data",
                                    journal_size=65536)["keys"]
        self.gate = EsGate(es_url, ca_path, {tenant: keys})
        self.ks = Keystore(self.root / "ks" / f"{tenant}.db", tenant)
        self.writer = Writer(lambda: self.gate.client(tenant, "ingest"), self.ks, tenant, Guard(TokenSource(self.ks)),
                             consts=self.consts, embed=self._emb)
        self.leaves: List[LeafRec] = []
        self.leaf_session: Dict[str, str] = {}
        self.session_tokens: Dict[str, int] = {}
        self._seen: set = set()
        self.max_seq = 0
        self.principal = Principal(tenant, "p-eval", frozenset([Role.READER]), frozenset([CLS]), frozenset())

    def _emb(self, text: str) -> list:
        """The writer's embedder; keeps the vector so the leaf record does not embed the same text a second time."""
        v = self._embed(text)
        self._last_vec = (text, v)
        return v

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
                                           self._vector_of(text), size, (), res.src_seq))
                self.leaf_session[res.leaf_id] = h
                self.session_tokens[h] = self.session_tokens.get(h, 0) + size
                self.max_seq = max(self.max_seq, res.src_seq)
                wrote += 1
        self.admin.request("POST", f"/lc-{self.tenant}-{CLS}-leaf/_refresh")
        return wrote

    def ingest_concurrent(self, sessions: Sequence[Tuple[str, Sequence[Tuple[str, str]]]], *, writers: int,
                          embed_many: Callable[[List[str]], Mapping[str, Sequence[float]]]) -> int:
        """`ingest` with the passage vectors computed up front by `embed_many` (texts in serial order; for instance embed_pool.passages
        over worker processes) and the leaves written by `writers` threads through an `OrderedWriter`. The leaves, their order, their
        `src_seq`, the session map and the sizes are those `ingest` produces (leaf ids are random either way); a vector is whatever
        `embed_many` returned for the text. A refused write raises, as in `ingest`, after the writes before it in serial order."""
        plan: List[Tuple[str, str]] = []
        for h, turns in sessions:
            if h in self._seen:
                continue
            self._seen.add(h)
            plan += [(h, text) for text in session_leaf_texts(turns, self.consts.leaf_cap)]
        vecs = embed_many([t for _, t in plan]) if plan else {}
        missing = [t for _, t in plan if t not in vecs]
        if missing:
            raise RuntimeError("the up-front embedding returned no vector for %d leaf texts" % len(missing))
        w = OrderedWriter(lambda: self.gate.client(self.tenant, "ingest"), self.ks, self.tenant, Guard(TokenSource(self.ks)),
                          consts=self.consts, embed=lambda text: list(vecs[text]))
        base = w.reserve(len(plan)) if plan else 0
        ref_of = {h: {"session": opaque_ref(h)} for h, _ in plan}

        def work(i):
            h, text = plan[i]
            return w.remember_at(base + 1 + i, "eval", CLS, text, source_ref=ref_of[h], refresh=False)

        def commit(i, _item, res):
            h, text = plan[i]
            if not res.ok:
                raise RuntimeError("the write path refused a leaf: %s" % res.status)
            size = segment.tokens_of(text, self.consts.chars_per_token_bound)
            self.leaves.append(LeafRec(res.leaf_id, res.lv, text, title.build_title(text, n=self.consts.title_terms),
                                       list(vecs[text]), size, (), res.src_seq))
            self.leaf_session[res.leaf_id] = h
            self.session_tokens[h] = self.session_tokens.get(h, 0) + size
            self.max_seq = max(self.max_seq, res.src_seq)
        par.run_ordered(list(range(len(plan))), work, commit, workers=max(1, writers))
        self.admin.request("POST", f"/lc-{self.tenant}-{CLS}-leaf/_refresh")
        return len(plan)

    def _vector_of(self, text: str) -> list:
        if self._last_vec is not None and self._last_vec[0] == text:
            return self._last_vec[1]
        return self._embed(text)

    def save_state(self, path: Path) -> None:
        """The harness-side maps (leaf to session content hash, session sizes, last source sequence) so a resumed run can attach to
        the built tenant instead of ingesting and building again."""
        import json
        Path(path).write_text(json.dumps({"leaf_session": self.leaf_session, "session_tokens": self.session_tokens, "max_seq": self.max_seq,
                                          "leaves": len(self.leaves)}, sort_keys=True))

    def load_state(self, path: Path) -> int:
        import json
        d = json.loads(Path(path).read_text())
        self.leaf_session, self.session_tokens, self.max_seq = d["leaf_session"], d["session_tokens"], d["max_seq"]
        self._seen = set(self.session_tokens)
        return int(d["leaves"])

    def load_leaves(self, expected: int) -> int:
        """Rebuilds `self.leaves` (the tree builder's input) from the leaf index of an ingested tenant, in write order (the source
        sequence). Text, title and size are what ingest held; a vector is the float32 the index stores, widened to float64: identical to
        the one ingest held when that was float32-valued (a leaf of one embedding window), and rounded to float32 (relative error about
        6e-8) for a leaf embedded in several windows, whose mean ingest held in float64. Used by a
        resumed run whose ingest finished but whose build never started."""
        out: List[LeafRec] = []
        after = None
        while True:
            body = {"size": 500, "sort": [{"src_seq": "asc"}], "query": {"match_all": {}},
                    "_source": ["text", "title", "size_tokens", "embedding", "src_seq"]}
            if after is not None:
                body["search_after"] = after
            st, r = self.admin.request("POST", f"/lc-{self.tenant}-{CLS}-leaf/_search", body)
            if st != 200:
                raise RuntimeError("could not read the leaves back (status %s)" % st)
            hits = r["hits"]["hits"]
            if not hits:
                break
            for h in hits:
                s = h["_source"]
                lid, lv = h["_id"].rsplit(":", 1)
                if lid not in self.leaf_session:
                    raise RuntimeError("a leaf in the index is not in the saved state")
                vec = np.asarray(s["embedding"], dtype=np.float32).astype(np.float64).tolist()     # the index keeps float32
                out.append(LeafRec(lid, int(lv), s["text"], s["title"], vec, int(s["size_tokens"]), (), int(s["src_seq"])))
            after = hits[-1]["sort"]
        if len(out) != expected or len({l.leaf_id for l in out}) != expected:
            raise RuntimeError("the leaf index holds %d leaves, the saved state %d" % (len(out), expected))
        self.leaves = out
        return len(out)

    @property
    def world(self) -> World:
        return World(dict(self.leaf_session), dict(self.session_tokens))

    def leaf_count_in_es(self) -> int:
        st, r = self.admin.request("POST", f"/lc-{self.tenant}-{CLS}-leaf/_count", {"query": {"match_all": {}}})
        return int(r["count"]) if st == 200 else -1

    # ---- structure ----------------------------------------------------------------------------------------------
    def build_and_publish(self, port, **build_kw) -> build.Tree:
        """`build_kw` reaches `build_tree` (`pmap`, `port_for`, `idgen`: concurrent group calls with a per-group journal scope)."""
        tree = build.build_tree(self.leaves, port, self.tenant, CLS, consts=self.consts, **build_kw)
        pub = Publisher(lambda: self.gate.client(self.tenant, "structure"), self.tenant, CLS)
        gen = pub.publish(tree, watermark=self.max_seq)
        if gen is None:
            raise RuntimeError("publication lost its compare-and-set")
        self.admin.request("POST", f"/lc-{self.tenant}-{CLS}-*/_refresh")
        return tree

    def view(self) -> EsView:
        return EsView(self.gate.client(self.tenant, f"read-{CLS}"), self.tenant, CLS, "topic")

    # ---- the direct list (A0) -------------------------------------------------------------------------------------
    def direct(self, window: int = 50, fields: Optional[Sequence[str]] = None) -> Callable[[str], List[Tuple[str, float]]]:
        ds = FlatSearch(lambda c: self.gate.client(self.tenant, f"read-{c}"), self.tenant, embed=self._query_embed,
                        fields=tuple(fields) if fields else self.a0_fields)

        def run(query: str) -> List[Tuple[str, float]]:
            hits = ds.search(self.principal, LATTICE, query, size=window, classes=[CLS])
            return [(h.doc_id.rsplit(":", 1)[0], h.score) for h in hits]
        return run

    def close(self) -> None:
        self.ks.close()
