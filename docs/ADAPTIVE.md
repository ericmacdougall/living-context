# living-context: adaptive mode (amendment to DESIGN, BUILD-PLAN, EXPERIMENT)

Status: design amendment, revision 3, not yet adopted. Nothing here has been
built or run. Revision 2 answered two independent reviews of revision 1 (review
A: authorization and isolation; review B: lifecycle, adaptation, tuning): it
removed the shared small-pool tier, the replay gate on transitions and the stage
0 queue records, and recomputed every cost from measured figures. Revision 3
answers a delta review of revision 2: probation is counted and capped inside the
attempt bound and measured by interleaving against the flat list, one principal
can neither force nor keep a tree, down-moves fence write-path jobs, and a stage
0 pool costs one shard. Section
numbers of DESIGN.md, BUILD-PLAN.md and EXPERIMENT.md refer to commit 6002839;
section 14 lists the edits to make when this is adopted. This file does not edit
them, and it changes nothing the registered experiment does (section 8).
Companion files: [POOLS.md](POOLS.md) (what a pool is, access, tools, selection,
injection) and [TUNING.md](TUNING.md) (how every default below is set; each
carries its TUNING id, D1 to D13).

Labels as in [DESIGN.md](DESIGN.md) section 0 (OWNER, AGREED, SOURCE,
ASSUMPTION, DEVIATION, PENDING_REAL_TEST). A number marked PLACEHOLDER is an
ASSUMPTION with no measurement behind it; TUNING says how it is replaced. Figures
marked "measured" come from one development run on a pool of 5,988 leaves
(written "the 6,000-leaf development pool"): 399 build calls (375 level-1 at
about 15k input tokens and 17 s each, 24 upper), about 67 model calls per tree
recall (59 routing, 8 organising), and recall@10 of about 0.67 for direct search
against about 0.77 to 0.80 for the tree, with intervals that overlap (n = 30, not
significant).

## 1. The requirement

The owner's words, verbatim (2026-11-20):

> "remember to make it adaptive -- it doesnt instantly start segmenting small
> memory, it starts as a fast easy normal rag and auto adapts as context and
> memory size/complexity demands"

and, on what the unit is:

> "memory pools are what shard/scale as their data grows.. and agents use
> tools to update/save to them"

Reading:

- OWNER: a new memory starts as plain retrieval ("a fast easy normal rag"): no
  segmenting into topic indexes, no summaries, no model calls to store or to
  read. It adapts upward on "context and memory size/complexity", and the unit
  that grows and scales is the memory pool.
- OWNER, earlier (DESIGN 1): "when the index itself gets large, segment it and
  index it" is already adaptive in depth (DESIGN 5.4). This amendment adds the
  missing first step: whether there is an index at all.
- Design reading: "segmenting" means topic segmentation into index nodes (the
  tree). Cutting a session into leaves at `LEAF_CAP` (DESIGN 7.2) uses no model
  call, is what ordinary retrieval does, and stays at every stage.
- Everything else below (stages, triggers, transition, per-query choice,
  physical layout) is a design proposal; each choice is labelled.

## 2. Unit and stages

### 2.1 The unit is the memory pool

A pool is a named memory collection with exactly one access class and its own
indices (POOLS 1). Each pool has its own stage, physical layout, signals and
transitions; a tenant with ten pools can have ten different stages.

- Signals of a pool are computed only from that pool's own documents and its own
  log rows (INV-2, INV-3).
- A tree is per pool. This replaces "one tree over the leaves of one class"
  (DESIGN 5.3); the class lattice still decides what a principal may read.
- A session querying several pools gets, per pool, the path that pool's stage
  provides, merged by rank (POOLS 5).

### 2.2 Retrieval stages

| Stage | Name | What exists in the pool | Write cost | Read cost |
|---|---|---|---|---|
| 0 | FLAT | leaves (text, vector, entities from code, code-built title) in the pool's own leaf index; leaf heads, control documents, protocol log rows and stored queries in class-level indices, unscored (POOLS 1.2); the tenant's source log holds the pool's source documents. No queue records, nodes, members, summaries, edges, notes or cache | 0 model calls (embedding runs on the embedding port, not the model port) | 0 model calls: direct search (DESIGN 8.5) with the version filter (POOLS 3.9), the same code as arm A0 |
| 1 | TREE | stage 0 plus the topic family: level-1 nodes (gists, summaries, facts), upper levels by the index-of-indexes rule (DESIGN 5.4), placement members, batch rebuilds, fan-out routing (DESIGN 8.2 to 8.4) | placement is model-free; a batch rebuild costs one build call per level-1 node with pending members, plus its ancestor path (DESIGN 7.6) | direct search plus the owner's fan-out on the full path: measured about 67 calls on the 6,000-leaf pool, capped at `T + 48` (5.4) |
| 2 | LEARNING | stage 1 plus typed edges and bounded expansion (DESIGN 7.7, 8.6, 16), reflection, the reverse audit, the reorganiser and its gate (DESIGN 10 to 13), each per pool (POOLS 9.5) | typing: one call per 20 pairs, 3 pairs per leaf (DESIGN 7.7); loop calls under their existing caps | stage 1 plus edge expansion (model-free) |

Why three stages:

- Depth is an outcome of the index-of-indexes rule (DESIGN 5.4), so "first
  level" and "upper levels" are one stage.
- Stage 2 groups the layers that are only useful on top of a tree: typing calls
  read titles, gists and facts (DESIGN 7.7), which exist only after a level-1
  build; `tree_missed` needs a tree (DESIGN 10); the reorganiser edits one.
  ASSUMPTION: edges, reflection, audit and reorganiser share one rung; if
  measurement shows a pool that needs edges without the loop, the rung splits.
- The entity and time families (DESIGN 5.3) are model-free. ASSUMPTION: they are
  built after the topic head exists, by their own ordinary builds; the stage is
  defined by the topic head only.

Stage is derived, not stored in a second field: stage 0 = no live topic
generation; stage 1 = topic head `hd:topic` names a live generation; stage 2 = a
live learning head `hd:learn` also exists (both are control documents of the pool
in the class control index, POOLS 1.2). The flip that
defines a stage is one compare-and-set of one document (`hd:topic` for 0 to 1,
creation of `hd:learn` for 1 to 2). A commit writes other documents first (`wp`;
for 1 to 2 the generation head), each harmless to every reader if the process
stops before the defining flip, because stage 0 never reads `wp` and stage 1
never reads edges; on restart the evaluator finds the adopted candidate and
completes the defining flip, which is idempotent. Generation number 0 is the
implicit empty generation of every pool: a task pinned at stage 0 pins `gen = 0`,
and no node pointer exists at generation 0.

### 2.3 Physical layout (the owner's "shard/scale as their data grows")

| Tier | Where the pool's documents live | Index cost |
|---|---|---|
| S1 DEDICATED | every scored kind in the pool's own index family `lc-<t>-<c>-<p>-<kind>` (POOLS 1.2), each an alias over one physical index of one primary shard; the leaf index from creation, the others created at the stage that needs them | 1 shard at stage 0, 6 at stage 1, 8 at stage 2 (9.4) |
| S2 SHARDED | S1 with more than one physical index per kind behind the same aliases: new writes roll over to a new physical index at a size or age bound; reads search all of them | one more index per rolled kind per rollover |

There is no shared small-pool tier. Revision 1 put small pools of one tenant and
class into one shared index and moved them out at a size trigger. To be as safe as
dedicated indices, a shared index would need at least these three rules:

1. scoring that ignores other pools: BM25 fields with `similarity: boolean` (no
   idf, term frequency or length norm), or BM25 computed in the gateway over the
   filtered candidates; PENDING_REAL_TEST, and boolean scoring changes lexical
   ranking, so stage 0 would no longer be the A0 arm and the measured direct-search
   figure would not carry over;
2. exact kNN under the pool filter (`script_score` cosine), because approximate
   kNN candidates compete across pools;
3. one gateway query builder that injects the pool filter into every request type
   and refuses any request without it, with get-by-id and multi-get replaced by
   filtered searches (a known pitfall: a `query` filter next to a top-level `knn`
   section does not restrict the kNN hits; only `knn.filter` does).

Written out, it needs more than those three:

4. a timing channel the three rules do not close: the postings of a query term
   include every pool's documents in the shard, so latency depends on other
   pools' term frequencies;
5. no evaluator signal may use index-level statistics (they count every pool);
6. a copier from the shared index to a dedicated one, with dual-read, its own
   erasure and lost-update races (review B M3, review A F16) and a restore rule
   for a half-moved pool;
7. a verifier check that no document in the shared index lacks a valid `pool`.

So the shared scored tier is dropped.

What is shared instead, at stage 0, is unscored. A stage 0 pool's heads and
control documents sit in the class control index, and its log rows and stored
queries in the class-level row indices (POOLS 1.2); only its leaves, the one kind
that is scored, have an index of their own. This does not bring back the round 1
findings:

- No cross-pool scoring statistic (review A F3). BM25 statistics and kNN
  candidates exist only for scored reads, and every scored read is of the pool's
  own leaf index. The shared documents are read by id (ids embed the pool; POOLS
  3.6 checks `head.pool`) or by filter-context term and aggregation requests,
  which have no idf, term frequency or candidate competition. Reformulation
  detection uses the conversation's recent query embeddings in gateway memory
  (4.1), not a vector search of stored queries.
- One enforcement point (review A F4). Every read of these indices goes through
  the gateway's class-level reader, which requires a `pool` term and refuses any
  scored request; a dedicated negative test covers it (POOLS NEG-16).
- No copier (review B M3, review A F16). At the 0 to 1 transition the pool's new
  rows start going to its own row indices; older rows stay in the class-level
  indices, read through the same reader with the pool term, until their retention
  ends. Heads and control documents never move.
- Residual channels. The time of an aggregation over a shared row index depends on
  other pools' row volume (activity, not content), and only background jobs run
  such aggregations, so no session can time them. The verifier checks that every
  document in a class-level index has a valid `pool` (9.3).

Layout 2 (every kind dedicated from creation, 4 shards at stage 0) remains a
configuration; the owner chooses between them (POOLS 12 Q2, with the per-node
figures of 9.4).

## 3. What each stage does on write and read

### 3.1 Write path

