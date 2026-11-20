"""The pinned local embedder, batch form, for the tuning kit (BUILD-PLAN 1.3): multilingual-e5-small at a pinned revision, run on CPU.
Same recipe as the product's real backend (query: and passage: prefixes, mean pooling over the attention mask, L2 normalisation, 384
dimensions, 480-token windows averaged and renormalised), re-implemented here so the kit depends on no other branch. It is a local
model, not a model-port call. torch and transformers are imported lazily; the model files are checked against pinned sha256 sums
before loading and loading is offline only."""
from __future__ import annotations

import hashlib
import time
from pathlib import Path
from typing import List, Sequence

import numpy as np

MODEL = "intfloat/multilingual-e5-small"
REVISION = "614241f622f53c4eeff9890bdc4f31cfecc418b3"
MODEL_ID = f"{MODEL}@{REVISION}"
DIMS = 384
WINDOW_TOKENS = 480
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


def _sha(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def verify_files(model_dir: Path) -> None:
    for name, want in FILE_SHA256.items():
        p = Path(model_dir) / name
        if not p.is_file():
            raise PinError("missing model file " + name)
        if _sha(p) != want:
            raise PinError("checksum mismatch for " + name)


class BatchEmbedder:
    is_real = True
    model_id = MODEL_ID
    dims = DIMS

    def __init__(self, model_dir, *, threads: int = 4, batch: int = 64, verify: bool = True):
        d = Path(model_dir)
        if verify:
            verify_files(d)
        import torch
        from transformers import AutoModel, AutoTokenizer
        torch.set_num_threads(threads)
        self._t = torch
        self._tok = AutoTokenizer.from_pretrained(str(d), local_files_only=True)
        self._model = AutoModel.from_pretrained(str(d), local_files_only=True).eval()
        self._batch = batch
        self.stats = {"texts": 0, "windows": 0, "seconds": 0.0}
        self._pre = {k: self._tok(p, add_special_tokens=False)["input_ids"] for k, p in (("query", "query: "), ("passage", "passage: "))}

    def _seqs(self, kind: str, text: str) -> List[List[int]]:
        ids = self._tok(text, add_special_tokens=False, truncation=False)["input_ids"]
        wins = [ids[i:i + WINDOW_TOKENS] for i in range(0, len(ids), WINDOW_TOKENS)] or [[]]
        if kind == "query":
            wins = [ids[:WINDOW_TOKENS]]
        return [[self._tok.cls_token_id] + self._pre[kind] + w + [self._tok.sep_token_id] for w in wins]

    def _run(self, seqs: List[List[int]]) -> np.ndarray:
        t = self._t
        n = max(len(s) for s in seqs)
        pad = self._tok.pad_token_id
        ids = t.tensor([s + [pad] * (n - len(s)) for s in seqs])
        mask = t.tensor([[1] * len(s) + [0] * (n - len(s)) for s in seqs])
        with t.inference_mode():
            h = self._model(input_ids=ids, attention_mask=mask).last_hidden_state
        m = mask.unsqueeze(-1).to(h.dtype)
        pooled = t.nn.functional.normalize((h * m).sum(1) / m.sum(1).clamp(min=1), p=2, dim=1)
        return pooled.double().numpy()

    def encode(self, kind: str, texts: Sequence[str]) -> np.ndarray:
        """One L2-normalised 384-float vector per text (a text of several windows: renormalised mean of its window vectors)."""
        t0 = time.monotonic()
        per = [self._seqs(kind, x) for x in texts]
        flat = [(i, s) for i, ss in enumerate(per) for s in ss]
        order = sorted(range(len(flat)), key=lambda j: len(flat[j][1]))   # length-sorted batches waste little padding
        out = np.zeros((len(flat), DIMS))
        for b in range(0, len(order), self._batch):
            idx = order[b:b + self._batch]
            out[idx] = self._run([flat[j][1] for j in idx])
        vec = np.zeros((len(texts), DIMS))
        cnt = np.zeros(len(texts))
        for j, (i, _) in enumerate(flat):
            vec[i] += out[j]
            cnt[i] += 1
        vec /= cnt[:, None]
        n = np.linalg.norm(vec, axis=1, keepdims=True)
        vec = vec / np.where(n == 0, 1, n)
        self.stats["texts"] += len(texts)
        self.stats["windows"] += len(flat)
        self.stats["seconds"] += time.monotonic() - t0
        return vec

    def passages(self, texts):
        return self.encode("passage", texts)

    def queries(self, texts):
        return self.encode("query", texts)
