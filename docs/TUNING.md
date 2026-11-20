# living-context: tuning the adaptive and pool defaults (plan)

Status: plan, revision 3, not yet run, and no paid stage in it is approved. It is
not a pre-registration of any confirmatory claim; the registered experiment
([EXPERIMENT.md](EXPERIMENT.md)) is unchanged, and this plan never touches its
held-out test split or its fresh set. Companion files: [ADAPTIVE.md](ADAPTIVE.md)
and [POOLS.md](POOLS.md); every default in them is a PLACEHOLDER (no measurement
behind it) or is derived, and carries one of the ids D1 to D13 below. Labels as in
DESIGN section 0. "Pool" means a memory pool (POOLS.md); the pools of EXPERIMENT
3.3 are called dataset pools here. Section numbers of the other files refer to
commit 6002839. Figures marked "measured" come from one development run on a
dataset pool of 5,988 leaves (ADAPTIVE, preamble): about 67 model calls per tree
recall, 399 build calls for the pool.

## 0. The requirement

The owner's words, verbatim:

> "you need to run enough synthetic tests and downloaded tests to tune the
> defaults well"

Reading: a default is accepted only after a stated amount of synthetic and
downloaded testing, with the evidence and a held-out check recorded, and with an
acceptance criterion that can fail. Whether the paid stages run is the owner's
decision (section 7); this plan states a count for each.

## 1. The defaults to tune

"Zero-call" means no call through the model port (the stub, plain retrieval,
local embeddings); "paid" means real model calls. Data: SYN = the generators of
section 2; DEV = the development splits of EXPERIMENT 3.2, split again into a
tuning part and a held-out part (4.2); DL = downloaded sets (section 3), their own
development portions.

| Id | Default (file) | Trades off | Tuned against | Data | Class |
|---|---|---|---|---|---|
| D1 | `N_FLOOR = F_MAX x F_TARGET` (ADAPTIVE 4.3) | a "tree" with no upper level vs flat | derived, not tuned; verified: at or below it a bulk build has no upper level | SYN | zero-call |
| D2 | memory gate `N_UP1`, `T_UP1`, `S_UP1`, `E_UP1` (ADAPTIVE 4.3), per-writer caps `S_PRINC`, `E_PRINC` (ADAPTIVE 4.1); `N_BACK` is absolute and not tuned | model calls of a tree vs recall lost by staying flat | the smallest grid size `N*` at which tree minus flat recall@10 has a lower bound above 0 (4.3) | DEV for the level, SYN for the shape | paid |
| D3 | difficulty gate `W_UNITS`, `R_MIN`, `H_UP1`, `TAU_RE`, unit caps `U_SESS`, `U_PRINC`, `F_FAIL_MAX` (ADAPTIVE 4.1, 4.3) | a tree built too early vs too late; dilution vs flooding | AUROC lower bound of `hard(unit)` against "flat missed a gold item in its top 10" | SYN, DEV, DL | zero-call |
| D4 | per-query rule `A_MIN`, `G_MIN`, `S_MAX`, `EPS_ESC` (ADAPTIVE 6.2) | full-path calls saved vs recall lost on queries called easy | non-inferiority of rule-gated against always-full recall@10; share kept flat | features on SYN and DEV flat arms; gain from P1's recorded outputs | zero-call, plus analysis of P1 |
| D5 | hysteresis and backoff `E_CONSEC`, `EVAL_PERIOD`, `A_EVERY`, `D_DWELL`, `D_DWELL1`, `COOL_BASE`, `COOL_MAX`, `RHO_DOWN`, `RHO_RETRY`, `E_ABORT_MAX`, `T_PAR`, `LEASE_BEAT`, the down-move fence wait `J_WAIT` (ADAPTIVE 4.4, 5.6) | reaction delay vs false moves and repeated attempts | delay from threshold crossing to move; false moves on stationary traces; attempts and spend on growth traces with failing probations | SYN traces | zero-call |
| D6 | stage 2 trigger `R2_MIN`, `H_UP2`, `M_UP2` (ADAPTIVE 4.3) | loop and typing cost vs repair of misses | stage 2 entered only with opened-share bounds held and persistent demand; the value of stage 2 is the loop's own experiment and is not decided here | SYN | zero-call |
| D7 | probation `N_PROB`, `G_KEEP`, `P_MIN`, `D_PROB`, `EPS_IL` (ADAPTIVE 5.3) | rolling back a useful tree vs keeping a useless one; probation latency and spend vs evidence | rollback rate of a random-router tree and keep rate of trees that help, both credited by opens through the team-draft interleave | stub on SYN; P1's recorded outputs with gold items as opens | zero-call, plus analysis of P1 |
| D8 | rollover `B_SHARD_TARGET`, `AGE_ROLL` (ADAPTIVE 4.5) | shard count vs latency | p95 search latency, indexing rate and shard count against document count on the host | SYN documents | zero-call |
| D9 | pool selection `P_ALL`, `P_MAX`, `TAU_CARD` (POOLS 4.2) | pools queried per recall vs the answer's pool being skipped | answer-pool hit rate (Wilson lower bound) | SYN layouts above `P_ALL`; DL multi-session sets | zero-call |
| D10 | merge `K_POOL`, `K_TOTAL`, `R_CAP`, the flat floor `D_FLOOR` (POOLS 5.1, 5.3) | breadth across pools vs depth in one; calls per recall | merged recall@10 against a per-pool oracle; small-pool starvation rate | SYN, DEV, DL | zero-call |
| D11 | injection triggers `TAU_SHIFT`, `SHIFT_WINDOW`, `MIN_TURN_TOKENS`, `I_RANK` (POOLS 6.2) | missed topic shifts vs spurious injections | trigger recall and precision against planted shifts | SYN conversations (G7) | zero-call |
| D12 | injection bounds `I_PER_TURN`, `I_TOKENS`, `I_COOLDOWN`, `I_SESSION` (POOLS 6.3) | usefulness vs prompt noise | tokens per turn and session, repeat rate (must be 0), relevant share | SYN | zero-call |
| D13 | `TOKEN_TTL`, `max_age`, `TOK_LIVE_MAX`, `SAVE_MAX`, `SAVE_RATE`, `RETRACT_MAX`, `RETRACT_RATE`, `RETRACT_POOL_RATE`, `R_SESSION`, `R_PRINC`, `MINT_RATE`, `CONV_IDLE` (POOLS 2.3, 2.6, 2.8, 3, 3.5, 5.3) | convenience vs exposure and cost | set by the security review and measured agent usage (renewals per session, save sizes); never tuned on retrieval data | SYN usage scripts | zero-call |

