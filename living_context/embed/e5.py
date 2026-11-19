"""The real embedding backend: multilingual-e5-small at a pinned revision, run locally on CPU (DESIGN 18.3; BUILD-PLAN 1.3).

  * `query: ` and `passage: ` prefixes (the model requires them; without them top-10 overlap fell to 0.718 in the earlier lab),
    mean pooling over the attention mask, L2 normalisation, 384 dimensions;
  * a leaf longer than 480 text tokens is embedded in 480-token windows; every window vector is L2-normalised and the leaf vector
    is the L2-normalised mean of the window vectors (ASSUMPTION of DESIGN 18.3; a leaf of one window is the plain E5 vector);
  * the model files are checked against the pinned sha256 sums before anything is loaded, and loading is offline only;
  * torch and transformers are imported lazily, so importing this module (and the pure helpers below) needs neither.

The backend plugs into the existing port: `EmbedPort(limiter, backend=e5.passage)` and a second port over the same limiter with
`backend=e5.query`, so every call passes the per-tenant fair queue (`ports`). `is_real` is True; the hashed stub has no such
attribute, which is how a real-stage runner tells them apart (`require_real`).
"""
from __future__ import annotations

import hashlib
import math
import os
from pathlib import Path
from typing import Callable, List, Optional, Sequence, Tuple

from ..model.fairq import FairLimiter
from .port import EmbedPort

MODEL = "intfloat/multilingual-e5-small"
REVISION = "614241f622f53c4eeff9890bdc4f31cfecc418b3"
DIMS = 384
WINDOW_TOKENS = 480
QUERY_PREFIX, PASSAGE_PREFIX = "query: ", "passage: "
MODEL_ID = f"{MODEL}@{REVISION}"
# sha256 of every file the loader reads, recorded when the pinned revision was downloaded (docs/HOST.md records them too)
FILE_SHA256 = {
    "config.json": "69137736cab8b8903a07fe8afaafdda25aac55415a12a55d1bffa9f581abf959",
    "model.safetensors": "1a55775f53449dac10a2bcbc312469fac40b96d53198c407081a831f81c98477",
    "sentencepiece.bpe.model": "cfc8146abe2a0488e9e2a0c56de7952f7c11ab059eca145a0a727afce0db2865",
    "special_tokens_map.json": "d05497f1da52c5e09554c0cd874037a083e1dc1b9cfd48034d1c717f1afc07a7",
    "tokenizer.json": "0b44a9d7b51c3c62626640cda0e2c2f70fdacdc25bbbd68038369d14ebdf4c39",
    "tokenizer_config.json": "a1d6bc8734a6f635dc158508bef000f8e2e5a759c7d92f984b2c86e5ff53425b",
}


class PinError(Exception):
    pass


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def verify_files(model_dir: Path, pins: Optional[dict] = None) -> None:
    """Refuse a model directory whose files are not exactly the pinned ones."""
    pins = FILE_SHA256 if pins is None else pins
    if not pins:
        raise PinError("no pinned checksums are recorded")
    for name, want in pins.items():
        p = Path(model_dir) / name
        if not p.is_file():
            raise PinError("missing model file %s" % name)
        if sha256_file(p) != want:
            raise PinError("checksum mismatch for %s" % name)


# ---- pure helpers (no torch) -------------------------------------------------------------------------------------------
def windows(ids: Sequence[int], size: int = WINDOW_TOKENS) -> List[List[int]]:
    """Consecutive windows of at most `size` token ids; an empty text is one empty window."""
    if not ids:
        return [[]]
    return [list(ids[i:i + size]) for i in range(0, len(ids), size)]


def l2(v: Sequence[float]) -> List[float]:
    n = math.sqrt(sum(x * x for x in v))
    return [x / n for x in v] if n else list(v)


