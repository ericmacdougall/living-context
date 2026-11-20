# living-context: tuning the adaptive and pool defaults (plan)

Status: plan, not yet run, and no paid stage in it is approved. It is not a
pre-registration of any confirmatory claim; the registered experiment
([EXPERIMENT.md](EXPERIMENT.md)) is unchanged, and this plan never touches its
held-out test split or its fresh set. Companion files:
[ADAPTIVE.md](ADAPTIVE.md) and [POOLS.md](POOLS.md); every default in them is a
PLACEHOLDER (no measurement behind it) that carries one of the ids D1 to D13
below. Labels as in DESIGN section 0. Wording: "pool" means a memory pool (POOLS.md); the pools of EXPERIMENT 3.3 are called dataset pools here. Section numbers of the other files refer to
commit 6002839.

## 0. The requirement

The owner's words, verbatim:

> "you need to run enough synthetic tests and downloaded tests to tune the
> defaults well"

Reading: a default is accepted only after a stated amount of synthetic and
downloaded testing, with the evidence and the held-out check recorded. Which
stages are paid, and how much testing is "enough", is the owner's decision (section
5); this plan states a count for each so that decision can be made.

## 1. The defaults to tune

Trade-off, metric and data for each. "Zero-call" means the default can be set
without any call through the model port (the stub, plain retrieval, local
embeddings); "paid" means tree quality or routing matters and real model calls are
needed. Data: SYN = synthetic generators of section 2; DEV = the development
splits of EXPERIMENT 3.2 (pilot, dev2, held_back), split again into a tuning part
and a held-out part (section 4); DL = downloaded benchmarks of section 3, their
own development or train portions.