Every default has a criterion: D2 to D12 in section 6 (AT-1 to AT-9), D1 by its
derivation check (Z3), D13 by the security review, which records the value and the
reason in the review file.

## 2. Synthetic tests

### 2.1 Generators (deterministic from a seed, ground truth by construction)

One generator module (`evalkit/synth/`), every random draw from one seeded stream,
so a corpus is reproduced byte for byte from (generator version, parameters, seed).
Ground truth is written at generation time and never inferred. The generator
version is frozen with the seed file before the first run (4.2).

| Generator | Controls | Ground truth |
|---|---|---|
| G1 size sweep | leaves, geometric from 32 to the host's limit (found at Z1, recorded) | the corpus itself |
| G2 topics | topic count, vocabulary overlap, topic size skew, distractor topics sharing entities | each leaf's topic; each question's target set |
| G3 sessions | leaves per session, session length, gaps | session of every leaf |
| G4 entities | vocabulary size, reuse (Zipf), entities per leaf, aliases | entity of every mention |
| G5 time | facts stated, superseded through `pool_update`, contradicted, retracted | the current and the superseded leaf per question |
| G6 pools | pools per principal from 2 to 32 (layouts above `P_ALL` required), sizes, classes, grants; answers in one, two or no granted pool; decoy pools with look-alike cards; an ungranted pool holding the same facts (leakage); cards written by a second generator with its own vocabulary, independent of the question generator | answer pools; the granted set |
| G7 conversations | scripted turns with planted topic shifts, new entities, a failed lookup, returns to an earlier topic | turn of each planted event and the memory that should be offered |
| G8 usage scripts | per-session saves, updates, retracts, renewals and delegations at stated rates | used for D13 and the security tests |

Text comes from controlled word lists and templates with a paraphrase dial (0 = a
query shares its words with the leaf, 1 = only the embedding links them). Two
embedding modes: the hashed stub (DESIGN 18.3) and the real embedder run on the
remote host; neither is a model-port call. Heavy runs go to the remote host, not
the owner's laptop (BUILD-PLAN 0).

