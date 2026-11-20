# Z2 and Z1 (partial): plain retrieval over generated memories, by size and complexity

Status: PRE-STATEMENT written before the grid ran; results are appended below it after the run. Everything measured here is
exploratory unless it meets a rule stated in this section. Nothing here tunes a default or sets one: it measures where plain
retrieval stops being enough, and how well ADAPTIVE's zero-call signals predict its failures, on generated data. Zero model calls
anywhere (the embedder is the pinned local E5 model, run on the test host).

## 1. Question and scope

The owner's reading (TUNING 0): the system starts as plain retrieval and adapts as memory size and complexity demand. This stage
asks, on generated memories: at which size, for each level of complexity, does plain hybrid retrieval stop being enough, and which
zero-call signal tells that a query was not served well?

It does NOT establish: anything about a tree arm (none exists in this kit without model calls), any crossover where a tree beats
plain retrieval, any absolute threshold for real language, or any default. The memories are template text over a controlled
vocabulary (tunekit/synth.py states the limits): lexical overlap between a question and its gold leaf is controlled.

## 2. Design, fixed before the run

- Write path: every leaf goes through the product's `Writer.remember` (real keystore, validator, index mappings), one tenant and one
  class per cell, on a security-on single-node Elasticsearch 9.5.6 (Basic licence), 1 primary shard, no replica, 2 GB heap.
- Retrieval: BM25 (`multi_match` over text, title, entities) plus kNN (cosine, k 50, 100 candidates) in one `_msearch`, client-side
  reciprocal rank fusion (k 60, window 50). The harness replays 20 questions per cell through the product's `DirectSearch` and
  records whether the fused top 10 is identical.
- Embedder: multilingual-e5-small at the pinned revision, `passage:` and `query:` prefixes, CPU.
- Size: 64, 256, 1,000, 4,000 and 20,000 leaves. Complexity level c in {0, 0.5, 1}: the knob vector interpolates between a LOW
  and a HIGH endpoint (topic overlap 0.1 to 0.8, entity reuse 1 to 5 topics per entity, near-duplicate siblings 0 to 0.4, superseded
  versions 0 to 0.4, attribute paraphrase 0 to 0.7, name-part aliases 0 to 0.6, share of questions that give one topic word instead of two 0 to 1); the endpoints are fixed by
  the generator and were checked on one pilot seed for a usable difficulty range, not tuned on the grid seeds.
- Growth modes: `wide` (topics and entities grow with size; complexity statistics stay constant, tested) is the main grid; `deep`
  (40 topics fixed; each topic and entity gets deeper) is run at c = 0.5 as the contrast.
- Seeds: tuning seeds 1, 2, 3 for sizes up to 4,000 and seeds 1, 2 at 20,000 (cost). Held-out seeds (101 to 103) are not used.
- Questions: up to 400 per cell (a corpus that cannot supply 400 valid questions gives fewer; the count is in the table). A question
  has a FIXED specification: the attribute phrase, the entity by full name or by one name part (probability `alias_rate`), and
  exactly one or two topic words (`one_word`); it is kept only if exactly one current leaf matches that specification under the
  generator's topic vocabularies, otherwise it is dropped and counted. The drop rate by size is reported; a drop rate that rises with
  size would bias later sizes towards easier questions, and the note says so if it does. (An earlier design used the fewest words
  that still identify the leaf; it made questions easier as memory grew and was replaced before any grid run.)
- Set questions: up to 200 more per cell, "what did <entity> report regarding <topic words>" whose gold is EVERY current leaf of
  that entity in that topic (two or more; about three on average). They are the regime a single top-10 list serves worst, since
  the gold leaves differ in attribute and share only the entity and the topic. They are scored apart from the point questions:
  `all10` is 1 when every gold leaf is in the fused top 10, and item recall@10 is found gold leaves over all gold leaves (Wilson
  over items, optimistic because items of a question are not independent). Their uniqueness rule is the same as for point
  questions (one entity-and-topic group matches), with the drop rate reported.
- Order and cut: cells run smallest first, sizes up to 4,000 before 20,000, and the knob contrasts last. If time or disk runs
  out, the cut is made from the end of that order and the note lists exactly which cells were not run.
