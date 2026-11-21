# Results

**Status: exploratory development figures only. The method is not proven and
the pre-registered confirmatory run has not happened.** No confirmatory claim
can be made yet.

All figures were recomputed on 2026-11-21 from the run ledgers (`done.jsonl`,
`calls.jsonl`). They are on the LongMemEval-S (cleaned) **pilot** split.
Metrics are gold-session metrics at 10; "n" is the number of questions; values
are means.

## Arms

| Arm | What it is |
|---|---|
| A0 | Flat hybrid search (BM25 over text, title and entities, plus e5 dense, fused). No model calls. |
| A0+LEN | A0 re-ranked with a session-length prior (control for "the tree just prefers long sessions"). |
| A1 | Tree recall fused with the direct flat list by reciprocal-rank fusion (RRF). |
| A1-tree-only | The tree list alone. |
| SHUFFLED-SUMMARIES | Tree with summaries shuffled across nodes (negative control; should be near chance). |

Storage: Elasticsearch 9.5.6 (pinned image); embedder
intfloat/multilingual-e5-small @614241f (480-token windows, mean-pooled).

## S3: pilot, n = 94 (primary exploratory result)

**Provenance.** Model: meta/muse-spark-1.3-contributor via OpenRouter, reasoning
medium, concurrency 16. Commit: afd0155. Pool: 4,259 sessions / 18,120 leaves in
one tenant. Questions: 94 non-abstention questions of the 102-question pilot.
The run date is not recorded on the facts sheet; the figures were recomputed
from the ledgers on 2026-11-21.

| Arm | n | Recall@10 [95% CI] | Precision@10 | All-gold@10 | MRR@10 |
|---|---|---|---|---|---|
| A0 | 94 | 0.621 [0.535, 0.705] | 0.109 | 0.521 | 0.485 |
| A0+LEN | 94 | 0.626 [0.538, 0.712] | 0.110 | 0.532 | 0.481 |
| A1 | 94 | 0.676 [0.594, 0.754] | 0.119 | 0.574 | 0.542 |
| A1-tree-only | 94 | 0.540 [0.452, 0.627] | 0.101 | 0.415 | 0.462 |
| SHUFFLED | 50 | 0.000 | 0.000 | 0.000 | 0.000 |

Paired differences (clustered bootstrap, 90 clusters; replicated independently):

- A1 - A0: **+0.054**, 95% CI [-0.009, +0.123]. The interval crosses zero.
- A1 - A0+LEN: +0.049, CI [-0.016, +0.118].
- SHUFFLED control (A1-tree-only - SHUFFLED on 50 questions): +0.538 [0.409,
  0.665]. The tree's signal comes from summary content.

### Honest reading

- **No separated effect.** A1 beats A0 by +0.054 recall@10, but the interval
  crosses zero.
- **The tree alone loses to flat search** (0.540 against 0.621).
- **The gain is not from the tree finding new sessions.** Source of gold in the
  A1 top 10 (75 questions with any gold): both lists 88 hits, direct-only 23,
  tree-only 1 (1 question). H1 condition (c), that the gain must come from the
  tree, is **not met** on this pilot: the fused gain is mostly re-ranking of
  sessions both lists found.
- **Per question:** A1 against A0, recall up on 17, down on 8, same on 69.
  Tree-only against A0: up 18, down 27.
- **A length effect is present.** A1 brings in longer sessions than it drops
  (length-entrant percentile shift +0.07, CI [0.02, 0.12]), but A0+LEN alone
  barely moves (+0.005), so a length prior alone does not explain A1.
- **Precision@10 is low for every arm** because most questions have 1-2 gold
  sessions (gold_n: 1 -> 21, 2 -> 34, 3+ -> 9 of 64 multi-analysis questions);
  it moves with recall.
- **The negative control behaves** (SHUFFLED at zero), which says the tree is
  using summary content. It does not say the tree helps.
