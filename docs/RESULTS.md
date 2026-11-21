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
| A0+LEN | A0's top 50 re-ranked by RRF score plus beta times ln(session tokens); beta chosen from a seven-value grid on these same 94 questions (in-sample). A weak control: it changes recall on 1 of 94. |
| A1 | Tree recall fused with the direct flat list by reciprocal-rank fusion (RRF). |
| A1-tree-only | The tree list alone. |
| SHUFFLED-SUMMARIES | Tree with summaries shuffled across nodes (negative control; should be near chance). |

Storage: Elasticsearch 9.5.6 (pinned image); embedder
intfloat/multilingual-e5-small @614241f (480-token windows, mean-pooled).

## S3: pilot, n = 94 (primary exploratory result)

**Provenance.** Model: meta/muse-spark-1.3-contributor via OpenRouter, reasoning
medium, concurrency 16. Commit: afd0155. Pool: 4,259 sessions / 18,120 leaves in
one tenant. Questions: 94 non-abstention questions of the 102-question pilot.
Run: started 2026-11-21 06:42 UTC, complete 09:15 UTC (STAMP.json, STATE.json).

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
  0.665]. SHUFFLED returned an empty list on 34 of the 50 questions (mean 1.3
  sessions), so this difference is just tree-only recall on those questions. The
  50 are the first 50 in run order. The registered holder-log check was not run.

All-gold@10 in the table is over all 94 questions. The registered definition
(two or more gold sessions, n = 59) gives A0 0.441, A0+LEN 0.458, A1 0.475,
tree-only 0.322.

### Honest reading

- **No separated effect.** A1 beats A0 by +0.054 recall@10, but the interval
  crosses zero.
- **The tree alone loses to flat search** (0.540 against 0.621).
- **The gain is not from the tree finding new sessions.** Source of gold in the
  A1 top 10 (75 questions with any gold): both lists 88 hits, direct-only 23,
  tree-only 1 (1 question). H1 condition (c) as registered (Wilson lower bound
  above 0 on 1 of 75) is met only trivially and is uninformative: any single
  tree-only hit satisfies it. The tree alone supplied 1 of 112 gold hits; the
  fused gain is mostly re-ranking of sessions both lists found. The condition
  will be tightened by an amendment before the confirmatory questions run.
- **Per question:** A1 against A0, recall up on 17, down on 8, same on 69.
  Tree-only against A0: up 18, down 27.
- **The length explanation is untested.** The project's analysis reports that A1
  brings in longer sessions than it drops (length-entrant percentile shift
  +0.07, CI [0.02, 0.12]; not recomputable from the ledgers). A0+LEN was fitted
  in-sample and changes one question of 94, so it does not rule a length effect
  in or out.
- **Post hoc.** A1 minus A0 is +0.126 on the 30 questions also used in S2 and
  +0.021 [-0.052, +0.096] on the other 64. Tree-only is shorter than 10 sessions
  on 42 of 94 questions (mean 7.3); against A0 cut to the same length it is
  0.540 against 0.509.
- **Precision@10 is low for every arm** because 79 of 94 questions have one
  or two gold sessions (gold count 1: 35, 2: 44, 3 or more: 15), which caps
  precision@10 near 0.19; it is recall rescaled and should not be read on its
  own.
- **The negative control is weak.** SHUFFLED returned empty lists on most
  questions (see above), so it shows only that a tree with scrambled summaries
  finds nothing. It does not say the tree helps.
- **Power.** From S3 variance, at the registered n = 293 the 80%-power
  detectable effect for A1 - A0 is about 0.054 from the observed variance
  (the 0.084 figure assumes a variance the data do not show). The S3 point
  estimate sits right at this boundary.
- **Model, n, pool and tree depth all changed between S2 and S3**, and the 30
  S2 questions are a subset of the 94 in S3. Tree-only went from best arm (S2,
  n = 30, Sonnet) to worst (S3, n = 94, Muse). Do not attribute.

### Cost and latency (S3, measured from the call ledger)

| Item | Figure |
|---|---|
| Total model spend | $4.47 for 6,291 calls (0 throttled, 3 unparseable build outputs, 1 connection error) |
| One-time tree build (18,120 leaves) | 1,212 calls, $1.70; build_l1 median latency 14.1 s, p90 17.4 s |
| Recall | 5,079 calls for 144 tree recalls (94 A1 + 50 SHUFFLED): about 35 calls and about $0.019 per recall, averaged with the SHUFFLED control; about 85.9k input and 53.1k output tokens per recall; A1 alone averages 31.7 route calls (plus up to 7.9 organise calls). The dollar figure reflects a $0.10 / $0.20 per million token tier |
| Route call latency | median 8.3 s (p90 15.2 s) |
| Organise call latency | 6.0 s (p90 11.7 s) |
| A0 flat search | about 45 ms per query (94 queries in 4.2 s), zero model calls |

In these runs each recall issued its calls one after another (one worker per
recall; the concurrency of 16 was across questions). Measured model time per
tree recall: about 307 s on S3, 152 s on S2. The parallel fan-out the product is
designed for was not timed. **End-to-end per-recall wall time was not logged**
(a gap to fix).

**Bottom line:** on this pilot, A1 costs about 35 LLM calls and about five
minutes of serial model time per recall (the parallel mode was not timed) for a +5 point recall@10 that is not statistically separated from
zero, versus a 45 ms zero-cost flat search.

## S2: pilot-30

**Provenance.** Model: Claude Sonnet via headless CLI, serial. Pool: 1,412
sessions / 5,988 leaves. n = 30. Completed 2026-11-20 01:55 UTC (STATE.json); the commit is not
recorded in the run directory.

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
record. These are the first 32 S3 questions on the same pool; A1 minus A0 is
-0.017 here and +0.134 on the same questions in the Muse run. The model was
changed after this partial run.

## S6-H1 (confirmatory): status

**Not run to completion. No result exists.**

- Design: 312 fresh questions; whole-pool build of 78,154 leaves in 3 levels
  (4,885 / 306 / 20); expected about 21-27k calls; cap 35,574.
- The tree build stopped at call 1,536 of about 5,211. The confirmatory run is
  paused pending API credit and build-host disk space.

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