### 2.2 What synthetic data cannot show

- Real language: paraphrase, ambiguity, negation, long-range reference. A threshold
  separating synthetic easy from hard queries does not give the real level.
- The real embedder's geometry on real text, on which agreement, margin and spread
  depend. Synthetic data gives the shape of each curve, the ordering of rules and the
  state machine's behaviour, not absolute thresholds.
- Real users' queries, reformulations, opens and agents' tool use.
- Routing and summary quality by a real model: the stub routes by a scoring
  function, so the tree-versus-flat crossover cannot be set on it.
- Template-planted topic shifts are easier to separate than real ones, so a
  synthetic injection score measures the generator as much as the trigger.

So synthetic data sets the mechanism's parameters (D1, D5, D6, D7's rollback side,
D8, D9 to D13) and the shape of D2 to D4; DEV sets their levels.

## 3. Downloaded tests

No dataset content goes in the repository, and nothing is downloaded without the
owner's approval of the file, source and size. The repository already uses the
cleaned S set of LongMemEval (EXPERIMENT 3.1; not stored here). Every fact below
about other datasets is "to verify"; none is stated as fact.

| Candidate | Use here | Status |
|---|---|---|
| LongMemEval (S, cleaned; already used) | DEV of section 1; development splits only | in use; never the fresh set |
| LoCoMo | multi-session dialogue: D9, D10 and a descriptive look at D11 | to verify; recalled (not checked) as a small number of long conversations with no train, development and test split, so it could be used for description only, not for tuning plus a held-out check |
| MSC (Multi-Session Chat) | topic shifts and returns, descriptive for D11 and D12 | to verify; recalled (not checked) as having splits but no ground truth of which memory should be offered |
| Standard retrieval sets (a BEIR corpus, a passage-ranking set, a multi-hop set) | flat-retrieval difficulty features with real relevance labels (D3, D4) | each to verify |

Because no candidate is known to hold "the memory that should be offered at this
turn", the injection criterion AT-6 is synthetic only; downloaded conversations are
reported, not decided.

Before any download, record per candidate: licence (internal evaluation and
publishing aggregates, compatible with a public repository that does not
redistribute the data); compressed and expanded size and the host's disk; format
and label granularity; version and checksum (in the run file, not the repository);
its own splits (tune on train or development portions only); personal data and
likely contamination; and the owner's approval of the file, source and size.

## 4. Method

### 4.1 What needs model calls

| No model call | Real model calls |
|---|---|
| stage 0 end to end; D8 | tree recall quality at each grid size (D2's level) |
| signals, difficulty features (D3), per-query features (D4) on flat arms | |
| state machine, backoff, probation by stub (D5, D6, D7) | |
| selection, merge, injection (D9 to D12); tokens and tools (D13) | |
| ADAPT against TREE-ONLY on SYN with the stub (AT-2) | |
| analysis of P1's recorded outputs (D4's gain, D7 on real trees) | |

A stub result shows that a mechanism works and says nothing about real routing.

### 4.2 Tuning and held-out data, and probes

- Synthetic: seeds are split into tuning and held-out seeds in a file committed
  before any run, together with the generator version; held-out seeds are used once,
  by the acceptance run.
- DEV: the 120 development questions with gold labels (EXPERIMENT 3.2; count to
  verify) are split by content cluster, with twins on the same side as their
  originals, by a seeded split fixed before tuning (seed 16096), assumed about 80
  tuning and 40 held-out questions (to verify after the split). Thresholds are chosen
  on the tuning part and checked once on the held-out part, which that check spends.
- The dataset pool for D2 is the largest development pool (EXPERIMENT 3.3: an
  estimated 18,900 leaves, to verify). The whole dataset pool (an estimated 57,730
  leaves) is excluded because it contains the fresh confirmatory set; this is why the
  grid stops below it.
- Probes. Every evaluation question is issued with `via = probe`: it is never
  written to stored queries, never a unit of any adaptive signal (ADAPTIVE 4.1),
  never counted in probation, and never seen by the reorganiser or its gate, before
  or after it is asked (an edit to EXPERIMENT 3.7). A Step 24 test plants a probe and
  asserts that it appears in none of these.
- Tuning tenants are separate from any confirmatory tenant; their indices and model
  caches are destroyed before a confirmatory run (EXPERIMENT 10.1).
