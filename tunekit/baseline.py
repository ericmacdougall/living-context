"""The plain-retrieval baseline harness (TUNING Z2, Z1 partly): ingest a generated corpus through the product's real write path
(`living_context.ingest.writer.Writer`, real keystore, real validator, real index mappings, real class and tenant indices) into a
security-on Elasticsearch, then run plain hybrid retrieval (BM25 plus kNN over the leaf index, client-side RRF with k 60 and window
50, exactly the shape of `living_context.recall.direct.DirectSearch`) and score it against the generator's ground truth.

Zero model-port calls. The embedder is the pinned local E5 model; its vectors are computed in batch beforehand and handed to the
writer's `embed` callable by text, so the product write path is unchanged and embedding time is reported separately.

The harness issues the `_msearch` itself (same bodies as DirectSearch) because DirectSearch does not expose the two ranked lists
or the kNN scores that the ADAPTIVE 6.2 features need; `check_against_product` replays a sample through DirectSearch and asserts
the fused top-10 agrees, so the harness cannot silently drift from the product's path.
"""
from __future__ import annotations

import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Sequence

import numpy as np

from living_context.gateway.es import EsClient
from living_context.gateway.handler import EsGate
from living_context.gateway.indices import class_index
from living_context.gateway.provision import provision_tenant
from living_context.ingest.subjects import Guard, TokenSource
from living_context.ingest.writer import Writer
from living_context.keystore import Keystore
from living_context.policy import ClassLattice, Principal, Role
from living_context.recall.direct import DirectSearch
from living_context.recall.fusion import K_RRF, WINDOW, rrf

CLS = "base"
LATTICE = ClassLattice([CLS], [])


@dataclass
class Env:
    url: str
    ca: str
    pw_file: str          # a 0600 file holding the bootstrap credential; read by path, never printed
    work: Path            # per-run scratch (keys, keystore); outside the repository

    def admin(self) -> EsClient:
        return EsClient(self.url, self.ca, self.pw_file, scheme="basic", user="elastic")


class Tenant:
    """A provisioned tenant with one class, its keystore and writer. `close` deletes everything it made."""

    def __init__(self, env: Env, tenant: str, embed_by_text: Dict[str, list], refresh_each: bool = False):
        self.env, self.tenant = env, tenant
        self.root = env.work / tenant
        self.admin = env.admin()
        r = provision_tenant(self.admin, tenant, [CLS], key_dir=self.root / "keys", data_root=self.root / "data", journal_size=65536)
        self.gate = EsGate(env.url, env.ca, {tenant: r["keys"]})
        self.ks = Keystore(self.root / "ks.db", tenant)
        self.guard = Guard(TokenSource(self.ks))
        self.writer = Writer(lambda: self.gate.client(tenant, "ingest"), self.ks, tenant, self.guard,
                             embed=lambda text: embed_by_text[text])
        self.leaf_index = class_index(tenant, CLS, "leaf")

    def close(self) -> None:
        st, rows = self.admin.request("GET", "/_cat/indices/lc-%s-*?format=json&h=index" % self.tenant)
        for row in rows if st == 200 and isinstance(rows, list) else []:
            self.admin.request("DELETE", "/" + row["index"])
        self.admin.request("DELETE", f"/lc-sys-registry/_doc/{self.tenant}?refresh=true")
        try:
            self.ks.close()
        finally:
            shutil.rmtree(self.root, ignore_errors=True)


def ingest(t: Tenant, leaves: Sequence[dict]) -> dict:
    """Write every leaf in order through Writer.remember (refresh once at the end). Returns timing and the doc id of each leaf."""
    doc_ids: List[str] = []
    refused = 0
    t0 = time.monotonic()
    for L in leaves:
        res = t.writer.remember("ingestor", CLS, L["text"], source_ref={"session": L["session"]}, refresh=False)
        if not res.ok:
            refused += 1
            doc_ids.append("")
            continue
        doc_ids.append(f"{res.leaf_id}:{res.lv}")
    t1 = time.monotonic()
    t.admin.request("POST", f"/{t.leaf_index}/_refresh")
    t2 = time.monotonic()
    st, stats = t.admin.request("GET", f"/{t.leaf_index}/_stats/store,docs")
    tot = stats["_all"]["primaries"] if st == 200 else {}
    st, shards = t.admin.request("GET", f"/_cat/shards/{t.leaf_index}?format=json&h=index,prirep,store")
    return {"n": len(leaves), "refused": refused, "ingest_s": t1 - t0, "refresh_s": t2 - t1,
            "leaves_per_s": len(leaves) / max(t1 - t0, 1e-9), "docs": tot.get("docs", {}).get("count"),
            "leaf_index_bytes": tot.get("store", {}).get("size_in_bytes"), "doc_ids": doc_ids}


