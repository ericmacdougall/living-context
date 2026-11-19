# living-context: experiment plan (pre-registration)

Status: written before any run, revised once after an adversarial review
(findings F1 to F21 and M1 to M5; section 13 maps each to its fix). This file
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
| H3 | Does the feedback and reorganisation loop improve recall forward in time, beyond simply re-using history? | LOOP vs FROZEN, and LOOP vs QUERY-MEMO (E2a) | tree-only recall@10 on exposed recurring queries; real-question recall as a non-inferiority guard |
| H4 | Do reflection and the reverse audit repair exposure bias, and is the repair driven by past searches? | LOOP vs FROZEN vs NO-AUDIT, plants vs decoys (E2b) | share of planted leaves offered by the tree in the top 10 for their held-back probe |
| S | Do tenant isolation, access policy, erasure and versioning hold? | security track, every build step | zero failures, with stated N |
| C | How do the comparisons do? | C1 (Elasticsearch-query routing), C2 (collapsed retrieval) vs A1 | same as H1, reported only |

Cost is reported beside every recall figure: model calls by task kind, input
and output tokens, wall latency (median and p90), Elasticsearch requests per
recall, cache hits (section 10.1).

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
  - "History helps": LOOP minus FROZEN on the primary.
  - "Reorganisation helps beyond memo": LOOP minus QUERY-MEMO on the primary.
    Only this reading supports the claim that the owner's reorganisation does
    the work.
  - Guard: real-question fused recall@10, LOOP minus FROZEN, lo >= -0.02. If
    the lower bound is below that, H3 FAILS ITS GUARD whatever the primary.
- H4 HOLDS only if all three hold, each with lo > 0:
  - Existence: LOOP minus FROZEN repair over all plants (paired; Tango score
    interval).
  - Cause: LOOP minus NO-AUDIT repair over plants in strata 2 and 3 (section
    3.6; paired; Tango).
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
   fully inserted (pre-registered). Contents already present are skipped
   (content hash). So the newest contents at question time are mostly other
   blocks', not that question's gold.
3. Exploration slots (DESIGN 9) follow the same policy and seed sequence in
   every agent-routed arm, FROZEN included, and their candidates exclude leaves
   inserted within the last `L` blocks. Exploration slots are logged and may
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
  and probe 1), computed at stream construction over the E2b end pool: stratum
  1 best rank 1 to 10, stratum 2 best rank 11 to 50, stratum 3 best rank above
  50. So in strata 2 and 3 the direct search never offers the planted session
  in its digest top 10 for any in-stream search, and neither reflection pool
  (b) nor an opened hit on a direct offer can repair it. Plants are chosen with
  quotas S7b 12 / 24 / 24 and S4b 8 / 16 / 16 (if a stratum cannot fill, the
  shortfall is reported and filled from the next stratum up). Pre-registered
  predictions: in stratum 1 the direct-search pool (b) of reflection may
  repair; in strata 2 and 3 only the deeper pool (c) and the reverse audit can;
  stratum 3 is where the reverse audit's rank-independent percolation and
  query-vector kNN should matter most. Stratum 1 is reported but excluded from
  the cause and search-driven comparisons.

Planting (review F3a, F3b):

- When a target block is inserted, the leaves of its chosen gold session are
  placed under the least-similar level-1 node instead of the placement rule's
  choice (the "host"). Each planted leaf gets plausible wrong metadata: the
  title and gist of a real child of the host node (the "donor sibling",
  seeded), not a marker such as "misc notes". Text is unchanged, so the direct
  search can still find it (the safety net is part of the system).
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
  differences.

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
  opened-share bounds) are frozen in a commit before S6 and S7; the freeze
  commit is named in the results.

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
- S0 stub reader: opens the pointers whose title and gist score in its own top 3
  (a scorer separate from the router), deterministic and gold-blind.
- Pre-registered bounds: per arm, the mean share of offered pointers opened per
  recall must lie in [0.05, 0.60]. Outside, the opened signal is void, and H3
  and H4 read NOT ESTABLISHED (signal void). This catches both a reader that
  opens nothing and a reader that opens everything (which would bring back the
  exposure-biased co-offer signal, DESIGN 11.1).
- Reported per arm, each with a Wilson interval: opened rate, opens per
  recall, `ask` share of opens, and the share of `used_behavioural` rows in the
  behavioural-use sample.

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
embedding-valley segmentation.

### 4.1 Lookups forced

Every arm returns pointers only. Recall metrics are computed from the offered
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
- Tree-only recall@10: the same on the tree list alone (H3 primary; also
  reported for every arm).
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
- Holder reasons: share rejected by the 5-gram leak check (DESIGN 8.2),
  per task and arm.
- Cost: model calls per recall by task kind, input and output tokens, wall
  latency p50 and p90, Elasticsearch requests, holder calls that hit the
  budget, fallbacks, hallucinated ids, failures, cache hits.
- Answer accuracy (secondary): only reported if the answer judge passes J1
  (section 8.4); never used for a decision.

## 6. Controls that must fail for their stated reason

Each control has a pre-registered expected failure and a check that it failed
for that reason (not a crash). A control that does not fail voids its arm. All
deciding controls run agent-routed; the proxy world only supports.