- Never touched: EXPERIMENT's fresh set, its held-out test questions and any
  confirmatory run. Development choices are frozen in a commit before the run that
  uses them (EXPERIMENT 3.7).

### 4.3 Threshold rules, sample sizes, intervals

Statistics follow EXPERIMENT 7 and 11: clustered paired bootstrap over questions,
10,000 resamples, seed 16096, percentile 95% intervals, effective n printed; Wilson
for single rates. Interval half-width for a paired recall difference at the standard
deviation of EXPERIMENT 7.2 (about 0.45, an upper figure because most paired
differences are 0): `1.96 x 0.45 / sqrt(n)`, so 0.10 at n = 80, 0.14 at n = 40,
0.050 at n = 312 (`(1.96 x 0.45 / 0.05)^2 = 311.2`, rounded up), 0.020 at n =
2,000, and 0.014 at n = 4,000. A non-inferiority margin of 0.02 has
power 0.8 at a true difference of 0 only at n of about 4,000.

General rules, fixed before any run:

- A setting is chosen by the lower bound of its criterion, never by the point
  estimate, and among settings within a cost ceiling the one with the highest lower
  bound wins.
- A value whose interval is too wide to choose stays at its PLACEHOLDER and is
  reported NOT ESTABLISHED.
- Order: D4's features (agreement, margin, spread) are set first on flat data,
  then D3 (which uses them), then D2 and D7 after P1.
- An AUROC is reported only with at least 30 positives and 30 negatives; otherwise
  the feature is NOT ESTABLISHED at that size.

| Default | Rule | Sample size |
|---|---|---|
| D2 | grid 2,000, 6,000 and the full development pool (about 18,900 leaves; the cost below is computed at 20,000); `N*` = the smallest grid size where tree minus flat recall@10 on the DEV tuning part has a lower bound above 0; `N_UP1 = N* / 1.5` (ASSUMPTION: the lead time of a transition); if no size qualifies, `N_UP1` = the largest tested size, reported NOT ESTABLISHED, so without evidence the shipped default builds trees late, not early. `T_UP1`, `S_UP1`, `E_UP1` are the same quantities measured at `N_UP1`. A denser grid near `N*` is a later stage once P1 shows where it lies | about 80 DEV questions per size (half-width 0.10: the measured direction at 6,000, about +0.10 to +0.13, is near the edge, so NOT ESTABLISHED is a likely outcome and is stated in advance; resolving it needs about 312 questions per size, costed in Q8). `S_PRINC`, `E_PRINC` are not tuned for recall: each is fixed at a quarter of `S_UP1`, `E_UP1` (half of `RHO_DOWN` times them), so one writer cannot hold a pool above its down threshold |
| D3 | `H_UP1`, `TAU_RE` at the operating point with AUROC lower bound at least 0.65 (ASSUMPTION); a feature with lower bound not above 0.55 is dropped; `U_SESS` from G8; `U_PRINC` such that `U_PRINC / W_UNITS` stays strictly below every threshold a unit share feeds (the lowest is `RHO_DOWN x H_UP2`, 0.075 by default), verified on G8 by one scripted principal with many root tokens and conversations moving `hard_rate` by at most `U_PRINC / W_UNITS` | at least 1,000 synthetic units per size; all DEV and DL questions with gold, at least 30 positives |
| D4 | the largest flat-sufficient share such that rule-gated minus always-full fused recall@10 has lower bound at least -0.02 on SYN; on DEV the same difference is reported (its n cannot resolve 0.02) | at least 4,000 synthetic queries per size; DEV reported |
| D5 | the smallest `E_CONSEC` and `EVAL_PERIOD` such that the false-move rate on stationary noise traces below a threshold has a Wilson upper bound at most 0.01 per pool-month, then the shortest `D_DWELL` with the same bound on oscillating traces; `COOL_BASE`, `RHO_RETRY` such that, on a growth trace whose every probation fails, attempts are at most `ceil(log_1.5(N_end / N_first)) + 1` and spend, probation included, is at most `9 x build(N_end, 2) + A(N_end) x 3,600` (ADAPTIVE 5.4); `J_WAIT` at the 99th percentile of placement and link job time on the stub's profile, so a cancel is rare and its leaf only turns flat (the move-per-dwell bound holds by construction and is not a criterion) | 1,000 traces per scenario |
| D6 | the smallest `R2_MIN` such that no trace with opened share outside the DESIGN 9 bounds enters stage 2, and `H_UP2`, `M_UP2` at the AUROC rule of D3 against "link expansion offered an opened item" on SYN | 1,000 traces |
| D7 | the smallest `N_PROB` and the `G_KEEP` such that a random-router tree (stub, in recalls whose gold items span two pools) is rolled back in at least 95% of seeded runs and the P1 trees at sizes where tree minus flat has a positive point estimate are kept in at least 95% of bootstrap resamples, both with credit by the team-draft interleave of ADAPTIVE 5.3 replayed on the recorded fused and direct lists, opens modelled as gold items (ASSUMPTION; real opens differ); if both cannot hold, placeholders kept, NOT ESTABLISHED. `P_MIN`, `D_PROB` and `EPS_IL` are not tuned for recall: `P_MIN` 3 is the smallest count for which leave-one-principal-out leaves two, `D_PROB` bounds the reader latency window, and `EPS_IL` is reported with the interval it gives the down-move test | 400 seeded runs; P1's outputs |
| D8 | the largest shard size at which p95 search latency is at most 2 times the value at 1 GiB on the same host (factor 2 an ASSUMPTION) | 5 runs per setting, medians with ranges |
| D9 to D12 | the setting with the highest lower bound of its primary metric within its cost ceiling (pools queried, injected tokens), on tuning seeds | at least 1,000 synthetic items per cell |

