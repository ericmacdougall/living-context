# A0 sanity on real data (no model calls)

Label: EXPLORATORY. Pilot split only, development only, decides nothing (EXPERIMENT 7.4). The held-out and fresh sets were not loaded.

What was run: every haystack of the 102 pilot questions (4,600 distinct sessions) ingested through the real writer into one tenant with
the pinned real embedder (multilingual-e5-small, pinned revision, CPU), then flat search (A0) only. No model call of any kind was made.

| Measure | Value |
|---|---|
| Distinct sessions / leaves | 4,600 / 19,605 (mean 4.26 leaves per session, max 17); the index holds 19,605 |
| Ingest wall time | 4,814 s (4.07 leaves per second, 0.96 sessions per second), 6 ingest threads, shared host |
| Embedding | 34,271 windows in 19,793 calls, 3,607 s of embedder time |
| Leaf index size | 95 MB store (control index 7.5 MB); whole node data directory 250 MB |
| A0 recall@10, 94 scored questions, text, title and entities | 0.621, clustered bootstrap 95% interval 0.534 to 0.705 |
| A0 recall@10, text only | 0.621, interval 0.534 to 0.705 (identical to the line above) |
| Search time for 94 questions | 4.2 s (strong), 3.2 s (text only); no failed search |

Reading. The data path, the real embedder, the writer and the flat arm work at real scale on one pooled tenant of the pilot
haystacks. The two A0 field variants return identical recall here: likely because on a tenant built without model calls the entity
field is empty (not checked), so the "stronger baseline" choice (EXPERIMENT A0 definition, `--a0-fields`) only differs once a
tree build has filled entity fields. Per-leaf figures on one pooled tenant are not those of the S2 pool (30 questions, each
haystack on its own, 1,412 sessions).
