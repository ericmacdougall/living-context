# living-context: experiment plan (pre-registration)

Status: written before any run, revised three times after adversarial
reviews (first review: findings F1 to F21 and M1 to M5, cited as "review
F<n>"; second review: findings F1 to F14, cited as "review 2, F<n>"; third
review: open findings F2, F7, F9, F11 and F15, cited as "review 3, F<n>";
section 13 maps each to its fix). This file
is committed alone, before the first stage that it governs. Any later change
is an amendment committed alone before the run it affects, with the reason;
results never move a threshold. The amendments this plan already requires
(measured token figures after S1, power after S3, S4a and S4b, the dataset
key listing at Step 20) are named where they arise.

Labels follow [DESIGN.md](DESIGN.md) section 0 (OWNER, AGREED, SOURCE, LAB,
ASSUMPTION, DEVIATION, COMPARISON, PENDING_REAL_TEST). MEASURED marks a figure
the designer computed on 2026-11-18 from the registered dataset file with the
earlier harness's split code; Step 20 re-computes each one in this repository
and an amendment records any difference.

## 1. What is being tested

| Id | Question | Arms compared | Primary measure |
|---|---|---|---|
| H1 | Does the owner's tree with agent routing find more gold sessions than a flat hybrid search, and not only by fusion or by preferring long sessions? | A1 vs A0 and vs A0+LEN | gold-session recall@10 |
| H1b (secondary, reported, not decided) | Does the originating agent, using the owner's tools on the digest (look up, ask, delegated `explore`), assemble a better pointer set than an agent with the same budget over flat search? | AGENTIC vs AGENT-A0 | recall@10 of the agent's final pointer set |
| H2 | Do cross-links help questions whose evidence is spread over several sessions? | A2 vs A1 | all-gold@10 on questions with 2 or more gold sessions |
| H3 | Does the feedback and reorganisation loop improve recall forward in time, beyond simply re-using history? | LOOP vs FROZEN, and LOOP vs QUERY-MEMO (E2a) | on exposed recurring queries: tree-only recall@10 for LOOP vs FROZEN; fused final-list recall@10 at the same output budget for LOOP vs QUERY-MEMO; real-question recall as a non-inferiority guard |
| H4 | Do reflection and the reverse audit repair exposure bias, and is the repair driven by past searches? | LOOP vs FROZEN vs NO-AUDIT, plants vs decoys (E2b) | share of planted leaves offered by the tree in the top 10 for their held-back probe |
| S | Do tenant isolation, access policy, erasure and versioning hold? | security track, every build step | zero failures, with stated N |
| C | How do the comparisons do? | C1 (Elasticsearch-query routing), C2 (collapsed retrieval) vs A1 | same as H1, reported only |

Cost is reported beside every recall figure: model calls by task kind, input
and output tokens, wall latency (median and p90), Elasticsearch requests per
recall, product cache hits (DESIGN 5.8; there is no evaluation cache, 10.1).

## 2. What would count as "did not hold"

### 2.1 Outcome words (used for every hypothesis)

For a difference D with a pre-registered minimum effect m and a 95% interval
[lo, hi]:

| Outcome | Rule |
|---|---|
| HOLDS | lo > 0 (and every extra condition the hypothesis lists) |
| HOLDS, BELOW MINIMUM | lo > 0 and hi < m: real but smaller than the pre-registered minimum; reported as such, never as a meaningful gain |
| DID NOT HOLD | lo <= 0 and hi < m: the interval is narrow enough to exclude the minimum |
| NOT ESTABLISHED | lo <= 0 and hi >= m: underpowered; no claim either way |
| HARM | hi < 0 |

The earlier rule "point estimate at least the minimum and lo > 0" is withdrawn:
at a true effect equal to the minimum it had at most about 50% power, so it
read underpowered nulls as failures (review F12).

### 2.2 Per hypothesis