@dataclass
class QueryRow:
    qid: int
    fused: List[str]            # doc ids, full fused list (at most 2 x window)
    bm25: List[str]
    knn: List[str]
    knn_cos: List[float]        # cosine of each kNN hit: ES cosine score is (1 + cos) / 2
    bm25_score: List[float]
    ms: float


def search(client: EsClient, index: str, text: str, qvec: Sequence[float], *, window: int = WINDOW) -> QueryRow:
    """BM25 plus kNN `_msearch` (the DirectSearch bodies), then client-side RRF."""
    lines = [{"index": index}, {"size": window, "_source": False,
                                "query": {"multi_match": {"query": text, "fields": ["text", "title", "entities"]}}},
             {"index": index}, {"size": window, "_source": False,
                                "knn": {"field": "embedding", "query_vector": [float(x) for x in qvec], "k": window,
                                        "num_candidates": max(2 * window, 100)}}]
    t0 = time.monotonic()
    st, body = client.request("POST", "/_msearch", lines, ndjson=True)
    ms = (time.monotonic() - t0) * 1000.0
    if st != 200 or not isinstance(body, dict):
        raise RuntimeError("msearch failed (%s)" % st)
    lists, scores = [], []
    for resp in body["responses"]:
        if "error" in resp:
            raise RuntimeError("sub-search failed")
        hits = resp["hits"]["hits"]
        lists.append([h["_id"] for h in hits])
        scores.append([h["_score"] for h in hits])
    fused = [i for i, _ in rrf(lists, K_RRF, window)]
    return QueryRow(-1, fused, lists[0], lists[1], [2.0 * s - 1.0 for s in scores[1]], scores[0], ms)


def run_queries(t: Tenant, questions: Sequence[dict], qvecs: np.ndarray, *, warmup: int = 5) -> List[QueryRow]:
    client = t.gate.client(t.tenant, "read-" + CLS)
    for i in range(min(warmup, len(questions))):   # not scored: warms caches so latency is steady-state
        search(client, t.leaf_index, questions[i]["text"], qvecs[i])
    rows = []
    for i, q in enumerate(questions):
        r = search(client, t.leaf_index, q["text"], qvecs[i])
        r.qid = q["qid"]
        rows.append(r)
    return rows


def check_against_product(t: Tenant, questions: Sequence[dict], qvecs: np.ndarray, rows: Sequence[QueryRow], *, n: int = 20) -> dict:
    """Replay the first n questions through the product's DirectSearch and compare the fused top-10 with the harness's."""
    vec_by_text = {q["text"]: qvecs[i] for i, q in enumerate(questions)}
    ds = DirectSearch(lambda cls: t.gate.client(t.tenant, "read-" + cls), t.tenant,
                      embed=lambda text: [float(x) for x in vec_by_text[text]])
    p = Principal(t.tenant, "tok", frozenset({Role.READER}), read_set=frozenset({CLS}))
    same = 0
    for i, q in enumerate(list(questions)[:n]):
        got = [h.doc_id for h in ds.search(p, LATTICE, q["text"], size=10)]
        same += got == rows[i].fused[:10]
    return {"checked": min(n, len(questions)), "identical_top10": same}


def score(questions: Sequence[dict], rows: Sequence[QueryRow], doc_ids: Sequence[str]) -> List[dict]:
    """Per-question outcome. gold, stale and sibling leaves are corpus indices; `doc_ids[i]` is leaf i's document id."""
    out = []
    for q, r in zip(questions, rows):
        pos = {d: i + 1 for i, d in enumerate(r.fused)}
        found = [pos[doc_ids[g]] for g in q["gold"] if doc_ids[g] in pos]
        rank = min(found) if found else None     # the best-ranked gold leaf; None: no gold leaf in the fused window
        stale_ranks = [pos[doc_ids[s]] for s in q["stale"] if doc_ids[s] in pos]
        top10 = r.fused[:10]
        out.append({
            "qid": q["qid"], "rank": rank, "hit1": rank == 1, "hit5": rank is not None and rank <= 5,
            "hit10": rank is not None and rank <= 10, "rr": (1.0 / rank) if rank is not None and rank <= 10 else 0.0,
            "bm25_hit10": any(doc_ids[x] in r.bm25[:10] for x in q["gold"]),
            "knn_hit10": any(doc_ids[x] in r.knn[:10] for x in q["gold"]),
            "stale_first": bool(stale_ranks) and (rank is None or min(stale_ranks) < rank),
            "has_stale": bool(q["stale"]), "top10": top10, "ms": r.ms,
        })
    return out
