"""Harness-side corpus pieces shared by the in-memory path (tests, S0-style gates) and the Elasticsearch path.

`session_leaf_texts` is the one place a session becomes leaves: the real segmenter, then the leaf text the real writer
stores. `MemCorpus` holds the same leaves in memory (stub embedder, code-built titles) so the arms can be checked without a
cluster; `MemDirect` is a hybrid BM25 + cosine + RRF searcher over them, a stand-in for the Elasticsearch direct search
whose semantics it mirrors (window 50, k 60). The Elasticsearch path uses `living_context.recall.direct.DirectSearch`."""
from __future__ import annotations

import math
from collections import Counter
from typing import Dict, List, Mapping, Sequence, Tuple

from living_context.embed import stub as embed_stub
from living_context.ingest import segment, title
from living_context.recall.fusion import K_RRF, WINDOW, rrf
from living_context.structure.build import LeafRec

from .arms import World
from .dataset import opaque_ref, turns_hash

Turns = Sequence[Tuple[str, str]]


def session_leaf_texts(turns: Turns, cap_tokens: int = 1536) -> List[str]:
    """Leaves of one session: the writer's segmenter at the leaf cap, each rendered as `role: content` lines."""
    try:
        leaves, _ = segment.segment([[segment.Turn(r, c) for r, c in turns]], cap_tokens)
    except segment.Empty:
        return []
    return [segment.render(l) for l in leaves]


class MemCorpus:
    def __init__(self, sessions: Sequence[Tuple[str, Turns]], *, cap_tokens: int = 1536):
        """sessions: (content hash, turns), distinct contents (a repeated content is skipped, as the writer's tenant does)."""
        self.leaves: List[LeafRec] = []
        leaf_session: Dict[str, str] = {}
        tokens: Dict[str, int] = {}
        seen = set()
        for h, turns in sessions:
            if h in seen:
                continue
            seen.add(h)
            for text in session_leaf_texts(turns, cap_tokens):
                lid = f"{len(self.leaves) + 1:032x}"
                size = segment.tokens_of(text)
                self.leaves.append(LeafRec(lid, 1, text, title.build_title(text), embed_stub.embed(text), size))
                leaf_session[lid] = h
                tokens[h] = tokens.get(h, 0) + size
        self.world = World(leaf_session, tokens)


class MemDirect:
    """BM25 (k1 1.2, b 0.75) over leaf TEXT only and cosine over the stub embedding, fused by RRF (k 60, window 50).
    The Elasticsearch leaf search also matches the title and entities fields (code-built, top terms of the leaf), which this
    stand-in leaves out on purpose: the planted-advantage text fixture is built on that difference and does not transfer to
    Elasticsearch (tests/security/test_eval_tenant_es.py runs the real stack and says what holds there)."""

    def __init__(self, leaves: Sequence[LeafRec], k1: float = 1.2, b: float = 0.75):
        self._leaves, self._k1, self._b = list(leaves), k1, b
        self._tf = [Counter(embed_stub.tokens(l.text)) for l in self._leaves]
        self._len = [sum(t.values()) for t in self._tf]
        self._avg = (sum(self._len) / len(self._len)) if self._len else 1.0
        df: Counter = Counter()
        for t in self._tf:
            df.update(t.keys())
        n = len(self._leaves)
        self._idf = {w: math.log(1 + (n - c + 0.5) / (c + 0.5)) for w, c in df.items()}

    def __call__(self, query: str) -> List[Tuple[str, float]]:
        q = set(embed_stub.tokens(query))
        bm = []
        for i, tf in enumerate(self._tf):
            s = 0.0
            for w in q:
                f = tf.get(w, 0)
                if f:
                    s += self._idf[w] * f * (self._k1 + 1) / (f + self._k1 * (1 - self._b + self._b * self._len[i] / self._avg))
            bm.append((s, self._leaves[i].leaf_id))
        bm = [x for x in sorted(bm, key=lambda x: (-x[0], x[1])) if x[0] > 0][:WINDOW]
        qv = embed_stub.embed(query)
        kn = sorted(((embed_stub.cosine(qv, l.embedding), l.leaf_id) for l in self._leaves), key=lambda x: (-x[0], x[1]))[:WINDOW]
        return [(i, float(s)) for i, s in rrf([[i for _, i in bm], [i for _, i in kn]], K_RRF, WINDOW)]


def opaque_for(question_sessions: Sequence[Tuple[str, Turns]]) -> Mapping[str, str]:
    """content hash -> model-facing opaque ref, for the sessions of one question."""
    return {turns_hash(t): opaque_ref(turns_hash(t)) for _, t in question_sessions}