- Knob contrasts at 4,000 leaves, c = 0.5 base, one knob moved to its HIGH value at a time, seeds 1 and 2; and paraphrase at 0, 0.5
  and 1 on the same base to quantify the lexical-overlap knob.

## 3. Metrics and rules, stated before any result

- Recall@10 of a question is 1 if its gold leaf (the current version of the asked fact) is in the fused top 10. MRR@10 and
  recall@1 and @5 are reported beside. `stale_first` is the share of questions with a superseded version in the index for which
  a superseded version outranks the current one (or the current one is absent).
- Intervals: pooled over seeds, Wilson 95% on recall (questions treated as independent given a corpus, which is optimistic:
  the per-seed range is printed beside). Signal AUCs use a cluster bootstrap over (cell, topic) clusters, 2,000 resamples, seed
  16096 (EXPERIMENT 7 names 10,000; 2,000 is a cost deviation), percentile 95%.
- A pilot on one seed outside the grid seeds (4,000 leaves or fewer, point questions only) showed recall@10 at or above 0.975
  at every level while recall@1 fell from about 0.92 to 0.60 as complexity rose; the set questions were added for that reason,
  before the grid. The expectation stated here: point recall@10 stays high across the grid and the fall shows in recall@1 and
  MRR; set `all10` falls with size and complexity. These expectations can fail.
- Fall points per series: (a) SEPARATED FALL: the smallest size whose pooled recall@10 Wilson upper bound lies below the Wilson lower
  bound at the series' smallest size; (b) the first size with the metric below 0.95; (c) below 0.90. Each is applied to point
  recall@10, to set `all10` and to set item recall@10. The bars 0.95 and 0.90 are assumptions, there to name a point, not claims
  about what is enough.
- Signals, as ADAPTIVE 4.1 and 6.2 define them, with the placeholder thresholds (A_MIN 0.4, G_MIN 0.1, S_MAX 6): `agree`, `margin`,
  `spread`, the rule "flat-sufficient", and the memory signals. Label: `gold not in the fused top 10` ("flat missed a gold item in
  its top 10"); for a set question, `not every gold leaf in the top 10`. Point and set questions are analysed separately. An AUC is reported only with at least 30 positives and 30 negatives in the group, else NOT ESTABLISHED. A signal
  counts as supported (still exploratory, synthetic) when its AUC lower bound is at least 0.65 (the TUNING D3 bar) and as dropped when
  that lower bound is not above 0.55. Where the second branch of `hard(unit)` (re-query within a conversation) or any tree signal is
  needed, it is not computable here and is not redefined.
- Latency, two numbers per `_msearch` (two sub-searches), query embedding excluded, 5 warm-up queries then the questions in
  sequence: the client wall time through the product's `EsClient`, which opens a new TLS connection for every request (so it includes
  a handshake and is a property of that client, not of Elasticsearch), and Elasticsearch's own `took`. In the main grid up to three
  cells ran concurrently on the same node and the host is shared, so both are indicative only. The size-versus-latency curve that is
  quoted comes from a separate quiet run: one worker, nothing else of ours running, one seed per size, three sequential passes, the
  median and range over passes. Ingest rate is the product write path (no connection reuse, no bulk path) with embeddings
  precomputed; embedding throughput is reported separately.
- kNN validity: for the first 50 questions of each cell, the kNN top 10 and cosines that Elasticsearch returns are compared with
  exact dot products of the saved vectors (set overlap, largest cosine error), so `margin` is computed from checked numbers and
  approximate-search misses can be told from embedding misses.
- Memory signals in wide growth are collinear with size (leaf count, session count and token total all grow with it); only the
  deep-growth contrast and the knob contrasts can separate them. The generator's entity names come from 60 first and 400 last
  names, and the product's entity extractor sees name tokens, so `entity_count` cannot exceed about 460; and a generated leaf is
  about 25 tokens, so `token_total` at 20,000 leaves is about 0.7 million. Both are below their placeholder thresholds (2,000
  entities and 2.5 million tokens) by construction, and the note reports them as NOT MEASURABLE on this generator in that range.
- Superseded leaves are ordinary separate leaves with no version filter in this harness; `stale_first` measures what that costs.
- Every number below is exploratory. None is used to set a default in this change.

## 4. Results

(appended after the run)