| Control | For | Broken change | Must show (decision rule) | Check that it is the stated reason |
|---|---|---|---|---|
| SHUFFLED-SUMMARIES | A1-tree-only (no direct list, so the safety net cannot mask the drop) | node summaries and child entries permuted among nodes of the same level | drop in A1-tree-only recall@10 with point at least 0.10 and clustered-bootstrap lower bound above 0, on 50 questions (stub gate: 0.30 on the fixture) | holder logs show selections following the shuffled text |
| RANDOM-EDGES | A2 | edges rewired to random leaves of the same class, degree kept | A2's gain over A1 on all-gold@10 disappears: upper bound of (A2-RANDOM minus A1) below the A2 minus A1 point estimate, and point at most +0.01 | expanded leaves are non-gold at the base rate |
| RANDOM-FEEDBACK | LOOP (E2a and E2b, agent-routed) | opened, used and judge signals replaced by random draws at LOOP's measured rates; no reader | E2a: RANDOM-FEEDBACK minus FROZEN on the H3 primary has hi < m; E2b: its repair in strata 2 and 3 at most 0.10. Valid only if the gate positive controls that cover that stage held (7.3) | gate decisions and adopted operations logged; repairs, if any, traced to random signals |
| SHUFFLED-LOG | LOOP (proxy only, supporting) | log rows reassigned to random searches | as RANDOM-FEEDBACK, reported | same |
| NO-AUDIT | H4 cause (E2b, agent-routed) | reflection's deep pool (c) and the reverse audit removed; pools (a), (b) and opened kept | its own expected failure, separate from the H4 decision: repair in strata 2 and 3 at most 0.10, checked in NO-AUDIT's own logs (strata are defined so that no in-stream direct offer exists there, 3.6) | every NO-AUDIT repair traces to a pool (a) or (b) note or an opened signal; a repair in strata 2 or 3 traced to a direct offer means the strata were mis-computed and voids the cause test |
| FROZEN-CEILING | E2b plants | none (FROZEN as run) | FROZEN repair over all plants at most 0.10 | any FROZEN repair traced to normal rebuilds; plant survival logged |
| DECOY | H4 search-driven | decoy plants (no relevant constructed search) | the search-driven condition of 2.2 | decoy repairs traced to their signals |
| RECENCY-ONLY | E2a and E2b replay | offer the 10 most recently inserted leaves' sessions | upper 95% bound of (RECENCY-ONLY minus RANDOM-10) recall@10 at most 0.03; otherwise the replay is void and `L` is raised by amendment | the 10 most recent contents logged per question |
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
- Single rates: Wilson 95% intervals; never used for differences.

### 7.2 Minimum effects and power

- Minimum effects: H1 +0.05, H2 +0.05, H3 +0.05 with non-inferiority margin
  -0.02, H4 +0.25. They define DID NOT HOLD and NOT ESTABLISHED (2.1); they
  are no longer a second pass condition.
- Sample sizes: H1 n = 293 primary (299 sensitivity; 312 with abstention,
  reported apart); H2 n = up to 175; H3 the exposed recurring queries
  (expected 60 to 150 of about 210) and 312 real questions; H4 48 plants in
  strata 2 and 3, 60 in all, 20 decoys.
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
  amendment before S6 or S7, stating the true effect at which power reaches
  80%. Minimum effects do not move.
- The low-overlap stratum (about 80 questions expected) resolves only about
  0.13 at d = 0.2 and is reported, not decided.

### 7.3 The replay gate inside the loop (DESIGN 13.3; review F4, F12d)

- One method: a one-sided anytime-valid confidence sequence for the mean of
  per-search differences in [-1, 1]: a one-sided betting capital process for
  "mean <= 0" with a predictable plug-in bet, capped at 1 on the [0, 1] scale
  (SOURCE for the method family, read 2026-11-19: Waudby-Smith and Ramdas,
  "Estimating means of bounded random variables by betting",
  https://arxiv.org/abs/2010.09686; the specific bet is ASSUMPTION and is the
  one simulated below). alpha 0.05,
  minimum n 20, at most 60 targeted searches per candidate, a look after every
  search (looks are free for a confidence sequence).
- Power, simulated (ASSUMPTION model of differences: +1 with probability p+,
  -1 with p-, else 0; `evalkit/gate_power.py`, 4,000 runs each): with
  p- = 0.05, adoption probability at 20 / 40 / 60 targeted searches is
  0.01 / 0.23 / 0.47 for p+ = 0.20; 0.12 / 0.64 / 0.86 for p+ = 0.30;
  0.36 / 0.89 / 0.98 for p+ = 0.40; 0.83 / 1.00 / 1.00 for p+ = 0.60; and
  0.00 when nothing changes. Hence targeted replay: a general replay of 20
  unrelated searches carries a mean near 0 and could never adopt anything.
- Targeted replay set per candidate: held-out logged searches whose
  judged-relevant items include a leaf the candidate changed, topped up to 60
  with gate-only synthetic queries generated from the changed leaves,
  judge-filtered, tagged synthetic, never stored, never probes. The primary is
  tree-source recall of judged-relevant items within the top 10.
