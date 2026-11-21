# Offline quickstart

```
make venv            # once: python3.13 venv with numpy, cryptography, pytest (no torch, no transformers)
make example         # or: python examples/quickstart.py
```

`quickstart.py` takes six toy sessions (garden, kayak, tax, piano, bread, bicycle) through the real code path and answers four
questions, with **no network, no Elasticsearch, no embedder download and no model call**. It prints how many leaves and tree nodes it
built, which sessions the tree recall and a flat cosine search return for each question, and the gold-session hit counts. It exits 0
when both found at least one gold session.

## What it exercises

| Step | Code | Stand-in used |
|---|---|---|
| Segment sessions into leaves, build titles | `living_context.ingest.segment`, `ingest.title` | none (real code) |
| Embed leaves and questions | `living_context.embed.stub` | hashed bag-of-words, not the pinned E5 model |
| Cluster leaves into balanced groups, build the tree bottom-up | `living_context.structure.cluster`, `structure.build` | `StubBackend`, a deterministic extractive "model" behind the real `ModelPort` (budget counter, cache, schema checks) |
| Recall by fan-out over the tree | `living_context.recall.fanout.Recall` over `MemView` | the same `StubBackend` answers the holder calls |
| Flat baseline | cosine over the stub embeddings | stands in for arm A0, which really is BM25 + kNN in Elasticsearch |

The tree uses a small fan-out (`Constants(f_target=4, f_max=6, f_min=2)`) so six leaves still form more than one node; the
product defaults are 16 / 24 / 4.

## What it does not exercise

- **Elasticsearch**: the ingest writer, per-tenant indices, BM25 and kNN direct search, the pinned tree view, publishing and rollback.
  The tests that cover these run against a scratch cluster (`tests/security/*_es.py`, `LC_TEST_ES_URL` / `LC_TEST_CA` / `LC_TEST_PW`).
- The gateway, keystore, class policy enforcement, erasure and pools.
- The real embedder and any real model. A stub "model" that copies sentences cannot say anything about answer quality, so the hit
  counts printed are a smoke of the data path, never a benchmark result. For a benchmark see `bench/README.md`.