def mean_normalised(vectors: Sequence[Sequence[float]]) -> List[float]:
    """The leaf vector: L2-normalised mean of the (already normalised) window vectors."""
    if len(vectors) == 1:
        return list(vectors[0])
    d = len(vectors[0])
    return l2([sum(v[i] for v in vectors) / len(vectors) for i in range(d)])


# ---- the model ---------------------------------------------------------------------------------------------------------
class E5Embedder:
    is_real = True
    model_id = MODEL_ID
    dims = DIMS

    def __init__(self, model_dir, *, threads: int = 4, batch_windows: int = 16, verify: bool = True,
                 broken_no_prefix: bool = False, broken_no_normalise: bool = False):
        """`broken_*` are the variants the tests must turn red; production code never sets them."""
        d = Path(model_dir)
        if verify:
            verify_files(d)
        import torch
        from transformers import AutoModel, AutoTokenizer
        torch.set_num_threads(threads)
        self._torch = torch
        self._tok = AutoTokenizer.from_pretrained(str(d), local_files_only=True)
        self._model = AutoModel.from_pretrained(str(d), local_files_only=True).eval()
        self._batch, self._no_prefix, self._no_norm = batch_windows, broken_no_prefix, broken_no_normalise
        self._cls, self._sep, self._pad = self._tok.cls_token_id, self._tok.sep_token_id, self._tok.pad_token_id
        self._prefix_ids = {k: self._tok(p, add_special_tokens=False)["input_ids"]
                            for k, p in (("query", QUERY_PREFIX), ("passage", PASSAGE_PREFIX))}

    # one text, either kind -> a 384-float list
    def _encode(self, kind: str, text: str) -> List[float]:
        ids = self._tok(text, add_special_tokens=False, truncation=False)["input_ids"]
        pre = [] if self._no_prefix else self._prefix_ids[kind]
        seqs = [[self._cls] + pre + w + [self._sep] for w in (windows(ids) if kind == "passage" else [ids[:WINDOW_TOKENS]])]
        vecs: List[List[float]] = []
        for i in range(0, len(seqs), self._batch):
            vecs += self._run(seqs[i:i + self._batch])
        return mean_normalised(vecs)

    def _run(self, seqs: List[List[int]]) -> List[List[float]]:
        t = self._torch
        n = max(len(s) for s in seqs)
        ids = t.tensor([s + [self._pad] * (n - len(s)) for s in seqs])
        mask = t.tensor([[1] * len(s) + [0] * (n - len(s)) for s in seqs])
        with t.inference_mode():
            h = self._model(input_ids=ids, attention_mask=mask).last_hidden_state
        m = mask.unsqueeze(-1).to(h.dtype)
        pooled = (h * m).sum(1) / m.sum(1).clamp(min=1)
        if not self._no_norm:
            pooled = t.nn.functional.normalize(pooled, p=2, dim=1)
        return pooled.double().tolist()

    def passage(self, text: str) -> List[float]:
        return self._encode("passage", text)

    def query(self, text: str) -> List[float]:
        return self._encode("query", text)

    def passages(self, texts: Sequence[str]) -> List[List[float]]:
        """Batch form; each result equals `passage` of the same text up to float rounding (tested)."""
        return [self._encode("passage", x) for x in texts]


def ports(embedder, limit: int = 2, limiter: Optional[FairLimiter] = None) -> Tuple[EmbedPort, EmbedPort]:
    """(passage port, query port) over ONE limiter: every embedding call of every tenant passes the same fair queue."""
    lim = limiter or FairLimiter(limit)
    return EmbedPort(lim, backend=embedder.passage), EmbedPort(lim, backend=embedder.query)


def require_real(embedder, *, stage: str) -> None:
    """A real-stage runner refuses anything that is not the pinned real embedder (the hashed stub in particular)."""
    if not (getattr(embedder, "is_real", False) and getattr(embedder, "model_id", "") == MODEL_ID):
        raise PinError("stage %s needs the pinned real embedder (%s); got %r" % (stage, MODEL_ID, type(embedder).__name__))
