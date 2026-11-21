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