| Step (DESIGN) | Stage 0 | Stage 1 | Stage 2 |
|---|---|---|---|
| 7.1 admission, 7.2 segmentation, 7.3 subjects (declarations and the code extractor at every stage) and class (POOLS 2.8) | unchanged | unchanged | unchanged |
| 7.4 steps 1 to 5 (sequence, key, source, embed, leaf, head) | unchanged | unchanged | unchanged |
| 7.4 step 6, enqueue for placement and linking | no queue record; the write-path tracker marks the sequence `flat` (5.2) | queue record; placement (7.5) | placement and link creation (7.7) |
| 7.5 placement, 7.6 batch rebuild | not run | run | run |
| 7.7 link creation | not run | not run | run |
| write-path watermark P | not advanced | set to `W` at the flip, then advanced as in DESIGN 5.6 with the gap rule of 5.2 | as stage 1 |

Subjects come from the tenant's declarations and the code entity extractor at
every stage, never from a build call's entity list alone, so a subject erasure
finds a leaf whatever stage it was stored at (an edit to DESIGN 7.3).

### 3.2 Read path (a single pool)

| Stage | Recall of one pool |
|---|---|
| 0 | direct search only (DESIGN 8.5): BM25 over `text`, `title`, `entities`, kNN over `embedding`, the version filter (POOLS 3.9), client-side RRF (k 60, window 50). Digest: pointers with title, size, reason `direct`, confidence null; `gen = 0`; no outline, no organise call. `explore` re-runs direct search with the delegate's question and an exclusion set |
| 1 | flat path or full path (section 6); with `per_query` off (the default) every recall takes the full path |
| 2 | as stage 1, plus bounded link expansion (DESIGN 8.6; no model call) |

The digest reports the stage (`flat`, `tree`, `learning`) of each pool in `S`
only, labelled as derived from the activity of every grantee of the pool. It does
not report that a build is running.

## 4. Triggers

The owner's words are "context and memory size/complexity". Size and complexity
of the memory are measured from the pool's own documents; the demand it makes is
measured from how retrieval is going. Every signal is computed without a model
call, from documents the gateway itself wrote.

### 4.1 Signals

Inputs are gateway-derived only:

- A unit is one recall of the pool: a protocol log row group with `via = recall`,
  written by the gateway. Rows with `via = injection` (POOLS 6.6) or `via = probe`
  (evaluation questions, TUNING 4.2) are never units.
- The session of a unit is its (authenticated principal, conversation) (POOLS
  2.3, 2.8), assigned by the gateway; no caller-supplied task id is read, and new
  root tokens do not make new sessions. Each session contributes at most `U_SESS`
  units to a window and each principal at most `U_PRINC`, unless it is the pool's
  only grantee with `query`. `U_PRINC / W_UNITS` (0.05 by default) is strictly
  below every threshold a unit share feeds, the lowest being `RHO_DOWN x H_UP2`
  (0.075).
- Rows of a conversation tainted above the pool's class, or with a private set
  other than this pool, are never units (POOLS 1.2). The pool's stage is shown to
  every grantee of the pool, and it is a function of the signals, so counting such
  a unit would let features of a query shaped by restricted or private content
  change what a lower-class reader sees (against DESIGN 12 step 1). Such a pool
  adapts on its memory gate and backstop only (limit, section 12).
- A recall whose feature computation failed (embedding unavailable, a typed
  search error) or whose holders fell back (DESIGN 8.2) is not a unit; it is
  counted in `feature_fail` (INV-11 is met by counting). An evaluation whose
  window has `feature_fail` above `F_FAIL_MAX` of its recalls is void: it moves
  nothing and neither advances nor resets the consecutive count. An outage
  therefore never reads as difficulty.