## 5. Stages and cost

Zero-call stages first, then the one paid tuning stage and its held-out check. No
paid stage is approved. Each needs the owner's approval with its count and a cap
enforced by the model-port counter (EXPERIMENT 10). Embedding on the remote host is
not a model-port call.

### 5.1 Zero-call stages

| Stage | Content | Sets | Model calls |
|---|---|---|---|
| Z1 | G1 on the remote host to its limit: index size, heap, latency, shard count of stage 0 | D8, AT-8 | 0 |
| Z2 | G2 to G5 on the flat arm, real embedder and stub: per-query features first, then difficulty and AUROC | D4 features, D3 | 0 |
| Z3 | state-machine simulations on generated traces (growth, shrink, noise, bursts, outages, one flooding principal, bulk import); probation with the stub and a random router; the stage 0 counter; the `N_FLOOR` derivation check | D1, D5, D6, D7 rollback side, AT-4 | 0 |
| Z4 | G6, G7, G8 with the stub: selection, merge, injection, token usage; descriptive runs on the downloaded multi-session sets | D9 to D13, AT-5, AT-6, AT-9 | 0 |
| Z5 | ADAPT against TREE-ONLY and FLAT-ONLY on SYN growth with the stub, checkpoints from `N_UP1` on | AT-2 | 0 |
| Z6 | the same features and labels on DEV and DL flat arms (A0) | D3, D4 levels | 0 |

Time is the remote host's and is not estimated here.

### 5.2 Paid stages, with the arithmetic

Inputs: build calls `build(N, m)` of ADAPTIVE 5.4 at m = 1 (measured: 399 at 5,988
leaves); recall calls 67 per full-path recall (measured at 6,000 leaves; the caps
`r_max = T + 48` at 2,000 and 20,000 leaves are 56 and 53, so 67 is an upper figure
there); `per_query` off, so every tree recall takes the full path. Recall@10 is
scored against gold labels, so no judge calls are needed. Flat arms cost 0.

P1. Crossover sweep on the DEV tuning part, the first paid stage. Trees by bulk
build (pinned stage 1) on the development pool sub-sampled at 2,000 and 6,000 leaves
and on the whole development pool; about 80 tuning questions per size, asked as
probes.

- Build: 133 + 399 + 1,334 = 1,866 (at 20,000 as the upper size), up to 3,732 with
  the retry allowance of DESIGN 18.1.
- Recalls: the counter caps each recall at `r_max` (56, 72 and 53 at the three
  sizes). At the measured 67 where it is under the cap and at the cap elsewhere:
  80 x (56 + 67 + 53) = 14,080; with every recall at its cap: 80 x (56 + 72 + 53)
  = 80 x 181 = 14,480, which recalls cannot exceed.
- P1 total: 1,866 + 14,080 = 15,946 expected, at most 3,732 + 14,480 = 18,212 (build
  retries and every recall at its cap), which is the stage's hard cap. Level-1
  input tokens: about 1,750 level-1 calls at about 15,000 tokens, about 26 million.