- H1 HOLDS only if all three hold: (a) A1 minus A0 has lo > 0; (b) A1 minus
  A0+LEN (a length-prior re-ranking of A0, section 4) has lo > 0; (c) the
  share of questions with at least one gold session in A1's top 10 that came
  only from the tree source (that session's leaves are in the tree list and not
  in the direct list's top 50) has a Wilson lower bound above 0. The outcome
  word is taken from (a) and (b) together (the worse of the two); (c) failing
  makes it NOT ESTABLISHED (the gain is not shown to come from the tree). The
  share of top-10 gold contributed by each source (tree only, direct only, both,
  linked) is reported.
- H2: A2 minus A1 on all-gold@10, with the merge rule of section 5.1.
- H3 has three readings, each decided:
  - "History helps": LOOP minus FROZEN on tree-only recall@10 (the
    structural measure: did the tree itself get better).
  - "Reorganisation helps beyond memo": LOOP minus QUERY-MEMO on fused
    final-list recall@10, the first 10 distinct sessions of each arm's merged
    list (LOOP: RRF of tree, direct and linked; QUERY-MEMO: RRF of tree,
    direct, linked and memo; section 5.1), so both treatments are inside the
    measure and both arms have the same output budget of 10 sessions (review
    2, F1: on tree-only recall QUERY-MEMO's memo list could never count, so a
    perfect memo would still have lost). Only this reading supports the claim
    that the owner's reorganisation does the work. LOOP minus QUERY-MEMO on
    tree-only recall is reported as a structural measure and not decided.
  - Guard: real-question fused recall@10, LOOP minus FROZEN, lo >= -0.02. If
    the lower bound is below that, H3 FAILS ITS GUARD whatever the primary.
- H4 HOLDS only if all three hold, each with lo > 0:
  - Existence: LOOP minus FROZEN repair over all plants (paired; Tango score
    interval).
  - Cause: LOOP minus NO-AUDIT repair over plants in strata 2 and 3 as fixed
    by the prefix selection before any arm runs (section 3.6; the same plants
    in both arms; the offer audit is descriptive and changes no stratum;
    review 3, F2; paired; Tango).
  - Search-driven: LOOP repair of plants in strata 2 and 3 minus LOOP repair of
    decoys (unpaired; Newcombe hybrid score interval).
  The outcome word is the worst of the three.
- HARM is defined for every hypothesis: H1 and H2 when hi < 0 on (a) or on the
  H2 difference; H3 when hi < 0 on "history helps" or the guard fails; H4 when
  hi < 0 on existence.
- Void conditions (the hypothesis reads NOT ESTABLISHED, with the reason):
  - any control in section 6 that does not fail for its stated reason voids the
    arm it controls;
  - the opened-signal bounds of section 3.8 are violated (H3, H4);
  - the gate positive controls that cover the stage that ran the arm fail
    (section 7.3: those of S0 and S4b; H3, H4);
  - the gate adopted no candidate in LOOP (H3, H4: "the loop never changed the
    structure");
  - the RECENCY-ONLY control of section 6 exceeds its margin (H3, H4).
- Multiplicity: H1 to H4 are four separate claims, each decided at 95% with no
  family-wise correction, and each reported as such. Any statement about the
  method as a whole ("the owner's method works") uses Holm-adjusted results
  across H1 to H4, also reported (review M4).
- S: any single confirmed failure of any security probe is a BLOCK for the
  build step that introduced it, regardless of recall results. A broken
  control that passes (the probe failed to catch it) voids that probe until
  fixed.
- The expected result for H3 on real LongMemEval questions alone is near-null
  (their topics barely recur: 8 shared answer ids in the dataset, LAB
  measurement). That is why H3's primary is the constructed recurring-query
  workload, and why the real-question measure is only a guard.

## 3. Data

### 3.1 Dataset and the adapter whitelist

LongMemEval-S cleaned, 500 questions (SOURCE: the public LongMemEval release;
the file is not in this repository; its sha256 is registered in
`evalkit/dataset.py` and checked at load). 30 abstention questions are excluded
from recall metrics and reported as their own stratum (the "nothing relevant"
path). Gold: session-level `answer_session_ids`, mapped by content hash
(23,867 session references collapse to 18,464 distinct contents). Model-facing
ids are opaque (`s` + 12 hex of a salted hash); a guard refuses gold-revealing
ids in any model-facing file (LAB: every gold id begins with `answer_`).

Field whitelist (review F6). MEASURED: the records carry the keys
`question_id, question_type, question, question_date, answer,
answer_session_ids, haystack_dates, haystack_session_ids, haystack_sessions`;
each turn carries `role`, `content` and, on 10,960 of 246,750 turns, a
turn-level `has_answer` flag (896 of them true). That flag marks the evidence
turns, so it is gold. The adapter passes to the write path exactly three things
per turn: `role`, `content`, and an opaque session reference (salted hash of
the content hash). Nothing else reaches any Elasticsearch document or any
prompt: no ids, no dates, no `has_answer`, no question type. The question text
is the only model-facing field taken from the question record. Test (BUILD-PLAN
Step 20): a record carrying every original field plus a planted marker in each
non-whitelisted field is ingested and run through all eleven task kinds with
the stub's prompt capture; every Elasticsearch document and every captured
prompt is scanned; any marker is a failure. Broken control: an adapter that
copies `has_answer` into `source_ref` must turn it red. Step 20 writes the
key listing to `docs/DATA.md`. `has_answer` is used only for the
development-only leaf-level judge validation (section 8.2) and never inside
the pipeline.

Dates are not model-facing in any recall run (the time family is disabled,
section 3.4). Answer-level runs (secondary) give the reader the question date
only if J1 passes (section 8.4).

### 3.2 Splits (reuse of the earlier harness's registered split, seed 16096)

| Split | Questions | Status for this experiment |
|---|---|---|
| pilot | 102 (94 with gold, 8 abstention) | SPENT on the earlier lab. Development only. No confirmatory claim may use it |
| dev2 | 16 knowledge-update | development only |
| held_back | 19 (10 with gold) | development only (twins of pilot items) |
| fresh confirmatory | 299 test questions not in the 51-question sensitivity exclusion, plus 13 test abstention questions (312) | PROPOSED, PENDING RELEASE by the owner. Until released, no stage below S6 touches it |

Fresh-set composition (MEASURED): knowledge-update 40, multi-session
73, single-session-assistant 38, preference 23, single-session-user 48,
temporal 77; 175 non-abstention questions have 2 or more gold sessions.

Exposure of the fresh set (review M2; MEASURED, reconciling two statements in
the first version):

- The 511 gold contents of the 299 non-abstention fresh questions appear in no
  pilot or dev2 haystack (that is the sensitivity rule's definition). But 6 of
  them appear in held_back haystacks, which this plan uses for development, so
  6 fresh questions have gold that development runs will show to models. Rule
  (pre-registered): these 6 form a DEV-EXPOSED stratum, reported apart; the
  confirmatory H1 denominator is 293, and the 299 is reported as a
  sensitivity. H2's denominator is 175 minus those of the 6 with 2 or more gold
  sessions (Step 20 counts it).
- The earlier lab's pooled levels drew distractors from the whole deduplicated
  pool (its `H3 = own haystack + every pool session outside the question's
  cluster`), so fresh gold text could have been shown to models as distractors
  in pilot runs at pooled levels. The two statements are therefore both true:
  "no pilot or dev2 haystack" is about each question's own haystack; "shown as
  distractors" is about pooled levels. Which fresh contents entered a lab prompt
  is UNRESOLVED until Step 20's exposure audit, which reads the lab's committed
  prompt and result files by content hash. Rule: any fresh question whose gold
  content entered a lab prompt joins the DEV-EXPOSED stratum by an amendment
  before S6 (new n stated). No prompt in this repository is tuned on any lab
  output; prompts are tuned on this repository's development runs only.

### 3.3 Pools

| Pool | Contents | chars/4 tokens | Real tokens (x1.35, ASSUMPTION from the lab's 165k vs 122k per haystack) | Leaves at mean 1,100 tokens (ASSUMPTION) | Tree levels at fan-out 16, membership 1.3 |
|---|---|---|---|---|---|
| small real (30 pilot questions' haystacks) | about 1,400 | about 3.6M | about 4.8M | about 4,400 | 358 / 23 |
| dev static (102 pilot haystacks) | 4,600 | 11.8M | 15.9M | 14,481 | 1,177 / 74 / 5 |
| dev replay (137 development haystacks) = DEV-BASE contents | 6,018 (MEASURED) | 15.4M | 20.8M | 18,900 | 1,536 / 96 / 6 |
| whole pool | 18,464 (MEASURED) | 47.04M | 63.5M | 57,730 | 4,691 / 294 / 19 |

MEASURED composition of the whole pool: DEV-BASE 6,018; the 312 fresh blocks
add 11,016 new contents (their union is 12,666); the 51 sensitivity-excluded
test blocks add the last 1,430. 6,018 + 11,016 + 1,430 = 18,464.

One global tree per pool (the earlier lab's per-question haystack levels do
not carry over to a tree built once). Headroom over plain retrieval was
resolvable only at the whole pool in the earlier lab (+0.127 [0.057, 0.201]
for its analyst arm; H1 and H2 sizes were not resolvable), so confirmatory
static claims (E1) are made only at the whole pool.

### 3.4 Time-ordered replay for the loop (E2) (AGREED "evaluate forward in time")

The dataset's dates do not give a stream (filler dates are synthetic per
question; 3,942 session ids carry different dates in different questions;
LAB re-measured). The replay is therefore constructed (ASSUMPTION, not the
owner's words). It is built once per stream by `evalkit` before any arm runs,
and every arm replays the same event list.

1. Blocks. A block is one question's own haystack sessions in their own date
   order. Seeded shuffle (seed 16096) of blocks.
2. Lagged, interleaved insertion (review F5). The stream is a list of insert
   events and question events. Each block's sessions are inserted
   interleaved with the next block's (alternating sessions, seeded), and a
   block's question is asked only after at least `L = 5` later blocks have been
   fully inserted (pre-registered; `L` is set per stream, E2a and E2b, and
   section 6 says when it may change). Contents already present are skipped
   (content hash). So the newest contents at question time are mostly other
   blocks', not that question's gold.
3. Exploration slots (DESIGN 9) follow the same policy and seed sequence in
   every agent-routed arm, FROZEN included, and their candidates exclude leaves
   inserted within the last `L` blocks (the stream's own `L`). Exploration slots are logged and may
   feed learning, but every recall metric is computed on the ranked list with
   exploration slots removed.
4. Holders never see `last_seen` (DESIGN 8.2 removes it from holder-visible
   child entries), so recency cannot reach routing through that field.
5. Each question is scored at its position before any learning from it.
   Reflection, reorganisation and the reverse audit run only on log rows from
   earlier positions. Learning runs in cycles of `c` stream items (E2a: 50 in
   S7a, 25 in S4a; E2b: 20).
6. Day-granularity as-of rule (a strict timestamp rule drops 70 of the 75 gold
   sessions dated hours after their question; LAB).
7. The time family (DESIGN 5.3) is disabled in both replays because the dates
   are synthetic. Topic and entity families are used.

Streams:

- E2a (H3). Development S4a: the 137 development blocks, starting from an empty
  store; the first 20 blocks are burn-in and not scored. Its FROZEN end state is
  DEV-BASE. Confirmatory S7a: every arm starts from DEV-BASE (6,018 contents);
  the 312 fresh blocks and, as unscored filler (inserted, question never asked),
  the 51 excluded blocks are streamed, ending at the whole pool, 18,464, the
  same pool as S6 (review M2).
- E2b (H4). A planted-items stream (section 3.6). S4b: base = the development
  blocks not chosen as S4b targets (43 blocks, inserted with normal batch
  rebuilds and no recalls), then 94 target and filler blocks. S7b: base =
  DEV-BASE (built alone when S4a has not run, section 10), then 160 fresh
  target and filler blocks.

### 3.5 Constructed recurring-query workload for E2a (ASSUMPTION)

The benchmark gives the loop almost nothing to learn from, so:

- For each fresh (or development) question with gold, one generation call
  writes one related question from the source question and its answer only,
  never from session text (review F3c, F7). It is tagged synthetic. Generation
  happens before the stream, by a process that never feeds the learner.
- Rank filter: over the stream's end pool, if any leaf of a gold session is in
  the BM25 top 10 or the dense top 10 for the recurring query, the query is
  regenerated (at most 3 attempts) and otherwise dropped. Drops are counted and
  reported.
- Schedule (review F7): placed uniformly at random 2c to 4c stream items after
  its source question (seeded), so at least one learning cycle that its source
  question could shape has completed; queries that would land past the end of
  the stream are dropped. Expected kept: about 210 of 299 (S7a), about 85 of
  120 (S4a).
- Exposed flag (pre-registered rule, computed from the run's own logs): a
  recurring query is EXPOSED in an arm if a candidate generation whose
  `lin_queries` includes its source question's search id was adopted before
  the recurring query was asked. Exposed queries are the primary stratum for
  H3 (in LOOP; the same queries form the stratum in every other arm). If fewer
  than 30 recurring queries are exposed, H3 reads NOT ESTABLISHED
  (underpowered).
- When asked, a recurring query is scored first and only afterwards becomes
  history the learner may use, like any other query.

### 3.6 Planted mis-filed leaves for E2b (direct test of exposure-bias repair, H4)

Selection, before the stream, by `evalkit` only (the learner never sees the
list):

- Targets: P plant questions and D decoy questions among fresh (or
  development) questions with gold, plus F filler questions. S7b: P = 60,
  D = 20, F = 80. S4b: P = 40, D = 14, F = 40 (seeded).
- Probes, generated from the source question and its answer only, never from
  session text: probe 1 (asked in the stream after its source question, at
  2c to 4c items later, learning may follow; plants only) and probe 2 (asked
  only at the end checkpoint; plants and decoys).
- Rank filter on probe 2: over the E2b end pool, the planted leaf must rank
  below 10 in BM25 and below 10 in dense for probe 2; at most 3 regenerations,
  otherwise the target is replaced by the next seeded candidate. Drops and
  replacements are reported.
- Strata by the best (lowest) fused direct-search (A0) rank of any planted
  leaf over every in-stream search the plant is gold for (its source question
  and probe 1), each rank computed against the actual corpus prefix at that
  search's scheduled position (every content inserted before it), not the end
  pool (review 2, F2: a leaf that ranks fourth when asked and 24th in the end
  pool was offered). Stream construction replays A0 over each prefix; A0
  depends only on leaf text and embeddings, so the rank is the same in every
  arm. Stratum 1: best rank 1 to 10; stratum 2: 11 to 50; stratum 3: above
  50. The strata are fixed by this selection, before any arm runs, and do not
  depend on any arm's outcome (review 3, F2); they are the confirmatory
  population of the cause and search-driven comparisons in every arm, and no
  later observation moves a plant between strata or removes it. They do not
  guarantee that no source offered the plant (the digest's direct section is
  the top 10 not already offered by the tree, DESIGN 8.5, which can reach
  below direct rank 10, and the fused list cuts each list at 50, 5.1); the
  offer audit below reports what was actually offered, as description only.
  Plants are chosen with quotas S7b 12 / 24 / 24 and S4b 8 / 16 / 16 (if a
  stratum cannot fill, the shortfall is reported and filled from the next
  stratum up). Pre-registered predictions: in stratum 1 the direct-search
  pool (b) of reflection may repair; in strata 2 and 3 mostly only the
  deeper pool (c) and the reverse audit can, so the cause comparison (LOOP
  minus NO-AUDIT on strata 2 and 3) measures what pool (c) and the audit add
  over everything NO-AUDIT keeps, including any exposure it had; stratum 3 is
  where the reverse audit's rank-independent percolation and query-vector kNN
  should matter most. Stratum 1 is reported but excluded from the cause and
  search-driven comparisons.
- Offer audit (review 2, F2; descriptive only since review 3, F2), from the
  protocol logs (DESIGN 9) after the end checkpoint: per arm, stratum and
  source, the plants that any logged search before the end checkpoint (a
  reader's `explore` call is a recall and is logged as one) offered through
  the tree, the digest's direct section (DESIGN 8.5, reflection's pool (b)),
  an exploration slot, or a reader's open or `ask`, with the count of
  offer-row writes that failed (DESIGN 9 counts them; the audit cannot see
  what they offered). Offers that only feed the fused ranking of 5.1 (a
  direct rank below the direct section, a linked offer never opened) are
  counted apart. Decoys' offers are counted the same way. The audit is
  mechanism evidence: it explains how repairs happened in each arm and never
  moves a plant between strata, removes it from the confirmatory population,
  or enters any H4 decision (it would otherwise condition the comparison on
  exposure, which the compared mechanisms themselves change).
- Eligibility (review 2, F3), reported separately from repair: per arm and
  stratum, the share of plants with at least `S_MIN = 2` distinct logged
  searches judged relevant by the run's judge and missed by the tree before
  the end checkpoint (the reverse audit's re-filing support, DESIGN 12). Each
  plant has exactly two scheduled in-stream searches (its source question and
  probe 1), plus any filler search that happens to be judged relevant, so a
  plant is eligible for the audit only if the tree missed it both times.
  Decoys have no constructed relevant search and are expected to be
  ineligible; any decoy that becomes eligible is reported.

Planting (review F3a, F3b):

- When a target block is inserted, the leaves of its chosen gold session are
  placed under the least-similar level-1 node instead of the placement rule's
  choice (the "host"). Each planted leaf's child entry gets plausible wrong
  metadata: the code-built title and the gist of a real child of the host
  node (the "donor sibling", seeded) in place of its own, not a marker such
  as "misc notes". This changes only what holders see; a pointer to the
  plant still carries its own stored title (DESIGN INV-7). Text is unchanged,
  so the direct search can still find it (the safety net is part of the
  system).
- Pinning: in every arm equally, normal batch rebuilds (DESIGN 7.6) neither
  regenerate the planted entry's title and gist nor move its membership; a
  split or merge carries it with its donor sibling. Only feedback-driven
  operations (DESIGN 11.2) may change them. Plant survival (planted member,
  title and gist unchanged at the end) is logged per arm.
- Decoys are planted the same way, but their source question is never asked
  and they have no probe 1: no constructed past search is relevant to them. If
  LOOP repairs decoys about as often as plants, the repair is not driven by
  search history and H4's search-driven condition fails.
- Fillers are ordinary blocks whose questions are asked and scored for
  nothing; they give the learner unrelated history.

Measurement (review F3e):

- At the end checkpoint, reflection, the reverse audit, the reorganiser and the
  gate are stopped. Probe-2 recalls write no learner log row and never enter the
  stored-query index; they are recorded in the evaluation output only.
- Repair = at least one leaf of the planted session is offered by the TREE
  source within the first 10 distinct sessions for probe 2 (exploration slots
  removed). Reported per arm and stratum with Wilson intervals and as paired
  differences. This held-back tree retrieval is the only definition of
  repair in this plan (review 3, F2): no metadata or membership change counts
  as a repair, and nothing else ends an observation window.

### 3.7 Leakage rules

- Reflection, the judge, the reorganiser, the reverse audit and the gate never
  see gold labels, `has_answer`, the source question of a recurring query, the
  planting, decoy or stratum lists, any probe 2, or any query before it is
  scored.
- Gate replay sets come only from held-out searches and gate-only synthetic
  queries (section 7.3); gate synthetic queries never enter the stored-query
  index and are never probes.
- Evaluation queries and their labels never enter the stored-query set before
  they are asked; probe 2 never does.
- No simulated opener or reader may depend on gold, the planting list or
  stratum (section 3.8).
- Development choices (thresholds, prompts, the merge rule, `L`, `c`, the
  opened-share bounds) are frozen in a commit before the first confirmatory
  stage that uses each of them, and never change after that commit. Under
  the default order (10.3) this means: S3 freezes what S6-H1 uses; the
  edge-typing prompt and the expansion settings S6-H2 uses (caps, degree cap,
  hub nodes) are frozen before S6-H2; S4b freezes the loop (gate code,
  judge, prompts, thresholds) and E2b's `L` and `c` before S7b; S4a can then fix only what E2a alone uses (E2a's `L` and `c`,
  the QUERY-MEMO threshold) before S7a, because 7.3 requires S7a to run the
  gate S4b froze. Each freeze commit is named in the results. The run driver
  refuses to start a confirmatory stage whose registered values differ from
  its freeze commit, and refuses a second stamped run of a confirmatory
  stage; resuming an interrupted run from its own checkpoint on the same
  commit is not a second run.

### 3.8 The reader in the stream and the opened signal (review F1)

The owner's "tracking if the actor actually did a lookup" needs an actor. In
E2a and E2b, every stream question (source questions, fillers, recurring
queries, probe 1) gets one reader session per agent-routed arm that produces
signals (LOOP, NO-AUDIT and FROZEN; FROZEN's opens feed only QUERY-MEMO and
the report). RANDOM-FEEDBACK replaces signals by random draws and runs no
reader.

- Reader input: the digest only, plus the tools `lookup`, `ask` and `explore`
  (DESIGN 8.4); at most 3 model calls and 8 lookups per session (ASSUMPTION);
  instruction: answer the question. No gold, planting or stratum information.
  `opened` is written by the gateway when `lookup` or `ask` serves a leaf.
- S0 stub reader: sees only the digest (pointers carry no gist, DESIGN INV-7)
  and opens the pointers whose title scores in its own top 3 (a scorer
  separate from the router), deterministic and gold-blind.
- Pre-registered bounds: per arm, the mean share of offered pointers opened per
  recall must lie in [0.05, 0.60]. Outside, the opened signal is void, and H3
  and H4 read NOT ESTABLISHED (signal void). This catches both a reader that
  opens nothing and a reader that opens everything (which would bring back the
  exposure-biased co-offer signal, DESIGN 11.1).
- Interpretation (review 3, F11): `opened` records lookups, not use. Titles
  and reasons are descriptors derived from content and can convey an answer
  (DESIGN INV-7, 20), so a reader can use a pointer without opening it; "not
  opened" never means "not used" and is never counted as evidence of
  irrelevance by itself, in any measure or learner signal. The opened-share
  bounds above test that the actor looks things up, not what it used.
- Reported per arm (review 2, F14: these are recall-level quantities, not
  independent binary trials): opens per recall, mean opened share per recall
  and `ask` share of opens, each with a percentile bootstrap interval over
  recalls (10,000 resamples, seed 16096); the share of `used_behavioural` rows
  in the behavioural-use sample, a binary outcome per sampled row, with a
  Wilson interval; the sample is drawn from every offered row of the sampled
  recalls, opened or not (DESIGN 9), so use through a descriptor alone can
  be seen.

## 4. Arms (each adds one thing)

| Arm | Adds | Notes |
|---|---|---|
| A0 | flat hybrid over leaves: BM25 + kNN, client-side RRF k = 60 | re-run in this harness on the same leaves; earlier-lab figures are context only, never baselines |
| A0+LEN | A0 re-ranked with a length prior: RRF score + beta x log(session tokens), beta fitted on development (S3) and frozen | control for H1 (review F10): session length predicts gold (LAB, AUROC 0.687) and holders see sizes (OWNER "size and total size") |
| A1 | + topic and entity trees with agent routing (OWNER read path, DESIGN 8.2 to 8.5); the direct search of A0 runs in parallel and is fused by the merge rule (AGREED) | A1-tree-only (no direct list) is reported, and is the list SHUFFLED-SUMMARIES runs on |
| A2 | + bounded link expansion over typed edges (DESIGN 8.6), merged as a third list | uses A1's routing outputs; no extra routing calls |
| AGENTIC (H1b) | A1's digest given to the originating agent, which may call `lookup`, `ask` and up to 3 `explore` calls (DESIGN 8.4; each at most 12 holder calls) and returns its final ranked pointer set | the owner's agent-side method; secondary |
| AGENT-A0 (H1b) | the same agent prompt and call cap over A0's list, with a `search(query)` tool over A0 instead of `explore` | cost-matched comparison for AGENTIC |
| LOOP (= A3) | + protocol log, reader, reflection, reverse audit, reorganisation with the replay gate (E2a, E2b) | the owner's loop. Reverse audit uses real past searches only; synthetic queries off (DESIGN 12 step 1(c), owner's "what searches happened") |
| FROZEN | A2 in E2 with inserts, normal batch rebuilds and the reader, but no feedback-driven reorganisation | comparison for LOOP |
| QUERY-MEMO (E2a) | FROZEN's structure and recalls, plus a memo list: for a new query, the items opened (FROZEN's own log) or judged relevant (a shadow reflection over FROZEN's searches with LOOP's pools and judge) for the nearest past query by query-embedding cosine (threshold fixed on S4a), merged as a fourth RRF list | history without reorganisation (review F7). Reads FROZEN's logs, never LOOP's |
| NO-AUDIT (E2b) | LOOP without reflection's deeper pool (c) and without the reverse audit; pools (a) and (b) and the opened signal kept | the cause control for H4 |
| LOOP+SYNTH (E2b, proxy only) | LOOP plus the reverse audit's synthetic queries (DESIGN 12 step 1(c)) | ASSUMPTION addition, measured apart from the owner's reverse search (review F3d) |
| C1 | COMPARISON: A1 with each holder call replaced by an Elasticsearch hybrid query over that holder's children | coordinator's suggestion, not adopted by the owner |
| C2 | COMPARISON: collapsed retrieval over all node summaries and leaves in one pool | research comparison |
| ORACLE | gold sessions first | answer-level ceiling only |

Ablations on development only (no confirmatory claim): leaf cap 768 vs 1,536;
membership 1 vs 2; organise at every parent vs default; bounded BFS vs
personalised PageRank; summaries with vs without extracted facts; LLM vs
embedding-valley segmentation. They are not needed for H1 and run after
S6-H1 (BUILD-PLAN 3A), each under its own approval and call cap; they are not
counted in 10.3 and cannot change anything already frozen (3.7).

### 4.1 Pointers only, lookups logged

Every arm returns pointers only, with their descriptors (title, size, path,
reason, confidence); no arm forces a lookup to reach content, because a
descriptor can convey an answer (DESIGN INV-7, 20). Recall metrics are computed from the offered
pointer list (exploration slots removed). Answer-level runs (secondary) and
the stream reader (section 3.8) give the reader only the digest and the
`lookup`, `ask`, `explore` tools, and `opened` is taken from the gateway's log,
never from the reader's claim. Tool use is counted per question (LAB: 0.55
compliance when tools were optional).

### 4.2 The proxy world (DEVIATION 7, review F8)

For cost, supporting runs use C1 routing in place of agent routing:
LOOP-proxy, FROZEN-proxy, NO-AUDIT-proxy, RANDOM-FEEDBACK-proxy,
SHUFFLED-LOG-proxy, LOOP+SYNTH-proxy. C1 is the coordinator's suggestion that
the owner did not adopt, so the proxy world is a DEVIATION (DESIGN 22), labelled
so in every result. It decides nothing: every deciding control and comparison
runs agent-routed (sections 2.2 and 6). Proxy results are reported as
supporting evidence and never compared with agent-routed results.

## 5. Metrics

### 5.1 Merge rule (pre-registered now, frozen before S2; review F9)

- Lists: tree (holders' leaf pointers, ordered by holder confidence, then
  shallower depth, then path order), direct (A0's fused list), linked (expanded
  leaves ordered by edge score, DESIGN 8.6), memo (QUERY-MEMO only).
- Each list is cut at 50 and fused by client-side RRF with k = 60; ties are
  broken by a salted hash of the leaf id. A0 = direct; A1 = RRF(tree, direct);
  A2 = RRF(tree, direct, linked); QUERY-MEMO = RRF(tree, direct, linked, memo).
- Diagnostics reported: each list's own recall@10, and A2 with linked appended
  last (an alternative merge, not decided).

### 5.2 Measures

- Gold-session recall@10: map each offered leaf to its session content hash;
  take the first 10 distinct sessions in merged order; recall = share of gold
  sessions among them. Mean over questions with gold. A question whose recall
  call failed (timeout, typed failure, budget refusal) scores 0 and is
  counted (INV-11).
- Tree-only recall@10: the same on the tree list alone (H3 "history helps",
  LOOP vs FROZEN; also reported for every arm).
- Fused final-list recall@10: gold-session recall@10 above on each arm's own
  merged list (5.1), the same 10-session output budget for every arm (H3
  "beyond memo", LOOP vs QUERY-MEMO).
- All-gold@10: 1 if every gold session is in the first 10, on questions with
  2 or more gold sessions.
- Agentic recall@10 (H1b): recall@10 of the agent's final ranked pointer set.
- Also reported: recall@5, @20, @50; knowledge-update gold-latest hit (stale
  only counts as a miss); per-type strata; the DEV-EXPOSED stratum; the
  low-overlap stratum (best gold round worse than rank 100 on both BM25 and
  dense at the whole pool), computed on the fresh set only after release.
- Size-matched control for every recall gain: a random list matched in number
  of sessions and total characters to the arm's list (LAB: session length
  predicts gold, AUROC 0.687; length matching cut earlier gains by about a
  quarter). A0+LEN is the deciding length control for H1.
- Holder reasons and outline labels: share rejected by the 5-gram check
  (DESIGN 8.2, 8.3), per task and arm; titles are code-built (DESIGN INV-7)
  and need no check. Opens, opened share and `ask` share are reported as
  lookup behaviour, never as relevance or use: a descriptor can convey an
  answer, so "not opened" is never evidence of irrelevance by itself (3.8;
  review 3, F11).
- Cost: model calls per recall by task kind, input and output tokens, wall
  latency p50 and p90, Elasticsearch requests, holder calls that hit the
  budget, fallbacks, hallucinated ids, failures, product cache hits.
- Answer accuracy (secondary): only reported if the answer judge passes J1
  (section 8.4); never used for a decision.

## 6. Controls that must fail for their stated reason

Each control has a pre-registered expected failure and a check that it failed
for that reason (not a crash). A control that does not fail voids its arm;
NO-AUDIT, the comparator of the cause test, has a switch check instead
(review 3, F2). All
deciding controls run agent-routed; the proxy world only supports.

| Control | For | Broken change | Must show (decision rule) | Check that it is the stated reason |
|---|---|---|---|---|
| SHUFFLED-SUMMARIES | A1-tree-only (no direct list, so the safety net cannot mask the drop) | node summaries and child entries permuted among nodes of the same level | drop in A1-tree-only recall@10 with point at least 0.10 and clustered-bootstrap lower bound above 0, on 50 questions (stub gate: 0.30 on the fixture) | holder logs show selections following the shuffled text |
| RANDOM-EDGES | A2 | edges rewired to random leaves of the same class, degree kept | A2's gain over A1 on all-gold@10 disappears: upper bound of (A2-RANDOM minus A1) below the A2 minus A1 point estimate, and point at most +0.01 | expanded leaves are non-gold at the base rate |
| RANDOM-FEEDBACK | LOOP (E2a and E2b, agent-routed) | opened, used and judge signals replaced by random draws at LOOP's measured rates; no reader | E2a: RANDOM-FEEDBACK minus FROZEN on the "history helps" measure (tree-only recall@10) has hi < m; E2b: its repair in strata 2 and 3 at most 0.10. Valid only if the gate positive controls that cover that stage held (7.3) | gate decisions and adopted operations logged; repairs, if any, traced to random signals |
| SHUFFLED-LOG | LOOP (proxy only, supporting) | log rows reassigned to random searches | as RANDOM-FEEDBACK, reported | same |
| NO-AUDIT | H4 cause (E2b, agent-routed) | reflection's deep pool (c) and the reverse audit removed; pools (a), (b) and opened kept | the switch holds, checked in NO-AUDIT's own logs: no NO-AUDIT note, candidate or adopted operation comes from pool (c) or the reverse audit; a trace to either voids the arm. Its repair in strata 2 and 3 (prediction: at most 0.10) is reported with the offer audit's exposure counts and is not a void condition (review 3, F2: on the fixed strata NO-AUDIT may legitimately repair a plant it saw through pool (b) or an open, and that success belongs in the cause comparison) | every NO-AUDIT repair traces to a pool (a) or (b) note, an opened signal, or no adopted operation at all (a probe-2 retrieval like FROZEN's, through normal rebuilds; counted apart and compared with FROZEN's rate) |
| FROZEN-CEILING | E2b plants | none (FROZEN as run) | FROZEN repair over all plants at most 0.10 | any FROZEN repair traced to normal rebuilds; plant survival logged |
| DECOY | H4 search-driven | decoy plants (no relevant constructed search) | the search-driven condition of 2.2 | decoy repairs traced to their signals |
| RECENCY-ONLY | E2a and E2b replay | offer the 10 most recently inserted leaves' sessions | upper 95% bound of (RECENCY-ONLY minus RANDOM-10) recall@10 at most 0.03. `L` is per stream: a failure on a stream's development stage (S4b for E2b, S4a for E2a) may raise that stream's `L` by an amendment in its freeze commit (3.7), before its confirmatory stage (S7b, S7a); after that stage has started its `L` never moves, and a confirmatory replay that fails is reported VOID and is not rerun (the run driver refuses both, 3.7) | the 10 most recent contents logged per question |
| NO-EXPLORATION | H4 | exploration slots off (proxy) | repair lower than LOOP-proxy | reported, not gating |
| SAME-JUDGE | reflection | judge = the routing prompt family | inflated judged-relevance agreement with the router | reported; a disclosed limitation of H4 if the judge shares the router's model (section 8.1) |
| CITED-VS-BEHAVIOURAL | log | compare `used_cited` with `used_behavioural` on a sample of 100 | they disagree on at least some items | reported |
| BM25-ONLY | A0 | lexical only | reported | reported |

## 7. Statistics (numbers fixed now)

### 7.1 Differences between arms on questions

- Clustered paired bootstrap over questions, resampling whole content clusters
  with replacement (LAB rule: questions that share gold content move
  together), percentile 95% intervals, 10,000 resamples, seed 16096. The
  effective n (number of clusters) is printed beside every interval; BCa
  intervals are reported beside as a sensitivity.
- Paired binary differences (H4 existence and cause): Tango's score interval
  for the difference of paired proportions. Unpaired binary differences (H4
  plants vs decoys): Newcombe's hybrid score interval. Plants sharing a gold
  content are merged into one cluster first.
- Single rates of binary outcomes: Wilson 95% intervals; never used for
  differences. Counts and shares per recall use the bootstrap over recalls
  (3.8; review 2, F14).

### 7.2 Minimum effects and power

- Minimum effects: H1 +0.05, H2 +0.05, H3 +0.05 with non-inferiority margin
  -0.02, H4 +0.25. They define DID NOT HOLD and NOT ESTABLISHED (2.1); they
  are no longer a second pass condition.
- Sample sizes: H1 n = 293 primary (299 sensitivity; 312 with abstention,
  reported apart); H2 n = up to 175; H3 the exposed recurring queries
  (expected 60 to 150 of about 210) and 312 real questions; H4 48 plants in
  strata 2 and 3, fixed by the prefix selection before any arm runs (the
  offer audit of 3.6 is descriptive and changes no count; review 3, F2), 60
  in all, 20 decoys.
- Provisional power (review F12): per-question differences of recall@10 lie
  close to {-1, 0, 1}, so their standard deviation is about the square root of
  the discordance rate d. With d = 0.2, sd is about 0.45, the 95% half-width
  at n = 293 about 0.05, and the true effect with 80% power about 0.074 (H1);
  for all-gold@10 at n = 175 about 0.095 (H2); for H3 at 100 exposed queries
  about 0.126; for H4 at 48 paired plants with d = 0.4, about 0.25. Clustering
  lowers the effective n further. So the H1 and H2 minimum of +0.05 is below
  the 80%-power effect: an interval wider than the minimum will read NOT
  ESTABLISHED, which is the honest reading.
- Amendments: S3 measures d and the cluster count for H1 and H2, S4a for H3,
  S4b for H4; each power figure is recomputed from them and committed as an
  amendment before the confirmatory stage it concerns, stating the true effect at which power reaches
  80%. Minimum effects do not move.
- The low-overlap stratum (about 80 questions expected) resolves only about
  0.13 at d = 0.2 and is reported, not decided.

### 7.3 The replay gate inside the loop (DESIGN 13.3; review F4, F12d; review 2, F12; review 3)

- What the gate is (review 3): an inspectable heuristic that decides which
  candidates the loop adopts, not a 5% false-adoption guarantee. Its p-value
  is exact only under the symmetry and independence assumptions below, and
  neither the p-value nor RANDOM-FEEDBACK (section 6) is ever cited as proof
  of a bounded false-adoption rate. Sign scoring counts how often clusters
  improve, not by how much: frequent tiny gains can outweigh rare large
  losses in the gate while mean recall falls. So the outcomes of H3 and H4
  rest only on their independent measures (2.2: forward recall on the
  stream for H3, held-back probe-2 repair for H4), never on the gate's
  statistics; the gate is part of the fixed learning algorithm those
  measures evaluate.
- One method, fixed horizon, evaluated once per candidate (no repeated
  looks). It replaces the betting confidence sequence of the previous
  revision, which took each search as an independent observation: sixty
  paraphrases of one changed leaf that all succeed or all fail together are
  one observation, not sixty (review 2, F12).
- Statistical unit: the cluster. One cluster per operation of the candidate
  (move, added membership, re-summary, split or merge, edge change), holding
  every replay query generated from a leaf that operation changed and every
  held-out search whose judged-relevant items include such a leaf;
  operations sharing a replay query are merged, so dependent queries stay
  together. At most 60 targeted queries, spread over as many clusters as
  possible (one per cluster in turn, held-out searches first), at most 6 per
  cluster (ASSUMPTION). A candidate is the whole batch of one learning
  cycle's operations.
- Decision formula (DESIGN 13.3). Per query q, d_q = sign(r_cand(q) -
  r_curr(q)) in {-1, 0, +1}, r = tree-source recall of judged-relevant items
  within the top 10. For clusters k = 1..K with n_k queries:

  ```
  D_k = (1/n_k) * sum over q in k of d_q          cluster-mean paired difference
  T   = (1/K)   * sum over k of D_k               mean over clusters
  p   = #{ e in {-1,+1}^K : (1/K) * sum_k e_k * D_k >= T } / 2^K
  ```

  Adopt only if K >= 8, p < 0.05 and the general replay guard below passes.
  p is exact (counted, not sampled; at most 120 K + 1 distinct sums because
  n_k <= 6). Validity: exact when the cluster means are symmetric about 0
  under no change and clusters are independent of each other; any dependence
  inside a cluster is allowed. Residual assumption: clusters that share no
  query are independent (two operations that touch nearby nodes may still
  interact).
- Simulation (`evalkit/gate_power.py`, plain Python, seed 16096, 4,000 runs
  per cell; model of per-query signs: +1 with probability p+, -1 with p-,
  else 0, either shared by every query of a cluster (fully correlated) or
  drawn per query (independent)). Printed results:
  - Null coverage under these symmetric sign models only (p+ = p-; target at
    most 0.05, met in every printed cell; not a guarantee for other
    distributions, see the asymmetric null below): largest observed
    adoption 0.0413 (10 held-out singletons plus 10 leaf clusters of 5,
    independent within cluster, p+ = p- = 0.20); 60 single-query clusters
    0.0140 / 0.0300 / 0.0370 at p+ = p- = 0.05 / 0.20 / 0.30; 10 clusters of 6
    fully correlated 0.0000 / 0.0103 / 0.0185; 8 clusters of 6 fully
    correlated 0.0000 / 0.0073 / 0.0123. One cluster of 60 fully correlated
    paraphrases never adopts (K = 1 < 8); the same 60 tested per query would
    adopt 0.1993 of the time, and 10 fully correlated clusters of 6 tested
    per query 0.2410, which is the defect this method removes.
  - Asymmetric null (mean-zero cluster means that are not symmetric about 0,
    so the test's assumption fails; printed separately). Rare large gains
    (+0.8 with probability 0.2, -0.2 otherwise): 0.0067 / 0.0070 / 0.0077 /
    0.0187 at K = 8 / 10 / 20 / 60, conservative. Rare large losses (+0.2
    with probability 0.8, -0.8 otherwise): 0.1715 / 0.1045 / 0.0698 / 0.0673,
    above 0.05 at every K. This is a stated limit, not a guarantee: a
    candidate that helps most clusters a little and badly hurts a few can be
    adopted when its losses do not show up in the replayed clusters. A test
    valid for every mean-zero distribution of cluster means in [-1, 1] (a
    Hoeffding bound) would need a mean T of at least 0.87 at K = 8, 0.55 at
    K = 20 and 0.32 at K = 60 (printed by the script) and would almost never
    adopt at the K a candidate reaches, so it is not used. Nothing bounds
    the false-adoption rate: the general replay guard limits visible losses
    on recent searches, the count of adoptions per arm is reported,
    RANDOM-FEEDBACK (section 6) shows what adoptions driven by random
    signals do to recall, and S4a and S4b report the distribution of cluster
    means of rejected and adopted candidates; the effect of any false
    adoption on recall is seen only in the independent measures of H3 and H4.
  - Power (p- = 0.05; p+ = 0.20 / 0.30 / 0.40 / 0.60): 60 single-query
    clusters 0.70 / 0.96 / 1.00 / 1.00; 20 clusters of 3, fully correlated
    0.19 / 0.45 / 0.70 / 0.96, independent 0.68 / 0.94 / 1.00 / 1.00; 10
    clusters of 6, fully correlated 0.02 / 0.11 / 0.26 / 0.61, independent
    0.62 / 0.92 / 0.99 / 1.00; 8 clusters of 6, fully correlated 0.01 / 0.05 /
    0.14 / 0.45, independent 0.47 / 0.79 / 0.96 / 1.00; 10 held-out singletons
    plus 10 leaf clusters of 5, fully correlated 0.18 / 0.44 / 0.71 / 0.95,
    independent 0.54 / 0.82 / 0.94 / 1.00. An oracle-like candidate (each
    cluster improves with probability 0.8, never worsens, fully correlated,
    clusters of 3) is adopted with probability 0.95 / 1.00 / 1.00 at K = 8 /
    16 / 40.
  - Hence: power comes from the number of clusters, not the number of
    queries, which is why queries are spread over clusters first. A candidate
    that changes few independent things, or whose replay queries move
    together, will rarely be adopted; "the gate adopted no candidate" voids
    H3 and H4 (2.2) rather than reading as a loop failure. S4a and S4b measure
    K per candidate and the within-cluster agreement of signs, and the power
    amendment of 7.2 restates the gate's power from them.
  - Repeated candidates: each candidate is tested once, but a learning arm
    tests one candidate per cycle (S4b 3 arms x 6 cycles = 18, S7b 3 x 10 =
    30). At a per-candidate null adoption of 0.02 the chance of at least one
    false adoption is 0.30 over 18 null candidates and 0.45 over 30 (printed
    by the script); under the rare-large-losses pattern above it is far
    higher. The number of adoptions per arm is reported; RANDOM-FEEDBACK
    (section 6) shows what adoptions driven by random signals do to recall
    and is not a bound on the false-adoption rate.
  - Cost of one exact evaluation at K = 60: under 0.1 s (the script prints
    the slowest of 200; runs gave 0.006 to 0.071 s, depending on the
    machine and its load). With
    raw fractional recall differences instead of signs, a trial evaluation at
    K = 60 took up to about 100 s, which is why the sign is the registered
    score.
- Targeted replay, because a general replay of 20 unrelated searches carries
  a mean near 0 and could never adopt anything. Targeted replay set per
  candidate: held-out logged searches whose
  judged-relevant items include a leaf the candidate changed, topped up to 60
  with gate-only synthetic queries generated from the changed leaves,
  judge-filtered, tagged synthetic, never stored, never probes, grouped into
  clusters as above.
- Unjudged items that the candidate offers are judged at replay by the same
  blind judge, inside the budget (DESIGN 13.3), so new items are neither
  assumed irrelevant nor assumed relevant.
- General replay guard: the 20 most recent held-out logged searches; the guard
  passes when at most 2 of them lose a judged-relevant item from the top 10 and
  losses do not exceed gains (ASSUMPTION; a count rule, because a 20-search
  interval cannot resolve a -0.02 margin).
- Positive controls, in S0 (stub) and again in S4b: a known-good candidate (an
  oracle re-filing of the planted leaves to the nodes the placement rule would
  have chosen; one move per plant, so one cluster per plant, and the S0
  fixture has at least 16 plants so that K >= 16) must be adopted, and a known-bad candidate (the same number of
  random moves) must be rejected. E2a has no plants, so the gate's positive
  controls for E2a are those of S0 and S4b: the gate code, judge, prompts and
  thresholds are the same frozen ones. Hence S4b must have passed before S4a's
  results are used and before S7a starts, whatever order X6 chooses for the
  other stages. If either control fails, the LOOP-family controls of every E2
  stage after it are void and H3 and H4 read NOT ESTABLISHED.

### 7.4 Security rates

- Zero-tolerance security rates: with 0 failures in N trials the Wilson 95%
  upper bound is 3.84 / (N + 3.84): 3.7% at N = 100, 1.26% at N = 300, 0.38% at
  N = 1,000. Each probe states its N.
- Development numbers (S2 to S5) are exploratory; no decision is made on them.

## 8. Judges

### 8.1 Relevance judge for reflection, the audit and the gate

Separate prompt family from the holder and organise calls, pinned model id,
at most 5 leaves per call for one query (ASSUMPTION; validated in that form).
Model (review M5; open decision X5): by default a different pinned model from
the router's, of no higher tier, provided it passes 8.2; if no such model
passes, the router's model with a different prompt family, and SAME-JUDGE
agreement is then reported as a disclosed limitation of H4 (correlated blind
spots can hide "relevant, never offered" items).

### 8.2 Validation against gold (development only)

- Leaf level (the level at which the judge labels in reflection, the audit and
  the gate): 400 development (query, leaf) pairs, half leaves containing a
  `has_answer` turn of a gold session and half hard negatives (non-evidence
  leaves of the same gold sessions and leaves from the top of A0's list). The
  judge must reach Cohen's kappa at least 0.60 and recall of evidence leaves at
  least 0.80. `has_answer` is used here only, on development questions only.
- Session level, as before: 200 development (query, session) pairs, half gold
  and half hard negatives from the top of A0's list; kappa at least 0.60 and
  recall of gold at least 0.80 (SOURCE for the scale: best prompt kappa 0.64,
  paraphrase spread 0.50 to 0.72, https://arxiv.org/html/2309.10621v3).
- If either fails, the loop runs are not started. The judge's prompts are not
  tuned on the fresh set.

### 8.3 Paraphrase spread

Three paraphrases of the judge prompt are run on the same pairs; the spread of
kappa is reported.

### 8.4 Answer judge (J1, secondary)

Answer-level claims need a human-labelled sample (160 items) the earlier lab
never ran. Without it, answer accuracy is not reported (open question for the
owner: who labels).

## 9. Security track (runs from the first build step, not after recall)

Every probe runs against the real gateway and real Elasticsearch with
security on, drives the real writer (LAB-R9), asserts the number of checks run
and no skips (LAB-R8), and has a broken-implementation control that must turn
it red for its stated reason. Fixture: tenants `tena` and `tenb`, each with classes
`base` < `restricted`, plus a third tenant `tenbx` whose name extends `tenb`
(look-alike; ASSUMPTION), each with planted marker strings and marker vectors
in every artefact kind, including authz records and query text.

Byte identity (A1 to A3, T3) is the gate with the deterministic stub. With a
real model, outputs vary run to run, so those probes compare structure instead:
the request plan (indices touched, holders asked, Elasticsearch requests) and
set equality of pointers, sizes and counts. Response bodies are normalised
before any comparison by removing timing fields (`took`, timestamps,
durations).

### 9.1 Tenant isolation (SEC-T)

| Probe | What | N | Pass | Broken control that must fail |
|---|---|---|---|---|
| T1 read | with `tena`'s gateway session and, separately, with each of `tena`'s raw keys: search, get, mget, msearch, count, kNN, percolate, terms lookup, `_cat/indices`, `_field_caps`, `_mapping`, wildcard and comma index lists, alias names, against every `tenb` and `tenbx` index kind including `lc-tenb-authz`; plus the cluster APIs `_cluster/state`, `_cat/shards`, `_cat/aliases`, `_tasks?detailed`, `_resolve/index`, `_search_shards`, `_nodes/stats`, `_cluster/stats` | every API x kind x direction, plus 300 randomised gateway requests with forged tenant fields and pointers | 0 results, 0 counts, 0 names, no query descriptions; 403 or the typed `unavailable` | role descriptor `lc-*`; gateway that reads tenant from the body; a key holding `cluster: monitor` (must leak through the cluster APIs) |
| T2 write | `tena` writes into `tenb` indices or names `tenb` in a body | as T1 | refused | same |
| T3 enumeration | does any normalised response, error, size, total or count differ when `tenb` has 0 vs 10,000 leaves? | 300 paired requests | identical after normalisation | error text that names the index |
| T4 routing and links | `tena`'s fan-out never asks a `tenb` holder; no edge, member or note links across tenants | every recall in the stub loop | 0 | edge writer that resolves endpoints across tenants |
| T5 availability | `tena` floods (8 idle connections, a 3 GiB line, 8 MiB lines, 2,000 concurrent recalls, model-bound tasks, a disk-fill write flood up to its byte cap); `tenb`'s writes must succeed and `tenb`'s recall p95 must stay under 1.5x its unloaded p95. Runs with a latency-injecting model stub that enforces one global concurrency of 2 and one shared embedder (the real contention points) | 3 runs of 10 minutes per flood kind | pass all; `tena` refused at its byte cap before the disk reaches the flood-stage watermark | no per-tenant caps; parse before admission (LAB-R5); no fair queue at the model port; no fair queue at the embedding port; no per-tenant byte cap |
| T6 key space | a constructed collision in a 32-bit hash of tenant plus principal | 1 constructed pair plus 10,000 random | no shared lock, cache, rate or idempotency slot | 32-bit hash key (LAB-R1) |
| T7 key invalidation | after invalidating `tena`'s keys, each fails within a fixed bound of 5 s (ASSUMPTION, fixed now; P6 measures the actual latency, and a measurement above 5 s fails T7 and is reported, the bound is not moved) | 100 | 0 successes after the bound; the gateway's own path refuses at once | gateway that caches an authenticated client |

### 9.2 Access classes (SEC-A)

Non-interference: build corpus C0 (no restricted leaves) and C1 (C0 plus 50
restricted leaves that are lexically and semantically close to base leaves,
with edges and shared entities). A `base`-only reader runs the same 300
queries on both.

| Probe | Pass | Broken control that must fail |
|---|---|---|
| A1 digests, pointer lists, titles, sizes, totals, counts | identical across C0 and C1 for all 300 queries (byte-identical with the stub; structural with a real model) | mixed summary built over base + restricted leaves and filtered at read; total size computed over all children |
| A2 edges and expansion | identical | edge kept when only one endpoint is readable; expansion that reads the latest edges instead of the pinned generation's |
| A3 request plan | identical list of holders asked, indices touched and Elasticsearch requests | post-filter instead of pre-filter on the fan-out |
| A4 errors | `lookup` of a restricted pointer and of a random nonexistent pointer return identical normalised responses; their timing is recorded under A7 | distinct error for forbidden |
| A5 notes and cache | no reflection note or cache entry readable by the base reader derives from restricted leaves or restricted-channel queries; no prompt-bearing cache entry is shared across principals | cache key without class; cache class equal to the node's class instead of the join with the prompt's class; route cache shared across principals; log row stored in the item's class instead of the join |
| A6 relabel | a base leaf relabelled to restricted is `unavailable` to a base reader, and absent from every base artefact a base reader can reach, from the moment the relabel call returns (it returns at its erasure boundary, DESIGN 14.1, 14.3) | relabel that waits for the next generation; relabel that leaves the base summary unchanged; relabel that returns before its boundary |
| A7 wall-clock timing | 2,000 interleaved requests per corpus, and the A4 pair; KS statistic under 0.0515 (alpha 0.01) and median difference under 2% | REPORTED, not gating: a failure is recorded as a disclosed timing channel (expected cause: shared node load; DESIGN 20). A1 to A6 are gating |
| A8 digest field contract (review 2, F11; review 3, F11) | no pointer or digest entry carries a body, gist or fact field (schema check on every digest); every title equals the code-built title of its leaf or node (top 4 tf-idf terms) and contains no token with a digit; every reason and organise outline label is at most 12 words and shares no 5-gram with the leaf text, facts or gists in its call's input; the fixture includes one-sentence leaves and leaves whose top terms include dates, amounts and account codes written with digits (their digit-free forms, such as a month name or a spelled-out number, may reach a title and are not a failure, DESIGN INV-7, 20). A8 tests this field contract and the digit rule only: it does not show that titles and reasons disclose nothing (DESIGN 20), and its result is never read as such | holder whose reasons quote child facts; pointer that carries its leaf's gist (on a one-sentence leaf it returns the whole leaf); a model-written title copied from the leaf's first sentence; a code-built title that keeps tokens containing a digit |

### 9.3 Revocation (SEC-R)

| Probe | Pass | Broken control |
|---|---|---|
| R1 revoke a reader while it floods recalls, holds 8 requests open, reads its socket slowly, and logs out and in to re-arm; a test hook injects a delay between the final epoch check and the hand-off to the kernel | 0 responses handed to the kernel after the epoch flip (gateway trace), in 100 of 100 trials plus 100 trials with the injected delay; measured at the gateway, the bytes the kernel still holds in each of the principal's connections' output queues at the flip are at most 64 KiB (the cap the hand-off enforces: at most 64 KiB minus the queued byte count per write, DESIGN 4.4), and every byte the client receives after `revoke` returns comes from hand-offs completed before the flip (bytes already in the client's receive buffer are outside the bound); `revoke` returns within 2 s at p99 | revocation checked only at session start; revoke that waits on a lock the session holds; no send fence (the injected delay must make it red deterministically); a blocking socket write inside the fence with a slow-reading client (revoke latency over bound); a hand-off that writes without the queue-capped count (the queue measured at the flip exceeds 64 KiB) |
| R2 revoke a writer mid-write | no write admitted after `revoke` returns; writes admitted before commit within 30 s | same |
| R3 revoke under stress | revoke still effective and under 2 s at p99 under a flood from the revoker's own tenant, and with `lc-<t>-authz` made unwritable (index blocked read-only, and the disk at the flood-stage watermark, P14) | revoke sharing the general admission queue; revoke that requires the Elasticsearch write to succeed |
| R4 administrators | a tenant administrator cannot grant itself any role or class; revoking an administrator or a revoker needs two distinct administrators; the operator's break-glass revoke works when every tenant administrator is compromised | self-grant allowed; single-administrator revoke of a revoker; break-glass removable by a tenant administrator |
| R5 class-grant removal | removing one class from a principal bumps its epoch; an in-flight fan-out over that class returns nothing further from that class | removal without an epoch bump |
| R6 key rotation | the tenant key owner (DESIGN 4.4) rotates and invalidates the tenant's keys without the bootstrap credential; the gateway key cannot | rotation that needs the superuser; gateway key with `manage_api_key` |
| R7 epoch journal faults (review 2, F9; review 3, F9) | with the tenant's journal volume out of space (ENOSPC), with fsync delayed by 10 s through a test hook, and with the gateway stopped abruptly at each persistence boundary (before the append, after the append before the fsync, after the fsync, during compaction before the name exchange, after the exchange), with a clean stop while an append is pending, and with a clean stop during which a revoke arrives after the drain has begun; and with a simulated power loss (a test I/O layer under the journal module records every write, fsync, name exchange and directory fsync and, at the injected point, treats as uncertain everything the filesystem's durability rules do not guarantee: file contents written after the file's last completed fsync may be kept, lost or torn, and each directory change after the directory's last completed fsync may be kept or lost, independently of the data, and every combination is run; ASSUMPTION: a model of those rules, not a power cut on a real device) (i) before the start record's fsync returns, (ii) after a previous clean shutdown and a durable start, between a revoke's denial in memory and its fsync, and (iii) at each compaction step (while writing the new file, after its fsync before the name exchange, after the exchange before the directory fsync, after the directory fsync), and (iv) during break-glass recovery after a case (ii) in which the revoke was a two-administrator revoke of an administrator, before and after the recovery's revokes are fsynced: `revoke` returns `denied` within 2 s at p99 in every case except a revoke that arrives during a clean stop (refused, below) and the revoked principal is handed nothing afterwards; on a journal fault the tenant is closed (all its principals denied) within `D_FSYNC`, stays closed across restarts until break-glass recovery, and another tenant's revoke still returns within 2 s; no request of the tenant is admitted before its start record and the directory are durable (layer trace); after every abrupt stop and every simulated power loss, the table rebuilt from the durable journal (the table that start and break-glass recovery use) holds every revoke whose `durable_at` was reported, checked on the table itself and not only by the principal being denied, since a closed tenant denies everyone; the restarted gateway either holds the other revokes or has closed the tenant, and never re-admits a revoked principal; after (ii) it comes up closed; in the clean-stop case a revoke arriving after the drain has begun is refused with a typed retryable failure, never answered `denied`; in (iv) recovery appends and fsyncs the revokes carried from the receipts without `durable_at` before any grant call is admitted, admits grant calls only from the administrators the operator named, refuses every call of the revoked administrator, and a power loss at either point leaves the tenant closed; `durable_at` is set only after the fsync returns | revoke that waits for the fsync (latency over bound under the delayed fsync); shutdown record written before pending appends drain (re-admission after a clean stop with an append pending); journal error that does not close the tenant (re-admission after restart); start that accepts a journal without a shutdown record; compaction that writes the snapshot in place instead of into the spare (a torn snapshot accepted); one journal file shared by all tenants (one tenant's fault delays another's revoke); admission that does not wait for the start record's fsync (layer trace shows a request admitted before the start record was durable, in (i)); start record appended without fsync (in (ii) the durable journal ends in the previous clean shutdown and the revoked principal is re-admitted); compaction that exchanges the names before fsyncing the new file (in (iii) a revoke reported durable before compaction is lost); compaction without the directory fsync (in (iii) the old file returns and a revoke reported durable after the exchange is lost); these two leave the tenant closed either way and go red only through the rebuilt-table check above; compaction that copies the previous run's shutdown state instead of the current start record (a later power loss reads as a clean shutdown and re-admits); a clean stop that writes a shutdown record for a fault-closed tenant (the restarted gateway serves it without break-glass recovery); a clean stop that leaves the revoke lane open during the drain (a revoke answered `denied` after the shutdown record is lost and its principal re-admitted after restart); break-glass recovery that reopens grant calls to every administrator of the durable table before re-applying the receipts' revokes (in (iv) the revoked administrator re-grants a colluding principal) |

N: R1 200 trials (100 plain, 100 with the injected delay); R2, R3 100 each;
R4, R5, R6 20 each; R7 20 per fault kind, 20 per stop point, 20 per
simulated power-loss point and 20 per recovery point.

### 9.4 Erasure (SEC-E)

Per run: 20 subjects and 20 single items erased, chosen so that together they
reach every artefact kind (leaf, src, node in current and 2 retained
generations, member, edge, log row, stored query, note, judge label, cache,
manifest, replay set, authz record, query-shaped summary and edge; the
plaintext log files are grepped for every canary).

| Probe | Pass | Broken control that must fail |
|---|---|---|
| E0 positive control | before erasure the verifier finds every planted canary (short, long-compressed, vector, query-only, authz) by every read path, and sees `docs.deleted > 0` after deletes and before purge; the plaintext Elasticsearch, container and gateway log grep finds a separate control marker (never an erased canary) deliberately logged through a scratch index with the slow log enabled at threshold 0 | (none: if E0 fails the run is void) |
| E1 residue | after erasure: 0 canaries by text, kNN, percolate, get, lineage query, in every index (authz included), every retained generation, every replay set, in the plaintext Elasticsearch, container and gateway logs, and (raw grep for the short canary) in every file that the gateway, its workers or the model port write under the run's data root outside Elasticsearch's own data directory, so a model-output store outside the closure is found (review 2, F10; test captures of prompts and responses are held in memory, not under the data root); closure residue 0; `docs.deleted == 0` after purge | erase only the current generation; key destroyed only (embedding left behind); raw-file grep of segments as verifier (the long canary must make it miss); closure without the authz index; a model-port wrapper that keeps outputs in a file outside the indices (the removed evaluation cache) |
| E2 paraphrase | a node whose summary paraphrases the subject without the canary string is caught by lineage | string-search-only closure |
| E3 resurrection | quarantine, retry, reseal, replay, rebuild, restore-from-snapshot and restore of an older snapshot together with an older keystore backup (both taken before the erasure) cannot bring erased content back; re-ingest of content naming the erased subject (in any canonical variant) is refused; after every restore, the verifier's residue checks (direct reads with its own keys, `lc-<t>-authz` included) find 0 before the tenant is enabled, so an erased principal's sessions and grants restored from the old snapshot are gone (review 2, F8) | reseal-before-quarantine; no erased-identifier HMACs; restore that serves before re-applying tombstones; tombstones kept only inside the snapshot (the older snapshot must resurrect content); keystore restore that does not destroy keys named by newer tombstones; restore that re-applies tombstones but does not replay the closure over restored authz records |
| E4 concurrency | erasure while a rebuild, a reflection pass and a gate replay are mid-flight: each job's commit waits at the tenant's write gate and aborts when the published tombstones cover its inputs | commit-time tombstone check removed (a job commit that does not check the published tombstones) |
| E5 hold | legal hold placed: erasure queued, receipt states the delay; hold released: erasure completes | hold ignored |
| E6 receipt | receipt counts match an independent recount by the verifier; no id lists; the requester's copy shows only totals over the requester's read set | receipt that says done while a log row still names the subject; requester copy with per-class counts |
| E8 boundary (review 2, F7; review 3, F7) | test hooks pause the executor (a) between the request and the boundary, including mid-way through the closure walk before it reaches an upper node linked only through transitive `lin_nodes` lineage, (b) between the boundary and the physical rewrite, and (c) inside a `lookup` and a recall of the subject, between their reads (before the boundary) and their send hand-off (after it); and (d) with `N_SEND = 8` concurrent senders of the tenant (ASSUMPTION), each a `lookup` or recall carrying the subject, paused by the hook of R1 after its final tombstone and epoch check and before its socket write while the boundary is published. In (a) no promise holds yet and commits of the tenant wait at the gate; in (c) the response is aborted with a typed failure and only bytes handed to the kernel before the boundary may still reach the peer (those the kernel still holds at the boundary at most 64 KiB per connection, measured at the gateway), while a response of another class's reader that carries nothing the boundary covers is sent unchanged; in (d) the gateway trace shows no hand-off whose check precedes the boundary and whose write follows it (the publication step waits on the tenant's send fence until every paused sender has written, and each later hand-off of those responses is aborted), and the bytes the kernel still holds in each paused sender's output queue at the boundary are at most 64 KiB, measured at the gateway; in (b) and at every later point, holders, organise calls, reflection, readers and direct searches that touch an affected leaf or ancestor node receive the placeholder or `unavailable`, never the old summary, gist, facts or text (stub prompts and gateway responses captured); request-to-boundary time is recorded against `B_ERASE` | redact set not consulted when presenting nodes; tombstone published before the closure is complete (an upper node reached only through transitive lineage leaks in (b)); commits not held at the gate during the closure (a leaf written mid-closure that names the subject survives); send hand-off without the tombstone re-check (a lookup read before the boundary is sent in full after it, in (c)); send hand-off that does not hold the tenant's send fence (in (d) a paused sender writes covered bytes after the boundary); send hand-off that writes without the queue-capped count (in (d) the measured queue of a paused sender exceeds 64 KiB) |
| E9 query-shaped artefacts | erasing subject X removes: X's own queries and log rows; other people's queries, stored queries, cache entries and notes that mention X; summaries re-made, `co_used` edges and memberships moved because of X's queries (reverted in every retained generation) | subject detection on leaves only; closure over `lin_subjects` of leaf-derived artefacts only |
| E7 scale and bound | a subject with 100,000 events in a 1,000,000-document tenant finishes; request to boundary within `B_ERASE` (a measurement above it fails E7 and is reported; the bound is not moved); physical purge within `P_PURGE` of the boundary; the delay of the tenant's own commits during the gate and the latency of other tenants reported | (scale test, no control) |

N: E1 to E4 and E9 run 3 times each (120 erasures, plus 60 for E9); 0
failures gives a Wilson upper bound of 3.1% per erasure; every residue is a
BLOCK. The older-snapshot restore case of E3 runs 3 times per run.

### 9.5 Versioning (SEC-V)

| Probe | Pass | Broken control |
|---|---|---|
| V1 pin generation G, reorganise to G+1 and G+2, resolve every pointer held from G | each returns `ok`, `superseded`, `moved` or `expired`, never a wrong leaf, in 1,000 of 1,000 | node ids reused for different content |
| V2 pinned reader consistency | a pinned task sees the same tree and the same edges for its whole life (except erasure rewrites), including while index refresh is paused by a test hook independently of write acknowledgement (the test starts tasks without waiting for refresh), and across a real retention expiry (`R_GEN` shortened in the test) of the generation named by an adopted edge's `gen_from` and by a retired edge's `gen_to`: the adopted edge stays visible and the retired one stays hidden (review 2, F4, F5) | reader that re-reads the head per wave; expansion that reads the latest edges; P or a head published on write acknowledgement without the refresh barrier; ancestry truncated to retention |
| V3 pointer across erasure | `unavailable` | pointer that still resolves from a cached document |
| V4 two concurrent reorganisations plus writes | one manifest becomes current, the other discarded with its edges and members, no mixed generation, no lost leaf, in 100 of 100; rollback restores the previous tree and edges exactly, also after a retention expiry has removed the generations the surviving edges were adopted in | flip without compare-and-set; rollback that leaves the candidate's edges visible; ancestry truncated to retention |
| V5 restore after erasure | erased content never served, including after restoring an older snapshot and an older keystore backup; the tenant is enabled only after the closure replay over every restored index and the verifier's residue checks pass | restore without tombstone re-application; tombstones only inside the snapshot; restore without the closure replay over authz and control indices |
| V6 candidate isolation | edges and members written by a candidate generation are invisible to every reader until the gate adopts it, and disappear if it is rejected | edges visible at write |
| V7 no stranding | insert-time edges and placement members written while a candidate is open stay visible to tasks pinned after the write-path watermark covers them, whether the candidate is adopted or rejected, in 100 of 100 cycles (DESIGN 13.2); and when a rebuild's build call fails for one node with pending members, no generation is published and tree-only recall still reaches the pending leaf (review 2, F6) | write-path versions stamped with the next generation number, which the rejected candidate took; rebuild that publishes the advanced W with the failed node at its old version (the pending leaf drops out of tree-only recall) |

### 9.6 Broken variants added by the review, with N (review F21)

| Variant | Probe | N |
|---|---|---|
| key with `cluster: monitor` | T1 | every cluster API x 2 tenants |
| cache class equals node class; route cache shared across principals | A5 | 300 queries |
| injected delay between check and send; blocking socket write inside the fence with a slow reader | R1 | 100 each |
| `lc-<t>-authz` unwritable during revoke | R3 | 100 |
| older snapshot plus older keystore backup | E3, V5 | 3 per run |
| expansion reads latest edges; candidate edges visible at write | V2, V4, V6, A2 | 100 |
| write-path versions stamped with the next generation number; P advanced without joining a timed-out link job | V7, V2 | 100 cycles each |
| relabel read before the next generation | A6 | 50 relabels |
| stub with one global model concurrency and one shared embedder | T5 | 3 runs per flood kind |
| subject detection on leaves only | E9 | 3 runs |
| model-output file store outside the closure | E1 | 3 runs |
| holder reasons quoting facts; pointer carrying its leaf's gist; model-written title copied from the leaf; code-built title keeping digit tokens | A8 | 300 queries |
| P or head published before refresh (refresh paused by a hook, writes acknowledged) | V2 | 100 |
| ancestry truncated to retention, across a real expiry | V2, V4 | 20 expiries |
| rebuild publishes W past a failed node | V7 | 100 cycles |
| tombstone published before the closure completes; commits not held at the gate; send without the tombstone re-check; send that does not hold the tenant's send fence, with 8 concurrent paused senders | E8 | 3 runs, every pause point |
| restore without closure replay over authz | E3, V5 | 3 per run |
| revoke waits on fsync; journal fault without closing the tenant; start without shutdown record; compaction in place instead of into the spare; shared journal; shutdown record before the drain; revoke lane open during the clean-stop drain; admission before the start record's fsync; start record without fsync; compaction that exchanges names before the file fsync, skips the directory fsync or drops the start record; shutdown record for a fault-closed tenant; break-glass that reopens every administrator before re-applying the receipts' revokes | R7 | 20 per fault kind, stop point, simulated power-loss point and recovery point |
| keystore key bytes left in the `-wal` file | BUILD-PLAN Step 4 | 1,000 keys |

## 10. Staged gates and model-call budget

Every stage needs the owner's explicit approval for that stage only, with a
hard call cap enforced by the counter in the model port (incremented before the
call; failures count). Approval for one stage is not approval for the next
(LAB rule). Heavy jobs run on a remote host, not the owner's laptop.

| Stage | What | Gate to pass before the next stage |
|---|---|---|
| S0 | stub-only loop on a synthetic fixture (2 tenants x 2 classes, about 300 leaves, planted needles, at least 16 plants, decoys and fillers); all security probes and broken controls. Two named, resumable checkpoints (review 2, F13): S0-core (every probe and gate below except the T5 floods) and S0-load (the T5 floods: 6 flood kinds x 3 runs x 10 minutes, 180 minutes of loaded measurement on the single node, which cannot run floods in parallel without changing each flood's baseline). Wall time per checkpoint: BUILD-PLAN Step 22 | S0 passes only when both checkpoints pass; a partial set is reported as S0 INCOMPLETE, never as a pass. Gates: every security probe passes and every broken control fails; stub A1-tree-only recall@10 on fixture queries at least 0.80; SHUFFLED-SUMMARIES drops A1-tree-only by at least 0.30; RANDOM-EDGES removes A2's gain on linked-needle queries; gate positive controls hold (oracle adopted, random rejected); planted repair LOOP minus NO-AUDIT at least 0.25 in fixture strata 2 and 3; decoy repair below plant repair; RECENCY-ONLY within margin; stub reader's opened share inside [0.05, 0.60] |
| S1 | canary: every task kind with the real model on the fixture (hard cap 25) | schema-valid outputs; measured tokens and latency per task kind replace the assumptions in 10.2 (amendment committed before S2); throttling responses recorded |
| S2 | small real: 30 pilot questions over their own haystacks (about 4,400 leaves); A0, A1, A2, SHUFFLED-SUMMARIES; relevance judge on 3 pools; C1 and C2 make no model calls, so their S2 rows are computed later on the stored S2 structures (BUILD-PLAN Step 21) | pipeline runs end to end; failures under 5%; SHUFFLED-SUMMARIES fails for its reason; sustained calls per 5-hour window measured (amendment: wall days per stage) |
| S3 | dev static E1: 102 pilot questions, dev static pool; A0+LEN fitted; AGENTIC and AGENT-A0 on 50 questions; judge validation (8.2) and paraphrases (8.3) | exploratory figures; judge passes 8.2; power amendment for H1 and H2; freeze of thresholds, prompts and the merge rule committed |
| S4a | dev E2a: 137 blocks, recurring queries; LOOP, FROZEN, RANDOM-FEEDBACK agent-routed; QUERY-MEMO; proxy supporting arms; behavioural-use sample | loop runs; opened-share bounds hold; RECENCY-ONLY within margin; controls fail for their reasons; power amendment for H3 |
| S4b | dev E2b: 40 plants, 14 decoys, 40 fillers; LOOP, FROZEN, NO-AUDIT, RANDOM-FEEDBACK agent-routed; gate positive controls; LOOP+SYNTH and other proxy arms | gate positive controls hold; opened-share bounds hold; controls fail for their reasons; power amendment for H4 |
| S5 | optional: J1 human-labelled answer judge + answer-level on development | J1 passes |
| S6-H1 | confirm static E1 for H1 on the fresh set, whole pool (PENDING RELEASE): whole-pool build; A0, A0+LEN, A1 with its source-contribution check (2.2 (c)), SHUFFLED-SUMMARIES | H1 decided |
| S6-H2 | the rest of S6 on the same whole-pool trees as S6-H1 (PENDING RELEASE), with the edge-typing prompt and expansion settings frozen before S6-H2 (3.7): edge construction and typing (edges and hub nodes change no tree node version, so A1's routing outputs stay valid); A2 and RANDOM-EDGES (re-using A1's routing outputs); C1, C2; AGENTIC and AGENT-A0 on a seeded 100-question subset | H2 decided; H1b and C reported |
| S7a | confirm E2a on the fresh set (PENDING RELEASE) | H3 decided |
| S7b | confirm E2b on the fresh set (PENDING RELEASE) | H4 decided |

### 10.1 What the counts assume

All counts are DERIVED ESTIMATES, recomputed by `evalkit/budget.py` from the
formulas below; S1 and S2 replace the per-call assumptions by measurements.

- Routing calls per recall (ASSUMPTION): every top-level holder of every family
  is asked (OWNER), then expansion is capped at `B_HOLDERS = top-level holders
  + 36` plus at most 12 organise calls (DESIGN 8.2). Development pools (about
  10 top-level holders): 25 to 60. Whole pool (about 29): 50 to 77; S7a uses the
  whole-pool figures throughout although its pool grows from 6,018 contents.
  E2b pools (about 6,000 to 11,000 contents): 35 to 70. 80% route, 20%
  organise.
- No evaluation cache (review 2, F10). The previous revision wrapped the
  model port with an evaluation-only cache that sat outside the product's
  erasure closure, so a cached output carrying an erased canary would have
  survived erasure; it is removed. Every arm is a separate tenant and arms
  share nothing, so k agent-routed arms cost k times one arm in both the LOW
  and the HIGH figure. The only model-output cache is the product's (DESIGN
  5.8), inside the erasure closure; its hits are counted and reported.
- Reader sessions 1 to 3 calls; reflection 1 variant call plus 5 to 8 judge
  calls per stream item (NO-AUDIT: 1 plus 1 to 3); reverse audit capped at
  `N_AUDIT = 100` leaves per cycle and `K_CAND = 5` candidate searches per leaf,
  one judge call per leaf (DESIGN 12).
- Gate, per candidate (one candidate per cycle per learning arm; review 3,
  F15): 12 generation calls, 30 to 60 judge calls and 80 replayed searches
  (60 targeted, 20 general), whose candidate traversal costs 2 to 8 routing
  calls each, because gate replays run as the class's service principal and
  the product cache, scoped to that principal (DESIGN 5.8), re-uses the
  current generation's routing of the same query for every unchanged node;
  only nodes whose version changed are asked again: 202 to 712 calls per
  candidate. That re-use needs the current generation's routing of the query
  first. A gate-only synthetic query (topping up the targeted set) is new,
  and a held-out search, in the targeted set or in the general guard (the 20
  most recent held-out searches, DESIGN 13.3), was routed under its reader's
  principal, whose cache entries the service principal cannot use, so every
  replay query first pays one cold traversal of the current generation at the
  stage's per-recall routing figure above. Cold queries per candidate
  (ASSUMPTION): all 80, in the LOW and the HIGH figure; a cache hit can only
  lower this. That is 2,000 to 4,800 calls per candidate on development pools,
  2,800 to 5,600 on E2b pools and 4,000 to 6,160 on the whole pool.
- Build: one call per node; a level-1 build call reads its new children's full
  text. Edge typing: 20 pairs per call over titles, gists and facts.

### 10.2 Per-call input tokens by task kind (ASSUMPTION until S1)

| Task kind | API input tokens per call | Headless CLI (adds about 10.5k fixed overhead, LAB measured) |
|---|---|---|
| route | 2,500 to 4,000 | 13,000 to 14,500 |
| organise | 2,000 to 3,000 | 12,500 to 13,500 |
| build_l1 (up to `F_MAX` leaves of full text) | 18,000 to 37,000 | 28,500 to 47,500 |
| build_upper and re-summaries | 4,000 to 8,000 | 14,500 to 18,500 |
| type_edges (20 pairs, titles, gists, facts) | 5,000 to 7,000 | 15,500 to 17,500 |
| judge_relevance (1 query, up to 5 leaves) | 3,000 to 7,000 | 13,500 to 17,500 |
| query_variants, synthetic_queries, probe and recurring generation | 1,000 to 2,500 | 11,500 to 13,000 |
| reader, ask, agent calls | 4,000 to 10,000 | 14,500 to 20,500 |
| answer_judge | 1,500 to 3,000 | 12,000 to 13,500 |

### 10.3 Calls, tokens and wall time per stage

Printed by `evalkit/budget.py` (plain Python, run with `python3 -I`), in the
default order of the stages (below). Review 3, F15 derived the gate from the
factors of 10.1 (202 to 712 calls per candidate where the previous revision
allotted 200 to 700), added the cold current-generation routing of replay
queries, and added DEV-BASE to the total, because S7b runs before S4a in the
default order. So the figures of S4b, S7b, S4a, S7a and the total changed;
those of S1 to S6-H2, S5 and DEV-BASE did not. The cold routing is now the
largest single item of every loop stage, in the low and the high figure (in
S7a narrowly: 80,000 to 123,200 against 78,300 to 120,582 for the stream
recalls).

| Stage | Calls low | Calls high | Input tokens, API basis | Input tokens, CLI basis | Wall hours at 9.36 s per call, concurrency 2 |
|---|---|---|---|---|---|
| S0 | 0 | 0 | 0 | 0 | 0 (no model calls; machine wall time in BUILD-PLAN Step 22) |
| S1 | 20 | 25 | under 1M | under 1M | under 1 |
| S2 | 2,391 | 5,454 | 12M to 43M | 37M to 100M | 3 to 7 |
| S3 | 7,022 | 17,252 | 39M to 141M | 112M to 322M | 9 to 22 |
| S6-H1 | 23,104 | 35,574 | 129M to 375M | 371M to 749M | 30 to 46 |
| S6-H2 | 5,129 | 18,632 | 24M to 121M | 78M to 317M | 7 to 24 |
| S4b | 70,575 | 170,582 | 246M to 925M | 987M to 2,716M | 92 to 222 |
| DEV-BASE alone (S7b runs before S4a in the default order, so it builds DEV-BASE) | 3,383 | 8,001 | 41M to 148M | 76M to 232M | 4 to 10 |
| S7b | 141,374 | 291,893 | 442M to 1,485M | 1,927M to 4,550M | 184 to 379 |
| S4a | 68,279 | 162,717 | 257M to 976M | 974M to 2,685M | 89 to 212 |
| S7a | 187,747 | 309,247 | 640M to 1,913M | 2,611M to 5,160M | 244 to 402 |
| Total, default order (every row above, DEV-BASE included) | 509,024 | 1,019,377 | 1,829M to 6,128M | 7,174M to 16,831M | 662 to 1,325 |
| S5 (optional, not in the default order or the total) | 1,180 | 1,180 | 3M to 8M | 16M to 20M | 2 |

Breakdown (formulas, so they can be recomputed; k arms cost k times one arm):

- S2: build 381 to 574; edges 330 to 1,100; A1 and SHUFFLED-SUMMARIES on 30
  questions each, 30 x (25 to 60); judge on 3 pools 180.
- S3: build 1,256 (membership 1.3) to 1,932 (membership 2.0); edges 1,086 to
  3,620; A1 102 x (25 to 60); SHUFFLED-SUMMARIES 50 x (25 to 60); judge
  validation and paraphrases 480; AGENTIC 50 x (1 to 3) agent calls plus 300 to
  1,800 explore holder calls; AGENT-A0 50 x (1 to 3).
- S6-H1: whole-pool build 5,004 to 7,700; A1 312 x (50 to 77) = 15,600 to
  24,024; SHUFFLED-SUMMARIES 50 x (50 to 77) = 2,500 to 3,850. A0 and A0+LEN
  cost no model call.
- S6-H2: edges 4,329 to 14,432; AGENTIC 100 x (1 to 3) agent calls plus 600 to
  3,600 explore holder calls; AGENT-A0 100 x (1 to 3). A2, C1, C2 and
  RANDOM-EDGES cost no model call (A2 re-uses A1's routing outputs from
  S6-H1). S6-H1 plus S6-H2 equals the former single S6 (28,233 to 54,206).
- S4a: recalls (137 + about 85 recurring = 222) x (25 to 60) x 3 agent-routed
  arms (16,650 to 39,960); readers LOOP and FROZEN 444 to 1,332; reflection
  LOOP 1,332 to 1,998; QUERY-MEMO shadow reflection 1,332 to 1,998; audit 9
  cycles x 100 (450 to 900); re-summaries 300 to 1,200; gate 2 arms x 9
  cycles, 18 x (202 to 712) = 3,636 to 12,816, plus cold routing 18 x (80 x 25
  to 80 x 60) = 36,000 to 86,400; incremental build 1,966 to 3,276 per arm x 3
  (5,898 to 9,828); edges 1,417 to 4,725; recurring generation 120 to 360;
  proxy judge 500 to 1,000; behavioural 200.
- S4b: base build 480 to 740 and edges 443 to 1,475; recalls (120 in stream +
  54 probe 2 = 174) x (25 to 60) x 4 arms (17,400 to 41,760); readers 360 to
  1,080; reflection LOOP and NO-AUDIT 960 to 1,560; audit 300 to 600;
  re-summaries 300 to 1,200; gate 3 arms x 6 cycles, 18 x (202 to 712) =
  3,636 to 12,816, plus cold routing 18 x (80 x 25 to 80 x 60) = 36,000 to
  86,400; gate positive controls, 2 candidates, 404 to 1,424 plus cold routing
  4,000 to 9,600; stream build 1,090 to 1,680 per arm x 4
  (4,360 to 6,720); edges 938 to 3,125; probe generation 94 to 282;
  LOOP+SYNTH proxy 200 to 600; proxy judge 500 to 1,000; behavioural 200.
- S7a: recalls (312 + about 210 recurring = 522) x (50 to 77) x 3 arms
  (78,300 to 120,582); readers 1,044 to 3,132; reflection LOOP 3,132 to 4,698;
  QUERY-MEMO shadow reflection 3,132 to 4,698; audit 500 to 1,000;
  re-summaries 1,200 to 4,800; gate 2 arms x 10 cycles, 20 x (202 to 712) =
  4,040 to 14,240, plus cold routing 20 x (80 x 50 to 80 x 77) = 80,000 to
  123,200;
  incremental build 4,000 to 6,700 per arm x 3 (12,000 to 20,100); edges
  2,900 to 9,700; recurring generation 299 to 897; proxy judge 1,000 to
  2,000; behavioural 200.
- S7b: recalls (200 in stream + 80 probe 2 = 280) x (35 to 70) x 4 arms
  (39,200 to 78,400); readers 600 to 1,800; reflection LOOP and NO-AUDIT 1,600
  to 2,600; audit 500 to 1,000; re-summaries 600 to 2,400; gate 3 arms x 10
  cycles, 30 x (202 to 712) = 6,060 to 21,360, plus cold routing 30 x (80 x 35
  to 80 x 70) = 84,000 to 168,000; stream build 1,650 to 2,550 per arm x 4 (6,600 to
  10,200); edges 1,174 to 3,913; probe generation 140 to 420; LOOP+SYNTH proxy
  200 to 600; proxy judge 500 to 1,000; behavioural 200.

Rate limits (review F13): the subscription's limits are not published as call
counts. The model client treats a throttle response as a pause, not a scored
failure, and records it; S2 measures sustained calls per 5-hour window, and an
amendment before S3 converts each stage into wall days. The hours above are a
floor. For scale, the earlier lab's largest approved stage was 800 calls and a
22,500-call extraction was never approved. The owner's routing costs 25 to 77
calls per recall; that cost is itself a result and is reported, not hidden.

Order (in force by default; review 2): S0 (both checkpoints), S1, S2, S3,
then S6-H1 (H1 only: A0, A0+LEN, A1 with its source-contribution check,
SHUFFLED-SUMMARIES). Printed sub-budget of this H1-first path (S1, S2, S3,
S6-H1): 32,537 to 58,305 calls; 179M to 559M API input tokens; 521M to
1,172M CLI input tokens; 42 to 76 wall hours. H2, H3, H4 and the comparison
arms stay registered and follow in later phases: S6-H2 (H2, H1b, C; 5,129 to
18,632 calls), then S4b and S7b (H4; 215,332 to 470,476 calls including
DEV-BASE), then S4a and S7a (H3; 256,026 to 471,964 calls); S4b before S4a
because S4b's gate positive controls cover E2a (7.3). This is an order, not a
reduction of what is tested: every stage still needs its own approval, and
any reduction or stop rule is the owner's decision (section 12, X4 and X6).
Recommended stop rule, not in force until the owner says so: stop after
S6-H1 if H1 reads HARM, or DID NOT HOLD on both (a) and (b).

Other ways to cut the budget without changing the method, each the owner's
decision (section 12): shorter streams (cost scales about linearly with stream
length); fewer learning cycles per stream (each cycle costs every learning
arm 2,000 to 6,160 calls of cold gate routing, 10.1); drop S3 if S2 is clear; typed-edge candidates 3 instead of 5; the
direct API instead of the headless CLI (removes the 10.5k fixed overhead per
call).

## 11. Reporting rules

- Every result file carries: commit, stage, arm, pool, n, metric definition,
  interval method and seed, the call counter reading, product cache hits, and whether
  the arm is agent-routed, proxy (DEVIATION 7) or COMPARISON (LAB: 0 of 276
  earlier result files carried all required fields).
- Ranked lists and per-question rows are committed so every figure can be
  recomputed from rows.
- One stamped run per stage (S0: one stamped run per checkpoint, S0-core and
  S0-load, on the same commit); no code tree is synced mid-run.
- Baselines are quoted at the one registered depth (10) and the same n as the
  arm they are compared with.
- A security result names each probe, N, failures, and which broken controls
  turned it red.
- Every hypothesis is reported with its outcome word (2.1), its interval, its
  effective n and its Holm-adjusted reading.

## 12. Open decisions for the owner (defaults in force until answered)

| Id | Decision | Default |
|---|---|---|
| X1 | Release the 312-question fresh set for S6, S7a and S7b | not released; S6, S7a and S7b do not start |
| X2 | Approve each stage and its cap | none approved; S0 needs no approval (no model calls) |
| X3 | Who labels the 160-item J1 sample | answer-level not reported |
| X4 | Shorter streams for S4a, S4b, S7a, S7b | full streams as written |
| X5 | Which model runs holder, build and judge calls | Sonnet for holders, build, organise and the reader; the judge on a different pinned model of no higher tier if it passes 8.2, otherwise Sonnet with a different prompt family and SAME-JUDGE disclosed; nothing on a higher tier without the owner's named approval |
| X6 | Order and stop rules for the stages (10.3) | order in force: S0, S1, S2, S3, S6-H1, then S6-H2, then S4b and S7b, then S4a and S7a, every stage still tested and approved one by one; the stop rule after S6-H1 is recommended and not in force until the owner says so |
| X7 | Direct API instead of the headless CLI for model calls | headless CLI on the subscription |
| X8 | Confirmatory H1 denominator: drop the 6 DEV-EXPOSED questions (n = 293, 299 as sensitivity), or keep 299 and report the 6 apart | recommended and in force until answered: 293 primary (3.2). Reason: development runs will show those 6 questions' gold to models |
| X9 | Readings chosen on the owner's behalf, each reversible without changing the method: "never offered" read as `tree_missed` (DESIGN 10); reverse-audit synthetic queries off in LOOP (DESIGN 12); the gate's general-replay count guard (7.3); stream reader limits (3.8); reverse-audit re-filing support `S_MIN` = 2 missed relevant searches (DESIGN 12; review 2, F3); the owner's "pure reference pointers" read together with "a tight summary" as a field contract (no body, gist or fact field in a pointer, code-built titles without digit tokens, bounded reasons), not as semantic non-disclosure, so descriptors may convey an answer and "not opened" is never read as irrelevance (DESIGN INV-7, 20; review 3, F11; ASSUMPTION about the owner's intent; the alternative, non-content descriptors, would leave no tight summary) | as written; each alternative is logged or run as a labelled variant where cheap (`any_missed`, LOOP+SYNTH) |

## 13. Review responses

### 13.1 First review

The first adversarial review (26 findings) was answered in the first revision.
None of the fixes replaces the owner's method; the one unlabelled substitution
it found (the proxy world) is now DEVIATION 7 and decides nothing. Rows marked
"changed by review 2" describe a mechanism that the second revision replaced
(13.2 says how).

| Finding | Response | Where |
|---|---|---|
| F1 opened has no producer | fixed: a reader per stream item in every signal-producing arm; opened-share bounds [0.05, 0.60] or the signal is void; opened rate, opens per recall and behavioural share reported (intervals changed by review 2, F14); no gold-dependent opener | 3.8, 10.3; DESIGN 9 |
| F2 "never offered" undefined; NO-AUDIT not independent | fixed: `tree_missed` per source (DESIGN 10); NO-AUDIT removes exactly pool (c) and the audit; strata by the best direct rank over the source question and probe 1 (changed by review 2, F2: ranks at each search's corpus prefix plus an offer audit; changed by review 3, F2: the strata are fixed before any arm runs and the audit is descriptive), with per-stratum predictions; NO-AUDIT has its own check in its own logs (a switch check since review 3, F2) | 3.6, 4, 6 |
| F3 planting repairs by construction | fixed: donor-sibling metadata, pinning in all arms with survival logged, FROZEN ceiling, probes from question and answer only with a rank filter on probe 2, decoy plants, LOOP+SYNTH apart from the owner's reverse search (synthetic queries off in LOOP), learning frozen at the end checkpoint and probe 2 never stored. Answered in part: the source question and probe 1 are not rank-filtered, because their best direct rank defines the strata; stratum 1 is reported and excluded from the cause test instead (review 2, F2 also moved any plant a logged offer row showed offered to stratum 1; changed by review 3, F2: no plant is moved, the offer audit is descriptive) | 3.6, 4, 6 |
| F4 gate has no positive control | fixed: oracle and random candidates in S0 and S4b, which also cover E2a (same frozen gate; S4b must pass before S7a); targeted replay; simulated power; unjudged items judged at replay; general replay as a count guard | 7.3; DESIGN 13.3 |
| F5 recency is a gold proxy | fixed: lag L = 5 with interleaving; identical exploration in every arm, excluding recent leaves, and removed from metrics; RECENCY-ONLY control; `last_seen` hidden from holders | 3.4, 6; DESIGN 8.2 |
| F6 gold through non-id fields | fixed, and confirmed real: the records carry a turn-level `has_answer` flag (MEASURED); field whitelist and an all-fields ingest test across all task kinds | 3.1; BUILD-PLAN Step 20 |
| F7 H3 confounded and weak | fixed: QUERY-MEMO arm reading FROZEN's logs; tree-only primary (changed by review 2, F1: "beyond memo" is decided on fused final-list recall); exposed stratum; schedule 2c to 4c; rank filter on recurring queries | 3.5, 4, 2.2 |
| F8 controls in the non-adopted routing | fixed: proxy world labelled DEVIATION 7; RANDOM-FEEDBACK and NO-AUDIT agent-routed on E2b decide; proxy is supporting only | 4.2, 6; DESIGN 22 |
| F9 H2 merge rule undefined | fixed: three-list RRF k = 60, cut 50, pre-registered now; linked list's own recall reported | 5.1 |
| F10 H1 from fusion or length | fixed: A0+LEN arm in the decision; tree-only contribution condition; per-source shares | 2.2, 4 |
| F11 agent-side method never exercised; reasons leak | fixed: AGENTIC vs AGENT-A0 as H1b (secondary, to avoid a fifth decided claim); reasons at most 12 words with a 5-gram leak check; A8 probe and Step 10 broken variant (extended by review 2, F11: no gist in returned pointers, titles and outline labels checked; changed by review 3, F11: a field contract with code-built titles) | 1, 4, 9.2; DESIGN 8.2 |
| F12 statistics do not fit | fixed: outcome words incl. NOT ESTABLISHED; discordance-based power with amendments at S3, S4a, S4b; one gate method (changed by review 2, F12: an exact cluster sign-flip test replaces the betting confidence sequence); SHUFFLED-SUMMARIES lower bound; Tango and Newcombe intervals; HARM for every hypothesis | 2, 6, 7 |
| F13 budget unrealistic | fixed: per-kind tokens with build at real size; audit caps; gate cost; rate limits measured at S2; staged order with stop rules as an owner decision. Changed by review 2, F10: the evaluation cache that lowered the low figure is withdrawn; changed by review 3, F15: gate cost derived from its factors, cold routing of replay queries and DEV-BASE added to the default-order total, now 509,024 to 1,019,377 calls (10.3) | 10 |
| F14 revocation blockable and racy | fixed in DESIGN 4.4: authoritative in-memory epochs with a local journal (changed by review 2, F9: bounded, durability reported separately, fail closed; review 3, F9: durable start record), send fence with no peer I/O inside it (one per tenant since review 3, F7), break-glass revoke, two-administrator rule, no self-grant, class-grant removal bumps the epoch, tenant key owner for rotation (P15); probes R1 to R6 | 9.3 |
| F15 shared authz index | fixed: per-tenant `lc-<t>-authz`, global registry without principals, HMAC principal tokens, authz inside the erasure closure and T1 | DESIGN 5.10 |
| F16 older snapshot resurrects | fixed: tombstone journal outside the snapshot timeline, applied to every restore, keystore restores destroy keys named by newer tombstones; E3 and V5 cases (extended by review 2, F8: closure replay over every restored index and verifier residue checks before the tenant is enabled) | DESIGN 14.6 |
| F17 query-shaped artefacts survive | fixed: subject detection on query text; subject tokens carried into every query-shaped artefact; closure reverts them; E9 | DESIGN 14.2 |
| F18 edges not pinned | fixed: every edge and member version is bound to the task's pin. Write-path versions (insert-time edges, placement members) carry `src_seq` and are visible when the pinned write-path watermark P covers them, so a rejected candidate cannot strand them and pending members stay visible to holders; rebuild and reorganiser versions carry `gen_from` and need ancestry (kept for the class's life since review 2, F5; publication behind a refresh barrier since review 2, F4); retirement is a `gen_to` set; V2, V4, V6, V7 | DESIGN 5.3, 5.5, 5.6, 7.5 to 7.7, 13.1, 13.2; BUILD-PLAN Steps 12, 13 |
| F19 side-path leaks | fixed: cache class join and no cross-principal caching of prompt-bearing tasks; requester receipts over the read set; relabel as erasure (returning at the erasure boundary since review 2, F7); slow logs off and log grep in E0 and E1; no cluster privileges and cluster APIs in T1 | DESIGN 5.8, 14, 6.3 |
| F20 keystore and contention | fixed: keystore file per tenant; WAL checkpoint and scan of `-wal` and `-journal` with a positive control; fair queues at the model and embedding ports; per-tenant byte caps; T5 with a concurrency-limited stub and a disk-fill flood | DESIGN 3, 15 |
| F21 probes that cannot fail | fixed: every listed broken variant added with N; T3 normalised; T7 fixed bound; structural checks for real models | 9 |
| M1 missing edge cases | fixed: rows added | DESIGN 17 |
| M2 inconsistent numbers | fixed by measurement: whole pool = 6,018 + 11,016 + 1,430; the 51 excluded blocks are streamed as unscored filler; the two exposure statements reconciled; 6 fresh questions with gold in held_back haystacks form a DEV-EXPOSED stratum (H1 n = 293). UNRESOLVED: which fresh contents entered a lab prompt (Step 20 exposure audit) | 3.2, 3.3, 3.4 |
| M3 defence in depth overstated | fixed: per-class control index and the claim restated | DESIGN 6.3 |
| M4 log-row strip; numbering; multiplicity | fixed: DEVIATION 6 covers the strip; list renumbered in order; Holm reported for joint claims | DESIGN 22; 2.2 |
| M5 judge level and model | fixed: leaf-level validation with `has_answer` on development only; different-model judge by default, else SAME-JUDGE disclosed | 8 |

### 13.2 Second review (review 2, F1 to F14)

The second review returned BLOCK with 14 findings. This revision prefers
removing or simplifying mechanism (the evaluation cache, the betting
confidence sequence, gists in returned pointers, the erasure lease wait and
the realtime tombstone multi-get are gone) and adds only what a finding
needs. The owner's method is unchanged.

| Finding | Change | Where |
|---|---|---|
| F1 H3 "beyond memo" excluded the memo treatment | LOOP vs QUERY-MEMO is decided on fused final-list recall@10 at the same 10-session output budget, so both treatments count; tree-only recall stays the decided measure for "history helps" (LOOP vs FROZEN) and is reported for LOOP vs QUERY-MEMO as structural only; a failing-first check with a perfect memo list catches a regression to tree-only scoring | 1, 2.2, 5.2; BUILD-PLAN Step 20 |
| F2 end-pool strata miss earlier offers | strata from A0 ranks against the corpus prefix at each scheduled search, a selection device only; an offer audit over the logged offer rows (DESIGN 9) moves any plant offered by any source before its repair to stratum 1, with counts and failed row writes reported; decoys not reclassified (changed by review 3, F2: the move is withdrawn; the audit is descriptive and repair has one definition) | 3.6, 6 (NO-AUDIT), 7.2, 13.1 (F3 row) |
| F3 support of 5 made H4 repair impossible | re-filing support `S_MIN = 2` missed relevant searches (ASSUMPTION), evidence accumulated across cycles; eligibility reported apart from repair | 3.6, 12 (X9); DESIGN 12 |
| F4 P and heads published before searchable | searchable-publication barrier: P and heads move only after an explicit refresh of the touched member, edge and node indices returns (for a cancelled write-path job, after its deletes); V2 with refresh paused independently of acknowledgement | 9.5 V2, 9.6; DESIGN 5.6, 7.6, 13.2; BUILD-PLAN Step 0 (P5), Step 13 |
| F5 ancestry truncation | ancestry ids kept for the life of the class; retention removes content only; V2 and V4 across a real retention expiry, including a retired edge | 9.5 V2, V4; DESIGN 5.6, 13.2; BUILD-PLAN Step 13 |
| F6 W advanced past a failed node | a failed build of a required node aborts publication of that generation; members stay pending; V7 tests tree-only reachability under a partial rebuild failure; a persistently failing node is a stated limit | 9.5 V7; DESIGN 7.6, 17, 20; BUILD-PLAN Step 13 |
| F7 erasure promised before the closure existed | one publication boundary: closure under the tenant's write gate, then tombstone and complete redact set published together; "erased" promised from the boundary, which follows the request within `B_ERASE` (measured); the 15-minute lease wait is removed; a response read before the boundary is aborted at its next send hand-off (only bytes already handed to the kernel, at most 64 KiB, may arrive, as for revocation; changed by review 3, F7: one per-tenant send fence makes check and write atomic with the boundary, and the bound is per connection, on bytes the kernel still holds at the boundary, measured at the gateway); E8 pauses before and after the boundary and between a read and its send | 9.4 E7, E8; DESIGN INV-9, 2A, 14.1, 14.3, 14.5, 14.6; BUILD-PLAN Step 14 |
| F8 restore left authz residue | after any restore: journal applied, closure replayed over every restored index (authz and control included), verifier residue checks with direct reads, then the tenant is enabled | 9.4 E3, 9.5 V5; DESIGN 14.4, 14.6; BUILD-PLAN Step 14 |
| F9 revocation tied to an unbounded journal | bounded preallocated per-tenant journal with compaction; `revoke` returns once memory denies and reports durability separately; journal fault or incomplete journal at start closes the tenant; the shutdown record follows the drain of pending appends; no fence across I/O; R7 (no space, delayed fsync, stop at every persistence boundary, clean stop with an append pending; extended by review 3, F9: durable start record, compaction ordering, simulated power loss) | 9.3 R7, 9.6; DESIGN 2A, 3, 4.4, 17, 20; BUILD-PLAN Step 15 |
| F10 evaluation cache escaped erasure | removed; budgets recomputed without sharing by `evalkit/budget.py`; E1 also greps every file the gateway, workers and model port write outside Elasticsearch's data directory, with the removed cache as a broken variant, so a model-output store outside the closure cannot come back unnoticed | 9.4 E1, 9.6, 10.1, 10.3; DESIGN 18.1, 19; BUILD-PLAN Steps 14, 20 |
| F11 gist returned leaf content without lookup | returned pointers carry pointer, title, size, path, reason and confidence only; as of review 2 (history, no longer current): titles at most 12 words and 5-gram checked at every level, and a leaf's stored title the code-built title set at store; outline labels checked; short-leaf limit stated; three A8 broken variants in every document. Current state since review 3, F11 (13.3): every title code-built without digit tokens, no title 5-gram check; INV-7 read as a field contract, not semantic non-disclosure | 3.8, 5.2, 9.2 A8, 9.6; DESIGN INV-7, 5.2, 7.4, 7.6, 8.2 to 8.4, 8.7, 18.2, 20; BUILD-PLAN Steps 6, 10 |
| F12 gate statistics did not fit the sampling | exact one-sided cluster sign-flip test, fixed horizon, once per candidate, K >= 8, formula written; simulation with correlated paraphrases, power, repeated candidates and an asymmetric null in both directions (`evalkit/gate_power.py`); the anti-conservative direction (rare large losses: 0.17 at K = 8) is a stated limit | 7.3; DESIGN 13.3, 20, 22; BUILD-PLAN Step 19 |
| F13 S0 exceeded its wall budget | S0 split into resumable checkpoints S0-core and S0-load; S0 passes only when both pass, and the S0 report has a failing-first check that refuses a partial set; wall time per checkpoint stated; build steps run the T5 floods only in a short smoke form, never reported as a T5 pass | 10, 11; BUILD-PLAN 0, Steps 5, 22 |
| F14 Wilson on count metrics | bootstrap over recalls for opens per recall, mean opened share and `ask` share; Wilson only for the binary behavioural-use share; the interval code refuses Wilson on a measure not declared binary (failing-first) | 3.8, 7.1; BUILD-PLAN Step 20 |
| Also: RECENCY-ONLY lag | `L` is per stream and may be raised only on that stream's development stage, in its freeze commit; a failed confirmatory replay is VOID and not rerun; the run driver refuses a confirmatory run whose values differ from its freeze commit, and a second stamped run | 3.4, 3.7, 6; BUILD-PLAN Step 20 |
| Also: stage order | S0, S1, S2, S3, then S6-H1 first, with its printed sub-budget; H2, H3, H4 and the comparisons follow, still registered; reductions stay the owner's decision. Each development choice is frozen before the first confirmatory stage that uses it; C1 and C2 rows and the development ablations are computed after S6-H1, the ablations under their own approval and cap | 3.7, 4, 10, 10.3, 12 (X6) |
| Also: build order | security lifecycle from the first steps; edges, reflection, reverse audit and reorganiser first as minimal real producers with stub models; refinements and comparison arms after H1; Step 21 not needed for H1 | BUILD-PLAN 3, Steps 12, 17 to 22 |

### 13.3 Review 3 responses

The third review returned BLOCK. It closed ten of the second review's
fourteen findings (F12 only with a limited reading: the gate is an
inspectable heuristic; F13 at the planning stage), left F2, F7, F9 and F11
open, and added F15. F7, F9 and F11 had to be fixed before the stub stage
(S0); F2 and F15 before their later stages. Review ended here, by the stated
cap of one independent review plus one delta: the fixes below were not
independently re-reviewed. Checks of the first fix revision against the
review found further gaps, also fixed below and also not re-reviewed: a
clean stop could write its shutdown record while a revoke was still
arriving, break-glass recovery reopened every administrator before
re-applying revokes that never became durable, compaction freed journal
space another tenant could take (F9); the 64 KiB residual was stated at the
peer instead of at the gateway, and no connection cap bounded the aggregate
(F7); INV-7 claimed that dates and codes never reach a title, which holds
only for their digit forms (F11); the low cold-routing figure assumed the
guard searches were already routed by the service principal (F15).

| Finding | Fix | Where |
|---|---|---|
| F7 erasure publication and the final send used different synchronization (before S0) | one per-tenant send fence in gateway memory serves both revocation and erasure publication, replacing the per-principal fence; every final send check (epoch and tombstone sequence) and its non-blocking socket write hold its shared side; revoke's epoch flip and the in-memory publication step hold its exclusive side; no journal, disk or Elasticsearch I/O under either side; it is the innermost lock (taken under the write gate, never the reverse), so no lock-order inversion; the wording that let a hand-off whose check preceded the boundary write after it is removed; residual: only bytes handed to the kernel before the boundary can still reach the peer; those the kernel still holds at the boundary are at most 64 KiB per connection (the gateway sets each connection's send buffer and checks its effective size, ASSUMPTION) and the tenant's open-connection cap (DESIGN 15) times 64 KiB in aggregate, measured at the gateway, since bytes already in the peer's receive buffer are outside it (a tightening of the decided wording); E8 (d) pauses 8 concurrent senders after their final check, with a broken variant whose send does not hold the fence; the broken variant "socket write inside the fence" became "blocking socket write inside the fence", since the design now writes inside it, non-blocking | 9.3 R1, 9.4 E8, 9.6; DESIGN INV-9, 2A, 4.4, 5.9, 14.3, 14.5, 17, 19, 22; BUILD-PLAN Steps 14, 15 |
| F9 an undurable start record could hide an unclean run (before S0) | a start record made durable (fsync of the journal file, then of its directory) before any of the tenant's requests is admitted; compaction in the order write new file, fsync, rename, fsync directory, the new file carrying the start record, where the rename is an atomic exchange with the preallocated spare so no journal space is freed (the closest sound variant of the decided order; ASSUMPTION: the filesystem supports an atomic name exchange, tested at provisioning); no shutdown record is ever written for a closed tenant, so a persistence fault stays closed across restarts until explicit break-glass recovery; a clean stop closes admission and the revoke lane before the drain; break-glass recovery first journals the revokes from receipts without `durable_at`, then reopens grant calls only to administrators the operator names (a lost receipt is a stated limit); R7 adds simulated power loss before the start record's fsync, between a denial and its fsync after a previous clean shutdown, at every compaction step and during recovery, plus a revoke arriving during a clean stop, each with a broken variant, and checks durable revokes on the journal-rebuilt table itself; the simulation lets data and name changes not yet fsynced each survive or be lost, in every combination, so an exchange before the file fsync goes red | 9.3 R7, 9.6; DESIGN 4.4, 15, 17, 19, 20, 22; BUILD-PLAN Steps 5, 15, 22 |
| F11 the 5-gram rule cannot enforce INV-7 (before S0) | INV-7 is an enforceable field contract: no body, gist or fact field in a pointer or the digest; every title code-built (top 4 tf-idf terms) with no token containing a digit, so model-written titles and their 5-gram check are gone (digit-free forms such as a month name still reach a title, and reasons have no digit rule: stated in the limit); reasons and outline labels bounded (12 words, 5-gram filter); stated as a limit that descriptors can convey an answer, so "not opened" never means "not used" and is never evidence of irrelevance by itself; A8 tests the field contract and the digit rule only; the reading is an ASSUMPTION about the owner's intent (12, X9); the counterfactual sample of `used_behavioural` draws from unopened offered rows too, and edge decay without descriptor-only confirmations is disclosed | 3.6, 3.8, 4.1, 5.2, 9.2 A8, 9.6, 12 (X9); DESIGN INV-7, 5.2, 7.6, 8.2 to 8.4, 8.7, 9 to 12, 16.4, 17, 18.2, 20, 22; BUILD-PLAN Steps 6, 10, 13 |
| F2 the offer audit conditioned the cause comparison on exposure (before S4b and S7b) | the confirmatory cause comparison (LOOP vs NO-AUDIT, and existence vs FROZEN) runs on the prefix-selected strata, fixed before any arm runs and identical in every arm; the offer audit is descriptive mechanism evidence and never moves or removes a plant; repair has one definition, held-back tree retrieval at probe 2, and no metadata or membership change ends a window; NO-AUDIT keeps a switch check, and its repair in strata 2 and 3 is reported, not a void condition; Step 20's reclassification tests are replaced by a test that the confirmatory population is identical across arms | 2.2, 3.6, 6, 7.2, 13.1, 13.2; BUILD-PLAN Step 20 |
| F15 the budget undercounted the gate (later stage) | `evalkit/budget.py` derives each candidate's gate cost from the stated factors (202 to 712 calls), adds the cold current-generation routing of replay queries without cached routing (every one of the 80 replay queries per candidate, at the stage's per-recall figure, ASSUMPTION; a check after review 3 found that the earlier low reading of 60 contradicted its own premise, since the 20 guard searches are held-out searches too), and prints the total for the default order with DEV-BASE: 509,024 to 1,019,377 calls; the H1-first path is unchanged at 32,537 to 58,305 | 10.1, 10.3, 13.1 (F13 row) |
| Also: the gate's meaning | the gate is an inspectable heuristic, not a 5% false-adoption guarantee; its p-value and RANDOM-FEEDBACK are never cited as proof of a bounded false-adoption rate; sign scoring measures how often clusters improve, not by how much, so the outcomes of H3 and H4 rest only on their independent forward and held-back measures | 7.3; DESIGN 13.3, 20 |

Nothing in these revisions was run except the dataset measurements marked
MEASURED, the gate simulation of 7.3 (`evalkit/gate_power.py`) and the budget
arithmetic of 10.3 (`evalkit/budget.py`), both plain Python with no model
calls; their printed numbers are quoted as printed.

## 14. Amendments

Each amendment is committed alone, before the run it affects, and moves no hypothesis, arm definition, threshold, statistic or
control of sections 1 to 8.

### 14.1 DEVIATION (order only): S1 before S0 (2026-11-19)

- Registered: S0 (both checkpoints) passes before S1 (section 10, order in 10.3).
- Amended: stage S1 runs before S0 completes. Reason: the owner asked, in chat on 2026-11-19, for the first question (H1,
  tree with agent routing against flat search) to be measurable in parallel with the security and plan-order build, and approved
  the 25-call canary for it. S1 touches no tenant data and no security-relevant path; its question is whether each task kind
  returns schema-valid output through the real model client and what each call costs.
- Scope: stage S1 only, the canary of section 10, on the synthetic fixture (no dataset content), with the headless CLI on the
  subscription and the Sonnet tier (X5, X7). Hard cap of 25 real model calls in total, enforced by the durable counter that is
  charged before every call; call 26 is refused without starting a process. This amendment approves nothing after S1: S2, S3,
  S6-H1 and every later stage still need the owner's approval of that stage and its cap, and the fresh set stays unreleased.
- What it does not change: S0 must still pass in full before any stage that the registered order places after it relies on a
  security claim; S1's results make no security claim.
- Consequence for reading S1: its figures describe the client and the prompts on a fixture of synthetic notes, where token counts
  per character differ from English text. Section 10.2 is replaced by measured figures only through the separate amendment that
  follows the run, which states the prompt sizes beside each figure.

### 14.2 Measured per-call figures from S1 (replaces the CLI column of 10.2; 2026-11-19)

Run: stage S1 as amended in 14.1, on commit 924c9f6, the synthetic fixture (64 leaves of nonsense-word notes, four of them long),
the headless CLI on the subscription, model tier Sonnet; the CLI reported one model id on every call (claude-sonnet-5-5) and one turn
per call. 22 of the 25 permitted calls were made (the durable counter reads 22; 3 held in reserve and not used). No call was
throttled, none failed, all 22 outputs were schema valid and all 22 passed the consumer-strict checks (entry count equals leaf
count, labels among those presented, confidence 0 to 3, reasons and labels at most 12 words, pointers among those presented).

Invocation (frozen, part of the registered client from here on): the CLI from an empty working directory with the prompt on stdin,
`-p --model <id> --output-format json --no-session-persistence --tools "" --disable-slash-commands --strict-mcp-config` and the
settings overlay `{"disableAllHooks":true}`, a minimal system prompt that asks for one JSON object, and the child environment
switches that turn off instruction files, auto-memory and self-update. Not `--bare` (it does not read the subscription login).

Fixed per-call overhead, measured: about 645 input tokens (a 158-character prompt came to 682 total input tokens and a 178-character
prompt to 689). The 10.5k figure of 10.2 was measured on a default CLI session (default system prompt, tools, instruction files);
with this invocation the CLI basis is the API basis plus about 645 tokens, not plus 10,500. The prompt is billed as cache creation by
the CLI (all 22 calls: 2 uncached input tokens, the rest cache creation, no cache reads); the totals below add the three input fields.

| Task kind | Calls | Prompt chars | Total input tokens (CLI) | Output tokens | Latency s (range) | Valid |
|---|---|---|---|---|---|---|
| route | 8 | 4,403 to 5,206 | 2,714 to 2,972 | 24 to 391 (median 24) | 2.05 to 4.33 (median 2.09) | 8 of 8 |
| organise | 2 | 1,024 | 1,251 and 1,257 | 427 and 380 | 3.94 and 3.66 | 2 of 2 |
| build_l1 (16 leaves, 3,800 characters) | 4 | 3,791 to 3,806 | 2,392 to 2,444 | 1,559 to 1,836 | 9.27 to 10.71 | 4 of 4 |
| build_upper (4 summaries) | 1 | 3,605 | 2,090 | 176 | 3.00 | 1 of 1 |
| type_edges (c0 and 5 items) | 1 | 2,475 | 1,842 | 115 | 2.54 | 1 of 1 |
| judge_relevance | 1 | 1,870 | 1,558 | 18 | 2.03 | 1 of 1 |
| query_variants | 1 | 158 | 682 | 69 | 2.37 | 1 of 1 |
| synthetic_queries | 1 | 1,819 | 1,526 | 138 | 2.96 | 1 of 1 |
| ask | 1 | 1,861 | 1,560 | 107 | 2.81 | 1 of 1 |
| reader_answer | 1 | 1,821 | 1,540 | 78 | 2.49 | 1 of 1 |
| answer_judge | 1 | 178 | 689 | 9 | 1.88 | 1 of 1 |

What this does and does not replace. The fixed overhead (645 tokens against 10,500), the output tokens, the latency per kind and
the validity of every kind are measured. The per-kind INPUT sizes are fixture-sized: the 16 leaves of a build_l1 prompt fit in 3,800
characters of prompt including its instructions (10.1 assumes leaves of 1,100 tokens), nonsense words cost about 0.46 tokens per character
where English costs less, and the route entries are short. So the registered API ranges of 10.2 stay as the estimate for real-data
inputs and S2 measures them at real sizes; the measured totals above are lower bounds for kinds whose input scales with text
(build_l1, build_upper, route, type_edges, judge_relevance, ask, reader_answer). Output tokens per build_l1 call (about 1,700, for 16
entries and a summary) scale with the number of leaves, not their length. Latency measured on short prompts is a floor.

Effect on 10.3 (`evalkit/budget_measured.py`, which runs `evalkit/budget.py` unchanged with the measured overhead; the calls per
stage do not change, because S1 measures tokens and time, not how many calls a stage makes):

| Stage | Calls | CLI input Mtok (registered API ranges plus 645 per call) | Fixture-sized lower bound, Mtok | Output Mtok | Hours at the measured mean latency per kind, concurrency 2 (a floor) |
|---|---|---|---|---|---|
| S2 | 2,391 to 5,454 | 14 to 46 (10.3 had 37 to 100) | 6 to 13 | 0.9 to 1.6 | 1.3 to 2.6 (10.3 had 3 to 7 at 9.36 s per call) |
| S3 | 7,022 to 17,252 | 43 to 152 (112 to 322) | 16 to 41 | 2.8 to 5.2 | 3.8 to 8.3 (9 to 22) |
| S6-H1 | 23,104 to 35,574 | 144 to 398 (371 to 749) | 58 to 89 | 11.0 to 16.9 | 13.8 to 21.2 (30 to 46) |
| S2 + S3 + S6-H1 | 32,517 to 58,280 | 200 to 597 | 80 to 143 | 14.7 to 23.7 | 18.9 to 32.1 (42 to 76 for the path with S1) |

The hours are a floor and not a promise: the measured latencies come from 22 sequential calls on short prompts, the subscription's
sustained limits were not exercised (no call was throttled), and S2 measures calls per 5-hour window (section 10, S2 gate).
The rest of 10.3 (S6-H2 and the loop stages) is not restated here.
