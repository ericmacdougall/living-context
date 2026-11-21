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
haystacks. The two A0 field variants return the same recall@10, and this is a real tie, not a parameter that did not take effect:
checked on the same index, title is filled for all 19,605 leaves and entities for 18,909 (code-built, no model call), the BM25 field
list reaches the query, and on 40 of the pilot questions the BM25 lists of the two variants differ in the top-10 order for 7 and in the
top-50 order for 13 (scores differ in 8 top-10 lists). The difference is absorbed by the fusion with the vector list and by cutting to
distinct sessions, so the mean recall@10 is identical to 16 digits at this size. Both variants stay registered as one parameter.

Stated limit (embedding). The tokenizer warning "560 > 512" printed during ingest comes from tokenizing a whole leaf before it is cut
into windows. Leaves are not truncated: a leaf longer than 480 tokens is embedded in 480-token windows and its vector is the normalised
mean of the window vectors (34,271 windows for 19,605 leaves). Queries are cut at 480 tokens, which no question reaches. A0 and A1 read
the same leaf vectors and the same query vector function, so both see the same windowing; the mean-of-windows vector is a design
assumption (DESIGN 18.3) that lowers the sharpness of long-leaf vectors for both arms alike.

Per-leaf figures on one pooled tenant are not those of the S2 pool (30 questions, 1,412 sessions, 5,988 leaves).
