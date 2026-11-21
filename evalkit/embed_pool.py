"""Passage embeddings for a whole ingest, computed up front in worker processes (the run host has no GPU; one process of the pinned
e5 model uses about six cores well, so a 78,700-leaf pool needs several).

Each worker loads its own pinned `E5Embedder` (checksums verified, offline) with the SAME torch thread count as a single-process run
and embeds one text at a time through `E5Embedder.passage`, so a text's windows are batched exactly as they are in-process: a vector
depends on the text, the model files and the thread count only, never on which worker embedded it or on the other texts (tested
against the in-process embedder on the run host). Results come back in input order. Duplicate texts are embedded once.

`reuse` maps sha256(text) to a vector computed earlier with the same pinned model and settings; a text found there is not embedded
again (the caller decides which earlier vectors are exact; `exact_reuse` is the rule for vectors read back from an index)."""
from __future__ import annotations

import hashlib
import multiprocessing as mp
from typing import Callable, Dict, List, Mapping, Optional, Sequence

_WORKER = None          # the embedder of this worker process


def text_key(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _init(factory: Callable[[], object]) -> None:
    global _WORKER
    _WORKER = factory()


def _embed_one(text: str) -> List[float]:
    return _WORKER.passage(text)


class E5Factory:
    """A picklable factory: the pinned embedder from `model_dir` with `threads` torch threads (one per worker process)."""
    def __init__(self, model_dir: str, threads: int):
        self.model_dir, self.threads = str(model_dir), int(threads)

    def __call__(self):
        from living_context.embed import e5
        return e5.E5Embedder(self.model_dir, threads=self.threads)


def passages(texts: Sequence[str], factory: Callable[[], object], *, workers: int,
             reuse: Optional[Mapping[str, Sequence[float]]] = None, progress: Optional[Callable[[int, int], None]] = None,
             chunksize: int = 4) -> Dict[str, List[float]]:
    """{text: vector} for every text. `workers` processes (spawned, so no torch state is forked), each built by `factory`."""
    if workers < 1:
        raise ValueError("workers must be at least 1")
    reuse = reuse or {}
    out: Dict[str, List[float]] = {}
    todo: List[str] = []
    pending: set = set()
    for t in texts:
        if t in out or t in pending:
            continue
        v = reuse.get(text_key(t))
        if v is not None:
            out[t] = list(v)
        else:
            pending.add(t)
            todo.append(t)
    if not todo:
        return out
    if workers == 1:
        emb = factory()
        for i, t in enumerate(todo):
            out[t] = emb.passage(t)
            if progress:
                progress(i + 1, len(todo))
        return out
    ctx = mp.get_context("spawn")
    with ctx.Pool(processes=workers, initializer=_init, initargs=(factory,)) as pool:
        for i, (t, v) in enumerate(zip(todo, pool.imap(_embed_one, todo, chunksize=chunksize))):
            out[t] = v
            if progress:
                progress(i + 1, len(todo))
    return out


def exact_reuse(window_count: int) -> bool:
    """A vector read back from the leaf index (float32) equals the one ingest held only for a leaf of one embedding window: E5 returns
    float32 values widened to float64, while a multi-window leaf's vector is a float64 mean of window vectors (eval_tenant.load_leaves)."""
    return window_count == 1


def one_window(embedder, text: str) -> bool:
    """True when the pinned embedder embeds `text` as a passage in one window (480 text tokens or fewer). Uses the embedder's own
    tokenizer, the same call `E5Embedder._encode_inner` makes."""
    from living_context.embed import e5
    ids = embedder._tok(text, add_special_tokens=False, truncation=False)["input_ids"]
    return len(e5.windows(ids)) == 1


def index_reuse(admin, tenant: str, keep: Callable[[str], bool], *, cls: str = "base", page: int = 500) -> Dict[str, List[float]]:
    """{sha256(text): vector} from another tenant's leaf index, READ ONLY, for the texts `keep` accepts (one-window leaves: the index's
    float32 values, widened, are exactly what ingest held for them). The vector is cast through float32 as `load_leaves` does."""
    import numpy as np
    out: Dict[str, List[float]] = {}
    after = None
    while True:
        body = {"size": page, "sort": [{"src_seq": "asc"}], "query": {"match_all": {}}, "_source": ["text", "embedding"]}
        if after is not None:
            body["search_after"] = after
        st, r = admin.request("POST", "/lc-%s-%s-leaf/_search" % (tenant, cls), body)
        if st != 200:
            raise RuntimeError("could not read the %s leaf index (status %s)" % (tenant, st))
        hits = r["hits"]["hits"]
        if not hits:
            return out
        for h in hits:
            s = h["_source"]
            if isinstance(s.get("text"), str) and s.get("embedding") and keep(s["text"]):
                out[text_key(s["text"])] = np.asarray(s["embedding"], dtype=np.float32).astype(np.float64).tolist()
        after = hits[-1]["sort"]


def verify_reuse(reuse: Mapping[str, Sequence[float]], texts: Sequence[str], embedder, sample: int = 20) -> int:
    """Re-embed up to `sample` reused texts in-process (first ones in input order) and require byte equality; returns how many were
    checked. Raises when any differs, so a wrong reuse map never reaches the tenant."""
    import json
    checked = 0
    for t in texts:
        v = reuse.get(text_key(t))
        if v is None:
            continue
        if json.dumps(list(v)) != json.dumps(embedder.passage(t)):
            raise RuntimeError("a reused vector differs from the pinned embedder's (text %d checked)" % (checked + 1))
        checked += 1
        if checked >= sample:
            break
    return checked