- **Power.** From S3 variance, at the registered n = 293 the 80%-power
  detectable effect for A1 - A0 is about 0.054 (cluster-ratio) to 0.084
  (sqrt-d formula). The S3 point estimate sits right at this boundary.
- **Model and n both changed between S2 and S3.** Tree-only went from best arm
  (S2, n = 30, Sonnet) to worst (S3, n = 94, Muse). Do not attribute.

### Cost and latency (S3, measured from the call ledger)

| Item | Figure |
|---|---|
| Total model spend | $4.47 for 6,291 calls (0 throttled, 3 unparseable build outputs, 1 connection error) |
| One-time tree build (18,120 leaves) | 1,212 calls, $1.70; build_l1 median latency 14.1 s, p90 17.4 s |
| Recall | 5,079 calls for 144 tree recalls (94 A1 + 50 SHUFFLED): about 35 calls and about $0.019 per recall |
| Route call latency | median 8.3 s (p90 15.2 s) |
| Organise call latency | 6.0 s (p90 11.7 s) |
| A0 flat search | about 45 ms per query (94 queries in 4.2 s), zero model calls |

Calls within a tree level run in parallel, so recall wall latency is roughly
depth x per-call latency (tens of seconds). **End-to-end per-recall wall time was
not logged per question** (a gap to fix).

**Bottom line:** on this pilot, A1 costs about 35 LLM calls and tens of seconds
per recall for a +5 point recall@10 that is not statistically separated from
zero, versus a 45 ms zero-cost flat search.

## S2: pilot-30

**Provenance.** Model: Claude Sonnet via headless CLI, serial. Pool: 1,412
sessions / 5,988 leaves. n = 30. The commit and run date are not recorded on the
facts sheet.

| Arm | n | Recall@10 | Precision@10 | All-gold@10 | MRR@10 |
|---|---|---|---|---|---|
| A0 | 30 | 0.674 | 0.127 | 0.633 | 0.569 |
| A1 | 30 | 0.768 | 0.153 | 0.667 | 0.703 |
| A1-tree-only | 30 | 0.803 | 0.150 | 0.667 | 0.633 |
| SHUFFLED | 30 | 0.000 | 0.000 | 0.000 | 0.000 |

A1 - A0 recall@10 = +0.094, 95% clustered bootstrap CI [-0.033, +0.224]. The
interval crosses zero. Cost: route median 1.9 s, build 17.4 s; about 67 calls per
recall; 4.39 h for build plus 60 recalls.

## Partial S3 on Sonnet (abandoned)

32 questions, abandoned when the run moved to Muse. A0 0.545, A1 0.528,
tree-only 0.433. Too small to read; listed so the abandoned run is on the
record.

## S6-H1 (confirmatory): status

**Not run to completion. No result exists.**

- Design: 312 fresh questions; whole-pool build of 78,154 leaves in 3 levels
  (4,885 / 306 / 20); expected about 21-27k calls; cap 35,574.
- The tree build stopped (LIMIT_REACHED) at call 1,536 of about 5,211 on an
  OpenRouter HTTP 403 (quota/billing refusal) at 04:11 AM PT on 2026-11-21.
  $2.24 spent on the run so far.
- Estimated remaining spend at S3 rates: build about $5-6 more, recalls about
  362 x $0.019 = about $7, so roughly $15-20 in total, under $50.
- Before resuming, the build host's root disk must be cleared: it is 100% full
  (1.1 GB free of 643 GB), and Elasticsearch's flood watermark is 5 GB.

Thresholds for this run were pre-registered in `docs/EXPERIMENT.md` (on
`feat/parallel-calls`) before any run, and results never move a threshold. The
pilot figures above must not be presented as the outcome of H1.

## Not measured yet

- H1 confirmatory (S6-H1)
- H1b
- H2 (cross-links)
- H3 (reorganisation loop)
- H4 (reflection / reverse audit)
- Comparisons C1 and C2
- Any public benchmark besides LongMemEval-S (for example LoCoMo)
- Answer accuracy (end-to-end QA)
- End-to-end recall latency