| Signal | Computation | Reads |
|---|---|---|
| `leaf_count` | live current leaf versions (heads with status live) | leaf heads |
| `token_total` | sum of `size_tokens` over the same leaves | leaf index |
| `session_count` | distinct `session_ref` holding at least 2 of those leaves, summed over writer principals with each principal's contribution capped at `S_PRINC` (below `RHO_DOWN x S_UP1`) unless the pool has a single writer (cardinality aggregation, approximate) | leaf index |
| `entity_count` | distinct `entities` occurring in at least 2 of those leaves, each writer principal's contribution capped at `E_PRINC` likewise | leaf index |
| `hard(unit)` | the recall was not flat-sufficient (6.2: low agreement, small margin, wide session spread), or the same conversation issued, within the window, a later recall with query cosine at least `TAU_RE` to it (from the conversation's recent query embeddings in gateway memory, POOLS 2.8) | log rows |
| `hard_rate` | share of `hard` units among the last `W_UNITS` units, defined only when at least `R_MIN` exist | log rows |
| `il_win`, `il_loss` (stage 1 and above) | over interleaved units (5.3): share where the opened items credited to the tree's list outnumber those credited to the flat list, and the reverse | log rows (`il_team`, `opened`) |
| `link_win`, `link_loss` (stage 2) | the same, interleaving the stage 2 list (with link expansion) against the stage 1 list | log rows |
| `multi_rate` | share of units in which leaves of two or more distinct sessions were opened | log rows |
| `open_share` | mean share of offered pointers opened per unit (DESIGN 9) | log rows |

Rules that keep these inside the existing design:

- No signal treats "offered and not opened" as evidence (DESIGN 9, 11.1). Opens
  are used only as positive evidence, and only from `opened` rows the gateway
  wrote.
- A pool's evaluator reads only its own pool's indices and, through the
  class-level reader with the pool term, its own stage 0 rows in the class-level
  indices of its own class. It never reads rows kept at a higher join class or for
  a private set (POOLS 1.2), so a pool never sees a higher-class query (DESIGN 12
  step 1).
- The per-recall features (`agree`, `margin`, `spread`, section 6.2) are scalars
  in the pool's log rows, which carry lineage, so erasure reaches them.
- The evaluator's state between evaluations is one control document `ad:<pool>`
  in the class control index: per trigger a consecutive-evaluation count,
  `cool_until`, an outcome count `k` (4.4), `leaf_count_at_outcome`, the
  attempt's spend `attempt_spent` (4.4), and the
  probation counters of 5.3. Scalars only, so it needs no lineage; a schema test
  asserts it. Signal values are recomputed at each evaluation, never stored.

Flood bounds. One principal contributes at most `U_PRINC` of the `W_UNITS` units
(0.05), below every threshold, and the difficulty gate also needs hard units from
at least `P_MIN` distinct principals (4.3), so one principal cannot meet it; its
sessions and entities are capped in the memory gate likewise. Probation needs
units from `P_MIN` principals, caps each at a quarter, and must hold with any one
principal's units removed (5.3), and a void probation rolls back; so one
principal can neither force a tree nor keep one. A principal that is the only
grantee can do both, at its own cost only. Several colluding principals can still
spend at most the attempt bound of 5.4, after which the backoff of 4.4 applies.
Starving (holding a pool flat with easy recalls) lasts at most until `N_BACK`
(4.3).

### 4.2 When it runs

An evaluator job per pool runs after `A_EVERY` committed writes or every
`EVAL_PERIOD`, whichever comes first, as the pool's service principal, through the
gateway, in the pool's background slot (DESIGN 15). It costs a few aggregation
queries and no model call. A pool with no writes and no recalls is not evaluated.

### 4.3 Up-moves

Stage 0 to 1 (propose a tree), all of:

1. `leaf_count > N_FLOOR`.
2. Memory gate: `leaf_count >= N_UP1` or `token_total >= T_UP1` or
   `session_count >= S_UP1` or `entity_count >= E_UP1`.
3. Difficulty gate: `hard_rate >= H_UP1` (so at least `R_MIN` units exist), with
   the hard units coming from at least `P_MIN` distinct principals unless the pool
   has a single grantee with `query`.
4. Conditions 1 to 3 hold at `E_CONSEC` consecutive non-void evaluations at least
   `EVAL_PERIOD` apart.
5. No transition of this pool in flight, `now >= cool_until`, at least `D_DWELL`
   since the pool's last adopted move, and `leaf_count >= RHO_RETRY x
   leaf_count_at_outcome` if a previous attempt failed or was rolled back (4.4).

Backstop: if `leaf_count >= N_BACK`, conditions 2 and 3 are waived, so a pool never
stays flat forever because the difficulty features are poor predictors or because
recalls are diluted. Conditions 1, 4 and 5 still apply. `N_BACK` is an absolute
PLACEHOLDER (50,000 leaves), above the largest development pool, so it is not
tuned (TUNING D2). A bulk import with no recalls therefore stays flat until
`N_BACK`, and moves at `N_BACK`.

`N_FLOOR = F_MAX x F_TARGET` leaves (384 with DESIGN's 24 and 16). Derived, not
tuned (D1): at or below it a first build has at most `F_MAX` level-1 nodes, so the
index-of-indexes rule (DESIGN 5.4) builds no upper level, and the "tree" is one
flat list of holders that adds a model call per holder per recall to what direct
search already returns.

Stage 1 to 2 (propose the learning layer), all of:

1. The pool has been at stage 1 for at least `D_DWELL1` and its probation (5.3)
   ended with the tree kept.
2. Loop-ready: at least `R2_MIN` units in the window with `open_share` inside the
   opened-share bounds of DESIGN 9 (0.05 to 0.60); outside them the opened signal
   is void for the loop (DESIGN 9).
3. Persistent demand: `hard_rate >= H_UP2` or `multi_rate >= M_UP2`. With
   `per_query` on, `hard_rate` here is computed over flat-answered units and the
   sampled escalations (weights `1 / p_esc`, 6.3), not over rule escalations,
   which are hard by selection.
4. Conditions 2 and 3 hold at `E_CONSEC` consecutive non-void evaluations; no
   transition in flight; `now >= cool_until`; dwell as above.

### 4.4 Hysteresis, backoff and down-moves

- Each up threshold has a down threshold at `RHO_DOWN` times it.
- A move needs its condition at `E_CONSEC` consecutive non-void evaluations spaced
  at least `EVAL_PERIOD` apart; a failing evaluation resets the count.
- After any adopted move, no further move for `D_DWELL`.
- Attempt. An attempt runs from a proposal until its outcome or until its tree is
  kept past probation. Its model calls are counted in `attempt_spent`, carried
  across erasure-abort retries and lease-expiry restarts (5.6) and reset only when
  an outcome is recorded or the tree is kept, so one attempt never spends more than
  `B_TRANS` however often it restarts. Probation's tree recalls are part of the
  attempt and count in `attempt_spent` (5.3, 5.4).
- Backoff. An outcome is an attempt whose candidate failed its structural checks,
  aborted on a failed build call or `budget_exceeded`, was aborted by
  `E_ABORT_MAX` erasures in a row (5.6), aborted at re-promotion because an
  earlier residency's member or edge was visible (below), or was rolled back at
  probation (5.3), or an adopted tree demoted within `D_DWELL` of its adoption. A single erasure abort
  or a lease expiry is not an outcome: the attempt continues under its remaining
  budget. Each outcome sets `k = k + 1`,
  `leaf_count_at_outcome = leaf_count`, and `cool_until = now + COOL_BASE x 2^k`
  capped at `COOL_MAX`; the next proposal also needs `leaf_count >= RHO_RETRY x
  leaf_count_at_outcome`. `k` returns to 0 when a tree stays adopted past
  `D_DWELL`. So adopt-then-demote cycles cannot repeat at one size, and their
  total spend is bounded (5.4).
- At most one transition per pool in flight (the build lease, 5.6), and at most
  `T_PAR` per tenant, so a tenant's growth cannot take the subscription's two
  model slots (DESIGN 15).

Down-moves. Memory shrinks through erasure, archiving and re-filing. ASSUMPTION
(design decision): a pool moves down one rung when the memory gate is below its
down thresholds, `hard_rate < RHO_DOWN x H_UP1` and (opens not void) `il_win -
il_loss < G_KEEP` over the sampled interleaved units (5.3), all at `E_CONSEC`
consecutive evaluations, after the dwell. A probation rollback (5.3) uses the
same mechanics at once. A down-move costs no model call.

Fence first. Every down-move starts by marking the pool `demoting` in the gateway's
write-path tracker: from then on a step 6 of the pool records `flat` and creates no
placement or link job (5.2). The down-move waits up to `J_WAIT` for the pool's open
jobs to commit, and cancels the rest: the tracker refuses a cancelled job's commit
(jobs run in the gateway and ask the tracker before committing), and the job's
leaf becomes a flat leaf. Only then does it stamp `R`. So no member or edge is
written after the stamp by a job that read the old stage.

- 1 to 0, in one commit under the shared side of the tenant's write gate, with the
  tombstone check (DESIGN 7.6 step 6): allocate a retirement number `R` from the
  pool's generation counter (a manifest with status `retired_marker` and no
  nodes); add `R` to `gen_to` of every member and edge of the pool (one update by
  query per kind; DESIGN 7.6 already writes `gen_to`); refresh (searchable
  publication, DESIGN 5.6); then compare-and-set `hd:topic` to generation 0 with
  `retired_by = R`. A task pinned at the old generation G does not have `R` in
  anc(G), so its view is unchanged (DESIGN 13.2). Every later generation of the
  pool lists `R` in its ancestry, so no member or edge stamped `R` (write-path ones
  included) is ever visible again: a re-promotion cannot resurrect them.
- 2 to 1: the same, with a retirement number stamped on edges and notes, and
  `hd:learn` retired by compare-and-set. Typing, reflection, audit and new
  candidates stop.
- Retention: the retired generation's nodes, and the members, edges and notes
  stamped `R`, are kept for `R_GEN` so pinned tasks and rollback resolve (DESIGN
  13.2), then deleted; ancestry ids are kept. Erasure closure covers all of them
  while kept (DESIGN 14.2, 14.4).
- Re-promotion is a fresh bulk build (5.2). Before its flip the committer asserts
  that, under the candidate's own pin, no member or edge is visible except the
  candidate's own; otherwise it aborts as a defect (counted) and the abort is an
  outcome (backoff applies), so a straggler the fence missed cannot make
  re-promotions repeat without bound. The `demoting` mark is cleared when the
  down-move's flip is done.

Physical tiers only move up (rollover); merging physical indices back is an
offline operator action.

### 4.5 Physical triggers

| Move | Trigger | Default |
|---|---|---|
| S1 to S2 (rollover) | the current physical index of a kind `>= B_SHARD_TARGET` bytes or older than `AGE_ROLL` | PLACEHOLDER (D8); vendor shard-size guidance as the starting range |

Computed from the pool's own index statistics (one pool per index, so they count
only the pool), with the same consecutive-evaluation rule.

### 4.6 Forced stage (experiments and operators)

`adaptive.mode` is `auto` (the product default) or `pinned:<stage>` for a pool or
a prefix of pool ids (the pin sets the stage floor and disables the evaluator and
probation; it does not turn layers on or off), set only in deployment
configuration by the operator, never by a tenant session (a session request that
names it is refused with the typed error of DESIGN 3). The pin is written into
the manifests of generations built under it. EXPERIMENT arms use `pinned:1` or
`pinned:2` from the first write (section 8).

## 5. The transition

A transition is a candidate generation of the pool's topic family, built in the
background from existing leaves while the current stage keeps serving, checked
structurally, adopted by compare-and-set of the head, and then kept or rolled
back by a probation measured from live use. It reuses versioning (DESIGN 5.6,
13.1, 13.2), the batch builder with its lease (7.6), the model cache (5.8) and the
tombstone guard (INV-8).

### 5.1 Which path it is, and why there is no replay gate

A transition is the DESIGN 7.6 builder run as a bulk build (BUILD-PLAN Step 9:
balanced clustering to `F_TARGET`, level-1 build calls, upper levels,
index-of-indexes rule).

What DESIGN gates. DESIGN 13.3's replay gate (labelled there OWNER "everything
versioned" plus a research recommendation) gates reorganiser candidates, and they
stay gated (section 13, row 7). First builds and DESIGN 7.6 batch rebuilds were
never gated. So adopting a first tree on structural checks plus probation is a
design decision about cost and latency, disclosed to the owner (section 15, Q7),
not a deviation from the owner's words.

Revision 1 added the replay gate to transitions at 80 replays, about 5,400 model
calls whatever the pool's size, against a build of 133 calls at 2,000 leaves. The
smallest gate DESIGN 13.3 allows has K = 8 clusters: at one query each, 8 routing
recalls plus about 32 judge calls (8 queries, both lists' top 10, 5 items per
call), that is 8 x 67 + 32 = 568 at the measured 67 calls per recall, or 480, 608
and 456 at the caps `r_max` of 2,000, 6,000 and 20,000 leaves (56, 72, 53), and at K
= 8 its power is low (EXPERIMENT 7.3). Probation (5.3) costs more model calls, up
to `B_PROB = N_PROB x r_max` (2,800, 3,600 and 2,650 at those sizes), but they are
the tree recalls of real readers, interleaved with the flat list, so they measure
the tree on the pool's own queries with the opens of its own users; the replay
gate needs stored queries and a judge model and measures neither. Both options and
their costs are in Q7.

Decision (ASSUMPTION, owner question Q7): adopt on structural checks alone (zero
model calls), then keep or roll back by probation (5.3). Trade-off, stated: a tree
serves some readers before there is evidence that it helps them. What a reader is
guaranteed (POOLS 5.1): during probation, only probation units consult the tree,
and their per-pool list is an interleave that holds the flat list's top
`ceil(K_POOL / 2)` (5); every other recall is flat. After a tree is kept, each
tree pool's list keeps the flat top `D_FLOOR` (3); other flat items can be
displaced by tree items (limit, section 12). The measured direction on the
6,000-leaf pool (tree about 0.77 to 0.80 against 0.67, not significant) is
consistent with that choice; it does not establish it.

### 5.2 Steps, 0 to 1

1. Propose: the evaluator (4.3) writes a candidate manifest (status building,
   `stage_target = 1`, the fired trigger names, not their values, and the pin
   state), takes the build lease (5.6), and sets the candidate's `W` to the
   highest source sequence `s` such that every entry of the pool at or below `s`
   is closed in the write-path tracker (its step 6 has recorded `flat` or its job
   has finished), so no sequence below `W` can still be uncommitted and fall into
   neither the build nor the gap. (The pool's `wp` is not used: stage 0 does not
   advance it.) The budget counter (DESIGN 18.1) gets
   `B_TRANS - attempt_spent`, with `B_TRANS` computed at the attempt's first `W`
   (5.4, 4.4).
2. Build: bulk build over every live leaf of the pool with `src_seq <= W`, read
   from the leaf heads (no queue record is needed). Members carry `gen_from =
   G+1`, so they are invisible until the flip. Build calls go through the model
   port, the tenant's fair queue and the cache, at the lower priority within the
   tenant (a recall is never queued behind a build).
3. Structural checks, zero model calls: no build call failed (else DESIGN 7.6
   aborts); every live leaf with `src_seq <= W` is a member of at least one
   level-1 node; every node holds between `F_MIN` and `F_MAX` children after
   splits and merges; the levels follow the index-of-indexes rule; every node,
   member and manifest carries lineage; top-level holders `T <= F_MAX`. A failure is
   an outcome (4.4), counted as `structure_fail`.
4. Commit under the shared side of the tenant's write gate, in one block: read the
   published tombstone sequence and abort if any tombstone newer than the build's
   start intersects the candidate's lineage (DESIGN 7.6 step 6) or names the pool
   (a pool delete); refresh the indices written; compare-and-set `wp` to `W`;
   compare-and-set `hd:topic` from 0 to `G+1`. The pool is now at stage 1 and in
   probation (5.3).
5. The gap, without queue records. DESIGN 20 already keeps P and its open jobs in
   the gateway's write-path tracker. Every allocated source sequence of the pool
   is an open entry there from DESIGN 7.4 step 1. At step 6 the entry becomes a
   placement job if the writer read stage 1 or above, or is marked `flat` if it
   read stage 0. P may pass an entry only when its job finished (written, or failed
   and counted), when the leaf is erased, or, for a `flat` entry above `W`, when the
   rebuild that places it has flipped. The gap is the set of `flat` entries above
   `W`; it is placed by the first batch rebuild after the flip (DESIGN 7.6 edited to
   place live leaves above `W` that have neither a member nor a job), with members
   bound by `gen_from`, atomically visible at that rebuild's flip. Consequences: a
   leaf allocated before the flip whose step 6 ran after it reads stage 1, gets a
   job, and P waits for that job, so its member never appears under a task pinned
   earlier (the case revision 1 got wrong); no leaf is placed both by a job and by
   the gap rebuild; direct search finds gap leaves throughout. After a gateway
   restart the tracker is rebuilt from the source log's `pool` and `src_seq` over
   `(P, counter]`: a leaf with a member is placed, one with a queue record and no
   member is an open job (re-run), one with neither above `W` is a gap leaf.
6. Entity and time families: built afterwards by their own builds (2.2).

Transition 1 to 2 has the same shape. The candidate is a new generation of the
topic family that shares every unchanged node and carries the backfilled typed
edges with `gen_from = G+1`; its structural checks are that every leaf with
`src_seq <= W_link` had its typing pass (written or failed and counted) and every
edge carries lineage; its commit flips the generation head and then creates
`hd:learn`. No write-path link job runs before the flip, so a failed candidate
leaves nothing behind; leaves stored after `W_link` and before the flip are linked
by the first generation after the flip, and leaves stored after the flip read
stage 2 at step 6 and get write-path link jobs under P.

### 5.3 Probation and rollback

Probation starts at the flip. It is part of the attempt: its tree recalls count
in `attempt_spent`, under the attempt bound of 5.4.

- Which recalls consult the tree. Only a recall that will count as a probation
  unit (4.1: gateway-derived, at most `U_SESS` per session, at most `N_PROB / 4`
  per principal, no injection or probe rows, no tainted rows, no failed features)
  takes the full path; every other recall of the pool during probation takes the
  flat path (logged `tree_consulted = false`, `esc_reason = probation`). So
  probation spends at most `B_PROB = N_PROB x r_max` model calls, and most readers
  see no change in latency.
- How a unit is measured, without being blind to losses. The flat list of the
  pool (its direct search, which runs on every recall, DESIGN 8.1) is computed in
  shadow. The pool's list shown to the reader is a team-draft interleave, seeded
  by the search id, of the fused list and the flat list, cut at `K_POOL`: each
  pick takes the picking list's best item not yet shown, the first picker
  alternates by a coin, and each shown item is logged with `il_team` (`tree` or
  `flat`). A unit is a win if the reader opened more items credited to the tree's
  list than to the flat list, a loss if fewer, a tie otherwise. A bad tree that
  pushes flat items down now loses units, because the flat list's picks are shown
  and opened; the reader always sees the flat top `ceil(K_POOL / 2)`.
- End. Probation ends at `N_PROB` units, when `B_PROB` is spent, or after `D_PROB`,
  whichever comes first.
- Decision. The tree is kept only if all hold: at least `N_PROB` units were
  collected; they come from at least `P_MIN` distinct principals unless the pool
  has a single grantee with `query`; `open_share` is inside the DESIGN 9 bounds;
  and `il_win - il_loss >= G_KEEP`, also with any one principal's units removed.
  Otherwise the tree is rolled back: a short, void or principal-dominated
  probation rolls back, never keeps (typed events `probation_short`,
  `probation_void`, `probation_loss`).
- After a kept probation, a share `EPS_IL` of tree-consulted units is still
  interleaved, seeded by the search id, so the down-move's benefit test (4.4) has
  the same measure.
- Rollback is the 1 to 0 down-move of 4.4, at once and with no dwell; it is an
  outcome (4.4), so the backoff applies.
- 1 to 2 has the same probation, interleaving the stage 2 list against the stage 1
  list (`link_win`, `link_loss`), rolling back to stage 1. Link expansion is
  model-free, so its probation spends no model call beyond stage 1's own recalls.

### 5.4 Cost

`build(N, m)`: level-1 nodes `ceil(m x N / F_TARGET)`, then upper levels by the
index-of-indexes rule (group by `F_TARGET` while a level has more than `F_MAX`
nodes); one build call per node. `m` is the mean number of level-1 parents per
leaf: measured 1.0 on the 6,000-leaf pool (5,988 leaves gave 375 level-1 and 24
upper nodes, 399 calls, matching the formula); at most `M_MAX = 2`. `T` is the
number of top-level holders, and `r_max = T + 48` the per-recall cap (`B_HOLDERS
= T + 36` plus 12 organise calls, DESIGN 8.2, chunked calls included), enforced by
the counter.

Per attempt:

| Part | Model calls |
|---|---|
| Build | `build(N, m)`; failed calls count, and retries are capped at the same figure again (DESIGN 18.1): at most `2 x build(N, m)` |
| Structural checks and adoption decision | 0 |
| Gap rebuild (first batch rebuild after the flip) | at most one call per level-1 node holding gap leaves plus its ancestor path: at most `build(N, m)` |
| Probation | at most `B_PROB = N_PROB x r_max`: only units consult the tree (5.3) |
| `B_TRANS`, enforced by the counter | `3 x build(N, M_MAX) + B_PROB`, computed at the attempt's first `W` |

Worst case from empty to a kept tree (failed calls doubling the build, every
level-1 node in the gap, `m = M_MAX`, probation spent in full), and the backoff's
cumulative bound. Each failed or rolled-back attempt raises the next attempt's
size by `RHO_RETRY = 1.5`, so the builds of all attempts up to size N form a
geometric series of ratio 2/3 and sum to at most `9 x build(N, 2)` (build is close
to linear in N). Probation does not shrink with size, so it is bounded by the
number of attempts: at most `A(N) = floor(log_1.5(N / N_FLOOR)) + 1` attempts can
fit between `N_FLOOR` and N, each spending at most `N_PROB x (F_MAX + 48) = 3,600`
on probation. Cumulative bound: `9 x build(N, 2) + A(N) x 3,600`.

| Pool size N | T at m = 1 (r_max) | build, m = 1 (planned) | build, m = 2 | `B_PROB` | worst one attempt `3 x build(N, 2) + B_PROB` | A(N) | worst cumulative | level-1 input tokens, m = 1 | build time, m = 1, two model slots, 17 s per call | revision 1 at 67 per replay, for comparison |
|---|---|---|---|---|---|---|---|---|---|---|
| 2,000 | 8 (56) | 133 | 266 | 2,800 | 3,598 | 5 | 20,394 | 1.9 M | about 19 min | about 5,700 |
| 6,000 | 24 (72) | 399 (measured) | 800 | 3,600 | 6,000 | 7 | 32,400 | 5.6 M | about 57 min | about 6,300 |
| 20,000 | 5 (53) | 1,334 | 2,667 | 2,650 | 10,651 | 10 | 60,003 | 18.8 M | about 3.1 h | about 8,300 |

The replay-gated alternative (Q7) spends `3 x build(N, 2)` plus one K = 8 replay
(480, 608, 456 at the caps) per attempt: 1,278, 3,008 and 8,457, and cumulatively
`9 x build(N, 2) + A(N) x 608`: 5,434, 11,456 and 30,083. It serves no reader
before adoption, and after adoption every recall pays the full path as under the
default.

Latency. A flat recall makes no model call. A tree recall makes up to `r_max`
model calls in the parallel waves of DESIGN 8.2 (at least one routing wave and one
organise call); its latency is not measured (PENDING_REAL_TEST).
During probation only the `N_PROB` unit recalls pay it; after a tree is kept,
every full-path recall of the pool does.

Tokens. Calls are capped; input tokens per attempt are bounded by the calls times
the largest build input, at most `F_MAX` children of at most `LEAF_CAP` tokens
(DESIGN 7.2, 7.6), and are reported per attempt (the measured level-1 input is
about 15k tokens per call). A separate token cap is not added.

Arithmetic: at 2,000, 125 level-1 nodes and 8 upper (m = 1), 250 and 16 (m = 2);
at 6,000, 375 and 24, or 750, 47 and 3; at 20,000, 1,250, 79 and 5, or 2,500, 157
and 10. Revision 1 is `2 x (N / 15 + L) + 72 + 80 x 67` with `L` the upper-level
nodes at m = 1 (8, 24, 84), and it omitted its judge calls (160 to 320 more). Upper-level call tokens and times are not measured; the
time column assumes 17 s for every call.

After a tree is kept, reads cost model calls that a flat pool does not: each
full-path recall at most `r_max` (about 67 measured at 6,000); that is what the
owner's method costs on a tree, not a transition cost. Several tree pools in
one recall are bounded by `R_CAP` (POOLS 5.3), and injections never call the
model (POOLS 6.1). Ordinary upkeep after adoption is DESIGN 7.6: per batch
rebuild at most `min(new leaves x M_MAX, level-1 nodes)` calls plus ancestor paths.

Enforcement:

- The counter refuses a call that would take the attempt's `attempt_spent` (4.4,
  carried across retries and restarts, gap rebuild included) past `B_TRANS`; the
  candidate aborts with the typed
  outcome `budget_exceeded` (counted apart from `no_benefit`, because it means
  the bound or the build is wrong), and the backoff applies.
- Per-tenant hourly budget and fair queue apply (DESIGN 15). Hitting the hourly
  budget pauses the builder, which keeps its lease (5.6); a transition takes as
  long as the budget allows and never exceeds it.
- Cache: build calls are prompt-free and cached per pool (DESIGN 5.8 with the pool
  in the key), so a builder restarted after a crash re-pays only nodes whose inputs
  changed. That saving is not claimed in the bounds above.

### 5.5 What a reader sees

- Before the flip: stage 0, bit for bit. A task started before the flip keeps `gen
  = 0` for its life (DESIGN 13.2); its delegates inherit it.
- At the flip: new tasks pin `G+1`; the candidate's members were invisible until
  the head changed (13.2). During probation, a recall that counts as a unit gets
  the interleaved list (5.3) and the tree's latency; every other recall stays on
  the flat path.
- At a rollback: tasks pinned at `G+1` keep it for their life; new tasks are flat.
- The digest reports the stage of pools in `S` (3.2), not that a build runs.
- Latency of an unrelated recall is unaffected: the build runs in the pool's
  background slot at lower priority (DESIGN 15).

### 5.6 Failure, abort and the lease

| Event | Handling |
|---|---|
| A build call fails | DESIGN 7.6: the generation aborts, unshared documents deleted, counted (INV-11); stage unchanged; outcome (4.4) |
| Structural check fails | delete the candidate; outcome `structure_fail` |
| `B_TRANS` reached | abort; outcome `budget_exceeded` |
| Probation rolls back (loss, short, void or one principal deciding) | 1 to 0 down-move (4.4) with its fence; outcome `no_benefit` |
| An erasure's boundary publishes mid-transition | the commit takes the shared side of the write gate and checks the published tombstone sequence; an intersecting tombstone aborts the candidate (DESIGN 7.6 step 6). Retry after the erasure's closure finishes, in the same attempt and under its remaining budget (4.4); `E_ABORT_MAX` such aborts in a row end the attempt as one outcome (a pool under constant erasure may stay at its stage; limit) |
| Pool deleted mid-run | the pool-scope tombstone aborts the commit; the candidate's documents are in the pool's closure |
| Builder paused by the hourly budget | keeps its lease: the lease is renewed by a heartbeat every `LEASE_BEAT` while the builder process lives, paused or not |
| Builder crash | no heartbeat for `3 x LEASE_BEAT`: the lease expires, the next evaluator deletes the candidate's unshared documents and restarts the same attempt under its remaining budget (4.4); a restarted build re-uses cached outputs; a crash loop ends `budget_exceeded` |
| Head compare-and-set lost | another writer changed the head: discard and re-propose; with the lease this cannot happen between two transitions of one pool |
| Stored-byte cap reached | typed refusal of the candidate's writes (DESIGN 15), abort |
| Revocation or tenant disable | the job runs as the pool's service principal; a disabled tenant stops its jobs |

A failed transition never degrades readers: they were never moved. A rolled-back
one moves new tasks back to the flat path.

## 6. Read path: the per-query choice

### 6.1 The question

Even in a tree pool, many queries are answered well by the flat path, and the full
path costs up to `r_max` model calls. DESIGN says otherwise today: "Every
top-level holder of every pinned family is asked in parallel (OWNER 'asks all
context index shard holders')" (8.2), and 8.1 step 3 starts the fan-out, the
direct search and link expansion in parallel, AGREED (a). Answering easy queries
flat is not in the owner's adaptive sentence, which speaks of memory and context;
reading "context ... demands" per query is a reading. It is a DEVIATION candidate
(DESIGN 22 would list it), built behind a switch, off by default, and left to the
owner (open question Q6).

### 6.2 The rule (proposal)

`adaptive.per_query` is `off` until TUNING D4 passes its acceptance. Off, every
recall of a stage 1 or 2 pool takes the full path, exactly as DESIGN 8.

On, for a pool at stage 1 or 2:

1. Run the direct search first (and link expansion at stage 2), both model-free.
2. Compute from the lists already fetched: `agree` (size of the intersection of
   the BM25 top 10 and the kNN top 10, over 10); `margin` ((s1 - s10) / s1 for the
   kNN cosines of ranks 1 and 10); `spread` (distinct sessions in the fused top
   10).
3. Flat-sufficient if `agree >= A_MIN` and `margin >= G_MIN` and `spread <=
   S_MAX`. Otherwise, or if the agent passed `deep = true`, or for every `explore`,
   or if a feature failed, it escalates to the full path (fused as DESIGN 8.4).
4. A flat-sufficient recall is escalated anyway with probability `EPS_ESC`, from
   a seeded hash of the search id; the sample gives the tree's behaviour on the
   queries the rule called easy, with a known propensity.
5. Cheap-first is serial: a full-path recall pays one direct-search round trip
   first.

### 6.3 Logging, so the log stays truthful

Each protocol log row (DESIGN 9) gains: `stage`, `route` (`flat` or `full`),
`tree_consulted`, `esc_reason` (`rule`, `sample`, `forced`, `budget`,
`probation`, `none`), `il_team` per shown item of an interleaved unit (5.3),
`p_esc` (the propensity of the action taken: 1 for `rule`, `forced` and `budget`;
`EPS_ESC` for `sample`; `1 - EPS_ESC` for a flat-sufficient recall not escalated),
`direct_rank`, and the scalars `agree`, `margin`, `spread`. Every consumer of
`offered_by_tree` excludes rows with `tree_consulted = false`:

| Consumer | Rule |
|---|---|
| DESIGN 10 step 3, `tree_missed` | only over searches with `tree_consulted = true`; estimates over the sampled escalations use weights `1 / p_esc` |
| DESIGN 12 steps 3 and 4 | a flat-answered search is "tree not consulted", never a miss |
| DESIGN 11.1 "judge: relevant but never offered", "reverse-audit misses" | same exclusion |
| Probation and down-move signals (5.3, 4.4) | interleaved tree-consulted units only |
| EXPERIMENT 2.2 (c) | arms force the full path (section 8), so every row is consulted |

`any_missed` is unaffected. The opened-share bounds (DESIGN 9) are over all
`via = recall` rows.

## 7. Invariants and the four concerns, in every stage and during a transition

| Invariant or concern | Stage 0 | Stage 1 and 2 | During a transition | Down-move or rollback |
|---|---|---|---|---|
| INV-1 tenant on everything | every document, including `ad:<pool>` and manifests, carries `tenant`, pool and class | same | lease, budget and cooldown keys embed the tenant (INV-10) | same |
| INV-2 trim before retrieval | only the pool's own leaf index is searched; its heads, control documents and rows in class-level indices are read only through the pool-term reader (POOLS 1.2); the version filter is not an access filter | same, plus the pool's own row and tree indices | the builder reads only its pool | n/a |
| INV-3 join | log rows and notes take the join class; a pool's evaluator reads only its own indices | edges in the pool's index, which is the pool's class | candidate documents take the pool's class | n/a |
| INV-4 lineage | the only stored adaptive state is `ad:<pool>` and manifests, scalars and ids only | same | candidate nodes, members and edges carry lineage; the structural check refuses any that lacks it | stamped documents keep their lineage |
| INV-5 no credential | the evaluator and builders call the gateway | same | same | same |
| INV-6 immutability | a stage change edits no version; a stage is a head flip | same | a candidate is new documents | `gen_to` stamps are the retirement update DESIGN 7.6 already makes |
| INV-7 pointers | the flat digest has the same field contract plus `pool`, `label`, `origin` (POOLS 3.8) | same | same | same |
| INV-8 tombstones | the guard covers ingest, lookup, restore and replay | rebuild, replay and restore check it | commit-time check under the shared gate (5.2 step 4) | the down-move commit checks it |
| INV-9 no cancellation | none | none | an erasure aborts the pool's own background candidate; no request is cancelled | none |
| INV-10 tenant in every key | thresholds are config constants; per-pool state keyed by tenant and pool | same | lease, fair queue and budget keys per tenant | same |
| INV-11 typed failures | a failed feature is counted in `feature_fail`, never read as easy or hard | an escalation failure is typed; fallback as DESIGN 8.2 | every abort and outcome in 5.6 is typed and counted | typed event per move |
| Tenant isolation | no cross-tenant read, write or statistic | same | the transition's calls go through the tenant's own fair queue and budget | same |
| Access policy | per-pool indices; sizes over the pool | one tree per pool, never a mixed summary | the builder runs as the pool's service principal | the stage of a pool outside `S` is never reported |
| Erasure | 7.1 | 7.1 | 7.2 | 7.3 |
| Versioning | leaf pointers resolve at `gen = 0` | as DESIGN 13 | candidate invisible until the flip; pinned tasks keep their generation | stamped documents invisible to later pins; old generation resolvable for `R_GEN` |

No content crosses tenants through thresholds or statistics: thresholds are
constants in `config.py`, set offline (TUNING), versioned with the code, never
fitted from live tenants and never stored in `lc-sys-registry`. A pool's signals
come from its own indices. Typed gateway events say which pool changed stage, with
no value.

### 7.1 Erasure in every stage

- Stage 0: the closure of DESIGN 14.2 runs over every index of the tenant and finds
  the pool's leaves, source documents, log rows, stored queries and authz. For a
  pool that has never left stage 0, the node, member, edge, note and cache indices
  do not exist (kinds are created lazily) or hold zero documents, and a test
  asserts that. The verifier (14.7) plants canaries through the real writer in the
  kinds that exist and runs its positive control on each; it still runs the
  full-kind controls where the kinds exist (a pinned stage 2 pool, the transition
  test, a demoted pool), so no kind's control is skipped (LAB-R8), and it prints the
  probe count per stage.
- Stage 1 and 2: as DESIGN 14, including the walk up `lin_nodes` in every retained
  generation.
- Physical indices: the closure and the verifier enumerate the pool's manifest of
  physical indices (9.3), and the class-level indices that hold the pool's heads,
  control documents and stage 0 rows (POOLS 1.2), matched by the `pool` field.

### 7.2 Erasure arriving mid-transition

Erasure takes the exclusive side of the tenant's write gate for its closure
(DESIGN 14.3 step 2); the builder's commit takes the shared side, so they cannot
interleave:

1. Before the build: the leaf is already deleted or guarded; the build reads only
   live leaves and the guard refuses an in-scope input.
2. During the build: candidate nodes already written carry lineage, so the closure
   finds and rewrites them, and the commit-time check aborts the candidate.
3. While the commit holds the shared gate: the erasure waits; after the flip its
   closure covers the new live generation.
4. While the builder waits for the shared gate: after the boundary it re-reads the
   published sequence, sees the intersection, aborts.

The head flip never happens on a tombstone check older than the last boundary.

### 7.3 Erasure and down-moves

- During the down-move commit: it takes the shared side of the write gate, so an
  erasure's closure runs entirely before or after it; after it, the closure finds
  the stamped members, edges and notes and the retired generation by lineage.
- After a down-move (1 to 0 and 2 to 1): the retained documents stay in the closure
  until `R_GEN` deletes them; the verifier's per-stage assertions include demoted
  pools, with a canary in the retired generation, its members, edges and notes.

## 8. Interaction with the registered experiment

EXPERIMENT's arms force the tree on regardless of size and are not changed. A0 is
stage 0 by construction (the same direct search; the version filter changes
nothing on the experiment's data, which has no supersede or retraction). A1, A2,
LOOP, FROZEN and the rest run `adaptive.mode = pinned:1` or `pinned:2` from the
first write, with `per_query = off` and no probation; C1 and C2 are unaffected. The
S0 fixture pins its stage for its recall gates; adaptive behaviour on it is
exercised by separate tests with lowered thresholds (section 10). The run driver
refuses an arm whose pin differs from its registration.

A later, separately approved measurement with a new hypothesis HA is proposed in
TUNING (sections 5 and 6), never on the fresh set: the crossover of tree and flat
recall over pool size, the timing of ADAPT's transition against that crossover,
zero model calls below it, and ADAPT's non-inferiority to an always-tree arm after
adoption. Thresholds and prompts are frozen before any confirmatory use
(EXPERIMENT 3.7).

## 9. Physical scaling, without downtime

Everything in this section is PENDING_REAL_TEST until probed on the Basic cluster
(P18, P19, section 11); fallbacks are stated.

### 9.1 Index layout

A pool's indices are `lc-<t>-<c>-<p>-<kind>`, each an alias over physical indices
`...-p<8 random hex>`. Pool and class ids are lowercase `[a-z0-9]{4,16}` without
hyphen. The class key's pattern `lc-<t>-<c>-*` covers every pool of the class
(DESIGN 6.3); which pools a session may touch is the gateway's decision (POOLS
2.4).

### 9.2 Rollover

Append-only kinds (leaf, log, stored queries) roll over: when the current physical
index crosses the size or age bound, a new physical index is created and recorded
in the pool's manifest of physical indices (a control document of the pool in the
class control index) first, then the write alias is moved to
it in one atomic `_aliases` action (PENDING_REAL_TEST P8); the search alias gains
the new index and keeps the old ones. Because the manifest is written before the
alias moves, an erasure closure never misses an index that can hold a document.
Reads search the alias, so nothing is reindexed and nothing pauses. Ids are unique
per version (`leaf_id:lv`), so a document lives in one physical index. Kinds
updated in place (heads, the control index) are not rolled; nodes use path-copied
immutable versions and roll like leaves; an erasure rewrite addresses the physical
index that holds the `_id` (found by a search on the alias). A different
primary-shard count needs the split API (PENDING_REAL_TEST P18) or a copy-and-swap
behind the alias (DESIGN 14.5); rollover needs neither.

### 9.3 Erasure and the verifier across physical indices

- Each pool keeps a manifest of its physical indices as a control document in the
  class control index (ids only). The erasure closure, the purge loop, copy-and-swap and the verifier
  enumerate that manifest, not an alias.
- The purge (DESIGN 14.5) runs per physical index; `docs.deleted == 0` is required
  on every physical index of the pool before the physical purge is reported.
- The verifier reads each physical index directly (14.7). Its positive control
  plants a canary in each physical index; it asserts that the manifest equals the
  set of indices matching the pool's pattern, that no document in a pool's index
  has a `pool` other than that pool, and that every document in a class-level
  index has a `pool` naming an existing pool of that class (a count of documents
  with a missing or unknown `pool` must be zero).
- Snapshots follow DESIGN 6.5 (source, control and authz indices); a restore
  rebuilds projections for every physical index named in the manifest after the
  tombstone journal is applied (14.6), then re-reads the epoch journal records of
  POOLS 2.5.

### 9.4 Cost of many pools

Layout 1 (the default, 2.3). Per tenant: `src`, `ctl`, `authz` (3 shards) plus,
per class, the class control index and the class-level row indices `log`, `sq`,
`note`, `cache` (up to 5, created lazily, POOLS 1.2). Per pool: stage 0 holds its
leaf index only (1 shard); stage 1 adds node, member, cache and its own log and
stored-query indices (6); stage 2 adds edge and note (8); each rollover adds one
per rolled kind. Layout 2: per tenant 3 plus 4 per class; per pool 4 at stage 0
(leaf, control, log, stored queries), 7 at stage 1, 9 at stage 2. Each shard
counts `(1 + r)` with `r` replicas.

With DESIGN 6.4's 1,000 shards per node, about 20 system shards and provisioning's
80% ceiling, about 780 shards are usable per node. One tenant with two classes:

| Pools per data node | Layout 1, no replica | Layout 1, one replica | Layout 2, no replica | Layout 2, one replica |
|---|---|---|---|---|
| tenant base shards | 13 | 26 | 11 | 22 |
| all pools at stage 0 | (780 - 13) / 1 = 767 | (780 - 26) / 2 = 377 | (780 - 11) / 4 = 192 | (780 - 22) / 8 = 94 |
| all pools at stage 2 | (780 - 13) / 8 = 95 | (780 - 26) / 16 = 47 | (780 - 11) / 9 = 85 | (780 - 22) / 18 = 42 |
| 500 person pools at stage 0 | 513 shards, 1 node | 1,026 shards, 2 nodes | 2,011 shards, 3 nodes | 4,022 shards, 6 nodes |

Provisioning refuses a pool that would take the node above the 80% ceiling, with a
typed refusal. Many small indices also cost cluster-state size and per-shard
overhead (SOURCE: the shard-size guidance cited in DESIGN 5.6). Tree pools still
cost 6 to 8 shards each, stated as a limit (12).

## 10. Build-plan step

Proposed as BUILD-PLAN Step 24, built after Step 19 and before Step 22's S0 run,
numbered 24 so existing numbers keep working. It uses no model call: the stub and
the counter only.

### Step 24: adaptive mode (stages 0 to 2, probation, per-query path, rollover)

- Modules: `adaptive/modes.py` (stage from heads, generation 0, pins),
  `adaptive/signals.py` (4.1, with the unit caps and exclusions),
  `adaptive/evaluator.py` (triggers, hysteresis, backoff, `ad:<pool>`),
  `adaptive/transition.py` (candidate, structural checks, commit, gap through the
  write-path tracker, probation, rollback; reuses `structure/`),
  `adaptive/tiers.py` (rollover, manifest of physical indices), `recall/route.py`
  (section 6), `config.py` (every constant with its TUNING id), pins in the run
  driver.
- Failing-first (each with the broken variant that must fail it):
  - Stage 0 recall and the A0 arm give the same digest on the same leaves, and a
    write plus a recall at stage 0 leave the model-port counter at 0 and write no
    queue record (broken: a stage 0 write that calls a build or writes a queue
    record; a stage 0 recall that calls `route`).
  - A pool at or below `N_FLOOR` never gets a candidate (broken: a size-only
    trigger with no floor).
  - Difficulty alone does not move a pool below its memory gate; size alone does not
    move it before `N_BACK`; a bulk import with no recalls moves at `N_BACK` and not
    before (broken: either gate alone fires).
  - Hysteresis: a pool at the threshold for one evaluation does not move; at
    `E_CONSEC` it does; an oscillating signal moves at most once per `D_DWELL`
    (broken: one threshold; no dwell; count not reset).
  - Signals: an embedding outage over a window with every gate otherwise met starts
    no transition and resets no count (broken: failures counted as hard); injection
    and probe rows are absent from every signal, and a planted probe appears in no
    stored query and no probation count (broken: `via` not filtered); one
    principal issuing 1,000 recalls moves `hard_rate` by at most `U_PRINC /
    W_UNITS` (broken: no cap); a caller-supplied task id is ignored.
  - Structural checks: a candidate with a leaf in no node, or a node without
    lineage, is not adopted (broken: adopt on build completion).
  - Probation, with opens scripted as the fixture's gold items opened (as TUNING
    D7) and credited through the team-draft interleave (5.3): with the stub's
    router replaced by a random router (a known-bad tree) in a recall whose gold
    items are spread over two pools, `il_loss` exceeds `il_win` and the tree is
    rolled back at `N_PROB` in at least 95% of seeded runs; with the normal stub,
    kept in at least 95% (broken: no probation; credit on offers instead of opens;
    losses not observed because the flat list is not interleaved). With opens void,
    with fewer than `N_PROB` units by `D_PROB`, with units from fewer than `P_MIN`
    principals in a pool with several grantees, or with the keep decided only by
    one principal's units (the leave-one-principal-out check fails), the tree is
    rolled back, never kept (broken: `probation_void` keeps the tree; units
    counted per token). During probation every interleaved digest holds the flat
    top `ceil(K_POOL / 2)` and every other recall of the pool has
    `tree_consulted = false`; after a kept tree every digest holds the flat top
    `D_FLOOR` (broken: the tree list alone).
  - Probation spend: probation calls count in `attempt_spent`, and probation ends
    at `B_PROB` calls with the tree rolled back if the keep rule is not met by then
    (broken: probation outside the attempt bound; no `B_PROB` cap).
  - Principal bounds: 300 root tokens minted by one principal, each with its own
    conversation, count as one principal in `session_count`, `U_PRINC`, `P_MIN`
    and the leave-one-principal-out check (broken: counts keyed by root token or
    conversation); one principal's units are at most `U_PRINC / W_UNITS` of the
    window and its sessions at most `S_PRINC` (broken: no per-principal cap);
    tainted rows and rows of a private set feed no signal and no probation unit
    (broken: `taint` not filtered); the difficulty gate is not met with fewer than
    `P_MIN` distinct principals among the hard units (broken: no principal count).
  - Backoff: after a rollback the pool is not proposed again until `cool_until` and
    `RHO_RETRY` growth; a scripted adopt-then-demote trace spends at most `9 x
    build(N, 2) + A(N) x 3,600` over all attempts, probation included (broken:
    re-propose at the next evaluation).
  - Budget: with the stub set to the measured profile (one build call per 16
    leaves at level 1, 67 calls per recall), a transition's counter never exceeds
    `B_TRANS`, and a call past it aborts with `budget_exceeded` (broken: no cap); an
    attempt aborted by `E_ABORT_MAX - 1` erasures and one killed builder still
    spends at most `B_TRANS` in total (broken: budget reset on retry or restart).
  - Lease: a builder paused by the hourly budget for longer than three heartbeat
    periods keeps its lease and its candidate (broken: wall-clock lease); a killed
    builder's lease expires and its candidate is deleted.
  - Gap and straddler: a leaf allocated before the flip whose step 6 runs after it
    (test hook) gets a job, P waits for it, and its member never appears under a
    task pinned before; a leaf whose step 6 ran at stage 0 above `W` is placed only
    by the gap rebuild; no leaf is placed twice (broken: P passes a sequence with
    no job; job created for a `flat` sequence; SEC-V2 red).
  - Resurrection: cycles 1-0-1 and 2-1-2 with a planted write-path member and edge
    from the first residency; the planted documents are invisible to every task
    pinned after re-promotion, and visible to a task pinned before the down-move
    (broken: no `R` stamp; `R` missing from later ancestry; stamping the old
    generation number instead of `R`).
  - Down-move fence: a placement job paused by test hook between its stage read
    and its commit, around a probation rollback and around a memory down-move;
    released before `J_WAIT` it commits before `R` is stamped, released after it
    is cancelled and its commit refused, and in both cases no member or edge
    without `R` in `gen_to` exists after the stamp; a step 6 after the `demoting`
    mark records `flat` (broken: stamp `R` with no fence; a cancelled job's commit
    accepted).
  - Re-promotion abort: a planted member without `R` (the fence bypassed by test
    hook) makes the re-promotion's pre-flip assertion abort the candidate as a
    defect, the abort counts as an outcome, and the next attempt waits for
    `cool_until` (broken: abort not counted; immediate re-proposal).
  - Erasure at each phase of a transition (before the build, mid-build, between the
    check and the compare-and-set by test hook, after the flip, during the gap
    rebuild) and during a down-move commit, and after a down-move with canaries in
    the retired generation, members, edges and notes: no live derived document of
    the canary after the boundary; the verifier reports zero residue (broken:
    tombstone checked at build start only; the flip without the shared gate; the
    verifier skipping demoted pools).
  - A pool deleted while its transition runs: the commit aborts and the closure
    finds the candidate's documents (broken: no pool-scope check at commit).
  - Logging: a flat-answered recall has `tree_consulted = false` and counts in no
    `tree_missed`; `p_esc` is the propensity of the action taken (broken: `EPS_ESC`
    logged for non-escalated recalls).
  - Isolation: a pool's evaluator issues no request to another pool's or a
    class-level index (request-plan assertion; broken: a class-wide count); two
    tenants with identical leaves and different query histories get identical
    thresholds and independent stages (broken: a global quantile; SEC-T red); the
    stage of a pool outside `S` never appears in a digest.
  - `adaptive.mode` set from a session request is refused (broken: accepted).
  - Rollover under concurrent writes loses no document; the manifest is written
    before the alias moves; the closure and the verifier cover every physical index
    (broken: closure over the alias only; alias moved first).
  - `ad:<pool>` holds scalars only (schema assertion; broken: a stored signal list).
- Acceptance criteria:
  - AC-1 A new pool is plain retrieval. Verify by: the stage 0 tests; the counter is
    0 across 1,000 stub writes and recalls, and no queue record exists.
  - AC-2 A pool changes stage only on the triggers of 4.3 with hysteresis, and only
    on gateway-derived, non-failed units. Verify by: a property test over generated
    signal sequences (at most one move per dwell, none on a single evaluation, none
    on a void evaluation) and the signal tests.
  - AC-3 A transition is a structurally checked candidate whose cost is bounded by
    `B_TRANS` (probation included), and whose total spend over failed attempts is
    bounded by `9 x build(N, 2) + A(N) x 3,600`. Verify by: the budget, probation
    spend, backoff and lease tests with the measured stub profile.
  - AC-4 Readers are unaffected by a transition, a rollback and a down-move. Verify
    by: a pinned task across each flip (version probe), digests before the flip equal
    those of a pool with no transition, and the resurrection tests.
  - AC-5 A tree that does not help is rolled back, and no single principal can
    force or keep one. Verify by: the probation tests with the random-router
    control and the principal-bound tests.
  - AC-6 The per-query choice is logged truthfully. Verify by: the log rows of a
    mixed run; `tree_missed` with and without the exclusion differ, and the first
    matches a hand computation on the fixture.
  - AC-7 Isolation, access, erasure and versioning hold in every stage, during a
    transition and after a down-move. Verify by: SEC-T, SEC-A, SEC-E and SEC-V rerun
    with the pool at stage 0, 1 and 2, with an erasure at each phase, and on a
    demoted pool.
  - AC-8 Rollover loses nothing and erases everything. Verify by: the rollover tests
    and the verifier's positive control on every physical index.
- Negative cases:
  - NEG-1 (AC-1) A pool that never left stage 0 holds a queue record, node, member,
    edge, note or cache document: red.
  - NEG-2 (AC-2) A single noisy evaluation, a burst from one principal, an outage, or
    a bulk import below `N_BACK` with no recalls moves a pool: red.
  - NEG-3 (AC-3) A transition at or below `N_FLOOR`, one past `B_TRANS`, or a second
    attempt before `cool_until` and growth: red.
  - NEG-4 (AC-4) A flip, rollback or down-move changes what a task pinned before it
    sees, a re-promotion shows a member or edge from an earlier residency, or a
    write-path job commits after the `R` stamp: red.
  - NEG-5 (AC-5) The random-router tree survives probation in more than 5% of runs;
    a void, short or single-principal probation keeps a tree; or one principal's
    root tokens count as several principals: red.
  - NEG-6 (AC-6) A flat-answered recall counted as a tree miss: red.
  - NEG-7 (AC-7) A tombstone that intersects the candidate does not abort it; a
    demoted pool's canary survives; or a threshold, count or stage of one tenant or
    pool is readable from another: red.
  - NEG-8 (AC-8) A physical index missing from the manifest, or a document whose
    `pool` differs from its index's pool: the verifier turns red.

## 11. Probes this adds

| Id | PENDING_REAL_TEST | Fallback |
|---|---|---|
| P18 | the split API is available on Basic and needs only a read-only block of the index | rollover only, with copy-and-swap for any primary-shard change |
| P19 | a search alias over several physical indices of one pool scores BM25 per physical index, and the cost of the multi-index search at the rollover counts expected | RRF across per-index lists (rank-only) |

P8 (atomic `_aliases`) is reused. P17 and P20 of revision 1 (the shared-tier copy
and filter) are withdrawn with the shared tier. Both run on the test cluster before
the tier code.

## 12. Limits this adds (for DESIGN 20)

| Limit | Why |
|---|---|
| Each pool costs its own shards | 1 at stage 0, 6 at stage 1, 8 at stage 2 in the default layout (9.4): about 767 stage 0 pools per data node with no replica, 377 with one; layout 2 (Q2 in POOLS 12) costs 4 at stage 0 (192 and 94); a tenant with many person pools needs more nodes, or per-person tenants (DESIGN 4.2) |
| Stage 0 pools share the class-level unscored indices | Log and stored-query rows of a stage 0 pool live in the class-level indices, read only by id or in filter context through the class-level reader with a mandatory pool term (2.3); the residual is that one pool's activity volume can affect another's background-job timing, never a score or a result |
| A tree serves probation units before any evidence that it helps | Structural adoption (5.1); during probation only unit recalls consult the tree and their list holds the flat top `ceil(K_POOL / 2)`; a void, short or one-principal probation rolls back; until then the tree spends model calls |
| A kept tree can displace flat items | After a kept probation each tree pool's list holds the flat top `D_FLOOR` (3) only; flat items below it can be pushed out by tree items (POOLS 5.1); the sampled interleave (`EPS_IL`) keeps measuring this and drives the down-move |
| Probation needs opens | With `open_share` outside the DESIGN 9 bounds probation is void and the tree is rolled back (`probation_void`); a pool whose readers never open items stays flat |
| Probation units pay the tree's latency | Up to `r_max` model calls in parallel waves per unit recall, not measured (5.4, PENDING_REAL_TEST); at most `N_PROB` recalls per probation |
| Tainted and private-set rows feed no signal | Stage is visible to every grantee and derived from the signals (DESIGN 12 step 1), so rows a grantee could not read never count; a pool used mostly from tainted conversations adapts on the rest only |
| The difficulty features are heuristics | Tuned on development data; a wrong "easy" costs recall, a wrong "hard" costs calls; the sampled escalation measures the error |
| Implicit supersedes and contradictions are detected only at stage 2 | Detection shares the typing call (DESIGN 7.7); an explicit `pool_update supersede` is honoured at every stage (POOLS 3.9) |
| A pool under constant erasure may never complete a transition | Each intersecting erasure aborts the candidate (5.6, DESIGN 7.6) |
| Adaptive state holds scalars derived from the pool | Counts and timestamps only; not content; recomputed from live data |
| A tree has upkeep costs | After adoption each batch rebuild calls the model per level-1 node with pending members (DESIGN 7.6) |
| Several colluding principals can still force a transition | One principal cannot (4.1 flood bounds: `U_PRINC`, `S_PRINC`, `E_PRINC`, `P_MIN`, leave-one-principal-out); `P_MIN` colluding principals can, and the cost they cause is the attempt bound of 5.4 and the backoff; a pool's only grantee can do both, at its own cost |
| After a failed attempt and a mass erasure, re-proposal waits for regrowth | The next attempt needs `RHO_RETRY` times the size at the last outcome, and `N_BACK` does not waive it; this keeps the spend bound of 5.4 |

## 13. Conflicts with DESIGN, and the edit that resolves each

| # | DESIGN says | Conflict | Resolution |
|---|---|---|---|
| 1 | 17: "New tenant, no tree yet: recall returns direct search only; digest says 'no index yet'" | here it is the normal first stage | rewrite the row as stage 0 (3.2) |
| 2 | 5.3: "An index family is one tree over the leaves of one class" | the tree is per pool | edit 5.3: per pool |
| 3 | 7.4 step 6, 7.5: enqueue and place every leaf | no queue record and no placement at stage 0 | add the stage condition and the `flat` tracker outcome (3.1, 5.2) |
| 4 | 7.7: link creation for each new leaf | only at stage 2 | add the stage condition |
| 5 | 8.1 step 3; 8.2 "Every top-level holder ... is asked in parallel"; AGREED (a) | the per-query path serialises and can skip the fan-out | DEVIATION proposal (6.1); off by default |
| 6 | 10 step 3 `tree_missed`; 12 steps 3 and 4; 11.1; 13.3 | they assume the tree was consulted | restrict to `tree_consulted` (6.3) |
| 7 | 13.3 gates reorganiser candidates by replay; first builds and 7.6 batch rebuilds were never gated | none: a transition is a first build (7.6 bulk build), so 13.3 does not apply to it; reorganisations of a tree pool stay replay-gated | none to DESIGN; adopting on structural checks plus probation instead of a replay is a disclosed cost and latency design decision (5.1, Q7), not a deviation |
| 8 | 5.6: generation 1 is the first generation; P advances with write-path jobs | stage 0 has none | generation 0 implicit; `wp` not advanced at stage 0, set to `W` at the flip; the `flat` outcome and gap rule (5.2) |
| 9 | 5.6: "The write-path watermark P (`_id = wp`, one per tenant in `lc-<t>-ctl` ...) ... one P serves every class" | the watermark and generations are per pool | `wp`, heads and manifests are per pool and live in the class control index `lc-<t>-<c>-ctl`, each `_id` carrying the pool, read by id (POOLS 1.2); `src_seq` stays one monotone counter per tenant, so sequences compare across pools; P of a pool counts only its own leaves |
| 10 | 6.4: shard budget `(9 x C + 3) x (1 + r)` per tenant | pools add an axis | `(3 + 5 x C + sum over pools of kinds present) x (1 + r)` in the default layout, `(3 + 4 x C + ...)` in layout 2 (9.4) |
| 11 | 5.7: log rows, stored queries, notes and cache entries live in the index of their join class | names now carry a pool | from a pool's first tree, a row whose join class is the pool's class lives in the pool's own index; at stage 0, and for a higher join class or a foreign private set, it lives in the class-level index of that class with `pool`, never scored (POOLS 1.2) |
| 12 | 7.3: subjects from declarations "and by the entity list from the build call" | stage 0 has no build call | subjects from declarations and the code extractor at every stage; the build call's list never the only source (3.1) |
| 13 | 13.2 visibility; 5.6 ancestry | a down-move must hide old write-path members and edges from later pins | the retirement number `R` stamped into `gen_to` and listed in every later ancestry (4.4) |
| 14 | 20 "Cost of the owner's routing: 25 to 77 model calls per recall ...; no design change can make agent-per-holder routing cheap" | adaptivity pays it only for pools at stage 1 or 2, and per query only with the switch on | edit the row; state the measured 67 at 6,000 leaves |
| 15 | 1: the owner's words and the 1.1 mapping | the new statements are not in it | add the quotes (section 14) |
| 16 | 4.1 POOLED tier | a pool is not the POOLED tier | state that the two are different |

DESIGN section 1 is not contradicted: the owner's tree, fan-out, cross-links,
reorganisation and versioning are all kept, and a pool that reaches stage 2 has all
of them.

## 14. Edits to make in DESIGN / BUILD-PLAN / EXPERIMENT when this is adopted

Section numbers are those of commit 6002839.

DESIGN.md
- 1: add the two quotes of section 1 after the existing quotes, and a sentence that
  adaptivity is an OWNER requirement. 1.1: rows "starts as a fast easy normal rag
  and auto adapts" to ADAPTIVE 2 to 6, and "memory pools ... shard/scale" to
  ADAPTIVE 2.3 and 9.
- 2: INV-4: `ad:<pool>` and manifests are scalars and ids only. INV-1: stage, size
  and rates are never in `lc-sys-registry`. INV-11: a failed feature is counted,
  not read as easy or hard.
- 2A: a line under each concern pointing to ADAPTIVE 7.
- 4.1: the POOLED tier is not a pool.
- 5.3: per pool. 5.6: generation 0, `wp` per pool, `hd:learn`, `ad:<pool>`, the
  retirement number `R`, the manifest of physical indices, all in the class
  control index (POOLS 1.2). 5.7 and 9: the new log fields of 6.3 (`direct_rank`
  and `il_team` included), `esc_reason = probation` and `via = probe`; stage 0
  log and stored-query rows in the class-level indices, read through the
  class-level reader (POOLS 1.2).
- 6.2, 6.4: pool names, lazily created kinds, the shard formula of 9.4.
- 7.3: subjects at every stage (conflict 12). 7.4 step 6, 7.5, 7.6, 7.7: stage
  conditions, the `flat` tracker outcome, the gap rebuild placing leaves without a
  member or job, the bulk-build transition (3.1, 5.2). 8.1 to 8.5: stage 0 path,
  version filter, per-query path behind its switch, stage line, `gen = 0`.
- 10, 11.1, 12: the `tree_consulted` exclusions (6.3).
- 13.2: the retirement number in later ancestries (conflict 13).
- 14.2, 14.3, 14.7: closure over every physical index of a pool, zero-count
  assertions at stage 0, demoted pools in the verifier (7.1, 7.3, 9.3). 15:
  transition jobs, lease heartbeat, budgets (5.4, 5.6).
- 17: rewrite the "New tenant, no tree yet" row; add rows for a transition at a tiny
  pool, erasure mid-transition, a bulk import with no recalls, an outage during
  evaluation, a pool shrunk below threshold, a probation rollback (loss, short,
  void, one principal), a write-path job running across a down-move (the fence of
  4.4).
- 18.1: the budget handle gets a per-transition cap and the `budget_exceeded`
  outcome.
- 19: add `adaptive/` and `recall/route.py`.
- 20: the rows of section 12 and the cost-of-routing edit (conflict 14).
- 22: the per-query path as a DEVIATION candidate; every PLACEHOLDER of this file in
  the cost-relevant ASSUMPTIONS list.

BUILD-PLAN.md
- Step 6: no queue record at stage 0; the `flat` tracker outcome. Step 9: the bulk
  build is also the transition, with structural checks. Step 10: stage 0 digest,
  flat path, version filter. Step 11: the new log fields. Step 13: gap rule, `wp`
  per pool, retirement number. Step 22: S0 runs pinned stages for its recall gates
  and the adaptive scenarios of Step 24; the verifier's per-stage probe counts.
- Add Step 24 (section 10) after Step 19 in build order; add P18 and P19 to
  section 2.
- 3A: the per-query path's tuned thresholds and the 1-to-2 transition after H1,
  behind the same interfaces; Step 24's stage 0, stage 1 and pins are needed before
  any arm runs, so arms can pin.

EXPERIMENT.md (committed alone before the run it affects, with the reason)
- 4: arms pin their stage, `per_query = off`, no probation; arms ADAPT, FLAT-ONLY,
  TREE-ONLY as proposals for the stage of TUNING 5.
- 1: HA (proposal) with its measures (TUNING 6). 2.2: HA outcome words. 3.7:
  evaluation questions are `via = probe` and never enter stored queries or signals,
  before or after they are asked. 6: control for ADAPT (a pool that never leaves
  stage 0 equals A0).
- 10: a proposed stage for TUNING's paid stages with their counts; 12: the open
  questions below.

## 15. Open questions for the owner (defaults in force until answered)

| Id | Question | Default in force |
|---|---|---|
| Q6 | May a recall of a tree pool be answered from the flat path alone when the query looks easy (a deviation from "asks all context index shard holders")? | built behind a switch, off until TUNING D4 passes, then on with 5% sampled escalation |
| Q7 | How is a tree adopted, and is its spend acceptable? (a) Default: adopt on structural checks, keep or roll back by probation (5.1, 5.3); probation counts inside the attempt bound, capped at `B_PROB`. Per attempt `3 x build(N, 2) + B_PROB`: 3,598, 6,000 and 10,651 model calls at 2,000, 6,000 and 20,000 leaves; over all attempts `9 x build(N, 2) + A(N) x 3,600`: 20,394, 32,400 and 60,003. Up to `N_PROB` (50) reader recalls pay the tree's latency (up to `r_max` calls in parallel waves, not measured) before the keep decision. (b) Alternative: gate adoption on one K = 8 replay of stored queries (the smallest gate DESIGN 13.3 allows: 8 x 67 + 32 = 568 calls at the measured 67 per recall; 480, 608 and 456 at the caps): per attempt 1,278, 3,008 and 8,457; over all attempts 5,434, 11,456 and 30,083; no reader sees a tree before the replay, but the replay needs stored queries in 8 clusters and a judge model, measures neither real readers nor their opens, and has low power at K = 8 (EXPERIMENT 7.3); with no probation, a bad tree that passes the replay is removed only by the down-move's sampled interleave (4.4). After either, every full-path recall of a kept tree pays up to `r_max` (about 67 measured at 6,000 leaves). All within the tenant's hourly budget, with no absolute cap. | (a), automatic, with a typed event per transition; the owner may pick (b) or set an absolute per-pool cap |

POOLS 12 (Q1 to Q4) and TUNING 7 (Q8, Q9) hold the other owner questions.

## 16. Index of ASSUMPTIONS and PLACEHOLDERS introduced here

Each is in TUNING's table (D1 to D8 here; D9 to D13 in POOLS). ASSUMPTIONS: three
stages and the rung grouping; leaf cut kept at stage 0; stage derived from heads;
every pool dedicated, no shared tier; `ad:<pool>` as a scalar control document; no
replay gate on transitions; structural adoption with probation; probation units
only consult the tree, interleaved with the flat list; roll the tree back when
probation is void, short or decided by one principal; the per-principal caps; the
down-move fence; the gap through the write-path tracker; the retirement number;
down-moves; backoff by outcomes; lower priority for transition calls; per-query
path and its sampled escalation; lazy creation of kinds; layout 1 with stage 0's
unscored rows in class-level indices.

PLACEHOLDERS (no measurement): [D2] `N_UP1` 2,000 leaves, `T_UP1` 2.5 million
tokens, `S_UP1` 300 sessions, `E_UP1` 2,000 entities, `N_BACK` 50,000 leaves
(absolute, not tuned); [D3] `W_UNITS` 200, `R_MIN` 50, `H_UP1` 0.20, `TAU_RE` 0.85,
`U_SESS` 5, `U_PRINC` 10 (so `U_PRINC / W_UNITS` = 0.05), `F_FAIL_MAX` 0.10;
[D2] `S_PRINC` 75 sessions, `E_PRINC` 500 entities; [D4] `A_MIN` 0.4, `G_MIN` 0.1,
`S_MAX` 6, `EPS_ESC` 0.05; [D5] `E_CONSEC` 3, `EVAL_PERIOD` 1 hour, `A_EVERY` 500
writes, `D_DWELL` 24 hours, `D_DWELL1` 24 hours, `COOL_BASE` 6 hours, `COOL_MAX` 7
days, `RHO_DOWN` 0.5, `RHO_RETRY` 1.5, `E_ABORT_MAX` 3, `T_PAR` 1, `LEASE_BEAT` 60
s, `J_WAIT` 60 s; [D6] `R2_MIN` 200, `H_UP2` 0.15, `M_UP2` 0.25; [D7] `N_PROB` 50
units, `G_KEEP` 0.05, `P_MIN` 3 principals, `D_PROB` 14 days, `EPS_IL` 0.05; [D8]
`B_SHARD_TARGET` 10 GiB, `AGE_ROLL` 30 days. Derived, not tuned: `N_FLOOR = F_MAX
x F_TARGET`, `build(N, m)`, `B_PROB = N_PROB x r_max`, `B_TRANS = 3 x build(N,
M_MAX) + B_PROB`, `A(N) = floor(log_1.5(N / N_FLOOR)) + 1`, `r_max = T + 48`.
