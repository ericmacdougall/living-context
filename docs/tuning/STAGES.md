# Zero-call tuning kit: stages, generators, what runs today

The kit is `tunekit/` (code) and `tests/tunekit/` (tests). It makes no model call of any kind: text comes from templates, the
embedder is the pinned local E5 model, and the model port is never constructed (the D1 check uses the stub backend of the product).
Heavy runs go to the test host, not a laptop. Product code for adaptive signals, transitions, pool selection, result combination
across pools and injection is not on this branch; where a stage needs it the kit has the generator and a harness interface with a
named adapter (`tunekit/stages.py`) that raises `ProductCodeMissing` instead of returning a stand-in.

## 1. Stages Z1 to Z6

| Stage | Measures | Informs | Inputs and outputs | Status today | Built | Waits on |
|---|---|---|---|---|---|---|
| Z1 | index size per leaf, ingest rate through the real write path, query latency, shard count of stage 0 | D8, AT-8 | G1 corpora 64 to 20,000 leaves; bytes per leaf, leaves per second, p50 and p95 latency | PARTIAL | `baseline`, `run_grid`: every cell reports these on one shard | D8 needs shards near 1 GiB (a generated leaf is about 2.7 KB, so about 400,000 leaves) and the rollover rule `B_SHARD_TARGET`, which does not exist; both exceed this budget |
| Z2 | difficulty features (`agree`, `margin`, `spread`), memory signals, their AUC against plain-retrieval failure | D4 features, D3 | G2 to G5 cells; curves by size and complexity, AUC tables with intervals | RUNS | `synth`, `baseline`, `signals`, `run_grid`, `analyze` | the second branch of `hard(unit)` (re-query in a conversation) needs recall streams; tree signals need a tree arm and opens |
| Z3 | false moves, attempts, spend, probation outcomes; the `N_FLOOR` derivation | D1, D5, D6, D7 (rollback side), AT-4 | traces (`traces`): growth, stationary noise, oscillation, burst, bulk import, one flooding principal | WAITS (the D1 derivation RUNS) | trace generators with tests; `MachineAdapter`, `ProbationAdapter`; the `N_FLOOR` check is a test against the product's bulk build with the stub | the evaluator and probation code (ADAPTIVE 4.3 to 5.3) |
| Z4 | answer-pool hit rate, small-pool starvation, trigger recall and precision, injection bounds | D9 to D13, AT-5, AT-6, AT-9 | G6 layouts (`synth_pools`), G7 conversations (`synth_conv`) | PARTIAL | both generators; D9 run with a reference reimplementation of the POOLS 4.2 rule (`pool_reference`) | the product's selection, combination and injection code; G8 usage scripts are not built; no downloaded multi-session set |
| Z5 | recall difference and cost of ADAPT against TREE-ONLY and FLAT-ONLY along growth | AT-2 | G1 growth cells | WAITS | the growth cells and the flat arm | the evaluator, a tree build and a model port with the stub |
| Z6 | the Z2 features and labels on real text with real labels | D3, D4 levels | an external set in the BEIR layout | WAITS | `external.load_beir_like` (tested on a fixture) | the owner's approval of downloads (`DATASETS.md`); the DEV split |

## 2. Generators

All are deterministic from (generator version, knobs, seed) and write ground truth at generation time. The corpus manifest holds
the knobs, the seed, the counts and a content hash. Text is template text; what that cannot show is in `tunekit/synth.py` and in
section 4 below.