- Its recorded outputs (fused and direct lists, per item) give D4's escalation gain
  and D7's real-tree side by analysis at 0 calls. It also measures calls per recall
  at each size. Stop rule: if more than 10% of the first size's recalls end with
  holders cut by the per-recall cap (`esc_reason = budget`), the cap is truncating
  the owner's fan-out, and the stage stops and is re-counted for the owner.

P6. Held-out check on the DEV held-out part, once, after the defaults are frozen.
The trees are question-independent (probes are never stored), so P1's trees are
reused and P6 pays recalls only: 40 x 176 = 7,040 expected, at most 40 x 181 =
7,240 calls.

There is no separate ADAPT arm on DEV. Below `N_UP1` ADAPT is stage 0, which is the
flat arm (0 calls); after its transition its tree is the same bulk build as
TREE-ONLY's at that size. So ADAPT's DEV figures are composed from P1 and P6 and
reported, and the transition mechanism is decided on SYN (Z5).

| Stage | Calls | Needed for |
|---|---|---|
| Z1 to Z6 | 0 | D1, D3 to D13 mechanisms and levels; AT-2, AT-4 to AT-6, AT-8, AT-9 |
| P1 | 15,946 to 18,212 | D2 (likely NOT ESTABLISHED at 80 per size, 4.3), D4's gain, D7 on real trees, AT-1's timing |
| P6 | 7,040 to 7,240 | the held-out check of AT-1 and D2 |
| Total | 22,986 to 25,452 | |
| D2 resolution (not planned; Q8) | 54,912 to 60,204 | `N*` at half-width 0.05 |

Revision 1 stated 16,400 to 40,700 calls, with a replay gate and an ADAPT arm,
using 25 to 77 calls per recall; at the measured 67 its count was about 37,300.

## 6. What "tuned well" means (acceptance)

The acceptance run is performed once, after the defaults are frozen in a commit, on
the held-out seeds and the DEV held-out part. A failed criterion reverts the
affected default to its PLACEHOLDER and is reported; nothing is retuned on held-out
data. Each criterion can fail as written; a criterion whose data cannot resolve it
is reported NOT ESTABLISHED, not passed.

- AT-1 ADAPT transitions when a tree starts to help, and costs nothing before.
  Verify by: (a) ADAPT's model-port counter is exactly 0 at every checkpoint below
  `N_UP1`; (b) on a SYN growth trace with hard queries, the evaluator proposes a
  transition within `E_CONSEC` evaluations of the first checkpoint at or above
  `N_UP1`, and the lag in leaves is reported; (c) on DEV, if `N*` exists, ADAPT is at
  stage 1 at the first checkpoint at or above `N*` on the held-out part; if `N*` is
  not in the grid, (c) is NOT ESTABLISHED. Control: an evaluator with its trigger
  disabled (never fires) must turn (b) red.
- AT-2 ADAPT is no worse than always-tree once it should have a tree. Verify by: on
  SYN with the stub, at every checkpoint from `N_UP1` on, including those between
  `N_UP1` and adoption (decided, with the lag reported), ADAPT minus TREE-ONLY
  recall@10 has a lower bound of at least -0.02, with at least 4,000 questions per
  checkpoint. Control: with a random router, ADAPT rolls back by probation and its
  recall@10 equals FLAT-ONLY's after rollback, within the same margin.
- AT-3 Cost, reported. Verify by: model calls per recall and in total for ADAPT,
  TREE-ONLY and FLAT-ONLY at each checkpoint, with intervals. No minimum saving is
  claimed; this criterion reports and cannot fail, and is labelled so.
- AT-4 The state machine is bounded. Verify by: on held-out traces, false moves on
  stationary traces within the D5 bound; on growth traces with probation outcomes
  drawn as kept with probability p, for p in {0, 0.25, 0.5, 1}, attempts within the
  D5 bound and total spend, probation included, at most `9 x build(N, 2) + A(N) x
  3,600` (counter); no proposal at or below `N_FLOOR`; stage 2 entered on no trace
  with opened share outside the DESIGN 9 bounds (D6); with credit by the team-draft
  interleave, the random-router tree (in recalls whose gold items span two pools)
  rolled back in at least 95% of runs and the stub tree kept in at least 95% (D7);
  a void, short or single-principal probation never keeps a tree; one principal
  with many root tokens never meets the difficulty gate alone.
