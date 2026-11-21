# Test host record

Pinned Elasticsearch image for every probe and security run (BUILD-PLAN 1.2).
The probe gate (`tools/probe_gate.py`) refuses a probe result whose recorded
image digest differs from this one.

ES-image-digest: sha256:79b4f1de4c15e6eca824723086a51e5934406d614d410cc11be77748cafac537
ES-version: 9.5.6

## Embedding model (BUILD-PLAN 1.3)

Model: intfloat/multilingual-e5-small, revision 614241f622f53c4eeff9890bdc4f31cfecc418b3 (MIT). Files are downloaded into a cache
directory outside the repository; their sha256 sums (the same constants as `living_context/embed/e5.py`, checked before every load).
The three large files match the LFS object ids the model repository lists for this revision; the three small ones match its git blob ids.

| File | sha256 |
|---|---|
| config.json | 69137736cab8b8903a07fe8afaafdda25aac55415a12a55d1bffa9f581abf959 |
| model.safetensors | 1a55775f53449dac10a2bcbc312469fac40b96d53198c407081a831f81c98477 |
| sentencepiece.bpe.model | cfc8146abe2a0488e9e2a0c56de7952f7c11ab059eca145a0a727afce0db2865 |
| special_tokens_map.json | d05497f1da52c5e09554c0cd874037a083e1dc1b9cfd48034d1c717f1afc07a7 |
| tokenizer.json | 0b44a9d7b51c3c62626640cda0e2c2f70fdacdc25bbbd68038369d14ebdf4c39 |
| tokenizer_config.json | a1d6bc8734a6f635dc158508bef000f8e2e5a759c7d92f984b2c86e5ff53425b |

Python dependencies of the real backend: `requirements-embed.lock` (hashed, Linux x86_64, CPU wheels of torch and transformers),
installed with `pip install --require-hashes -r requirements-embed.lock` on top of `requirements.lock`.