| Generator | Module | Knobs | Ground truth |
|---|---|---|---|
| G1 size | `synth` | `n_leaves` (exact: tested at every complexity), `growth` wide or deep | the corpus itself |
| G2 topics | `synth` | `topic_size`, `topic_overlap`, `topic_skew`, `fixed_topics` (deep) | topic of each leaf and question |
| G3 sessions | `synth` | `session_len` (mean, geometric) | session of each leaf (a session stays in one topic) |
| G4 entities | `synth` | `entity_reuse` (mean topics per entity), `alias_rate` | entity of each leaf; the name part a question uses |
| G5 time | `synth` | `update_rate` (a superseded older version of a key), `near_dup` (a same-wording sibling about another entity) | the current and the superseded leaf of each question; siblings |
| questions | `synth` | `paraphrase` (attribute phrase replaced by another surface form), `alias_rate`, `one_word`, `n_questions` | gold leaf, gold value, stale and sibling leaves |
| G6 pools | `synth_pools` | pools, leaves per pool, decoy pools with look-alike cards, ungranted leak pools, `card_coverage`, share of two-pool and no-answer questions | answer pools; the granted set; copies in the leak pool |
| G7 conversations | `synth_conv` | turns, shift, return, new-entity, chat and failed-lookup rates | the turn of each planted event and the leaves that should be offered |
| traces | `traces` | seed and shape | stationary or growing pools with known hard-rate series |

Complexity is a vector; `synth.complexity(level)` interpolates between a LOW and a HIGH endpoint, level 0 to 1.

## 3. Independence of the two variables

Size is one number. Complexity is a vector of per-leaf or per-topic quantities that do not mention the leaf count. In `wide` growth
topics and entities are added with the leaves, so the complexity statistics should not move with size; in `deep` growth the topic
count is fixed, so they must move (the control). The tests (`tests/tunekit/test_synth.py`):

- the leaf count equals the size knob exactly at sizes 64, 257 and 1,000 and complexity 0, 0.5 and 1;
- ten statistics (topic vocabulary overlap, entity reuse, near-duplicate share, superseded share, session length, words per leaf,
  leaves per topic, share of aliased and paraphrased questions, topic words per question) are within 20% (absolute floor 0.03)
  between 600 and 6,000 leaves, at each of three levels, averaged over three seeds;
- the deep-growth control turns that check red (leaves per topic differs ten times between the sizes);
- raising complexity from 0 to 1 at 2,000 leaves moves the statistics it should and leaves leaves-per-topic, session length and
  words per leaf within 20%, 20% and 10%;
- each knob moved alone moves its own statistic and leaves the others within a quarter.

The statistics themselves are in `data/independence.json`. Short version: leaves per topic is 25 at every size; entity reuse is 1.54
and 1.57, 3.05 and 3.03, 4.79 and 4.75 at 600 and 6,000 leaves for levels 0, 0.5 and 1; near-duplicate share is 0.141 and 0.140
at level 0.5; under deep growth leaves per topic is 25 at 600 leaves and 250 at 6,000. Topic vocabulary overlap (mean Jaccard
between topic vocabularies) is small in absolute terms (0.001, 0.013, 0.044) because a topic draws 10 words from an invented space;
its relative steadiness is what is tested. The share of questions dropped as not uniquely identifiable stays at or below 0.001 at
these sizes.

## 4. What generated text cannot show

- It is not natural language. Entity names and most topic words are invented pronounceable strings; some topic words are common
  English words from one shared list; attribute phrases are real English with three surface forms each.
- Lexical overlap between a question and its gold leaf is controlled: the entity (or a name part), one or two topic words and an
  attribute phrase. BM25 therefore finds the gold leaf more often than it would on real data unless `paraphrase`, `alias_rate` and
  `one_word` strip the overlap; the embedder cannot link invented words to each other, so `paraphrase` is the only knob that tests
  semantic matching, and only for the attribute phrase. The paraphrase section of `RESULTS-Z2.md` quantifies it.
- No negation, no long-range reference, no real ambiguity, no contradictions inside one leaf. Leaves are short (about 25 tokens),
  far below the 1,536 token cap, so token-based memory signals are not representative.
- A superseded fact and its replacement are two ordinary leaves; the product's version filter (POOLS 3.9) is not applied.
- A planted topic shift is easier to separate than a real one, so an injection score on G7 measures the generator as much as the
  trigger.