- Unjudged items that the candidate offers are judged at replay by the same
  blind judge, inside the budget (DESIGN 13.3), so new items are neither
  assumed irrelevant nor assumed relevant.
- General replay guard: the 20 most recent held-out logged searches; the guard
  passes when at most 2 of them lose a judged-relevant item from the top 10 and
  losses do not exceed gains (ASSUMPTION; a count rule, because a 20-search
  interval cannot resolve a -0.02 margin).
- Positive controls, in S0 (stub) and again in S4b: a known-good candidate (an
  oracle re-filing of the planted leaves to the nodes the placement rule would
  have chosen) must be adopted, and a known-bad candidate (the same number of
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
it red for its stated reason. Fixture: tenants `ta` and `tb`, each with classes
`base` < `restricted`, plus a third tenant `tbx` whose name extends `tb`
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
| T1 read | with `ta`'s gateway session and, separately, with each of `ta`'s raw keys: search, get, mget, msearch, count, kNN, percolate, terms lookup, `_cat/indices`, `_field_caps`, `_mapping`, wildcard and comma index lists, alias names, against every `tb` and `tbx` index kind including `lc-tb-authz`; plus the cluster APIs `_cluster/state`, `_cat/shards`, `_cat/aliases`, `_tasks?detailed`, `_resolve/index`, `_search_shards`, `_nodes/stats`, `_cluster/stats` | every API x kind x direction, plus 300 randomised gateway requests with forged tenant fields and pointers | 0 results, 0 counts, 0 names, no query descriptions; 403 or the typed `unavailable` | role descriptor `lc-*`; gateway that reads tenant from the body; a key holding `cluster: monitor` (must leak through the cluster APIs) |
| T2 write | `ta` writes into `tb` indices or names `tb` in a body | as T1 | refused | same |
| T3 enumeration | does any normalised response, error, size, total or count differ when `tb` has 0 vs 10,000 leaves? | 300 paired requests | identical after normalisation | error text that names the index |
| T4 routing and links | `ta`'s fan-out never asks a `tb` holder; no edge, member or note links across tenants | every recall in the stub loop | 0 | edge writer that resolves endpoints across tenants |
| T5 availability | `ta` floods (8 idle connections, a 3 GiB line, 8 MiB lines, 2,000 concurrent recalls, model-bound tasks, a disk-fill write flood up to its byte cap); `tb`'s writes must succeed and `tb`'s recall p95 must stay under 1.5x its unloaded p95. Runs with a latency-injecting model stub that enforces one global concurrency of 2 and one shared embedder (the real contention points) | 3 runs of 10 minutes per flood kind | pass all; `ta` refused at its byte cap before the disk reaches the flood-stage watermark | no per-tenant caps; parse before admission (LAB-R5); no fair queue at the model port; no fair queue at the embedding port; no per-tenant byte cap |
| T6 key space | a constructed collision in a 32-bit hash of tenant plus principal | 1 constructed pair plus 10,000 random | no shared lock, cache, rate or idempotency slot | 32-bit hash key (LAB-R1) |
| T7 key invalidation | after invalidating `ta`'s keys, each fails within a fixed bound of 5 s (ASSUMPTION, fixed now; P6 measures the actual latency, and a measurement above 5 s fails T7 and is reported, the bound is not moved) | 100 | 0 successes after the bound; the gateway's own path refuses at once | gateway that caches an authenticated client |

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
| A6 relabel | a base leaf relabelled to restricted is `unavailable` to a base reader, and absent from every base artefact a base reader can reach, from the moment the relabel call returns (redact set at once, DESIGN 14.1) | relabel that waits for the next generation; relabel that leaves the base summary unchanged |
| A7 wall-clock timing | 2,000 interleaved requests per corpus, and the A4 pair; KS statistic under 0.0515 (alpha 0.01) and median difference under 2% | REPORTED, not gating: a failure is recorded as a disclosed timing channel (expected cause: shared node load; DESIGN 20). A1 to A6 are gating |
| A8 holder reasons | no holder reason in any digest shares a 5-gram with the child facts, gists or leaf text it was produced from; reasons are at most 12 words | holder whose reasons quote child facts |

### 9.3 Revocation (SEC-R)

| Probe | Pass | Broken control |
|---|---|---|
| R1 revoke a reader while it floods recalls, holds 8 requests open, reads its socket slowly, and logs out and in to re-arm; a test hook injects a delay between the final epoch check and the hand-off to the kernel | 0 responses handed to the kernel after the epoch flip (gateway trace), in 100 of 100 trials plus 100 trials with the injected delay; bytes arriving after `revoke` returns come only from responses handed over before the flip and total at most the socket send buffer; `revoke` returns within 2 s at p99 | revocation checked only at session start; revoke that waits on a lock the session holds; no send fence (the injected delay must make it red deterministically); socket write inside the fence with a slow-reading client (revoke latency over bound) |
| R2 revoke a writer mid-write | no write admitted after `revoke` returns; writes admitted before commit within 30 s | same |
| R3 revoke under stress | revoke still effective and under 2 s at p99 under a flood from the revoker's own tenant, and with `lc-<t>-authz` made unwritable (index blocked read-only, and the disk at the flood-stage watermark, P14) | revoke sharing the general admission queue; revoke that requires the Elasticsearch write to succeed |
| R4 administrators | a tenant administrator cannot grant itself any role or class; revoking an administrator or a revoker needs two distinct administrators; the operator's break-glass revoke works when every tenant administrator is compromised | self-grant allowed; single-administrator revoke of a revoker; break-glass removable by a tenant administrator |
| R5 class-grant removal | removing one class from a principal bumps its epoch; an in-flight fan-out over that class returns nothing further from that class | removal without an epoch bump |
| R6 key rotation | the tenant key owner (DESIGN 4.4) rotates and invalidates the tenant's keys without the bootstrap credential; the gateway key cannot | rotation that needs the superuser; gateway key with `manage_api_key` |

N: R1 200 trials (100 plain, 100 with the injected delay); R2, R3 100 each;
R4, R5, R6 20 each.

### 9.4 Erasure (SEC-E)

Per run: 20 subjects and 20 single items erased, chosen so that together they
reach every artefact kind (leaf, src, node in current and 2 retained
generations, member, edge, log row, stored query, note, judge label, cache,
manifest, replay set, authz record, query-shaped summary and edge; the
plaintext log files are grepped for every canary).

| Probe | Pass | Broken control that must fail |
|---|---|---|
| E0 positive control | before erasure the verifier finds every planted canary (short, long-compressed, vector, query-only, authz) by every read path, and sees `docs.deleted > 0` after deletes and before purge; the plaintext Elasticsearch, container and gateway log grep finds a separate control marker (never an erased canary) deliberately logged through a scratch index with the slow log enabled at threshold 0 | (none: if E0 fails the run is void) |
| E1 residue | after erasure: 0 canaries by text, kNN, percolate, get, lineage query, in every index (authz included), every retained generation, every replay set, and in the plaintext Elasticsearch, container and gateway logs; closure residue 0; `docs.deleted == 0` after purge | erase only the current generation; key destroyed only (embedding left behind); raw-file grep of segments as verifier (the long canary must make it miss); closure without the authz index |
| E2 paraphrase | a node whose summary paraphrases the subject without the canary string is caught by lineage | string-search-only closure |
| E3 resurrection | quarantine, retry, reseal, replay, rebuild, restore-from-snapshot and restore of an older snapshot together with an older keystore backup (both taken before the erasure) cannot bring erased content back; re-ingest of content naming the erased subject (in any canonical variant) is refused | reseal-before-quarantine; no erased-identifier HMACs; restore that serves before re-applying tombstones; tombstones kept only inside the snapshot (the older snapshot must resurrect content); keystore restore that does not destroy keys named by newer tombstones |
| E4 concurrency | erasure while a rebuild, a reflection pass and a gate replay are mid-flight | fence removed |
| E5 hold | legal hold placed: erasure queued, receipt states the delay; hold released: erasure completes | hold ignored |
| E6 receipt | receipt counts match an independent recount by the verifier; no id lists; the requester's copy shows only totals over the requester's read set | receipt that says done while a log row still names the subject; requester copy with per-class counts |
| E8 window | between the tombstone and the in-code rewrite, holders, organise calls, reflection and readers that touch an affected ancestor node receive the placeholder, never the old summary, gist or facts (prompts captured by the stub) | redact set not consulted when presenting nodes |
| E9 query-shaped artefacts | erasing subject X removes: X's own queries and log rows; other people's queries, stored queries, cache entries and notes that mention X; summaries re-made, `co_used` edges and memberships moved because of X's queries (reverted in every retained generation) | subject detection on leaves only; closure over `lin_subjects` of leaf-derived artefacts only |
| E7 scale and bound | a subject with 100,000 events in a 1,000,000-document tenant finishes; physical purge within `P_PURGE`; latency of other tenants during it reported | (scale test, no control) |

N: E1 to E4 and E9 run 3 times each (120 erasures, plus 60 for E9); 0
failures gives a Wilson upper bound of 3.1% per erasure; every residue is a
BLOCK. The older-snapshot restore case of E3 runs 3 times per run.

### 9.5 Versioning (SEC-V)

| Probe | Pass | Broken control |
|---|---|---|
| V1 pin generation G, reorganise to G+1 and G+2, resolve every pointer held from G | each returns `ok`, `superseded`, `moved` or `expired`, never a wrong leaf, in 1,000 of 1,000 | node ids reused for different content |
| V2 pinned reader consistency | a pinned task sees the same tree and the same edges for its whole life (except erasure rewrites) | reader that re-reads the head per wave; expansion that reads the latest edges |
| V3 pointer across erasure | `unavailable` | pointer that still resolves from a cached document |
| V4 two concurrent reorganisations plus writes | one manifest becomes current, the other discarded with its edges and members, no mixed generation, no lost leaf, in 100 of 100; rollback restores the previous tree and edges exactly | flip without compare-and-set; rollback that leaves the candidate's edges visible |
| V5 restore after erasure | erased content never served, including after restoring an older snapshot and an older keystore backup | restore without tombstone re-application; tombstones only inside the snapshot |
| V6 candidate isolation | edges and members written by a candidate generation are invisible to every reader until the gate adopts it, and disappear if it is rejected | edges visible at write |
| V7 no stranding | insert-time edges and placement members written while a candidate is open stay visible to tasks pinned after the write-path watermark covers them, whether the candidate is adopted or rejected, in 100 of 100 cycles (DESIGN 13.2) | write-path versions stamped with the next generation number, which the rejected candidate took |

### 9.6 Broken variants added by the review, with N (review F21)

| Variant | Probe | N |
|---|---|---|
| key with `cluster: monitor` | T1 | every cluster API x 2 tenants |
| cache class equals node class; route cache shared across principals | A5 | 300 queries |
| injected delay between check and send; slow reader inside the fence | R1 | 100 each |
| `lc-<t>-authz` unwritable during revoke | R3 | 100 |
| older snapshot plus older keystore backup | E3, V5 | 3 per run |
| expansion reads latest edges; candidate edges visible at write | V2, V4, V6, A2 | 100 |
| write-path versions stamped with the next generation number; P advanced without joining a timed-out link job | V7, V2 | 100 cycles each |
| relabel read before the next generation | A6 | 50 relabels |
| stub with one global model concurrency and one shared embedder | T5 | 3 runs per flood kind |
| subject detection on leaves only | E9 | 3 runs |
| holder reasons quoting facts | A8 | 300 queries |
| keystore key bytes left in the `-wal` file | BUILD-PLAN Step 4 | 1,000 keys |

## 10. Staged gates and model-call budget

Every stage needs the owner's explicit approval for that stage only, with a
hard call cap enforced by the counter in the model port (incremented before the
call; failures count). Approval for one stage is not approval for the next
(LAB rule). Heavy jobs run on a remote host, not the owner's laptop.

| Stage | What | Gate to pass before the next stage |
|---|---|---|
| S0 | stub-only loop on a synthetic fixture (2 tenants x 2 classes, about 300 leaves, planted needles, plants, decoys and fillers); all security probes and broken controls | every security probe passes and every broken control fails; stub A1-tree-only recall@10 on fixture queries at least 0.80; SHUFFLED-SUMMARIES drops A1-tree-only by at least 0.30; RANDOM-EDGES removes A2's gain on linked-needle queries; gate positive controls hold (oracle adopted, random rejected); planted repair LOOP minus NO-AUDIT at least 0.25 in fixture strata 2 and 3; decoy repair below plant repair; RECENCY-ONLY within margin; stub reader's opened share inside [0.05, 0.60] |
| S1 | canary: every task kind with the real model on the fixture (hard cap 25) | schema-valid outputs; measured tokens and latency per task kind replace the assumptions in 10.2 (amendment committed before S2); throttling responses recorded |
| S2 | small real: 30 pilot questions over their own haystacks (about 4,400 leaves); A0, A1, A2, C1, C2, SHUFFLED-SUMMARIES; relevance judge on 3 pools | pipeline runs end to end; failures under 5%; SHUFFLED-SUMMARIES fails for its reason; sustained calls per 5-hour window measured (amendment: wall days per stage) |
| S3 | dev static E1: 102 pilot questions, dev static pool; A0+LEN fitted; AGENTIC and AGENT-A0 on 50 questions; ablations on cheap arms; judge validation (8.2) and paraphrases (8.3) | exploratory figures; judge passes 8.2; power amendment for H1 and H2; freeze of thresholds, prompts and the merge rule committed |
| S4a | dev E2a: 137 blocks, recurring queries; LOOP, FROZEN, RANDOM-FEEDBACK agent-routed; QUERY-MEMO; proxy supporting arms; behavioural-use sample | loop runs; opened-share bounds hold; RECENCY-ONLY within margin; controls fail for their reasons; power amendment for H3 |
| S4b | dev E2b: 40 plants, 14 decoys, 40 fillers; LOOP, FROZEN, NO-AUDIT, RANDOM-FEEDBACK agent-routed; gate positive controls; LOOP+SYNTH and other proxy arms | gate positive controls hold; opened-share bounds hold; controls fail for their reasons; power amendment for H4 |
| S5 | optional: J1 human-labelled answer judge + answer-level on development | J1 passes |
| S6 | confirm static E1 on the fresh set, whole pool (PENDING RELEASE): A0, A0+LEN, A1, A2, C1, C2, SHUFFLED-SUMMARIES, RANDOM-EDGES; AGENTIC and AGENT-A0 on a seeded 100-question subset | H1 and H2 decided; H1b reported |
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
- EVAL-CACHE (ASSUMPTION, evaluation only, never in the product; the product
  cache does not share prompt-bearing calls across principals, DESIGN 5.8):
  every arm is a separate tenant, so arms share nothing through the product
  cache. Pre-registered ON for S2 to S7b (it changes variance, so it is not a
  run-time choice); the evaluation harness wraps the model port with a cache keyed on
  the exact model, prompt version and full prompt text (children are presented
  as local labels c1 to cN, so identical nodes give identical prompts). LOOP,
  FROZEN and the controls start from the same structure and diverge only after
  the first adopted change, so later arms re-pay only where they diverge. The
  LOW figure assumes a divergence share of 0.3 per additional arm; the HIGH
  figure assumes no sharing at all. Cache hits are counted and reported; a
  cached output is an identical input's output, so paired arms also share model
  noise on unchanged nodes.
- Reader sessions 1 to 3 calls; reflection 1 variant call plus 5 to 8 judge
  calls per stream item (NO-AUDIT: 1 plus 1 to 3); reverse audit capped at
  `N_AUDIT = 100` leaves per cycle and `K_CAND = 5` candidate searches per leaf,
  one judge call per leaf (DESIGN 12); gate per cycle per learning arm: 12
  generation calls, 30 to 60 judge calls and 80 replayed searches (60 targeted,
  20 general) at 2 to 8 routing calls each (gate replays run as the class's
  service principal, so the product cache, scoped to that principal, re-uses
  every unchanged node's routing for the same query; only nodes whose version
  changed are asked again).
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

| Stage | Calls low | Calls high | Input tokens, API basis | Input tokens, CLI basis | Wall hours at 9.36 s per call, concurrency 2 |
|---|---|---|---|---|---|
| S0 | 0 | 0 | 0 | 0 | 0 |
| S1 | 20 | 25 | under 1M | under 1M | under 1 |
| S2 | 2,391 | 5,454 | 12M to 43M | 37M to 100M | 3 to 7 |
| S3 | 7,022 | 17,252 | 39M to 141M | 112M to 322M | 9 to 22 |
| S4a | 21,721 | 76,101 | 105M to 647M | 333M to 1,446M | 28 to 99 |
| S4b | 19,111 | 74,342 | 89M to 559M | 290M to 1,339M | 25 to 97 |
| S5 (optional) | 1,180 | 1,180 | 3M to 8M | 16M to 20M | 2 |
| S6 | 28,233 | 54,206 | 153M to 497M | 449M to 1,066M | 37 to 70 |
| S7a | 65,567 | 185,807 | 265M to 1,444M | 953M to 3,395M | 85 to 242 |
| S7b | 33,269 | 123,533 | 132M to 845M | 481M to 2,142M | 43 to 161 |
| DEV-BASE alone (only when S4a has not run and S7b or S7a needs it) | 3,383 | 8,001 | 41M to 148M | 76M to 232M | 4 to 10 |
| Total S1 to S7 | 178,514 | 537,900 | 797M to 4,184M | 2,672M to 9,832M | 232 to 699 |

Breakdown (formulas, so they can be recomputed):

- S2: build 381 to 574; edges 330 to 1,100; A1 and SHUFFLED-SUMMARIES on 30
  questions each, 30 x (25 to 60); judge on 3 pools 180.
- S3: build 1,256 (membership 1.3) to 1,932 (membership 2.0); edges 1,086 to
  3,620; A1 102 x (25 to 60); SHUFFLED-SUMMARIES 50 x (25 to 60); judge
  validation and paraphrases 480; AGENTIC 50 x (1 to 3) agent calls plus 300 to
  1,800 explore holder calls; AGENT-A0 50 x (1 to 3).
- S4a: recalls (137 + about 85 recurring = 222) x (25 to 60) x 3 agent-routed
  arms, LOW with EVAL-CACHE sharing (8,880 to 39,960); readers LOOP and FROZEN
  444 to 1,332; reflection LOOP 1,332 to 1,998; QUERY-MEMO shadow reflection
  1,332 to 1,998; audit 9 cycles x 100 (450 to 900); re-summaries 300 to
  1,200; gate 2 arms x 9 cycles (3,600 to 12,600); incremental build 1,966 to
  3,276 per arm (3,146 to 9,828); edges 1,417 to 4,725; recurring generation
  120 to 360; proxy judge 500 to 1,000; behavioural 200.
- S4b: base build 480 to 740 and edges 443 to 1,475; recalls (120 in stream +
  54 probe 2 = 174) x (25 to 60) x 4 arms (8,265 to 41,760); readers 360 to
  1,080; reflection LOOP and NO-AUDIT 960 to 1,560; audit 300 to 600;
  re-summaries 300 to 1,200; gate 3 arms x 6 cycles (3,600 to 12,600); gate
  positive controls 400 to 1,400; stream build 1,090 to 1,680 per arm (2,071 to
  6,720); edges 938 to 3,125; probe generation 94 to 282; LOOP+SYNTH proxy 200
  to 600; proxy judge 500 to 1,000; behavioural 200.
- S6: whole-pool build 5,004 to 7,700; edges 4,329 to 14,432; A1 312 x (50 to
  77) = 15,600 to 24,024; SHUFFLED-SUMMARIES 50 x (50 to 77); AGENTIC 100 x (1
  to 3) agent calls plus 600 to 3,600 explore holder calls; AGENT-A0 100 x (1 to
  3). A0, A0+LEN, A2, C1, C2 and RANDOM-EDGES cost no model call.
- S7a: recalls (312 + about 210 recurring = 522) x (50 to 77) x 3 arms
  (41,760 to 120,582); readers 1,044 to 3,132; reflection LOOP 3,132 to 4,698;
  QUERY-MEMO shadow reflection 3,132 to 4,698; audit 500 to 1,000;
  re-summaries 1,200 to 4,800; gate 2 arms x 10 cycles (4,000 to 14,000);
  incremental build 4,000 to 6,700 per arm (6,400 to 20,100); edges 2,900 to
  9,700; recurring generation 299 to 897; proxy judge 1,000 to 2,000;
  behavioural 200.
- S7b: recalls (200 in stream + 80 probe 2 = 280) x (35 to 70) x 4 arms
  (18,620 to 78,400); readers 600 to 1,800; reflection LOOP and NO-AUDIT 1,600
  to 2,600; audit 500 to 1,000; re-summaries 600 to 2,400; gate 3 arms x 10
  cycles (6,000 to 21,000); stream build 1,650 to 2,550 per arm (3,135 to
  10,200); edges 1,174 to 3,913; probe generation 140 to 420; LOOP+SYNTH proxy
  200 to 600; proxy judge 500 to 1,000; behavioural 200.

Rate limits (review F13): the subscription's limits are not published as call
counts. The model client treats a throttle response as a pause, not a scored
failure, and records it; S2 measures sustained calls per 5-hour window, and an
amendment before S3 converts each stage into wall days. The hours above are a
floor. For scale, the earlier lab's largest approved stage was 800 calls and a
22,500-call extraction was never approved. The owner's routing costs 25 to 77
calls per recall; that cost is itself a result and is reported, not hidden.

Order and stop rules (open decision X6; recommended default, not in force
until the owner says so): phase 1 = S0, S1, S2, S3, S6 (H1, H2; 37,666 to
76,937 calls). Stop after S6 if H1 reads HARM, or DID NOT HOLD on both (a) and
(b). Phase 2 = S4b, then S7b (H4; 55,763 to 205,876 calls including DEV-BASE).
Phase 3 = S4a, then S7a (H3; 87,288 to 261,908 calls). Until X6 is answered,
the plan in force is the full plan above, stage by stage; every stage needs its
own approval anyway.

Other ways to cut the budget without changing the method, each the owner's
decision (section 12): shorter streams (cost scales about linearly with stream
length); drop S3 if S2 is clear; typed-edge candidates 3 instead of 5; the
direct API instead of the headless CLI (removes the 10.5k fixed overhead per
call).

## 11. Reporting rules

- Every result file carries: commit, stage, arm, pool, n, metric definition,
  interval method and seed, the call counter reading, cache hits, and whether
  the arm is agent-routed, proxy (DEVIATION 7) or COMPARISON (LAB: 0 of 276
  earlier result files carried all required fields).
- Ranked lists and per-question rows are committed so every figure can be
  recomputed from rows.
- One stamped run per stage; no code tree is synced mid-run.
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
| X6 | Order and stop rules for the stages (10.3) | full plan in force; recommended: phase 1, then phase 2, then phase 3, with the stop rule after S6 |
| X7 | Direct API instead of the headless CLI for model calls | headless CLI on the subscription |
| X8 | Confirmatory H1 denominator: drop the 6 DEV-EXPOSED questions (n = 293, 299 as sensitivity), or keep 299 and report the 6 apart | recommended and in force until answered: 293 primary (3.2). Reason: development runs will show those 6 questions' gold to models |
| X9 | Readings chosen on the owner's behalf, each reversible without changing the method: "never offered" read as `tree_missed` (DESIGN 10); reverse-audit synthetic queries off in LOOP (DESIGN 12); the gate's general-replay count guard (7.3); stream reader limits (3.8) | as written; each alternative is logged or run as a labelled variant where cheap (`any_missed`, LOOP+SYNTH) |

## 13. Review responses

The adversarial review (26 findings) was answered in this revision. None of the
fixes replaces the owner's method; the one unlabelled substitution it found
(the proxy world) is now DEVIATION 7 and decides nothing.

| Finding | Response | Where |
|---|---|---|
| F1 opened has no producer | fixed: a reader per stream item in every signal-producing arm; opened-share bounds [0.05, 0.60] or the signal is void; opened rate, opens per recall and behavioural share reported with Wilson intervals; no gold-dependent opener | 3.8, 10.3; DESIGN 9 |
| F2 "never offered" undefined; NO-AUDIT not independent | fixed: `tree_missed` per source (DESIGN 10); NO-AUDIT removes exactly pool (c) and the audit; strata by the best direct rank over the source question and probe 1, with per-stratum predictions; NO-AUDIT has its own expected failure in its own logs | 3.6, 4, 6 |
| F3 planting repairs by construction | fixed: donor-sibling metadata, pinning in all arms with survival logged, FROZEN ceiling, probes from question and answer only with a rank filter on probe 2, decoy plants, LOOP+SYNTH apart from the owner's reverse search (synthetic queries off in LOOP), learning frozen at the end checkpoint and probe 2 never stored. Answered in part: the source question and probe 1 are not rank-filtered, because their best direct rank defines the strata; stratum 1 is reported and excluded from the cause test instead, and strata 2 and 3 have no in-stream direct offer at all | 3.6, 4, 6 |
| F4 gate has no positive control | fixed: oracle and random candidates in S0 and S4b, which also cover E2a (same frozen gate; S4b must pass before S7a); targeted replay; simulated power; unjudged items judged at replay; general replay as a count guard | 7.3; DESIGN 13.3 |
| F5 recency is a gold proxy | fixed: lag L = 5 with interleaving; identical exploration in every arm, excluding recent leaves, and removed from metrics; RECENCY-ONLY control; `last_seen` hidden from holders | 3.4, 6; DESIGN 8.2 |
| F6 gold through non-id fields | fixed, and confirmed real: the records carry a turn-level `has_answer` flag (MEASURED); field whitelist and an all-fields ingest test across all task kinds | 3.1; BUILD-PLAN Step 20 |
| F7 H3 confounded and weak | fixed: QUERY-MEMO arm reading FROZEN's logs; tree-only primary; exposed stratum; schedule 2c to 4c; rank filter on recurring queries | 3.5, 4, 2.2 |
| F8 controls in the non-adopted routing | fixed: proxy world labelled DEVIATION 7; RANDOM-FEEDBACK and NO-AUDIT agent-routed on E2b decide; proxy is supporting only | 4.2, 6; DESIGN 22 |
| F9 H2 merge rule undefined | fixed: three-list RRF k = 60, cut 50, pre-registered now; linked list's own recall reported | 5.1 |
| F10 H1 from fusion or length | fixed: A0+LEN arm in the decision; tree-only contribution condition; per-source shares | 2.2, 4 |
| F11 agent-side method never exercised; reasons leak | fixed: AGENTIC vs AGENT-A0 as H1b (secondary, to avoid a fifth decided claim); reasons at most 12 words with a 5-gram leak check; A8 probe and Step 10 broken variant | 1, 4, 9.2; DESIGN 8.2 |
| F12 statistics do not fit | fixed: outcome words incl. NOT ESTABLISHED; discordance-based power with amendments at S3, S4a, S4b; one gate method (betting confidence sequence); SHUFFLED-SUMMARIES lower bound; Tango and Newcombe intervals; HARM for every hypothesis | 2, 6, 7 |
| F13 budget unrealistic | fixed: per-kind tokens with build at real size; audit caps; gate cost; EVAL-CACHE only in the low figure; rate limits measured at S2; staged order with stop rules as an owner decision. The honest total is 178,514 to 537,900 calls; it is larger than the first version's because the fixes add arms and controls | 10 |
| F14 revocation blockable and racy | fixed in DESIGN 4.4: authoritative in-memory epochs with a local journal, send fence with no peer I/O inside it, break-glass revoke, two-administrator rule, no self-grant, class-grant removal bumps the epoch, tenant key owner for rotation (P15); probes R1 to R6 | 9.3 |
| F15 shared authz index | fixed: per-tenant `lc-<t>-authz`, global registry without principals, HMAC principal tokens, authz inside the erasure closure and T1 | DESIGN 5.10 |
| F16 older snapshot resurrects | fixed: tombstone journal outside the snapshot timeline, applied to every restore, keystore restores destroy keys named by newer tombstones; E3 and V5 cases | DESIGN 14.6 |
| F17 query-shaped artefacts survive | fixed: subject detection on query text; subject tokens carried into every query-shaped artefact; closure reverts them; E9 | DESIGN 14.2 |
| F18 edges not pinned | fixed: every edge and member version is bound to the task's pin. Write-path versions (insert-time edges, placement members) carry `src_seq` and are visible when the pinned write-path watermark P covers them, so a rejected candidate cannot strand them and pending members stay visible to holders; rebuild and reorganiser versions carry `gen_from` and need ancestry; retirement is a `gen_to` set; V2, V4, V6, V7 | DESIGN 5.3, 5.5, 5.6, 7.5 to 7.7, 13.1, 13.2; BUILD-PLAN Steps 12, 13 |
| F19 side-path leaks | fixed: cache class join and no cross-principal caching of prompt-bearing tasks; requester receipts over the read set; relabel as erasure at once; slow logs off and log grep in E0 and E1; no cluster privileges and cluster APIs in T1 | DESIGN 5.8, 14, 6.3 |
| F20 keystore and contention | fixed: keystore file per tenant; WAL checkpoint and scan of `-wal` and `-journal` with a positive control; fair queues at the model and embedding ports; per-tenant byte caps; T5 with a concurrency-limited stub and a disk-fill flood | DESIGN 3, 15 |
| F21 probes that cannot fail | fixed: every listed broken variant added with N; T3 normalised; T7 fixed bound; structural checks for real models | 9 |
| M1 missing edge cases | fixed: rows added | DESIGN 17 |
| M2 inconsistent numbers | fixed by measurement: whole pool = 6,018 + 11,016 + 1,430; the 51 excluded blocks are streamed as unscored filler; the two exposure statements reconciled; 6 fresh questions with gold in held_back haystacks form a DEV-EXPOSED stratum (H1 n = 293). UNRESOLVED: which fresh contents entered a lab prompt (Step 20 exposure audit) | 3.2, 3.3, 3.4 |
| M3 defence in depth overstated | fixed: per-class control index and the claim restated | DESIGN 6.3 |
| M4 log-row strip; numbering; multiplicity | fixed: DEVIATION 6 covers the strip; list renumbered in order; Holm reported for joint claims | DESIGN 22; 2.2 |
| M5 judge level and model | fixed: leaf-level validation with `has_answer` on development only; different-model judge by default, else SAME-JUDGE disclosed | 8 |

Nothing in this revision was run except the dataset measurements marked
MEASURED, the gate-power simulation of 7.3 and the budget arithmetic of 10.3
(no model calls in any of them).