| Id | Default (file) | Trades off | Tuned against | Data | Class |
|---|---|---|---|---|---|
| D1 | `N_FLOOR = 8 x F_TARGET` (ADAPTIVE 4.3) | tree that cannot pass its gate vs flat | derived; verified, not tuned: a tree of fewer nodes cannot reach K >= 8 clusters | SYN | zero-call |
| D2 | memory gate `N_UP1`, `T_UP1`, `S_UP1`, `E_UP1`, backstop `N_BACK` (ADAPTIVE 4.3) | model calls and latency of a tree vs recall lost by staying flat | the pool size at which tree minus flat recall@10 has a lower bound above 0 (EXPERIMENT 2.1 "HOLDS"), less the transition's lead time | SYN for the shape, DEV and DL for the level | paid |
| D3 | difficulty gate `W_TASKS`, `R_MIN`, `H_UP1`, `TAU_RE` (ADAPTIVE 4.1, 4.3) | a tree built too early (wasted calls) vs too late (lost recall) | AUROC (bootstrap lower bound) of `hard(task)` against "flat missed a gold item in its top 10"; time to trigger on a growing pool | SYN, DEV, DL | zero-call (flat arm and labels only) |
| D4 | per-query rule `A_MIN`, `G_MIN`, `S_MAX`, `EPS_ESC` (ADAPTIVE 6.2) | full-path calls saved vs recall lost on queries called easy | non-inferiority of rule-gated recall against always-full (lower bound of the difference >= -0.02) and the share of queries kept flat; the sampled escalation's measured miss rate | SYN, DEV, DL | features zero-call; gain of escalation paid (reuses D2's tree runs) |
| D5 | hysteresis `E_CONSEC`, `EVAL_PERIOD`, `A_EVERY`, `D_DWELL`, `D_DWELL1`, `COOL_BASE`, `COOL_MAX`, `RHO_DOWN`, `RHO_RETRY`, `E_ABORT_MAX`, `T_PAR` (ADAPTIVE 4.4) | reaction speed vs flapping and wasted rebuilds | moves per pool per dwell (must be at most 1); delay from crossing a threshold to the move; wasted builds per 1,000 evaluations | SYN traces (growth, shrink, noise, bursts) | zero-call |
| D6 | stage 2 trigger `R2_MIN`, `H_UP2`, `M_UP2`, `G_KEEP` (ADAPTIVE 4.3) | loop and typing cost vs repair of misses | pools that enter stage 2 only when the opened-share bounds hold and persistent demand exists; later, the loop's own measures (EXPERIMENT H3 and H4 stages) | SYN, DEV | zero-call for the mechanism; the value of stage 2 is the loop's own experiment and is not decided here |
| D7 | transition gate `Q_REPLAY`, `Q_REAL_MIN`, `m_TRANS` (ADAPTIVE 5.4) | cost of the gate (it dominates a transition, ADAPTIVE 5.3) vs false adoption and false rejection | power and null coverage of the gate at Q (the repo's `gate_power.py` simulation, EXPERIMENT 7.3) at zero calls; then one real gate per size | SYN simulation; DEV for the real run | zero-call simulation, then paid |
| D8 | physical thresholds `B_SHARED_MAX`, `DOC_SHARED_MAX`, `B_SHARD_TARGET`, `AGE_ROLL` (ADAPTIVE 4.5) | shard count and cluster overhead vs query latency and per-pool isolation | p50 and p95 search latency, indexing rate and shard count against document count on the host; recovery and move time | SYN (documents only) | zero-call |
| D9 | pool selection `P_ALL`, `P_MAX`, `TAU_CARD`, `STICKY_TURNS`, `EPS_POOL`, and whether R1 is worth its call (POOLS 4.2) | queries per recall (and cost) vs the answer's pool being skipped | share of questions whose answer pool is in `S` when granted (Wilson lower bound); skipped-pool recall loss; R1 minus R0 on the same | SYN multi-pool layouts, DL multi-session sets | R0 zero-call; R1 paid |
| D10 | merge `K_POOL`, `K_TOTAL`, per-pool floor (POOLS 5.1) | breadth across pools vs depth in one | recall@10 and all-pool recall of the merged list against a per-pool oracle; small-pool starvation rate | SYN, DEV, DL | zero-call |
| D11 | injection triggers `TAU_SHIFT`, `SHIFT_WINDOW`, `MIN_TURN_TOKENS`, `I_RANK` (POOLS 6.2) | missed topic shifts vs spurious injections | trigger recall and precision against planted shifts; share of injected items that match the turn's ground truth | SYN conversations, DL multi-session dialogue | zero-call |
| D12 | injection bounds `I_PER_TURN`, `I_TOKENS`, `I_COOLDOWN`, `I_SESSION` (POOLS 6.3) | usefulness vs prompt noise and tokens | injected-token budget per turn and per session, repeat rate (must be 0), relevant-injection share | SYN, DL | zero-call |
| D13 | `TOKEN_TTL`, `max_age`, `SAVE_MAX`, `SAVE_RATE`, `FORGET_MAX`, `SCRATCH_TTL` (POOLS 2.3, 3) | convenience vs exposure and cost | not recall: set by the security review and by measured agent usage (renewals per session, size of saves); never tuned on retrieval data | SYN usage scripts | zero-call |

D1 is derived and D13 is a security parameter; neither is "tuned well" in the
recall sense, and the acceptance criteria of section 6 do not depend on them.

## 2. Synthetic tests

### 2.1 Generators (deterministic from a seed, ground truth by construction)

One generator module (`evalkit/synth/`), a seed per generated corpus, every
random draw from one seeded stream, so a corpus is reproduced byte for byte from
(parameters, seed). Ground truth is written at generation time and never inferred.
Each variable the owner named is an independent parameter.

| Generator | Controls | Ground truth |
|---|---|---|
| G1 size sweep | number of leaves, geometric from tens (32) to the largest the host can hold (found at Z1: the point where index size, heap or the shard budget stops it; recorded, not assumed) | the corpus itself |
| G2 topics | topic count K, vocabulary overlap between topics (0 to 1), topic size skew, distractor topics sharing entities with the target | each leaf's topic, each question's target leaf set |
| G3 sessions | leaves per session (distribution), session length in tokens, gap structure | session of every leaf |
| G4 entities | entity vocabulary size, reuse (Zipf exponent), entities per leaf, aliases (one entity under several spellings, a known share missed by the code extractor) | entity of every mention |
| G5 time | fact chains: a fact stated, later updated, later contradicted, with dates; share of updates; unverified updates | for each question, the current and the superseded leaf |
| G6 pools | number of pools P, size per pool (skewed), class per pool, grants per principal, questions whose answer sits in exactly one pool, in two, or in none granted; lookalike decoys in other pools; an ungranted pool holding the same facts (to test leakage) | answer pool(s) of every question; the granted set |
| G7 conversations | scripted turns with planted topic shifts, introductions of new entities, a failed lookup (a superseded or erased target), and returns to an earlier topic | turn index of every planted event and the memory that should be offered |
| G8 usage scripts | per-session save, update, renew and delegate sequences at stated rates | used only for D13 and for the security tests |

Text is built from controlled word lists and templates with a paraphrase parameter
(0 = a query shares its words with the leaf, 1 = no word shared, only the
embedding links them), so the difficulty of retrieval is a dial. Two embedding
modes: the hashed stub (DESIGN 18.3) and the real embedder run locally on the
remote host; neither is a model-port call. All heavy runs go to the remote host,
not the owner's laptop (BUILD-PLAN 0).

### 2.2 What synthetic data cannot show

- Real language: paraphrase, ambiguity, negation, slang, long-range reference. A
  threshold that separates synthetic easy from hard queries does not give the level
  at which real queries become hard.
- The real embedder's geometry on real text: cosine margins, agreement between
  lexical and vector lists and spread across sessions depend on it. Synthetic
  data gives the direction and the shape of each curve (for example, hard rate
  rising with size and with overlap), the ordering of rules and the behaviour of
  the state machine, not absolute thresholds.
- Real users' query distributions, reformulation habits and agents' tool use.
- The quality of tree routing and summaries by a real model: the stub routes by
  content but is not a model, so tree-versus-flat crossover cannot be set on it.
- Realistic contamination, noise and mislabelled data.

So synthetic data sets the mechanism's parameters (D1, D5, D6 mechanism, D8, D13,
the zero-call parts of D9 to D12) and gives the shape of D2 to D4; downloaded and
development data set their levels.