- AT-5 Pool selection. Verify by: on held-out layouts with more than `P_ALL` granted
  pools, decoy cards and cards from the second generator, the share of questions
  whose granted answer pool is in `S` has a Wilson lower bound of at least 0.90 (at
  least 400 questions per layout); selections including a pool outside the grant are
  exactly 0 (request plan).
- AT-6 Injection (synthetic only). Verify by: on held-out G7 conversations, trigger
  recall for planted shifts and the share of injected items matching ground truth
  each have a Wilson lower bound of at least 0.80; repeats of a held item are
  exactly 0; no injection exceeds its bounds; total model calls of injections are 0.
  Stated: this measures the trigger on the generator's shifts, not on real
  conversations.
- AT-7 Honest accounting. Verify by: every result file carries the fields of
  EXPERIMENT 11; the held-out run was stamped once on the frozen commit; the run
  driver refused the fresh set and the registered test split; no probe appears in
  stored queries, signals, probation or any replay set.
- AT-8 Rollover (D8). Verify by: at the chosen `B_SHARD_TARGET`, p95 search latency
  over the rolled pool is at most 2 times the 1 GiB value on the same host.
- AT-9 Merge (D10). Verify by: the share of questions whose gold item sits in a
  selected small pool and is dropped by the merge has a Wilson upper bound of at most
  0.05; merged recall@10 against the per-pool oracle has a lower bound of at least
  -0.02 on SYN; no recall exceeds `R_CAP` calls.

The 0.90, 0.80, 0.05 and the factor 2 are ASSUMPTIONS fixed here; the margin 0.02 is
the registered H3 guard (EXPERIMENT 7.2), used only where n resolves it.

### 6.1 A later hypothesis HA (proposal, not registered)

A separately approved measurement, never on the fresh set: the crossover size of
tree and flat recall over pool size on a fresh development stream; the timing of
ADAPT's transition against it (AT-1 c with resolving n); zero calls below it; and
ADAPT's non-inferiority to TREE-ONLY after adoption on real queries. Its sample size
is set when it is proposed, from P1's measured variance; it is not counted here.

## 7. Open questions for the owner

| Id | Question | Default in force |
|---|---|---|
| Q8 | (a) Approve P1, the first paid tuning stage, at 15,946 model calls expected and a hard cap of 18,212 (stop and re-count if more than 10% of the first size's recalls are cut by the per-recall cap), and later P6 at 7,040 to 7,240? P1 will likely leave D2 (when a pool should get a tree) NOT ESTABLISHED: at 80 questions per size the half-width is 0.10 and the measured direction is +0.10 to +0.13 (4.3), so the shipped default would then build trees late. (b) Fund resolving D2 at half-width 0.05? That needs `(1.96 x 0.45 / 0.05)^2 = 311.2`, so 312 tuning questions per size, and the DEV set has 120 questions in all (80 tuning), so 232 or more new questions with gold labels from a source beyond DEV, approved like any download (Q9). Recalls: 312 x 176 = 54,912 expected, at most 312 x 181 = 56,472 over the three sizes (232 x 176 = 40,832 to 232 x 181 = 41,992 if P1's 80 count toward it); builds 0 if the questions are over the same development pool (trees are question-independent, so P1's are reused), else 1,866 to 3,732 for a new pool. In all 54,912 to 60,204 calls standalone. | (a) not run until approved, recommended yes for P1, P6 asked again with P1's results; (b) not funded, and D2 ships NOT ESTABLISHED at the largest tested size unless the owner funds it |
| Q9 | Approve the downloads, file by file, after licence, size and format are checked (section 3)? | none downloaded; recommended one candidate at a time, starting with the retrieval sets for D3 and D4 |

The margins and bounds of section 6 are design decisions, not owner questions.
POOLS 12 (Q1 to Q4) and ADAPTIVE 15 (Q6, Q7) hold the other owner questions.

## 8. Edits to make in EXPERIMENT when this is adopted

- 3.7: evaluation questions are `via = probe` and never enter stored queries,
  signals, probation, the reorganiser or a replay set, before or after they are asked.
- 10: a proposed stage for P1 and P6 with their counts and caps; tuning tenants and
  caches destroyed before any confirmatory run.
- 12: Q8 and Q9.