## 3. Downloaded tests

No dataset content goes in the repository, and nothing is downloaded without the
owner's approval of the file, source and size (a download is an action that needs
explicit permission). The repository already uses the cleaned S set of
LongMemEval (EXPERIMENT 3.1, SOURCE: its public release; not stored here). The
names below are from memory and every one is "to verify": nothing about them
(licence, size, URL, format, splits) is stated here as fact.

| Candidate | Use here | Status |
|---|---|---|
| LongMemEval (S, cleaned; already used) | size sweeps by sub-sampling the development dataset pool; difficulty features; D2 to D4 on its development parts only | already in use; tuning uses the development splits of EXPERIMENT 3.2 only, never the fresh set |
| LoCoMo | multi-session dialogue with evidence labels: follow-up injection triggers and pool-selection style tests | to verify |
| MSC (Multi-Session Chat) | multi-session conversations: topic shifts and returns for D11 and D12 | to verify |
| Standard retrieval sets (the BEIR collection's corpora; a passage-ranking set; a multi-hop question set) | size and topic-spread sweeps for flat retrieval with real relevance labels; D2, D3 | each to verify, one by one |
| A conversational-memory benchmark released after the above, if one exists | cross-check | to verify; not assumed to exist |

Before any download, check and record, per candidate:

1. Licence: that it permits the use intended (internal evaluation, and publishing
   aggregate results) and is compatible with a Business Source Licence public
   repository that does not redistribute the data.
2. Size: compressed and expanded, and the disk and the host's capacity, so that the
   download lands on the remote host.
3. Format and structure: how documents, sessions, evidence and relevance labels are
   stored; whether labels exist at the item level needed for recall; whether dates
   are real or synthetic.
4. Version and checksum, to be recorded in the run file (not the repository).
5. Its own train, development and test splits: only train and development portions
   are used for tuning; its test portion is touched once, at the end, if at all.
6. Personal data and contamination: whether it contains real people's data, and
   whether the models used are likely to have seen it.
7. The owner's approval of the file name, source and size.

## 4. Method

### 4.1 What needs model calls and what does not

| Needs no model call | Needs real model calls |
|---|---|
| the plain-retrieval stage end to end (stage 0), including D8 | any measure of tree quality or routing: D2's level, D7's real gate, D4's measure of what escalation gains, the 1-to-2 transition |
| signal computation and the difficulty features (D3), hysteresis (D5) on synthetic traces | R1 pool routing (D9) |
| the per-query rule's features and the flat side of its non-inferiority (D4) | the loop's own value (EXPERIMENT's H3 and H4 stages, not this plan) |
| pool selection by cards, merge, injection triggers and bounds (D9 R0, D10 to D12) | |
| gate power by simulation (D7) | |
| tokens and tools (D13) | |
| the deterministic stub for every mechanism test | |

The stub's tree is real in structure but the stub routes by a scoring function of
its own; a stub result shows the mechanism works and says nothing about real routing.

### 4.2 Tuning and held-out data

- Synthetic: seeds are partitioned before any run into tuning seeds and held-out
  seeds, recorded in a file committed before the first run; held-out seeds are used
  once, by the acceptance run of section 6.
- Development data: the questions of pilot, dev2 and held_back (EXPERIMENT 3.2) are
  split by content cluster (questions sharing gold content stay together, as in
  7.1) into a tuning part and a held-out part, by a seeded split fixed before
  tuning (the repo's seed 16096); thresholds are chosen on the tuning part only and
  checked once on the held-out part. The held-out part is spent by that check.
- Downloaded data: tune on its train or development portion; keep its test portion
  untouched until the single final check.
- Never touched by any stage here: EXPERIMENT's fresh confirmatory set, its
  held-out test questions, and any stage-confirmatory run. Development choices are
  frozen in a commit before the run that uses them (EXPERIMENT 3.7).

### 4.3 Threshold rules, sample sizes, intervals

Statistics follow EXPERIMENT 7 and 11: clustered paired bootstrap over questions
(whole content clusters resampled), 10,000 resamples, seed 16096, percentile 95%
intervals, the effective n printed; Wilson for single rates; Tango and Newcombe for
paired and unpaired binary differences; the outcome words of 2.1; the result file
fields of 11.

The rule per default, stated before any run:

| Default | Threshold rule | Sample size |
|---|---|---|
| D2 | grid of sizes (geometric); at each size the paired difference tree minus flat with its interval; `N_UP1` = the smallest size whose outcome is HOLDS, divided by 1.5 (the lead time of a transition, ASSUMPTION); if no size holds, `N_UP1` = `N_BACK`, reported NOT ESTABLISHED; `T_UP1`, `S_UP1`, `E_UP1` are the same quantities measured at that size | at least 60 questions per size on DEV (the interval half-width is then about 0.11 at the standard deviation of EXPERIMENT 7.2, so only large effects resolve; a wide interval selects the conservative, larger size); at least 1,000 synthetic questions per size |
| D3 | choose `H_UP1` and `TAU_RE` at the operating point with AUROC lower bound at least 0.65 (ASSUMPTION) and trigger delay within one transition lead time; a feature whose AUROC lower bound does not exceed 0.55 is dropped | at least 1,000 synthetic tasks per size; all DEV and DL questions with gold labels |
| D4 | largest flat-sufficient share such that rule-gated minus always-full fused recall@10 has lower bound at least -0.02 on the tuning part; features dropped as D3 | at least 1,000 synthetic queries per size; DEV 60 per size |
| D5 | smallest `E_CONSEC` and `D_DWELL` such that no synthetic trace (noise, bursts, oscillation around a threshold) produces more than one move per dwell, then the shortest delay that keeps wasted builds at most 1 per 1,000 evaluations | 1,000 generated traces per scenario |
| D7 | smallest `Q_REPLAY` whose simulated power at the minimum effect `m_TRANS` is at least 0.8 and null adoption at most 0.05 (the gate's own known limits apply, DESIGN 13.3, 20) | 10,000 simulated gates per setting |
| D8 | the largest document count and the largest shard size at which p95 latency stays within a stated factor of the S0 value, measured on the host | repeated runs per setting, medians with ranges |
| D9 to D12 | for each, the setting that maximises the primary metric subject to a cost ceiling (queries per recall, injected tokens), on the tuning seeds; the Wilson lower bound of the answer-pool hit rate at least 0.90 and of injection relevance at least 0.80 (ASSUMPTION) | at least 1,000 synthetic items per cell; all development conversations of the downloaded sets |

Every tuned value is committed with its evidence file; a value for which the
interval is too wide to choose is left at its conservative PLACEHOLDER and reported
NOT ESTABLISHED, never chosen by the point estimate.

## 5. Order of stages and cost

Zero-call stages first, then the cheapest informative paid stage. None of the paid
stages is approved: only the stages already approved for the registered question
are. Each paid stage needs the owner's approval with its call count and a cap
enforced by the model port's counter (EXPERIMENT 10). Embedding on the remote host
is not a model-port call.

### 5.1 Zero-call stages

| Stage | Content | Parameters set | Model calls |
|---|---|---|---|
| Z1 | G1 on the remote host up to the host's limit: index size, memory, latency and shard count of stage-0 retrieval; the largest size recorded | D8 | 0 |
| Z2 | G2 to G5 sweeps on the flat arm with the real local embedder and the stub: signal computation, difficulty features, their AUROC against flat misses | D3, features of D4 | 0 |
| Z3 | state-machine simulations on generated traces; the stage-0 write and read model-call counter | D5, D6 mechanism, D1 verification | 0 |
| Z4 | G6, G7, G8 with the stub: selection R0, merge, injection triggers and bounds; the same on the development parts of the downloaded multi-session sets | D9 (R0), D10, D11, D12, D13 | 0 |
| Z5 | gate power simulation across `Q_REPLAY` and `m_TRANS` | D7 (simulated part) | 0 |
| Z6 | the same on DEV flat arms (A0) and on the downloaded sets' flat arms: difficulty and AUROC with real labels | D3, D4 (feature part) | 0 |

Total zero-call estimate: 0 model calls; time is the remote host's, not
estimated here, and the largest corpus is bounded by the host.

### 5.2 Paid stages, with the arithmetic

Inputs that are formulas of the repo: build calls per transition `N / (F_TARGET -
1) + L` (ADAPTIVE 5.3), with `F_TARGET = 16`; routing calls per full-path recall `r`
between 25 and 77 (EXPERIMENT 10.1, an ASSUMPTION until S1 and S2 measure it; the
low end is for about 10 top-level holders and is likely high for small pools);
gate calls `72 + Q_REPLAY x r_max`, `r_max = T + 48`, `T` top-level holders, here 5
to 10 (so `r_max` 53 to 58), `Q_REPLAY = 80`. Inputs I could not measure, marked
UNMEASURED: `r` at each size, the share `f_esc` of queries that take the full path
under the tuned rule, and the rebuild overhead factor of 2 (the retry allowance of
ADAPTIVE 5.3).

P1. Crossover and escalation-gain sweep, the cheapest informative paid stage.
Sizes 512, 2,048 and 8,192 leaves (sub-samples of the development dataset pool), 60
development questions per size, pinned stage 1 (a tree), flat arm free.

- Build: (512 + 2,048 + 8,192) / 15 = 716.8, plus at most 3 levels per size (9) =
  at most 726.
- Routing: 3 sizes x 60 questions x r = 180 x 25 = 4,500 up to 180 x 77 = 13,860
  (UNMEASURED r).
- P1 total: 5,230 to 14,590 calls.
- It sets D2 (level) and, with its recorded routing outputs, D4's escalation gain:
  P2 below is analysis of P1's outputs and costs 0.

P4 (optional). R1 pool routing versus R0 (D9), only if Z4 shows R0 losing recall.
150 conversations x 12 turns = 1,800 `route_pools` calls (UNMEASURED: input is cards
only, so small calls). Skipped if R0 passes its rule.

P5. Adaptive versus fixed arms on a checkpointed growth, tuning part: 60 questions
at sizes 512, 2,048 and 8,192; ADAPT transitions once near 2,048 (flat below).
TREE-ONLY reuses P1's results at the same sizes and FLAT-ONLY is free.

- ADAPT build, one transition at 2,048 then incremental to 8,192: lower bound
  2,048/15 + (8,192 - 2,048)/15 = 137 + 410 = 547 (about 550); upper bound with the
  retry allowance, 2 x (136.5 + 3) + 2 x 409.6 = 279 + 819 = 1,098.
- Gate, one transition: 72 + 80 x (53 to 58) = 4,312 to 4,712.
- Routing for ADAPT at the two tree checkpoints: 2 x 60 x f_esc x r. Low: f_esc 0.3,
  r 25: 900. High: f_esc 1, r 77: 9,240 (UNMEASURED f_esc, r).
- P5 total: 550 + 4,312 + 900 = 5,762 up to 1,098 + 4,712 + 9,240 = 15,050; about
  5,760 to 15,050.

P6. The same as P5 on the held-out development part (34 questions per size, once):
routing 2 x 34 x f_esc x r = 510 to 5,236; build 550 to 1,098; gate 4,312 to 4,712;
total 5,372 to 11,046; about 5,370 to 11,050. It is the check that spends the
held-out part.

| Stage | Calls (low to high) | Needed for |
|---|---|---|
| P1 | 5,230 to 14,590 | D2, D4 gain |
| P4 (optional) | 1,800 | D9 R1 |
| P5 | 5,760 to 15,050 | D7 real run, D4, acceptance AT-1 to AT-3 on tuning data |
| P6 | 5,370 to 11,050 | acceptance on held-out data |
| Total with P4 | about 18,160 to 42,490 | |
| Total without P4 | about 16,360 to 40,690 | |

Total zero-call: 0. Total paid: about 16,400 to 40,700 calls, or 18,200 to 42,500
with the optional P4. None is approved. If the owner approves only part, the order
above is the order of value: P1 first (it also tells whether the later estimates were
too high, because it measures `r`), then P5 and P6.

Downloaded sets add paid calls only if a tree is built on them; this plan uses them
zero-call (flat arms, features, conversations) and leaves a tree on downloaded data
as a possible later stage, counted when it is proposed.

## 6. What "tuned well" means (acceptance)

The held-out run is performed once, after the defaults are frozen in a commit, on
the held-out seeds, the held-out development part and the downloaded sets' held-out
portions. A failed criterion reverts the affected default to its PLACEHOLDER and is
reported; no retuning on the held-out data.

- AT-1 Adaptive is no worse than always-plain at small size. Verify by: at every
  checkpoint below the first transition, ADAPT minus FLAT-ONLY recall@10 has a lower
  bound of at least -0.02 (the margin of the H3 guard, EXPERIMENT 7.2), and the
  model-port counter for ADAPT at those checkpoints is exactly 0.
- AT-2 Adaptive is no worse than always-tree at large size. Verify by: at every
  checkpoint after a transition has been adopted and its dwell has passed, ADAPT
  minus TREE-ONLY recall@10 has a lower bound of at least -0.02, with the windows
  during a transition reported and not decided.
- AT-3 The cost saved is reported. Verify by: model calls per recall and in total
  for ADAPT and for TREE-ONLY at each checkpoint, with intervals (bootstrap over
  recalls), and the difference. No minimum saving is claimed in advance.
- AT-4 No flapping. Verify by: on the held-out synthetic traces and on the real
  growth stream, at most one move per pool per `D_DWELL`, and no transition proposed
  for a pool below `N_FLOOR`.
- AT-5 Pool selection. Verify by: the share of held-out synthetic questions whose
  granted answer pool is in `S` has a Wilson lower bound of at least 0.90; the share
  of selections that include a pool outside the grant is exactly 0 (checked by
  request plan).
- AT-6 Injection. Verify by: on held-out planted conversations, trigger recall for
  planted shifts and the share of injected items matching ground truth each have a
  Wilson lower bound of at least 0.80, repeats of a held item are exactly 0, and no
  injection exceeds its bounds.
- AT-7 Honest accounting. Verify by: every result file carries the fields of
  EXPERIMENT 11, the held-out run was stamped once on the frozen commit, and the
  fresh set and the registered held-out test split were not read (the run driver
  refuses them).

The values of AT-5 and AT-6 (0.90, 0.80) are ASSUMPTIONS fixed here; the margin
0.02 is the registered H3 guard.

## 7. Open questions for the owner

| Id | Question | Recommended default |
|---|---|---|
| T1 | Approve P1 (about 5,230 to 14,590 calls) as the first paid tuning stage? | yes, with a hard cap at the upper figure and a stop if the first size shows `r` far above 77 |
| T2 | Approve the downloads, file by file, once their licence, size and format are checked (section 3)? | yes, one candidate at a time, starting with LoCoMo and MSC for the conversation-shaped defaults |
| T3 | Accept the acceptance margin of 0.02 and the Wilson bounds of 0.90 and 0.80? | yes |
