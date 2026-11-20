# living-context: adaptive mode (amendment to DESIGN, BUILD-PLAN, EXPERIMENT)

Status: design amendment, not yet adopted. Nothing here has been built or run.
Section numbers of DESIGN.md, BUILD-PLAN.md and EXPERIMENT.md refer to the
branch head this was drafted against (commit 6002839), because those files
are still moving; section 14 lists the edits to make when this is adopted.
This file does not edit them, and it changes nothing the registered
experiment does (section 8). Companion files: [POOLS.md](POOLS.md) (what a
pool is, access, tools, selection, injection) and [TUNING.md](TUNING.md)
(how every default below is set; each default here carries its TUNING id,
D1 to D13).

Labels as in [DESIGN.md](DESIGN.md) section 0 (OWNER, AGREED, SOURCE,
ASSUMPTION, DEVIATION, PENDING_REAL_TEST). A number marked PLACEHOLDER is an ASSUMPTION with no
measurement behind it; it exists so the mechanism can be built and tested,
and TUNING says how it is replaced.

## 1. The requirement

The owner's words, verbatim (2026-11-20):

> "remember to make it adaptive -- it doesnt instantly start segmenting small
> memory, it starts as a fast easy normal rag and auto adapts as context and
> memory size/complexity demands"

and, on what the unit is:

> "memory pools are what shard/scale as their data grows.. and agents use
> tools to update/save to them"

Reading (what is OWNER and what is proposal):

- OWNER: a new memory starts as plain retrieval ("a fast easy normal rag"):
  no segmenting into topic indexes, no summaries, no model calls to store or
  to read. It adapts upward on "context and memory size/complexity", and
  the unit that grows and scales is the memory pool. Both are the owner's
  decision, not assumptions.
- OWNER, earlier (DESIGN 1): "when the index itself gets large, segment it
  and index it" is already adaptive in depth (DESIGN 5.4: depth is an
  outcome). This amendment adds the missing first step: whether there is an
  index at all.
- ASSUMPTION: "segmenting" in the owner's sentence means topic segmentation
  into index nodes (the tree). Cutting a session into leaves at `LEAF_CAP`
  (DESIGN 7.2) uses no model call, is what ordinary retrieval does, and stays
  in every stage (open question Q1).
- Everything else below (stages, triggers, transition, per-query choice,
  physical tiers) is a design proposal; each choice is labelled.

## 2. Unit and stages

### 2.1 The unit is the memory pool

A pool is a named memory collection with exactly one access class (POOLS 1).
Each pool has its own stage, its own physical tier, its own signals and its
own transitions. Tenants and classes are not units of adaptation; a tenant
with ten pools can have ten different stages. Consequences, all consistent
with DESIGN:

- Signals of a pool are computed only from that pool's own documents and log
  rows (INV-2, INV-3). No pool's signal reads another pool, another class or
  another tenant, so there is nothing to leak through them (section 7).
- A tree is per pool. This replaces "one tree over the leaves of one class"
  (DESIGN 5.3); the class lattice still decides what a principal may read
  (POOLS 1, 2.4).
- A session querying several pools gets, per pool, the path that pool's stage
  provides, merged by rank (POOLS 5). A flat pool and a tree pool answer one
  recall together.

### 2.2 Retrieval stages

| Stage | Name | What exists in the pool | Write cost | Read cost |
|---|---|---|---|---|
| 0 | FLAT | source log, leaves (text, vector, entities from code, code-built title), leaf heads, protocol log, stored queries. No nodes, members, summaries, edges, notes, cache | 0 model calls (embedding runs on the embedding port, not the model port) | 0 model calls: direct search (DESIGN 8.5), the same code as arm A0 |
| 1 | TREE | stage 0 plus the topic family: level-1 nodes (gists, summaries, facts), upper levels by the index-of-indexes rule (DESIGN 5.4), placement members, batch rebuilds, fan-out routing (DESIGN 8.2 to 8.4) | placement is model-free; a batch rebuild costs one build call per changed node (DESIGN 7.6) | direct search plus, for a recall that takes the full path, the owner's fan-out (DESIGN 8.2 to 8.3) |
| 2 | LEARNING | stage 1 plus typed edges and bounded expansion (DESIGN 7.7, 8.6, 16), reflection, the reverse audit, the reorganiser and its gate (DESIGN 10 to 13) | typing: one call per 20 pairs, 3 pairs per leaf (DESIGN 7.7); loop calls under their existing caps | stage 1 plus edge expansion (model-free) |

Why three and not more:

- Depth is already an outcome of the index-of-indexes rule (DESIGN 5.4), so
  "first level" and "upper levels" are one stage. A stage per level would
  duplicate a rule that exists.
- Stage 2 groups the layers that are only useful on top of a tree: typing
  calls read titles, gists and facts (DESIGN 7.7), which exist only after a
  level-1 build; `tree_missed` needs a tree (DESIGN 10); the reorganiser
  edits one. ASSUMPTION: edges, reflection, audit and reorganiser share one
  rung. They are separate DESIGN layers; if measurement shows a pool that
  needs edges without the loop, the rung splits, and nothing else changes.
- The entity and time families (DESIGN 5.3) are model-free. ASSUMPTION: they
  are built after the topic head exists, by their own ordinary builds, with
  no trigger of their own; the stage is defined by the topic head only.

Stage is derived, not stored in a second field: stage 0 = no live topic
generation; stage 1 = topic head `hd:topic` names a live generation; stage 2 =
the pool's control index also holds the learning head `hd:learn`. The flip that
defines a stage is one compare-and-set of one document (`hd:topic` for 0 to 1,
creation of `hd:learn` for 1 to 2). A commit writes other documents first (`wp`;
for 1 to 2 the generation head), and each of those is harmless to every reader if
the process stops before the defining flip, because stage 0 never reads `wp` and
stage 1 never reads edges; on restart the evaluator finds the adopted candidate
and completes the defining flip, which is idempotent. So no reader ever sees two
documents disagree about the stage. Generation number 0 is the implicit empty generation
of every pool: a task pinned at stage 0 pins `gen = 0`, leaf pointers read
`(0, leaf_id, lv)`, and no node pointer exists at generation 0.

### 2.3 Physical tiers (the owner's "shard/scale as their data grows")

The retrieval stage says what a pool computes. The physical tier says where
its documents live. They are separate ladders with separate triggers (section
4.5) because a pool can be large and easy, or small and hard.

| Tier | Where the pool's documents live | Index cost |
|---|---|---|
| S0 SHARED | in the tenant's shared small-pool indices of the pool's class, tagged with the `pool` field and routed by pool id | none per pool; one shared family per (tenant, class) |
| S1 DEDICATED | in the pool's own index family `lc-<t>-<c>-<p>-<kind>` (POOLS 1.2), one primary shard per index | up to 9 shards per pool (DESIGN 6.4) |
| S2 SHARDED | S1 with more than one physical index per kind behind the same aliases: new writes roll over to a new physical index when a size or age bound is reached; reads search all of them | one more index per kind per rollover |

Trade-off, index-per-pool versus shared index with routing (SOURCE for the
shard cost: DESIGN 6.4, the vendor shard-size guidance cited in DESIGN 5.6):

- Index per pool gives the strongest isolation and the simplest erasure (the
  pool is its own indices, BM25 statistics never include another pool's
  documents) but costs `9 x (1 + r)` shards per pool; with the per-node limit
  of DESIGN 6.4 that is on the order of one hundred pools per node with no
  replicas. A scratch pool per session, or a pool per person, cannot be
  built this way.
- A shared index with `pool` filter and routing is nearly free per pool but
  shares term statistics between its pools (the same leak DESIGN 4.1 states
  for the POOLED tenancy tier, SOURCE: vendor DLS page) and puts erasure of a
  pool inside a larger index.
- Recommendation (ASSUMPTION; open question Q5): a pool starts SHARED and
  graduates to DEDICATED at a size trigger, so cost is paid only by pools
  that grow, which is the owner's sentence applied to storage. Sharing stays
  inside one tenant and one class: it never crosses tenants (INV-1) or classes
  (the class read key still bounds the index, DESIGN 6.3). The isolation
  consequence is labelled wherever the shared tier is used: pools of the same
  tenant and class share term statistics while small; a session sees no
  document, title or count of a pool it may not read, but its own result
  scores can shift slightly with another pool's vocabulary. A tenant that
  cannot accept this sets `pool_isolation = dedicated`, which skips tier S0
  (every pool starts at S1) at the shard cost above.
- This is a different thing from the POOLED tenancy tier of DESIGN 4.1
  (shared indices across tenants, not built). "Pool" in this file is the
  memory collection of POOLS.md; the shared small-pool index is within one
  tenant and one class.

## 3. What each stage does on write and read

### 3.1 Write path

| Step (DESIGN) | Stage 0 | Stage 1 | Stage 2 |
|---|---|---|---|
| 7.1 admission, 7.2 segmentation (cap, session cuts), 7.3 subjects and class | unchanged | unchanged | unchanged |
| 7.4 steps 1 to 5 (sequence, key, source, embed, leaf, head) | unchanged | unchanged | unchanged |
| 7.4 step 6, enqueue for placement and linking | dirty-queue document written, never consumed | consumed: placement (7.5) | placement and link creation (7.7) |
| 7.5 placement | not run | run | run |
| 7.6 batch rebuild | not run | run when members are pending | run |
| 7.7 link creation | not run | not run | run; supersedes and contradicts detection exists only from here |
| write-path watermark P | not advanced (nothing reads it at stage 0: no member or edge exists) | set by the transition, then advanced as in DESIGN 5.6 | as stage 1 |

Step 6 is kept at stage 0 on purpose: every leaf then has a queue record
whatever the stage was when it was stored, so a transition never has to
discover leaves that were stored without one (section 5.2). The record is an
id and a sequence number; it costs one small control write per leaf.

### 3.2 Read path (a single pool)

| Stage | Recall of one pool |
|---|---|
| 0 | direct search only (DESIGN 8.5): BM25 over `text`, `title`, `entities`, kNN over `embedding`, client-side RRF (k 60, window 50). Digest: pointers with pointer, title, size, reason `direct`, confidence null; `gen = 0`; no outline, no organise call. `explore` re-runs direct search with the delegate's question and an exclusion set (no seeds can exist) |
| 1 | flat path or full path, chosen per query (section 6) |
| 2 | as stage 1, plus bounded link expansion on both paths (DESIGN 8.6; it costs no model call) |

The digest reports, for each pool the session may read, its stage (`flat`,
`tree`, `learning`). It reports nothing about a pool the session may not read.
ASSUMPTION; open question Q3.

## 4. Triggers

The owner's words are "context and memory size/complexity". Size and
complexity of the memory are measured from the pool's own documents; the
demand the memory actually makes is measured from how retrieval is going.
Every signal below is computed without a model call.

### 4.1 Signals

All memory signals are scalars computed on demand by the gateway from index
statistics and aggregations over the pool's own documents (the gateway already
reads its tenants' statistics with index-level `monitor`, DESIGN 6.3). Nothing
derived from leaf content (no centroid, no embedding statistic, no entity
list) is stored, so nothing needs lineage or joins the erasure closure; a
stored statistic would (INV-4, 14.2).

| Signal | Computation | Reads |
|---|---|---|
| `leaf_count` | live current leaf versions (heads with status live; archived leaves count, they are still searched) | leaf heads |
| `token_total` | sum of `size_tokens` over the same leaves | leaf index |
| `session_count` | distinct `session_ref` over the same leaves (cardinality aggregation, approximate) | leaf index |
| `entity_count` | distinct `entities` over the same leaves (cardinality aggregation, approximate) | leaf index |
| `hard(task)` | true if any recall of the task was not flat-sufficient (section 6.2: low agreement, small margin, or wide session spread) or was followed in the same task, by the same actor, by a reformulation (a later recall whose query vector has cosine at least `TAU_RE` to the earlier one) | protocol log rows of the pool |
| `hard_rate` | share of the last `W_TASKS` distinct tasks of the pool with `hard(task)`, defined only when at least `R_MIN` tasks exist. A task, not a recall, is the unit, so one agent hammering one question counts once | protocol log |
| `gain_rate` (stage 1 and above) | share of full-path recalls in the last `W_TASKS` tasks where an item offered only by the tree (absent from the flat top 10) was opened. `opened` is gateway-logged (DESIGN 9) | protocol log |
| `multi_rate` | share of tasks that opened leaves of two or more distinct sessions | protocol log |
| `open_share` | mean share of offered pointers opened per recall (DESIGN 9) | protocol log |

Rules that keep these inside the existing design:

- No signal treats "offered and not opened" as evidence of irrelevance or of
  difficulty (DESIGN 9, 11.1: a descriptor can convey an answer). `hard`
  uses the shape of the flat result and the reformulation of the question;
  `gain_rate` and `multi_rate` use opens only as positive evidence.
- A failed feature computation (embedding unavailable, a typed search error)
  makes the recall `hard`, never "easy" (INV-11).
- A pool's evaluator reads only that pool's log rows, which are in the join
  class of query and item (DESIGN 5.7); a pool never sees a higher-class
  query (DESIGN 12 step 1). The consequence, stated: a lower-class pool's
  `hard_rate` sees only the queries made in channels of its own class.
- The per-recall features (`agree`, `margin`, `spread`, section 6.2) are scalars
  written into the pool's log rows, which already carry lineage, join class
  and query subject tokens, so erasure reaches them.
- The state the evaluator keeps between evaluations is a control document of
  the pool, `ad:<pool>`, in the pool's control index (not the tenant-wide
  `lc-<t>-ctl`, which every class key can read, DESIGN 20). It holds scalars
  only: per trigger a consecutive-evaluation count, `cool_until`, an abort
  count, and `leaf_count_at_reject`. It holds no text, vector or id list, like
  the write-path watermark `wp` (DESIGN 5.6), so it needs no lineage; a test
  asserts that shape. The values of the signals themselves are not stored; they
  are recomputed at each evaluation.

### 4.2 When it runs

An evaluator job per pool runs after `A_EVERY` committed writes or every
`EVAL_PERIOD`, whichever comes first, as the pool's service principal,
through the gateway, in the pool's background job slot (DESIGN 15). Evaluating
costs a few aggregation queries and no model call. A pool with no writes and
no recalls is not evaluated.

### 4.3 Up-moves

Stage 0 to 1 (propose a tree), all of:

1. `leaf_count >= N_FLOOR`.
2. Memory gate: `leaf_count >= N_UP1` or `token_total >= T_UP1` or
   `session_count >= S_UP1` or `entity_count >= E_UP1`.
3. Difficulty gate: `hard_rate >= H_UP1` (defined, so at least `R_MIN` tasks).
4. Conditions 1 to 3 hold at `E_CONSEC` consecutive evaluations at least
   `EVAL_PERIOD` apart.
5. No transition of this pool in flight, `now >= cool_until`, and at least
   `D_DWELL` since the pool's last adopted move.

Backstop: if `leaf_count >= N_BACK` conditions 2 and 3 are waived (a pool never
stays flat forever because the difficulty features are poor predictors).
Conditions 1, 4 and 5 still apply.

`N_FLOOR = 8 x F_TARGET` leaves. This one is derived, not tuned (D1): the
adoption gate needs at least 8 clusters (DESIGN 13.3), one cluster per
level-1 node in a first build (section 5.4), and a level-1 node holds
`F_TARGET` children (DESIGN 5.4). Below it a tree cannot pass its own gate,
and DESIGN 5.4 drops a one-node top level anyway. Its reading (cluster equals
level-1 node) is an ASSUMPTION.

Stage 1 to 2 (propose the learning layer), all of:

1. The pool has been at stage 1 for at least `D_DWELL1`.
2. Loop-ready: at least `R2_MIN` tasks in the window with `open_share` inside
   the opened-share bounds of DESIGN 9 (0.05 to 0.60). Outside the bounds the
   opened signal is void for the loop (DESIGN 9), so the layer would learn from
   nothing.
3. Persistent demand: `hard_rate` among tasks whose recalls took the full path
   is at least `H_UP2`, or `multi_rate >= M_UP2` (evidence spread over several
   sessions is what edges are for).
4. Conditions 2 and 3 hold at `E_CONSEC` consecutive evaluations; no transition
   in flight; `now >= cool_until`; dwell as above.

### 4.4 Hysteresis and down-moves

- Entry and exit thresholds differ: each up threshold has a down threshold at
  `RHO_DOWN` times it (PLACEHOLDER 0.5), so a pool near the line does not flap.
- A move needs the condition at `E_CONSEC` consecutive evaluations spaced at
  least `EVAL_PERIOD` apart; any evaluation that fails the condition resets the
  count.
- After any adopted move, no further move for `D_DWELL`.
- After a rejected, aborted or rolled-back candidate: `cool_until = now +
  COOL_BASE x 2^(aborts)` up to `COOL_MAX`, and a new proposal also needs
  `leaf_count >= RHO_RETRY x leaf_count_at_reject`. A pool whose tree did not
  pay off is not rebuilt until it has grown.
- At most one transition per pool in flight (the build lease of DESIGN 7.6),
  and at most `T_PAR` transitions per tenant at once (ASSUMPTION, 1), so a
  tenant's own growth cannot take the subscription's two model slots (DESIGN
  15).

Down-moves. Memory shrinks through erasure, archiving and re-filing, so a pool
can fall below its thresholds. ASSUMPTION (open question Q4): it moves down one
rung at a time when the memory gate is below its down thresholds, `hard_rate`
is below `RHO_DOWN x H_UP1` (stage 1) and `gain_rate < G_KEEP`, all at
`E_CONSEC` consecutive evaluations, after the dwell. A down-move is the
reverse flip and costs no model call:

- 2 to 1: the learning head `hd:learn` is retired by compare-and-set. Spending
  stops (no typing, reflection, audit, new candidates) and edge expansion stops.
  Existing edges, notes and generations stay until their normal retention or an
  erasure; they are not read while the pool is at stage 1.
- 1 to 0: `hd:topic` is set to generation 0 by compare-and-set. The tree
  generation becomes an ordinary retired generation, kept for `R_GEN` so pinned
  tasks and rollback still resolve (DESIGN 13.2), then its unshared documents
  are deleted. Pinned tasks keep their generation for their life; new tasks
  are flat. Placement and rebuilds stop. Because the dirty queue is kept at
  stage 0 (3.1), a later re-promotion finds every leaf.
- Erasure closure keeps covering the retired generation while it is retained
  (DESIGN 14.2, 14.4). Nothing is ever deleted early because a pool went down.
- Re-promotion is a fresh build in this version. Re-using a still-retained
  retired generation as the parent (so only the delta is rebuilt) is a cost
  optimisation that is not specified here.

Physical tiers only move up. A pool is not demoted from S1 or S2 to a shared
index automatically; merging indices back is an offline operator action.

### 4.5 Physical triggers

All computed from the pool's own index statistics, no model call, same
hysteresis (consecutive evaluations, dwell; no down-move).

| Move | Trigger | Default |
|---|---|---|
| S0 to S1 | the pool's stored bytes in the shared indices `>= B_SHARED_MAX` or its document count `>= DOC_SHARED_MAX` | PLACEHOLDER (D8) |
| S1 to S2 (rollover) | the current physical index of a kind `>= B_SHARD_TARGET` bytes or older than `AGE_ROLL` | PLACEHOLDER (D8); vendor shard-size guidance as the starting range |

### 4.6 Forced stage (experiments and operators)

`adaptive.mode` is `auto` (the product default) or `pinned:<stage>` for a pool
or a prefix of pool ids (the pin sets the stage floor and disables the evaluator; it does not turn layers on or off), set only in deployment configuration by the operator
(never by a tenant session: a session request that names it is refused with the
typed error of DESIGN 3). The pin is written into the manifests of the
generations built under it, so a replay can tell a pinned run from an adaptive
one. EXPERIMENT arms use `pinned:1` or `pinned:2` from the first write (section 8).

## 5. The transition

A transition is a candidate generation of the pool's topic family, built in the
background from existing leaves while the current stage keeps serving, then
adopted by compare-and-set of the head. It reuses, and does not replace,
versioning (DESIGN 5.6, 13.1, 13.2), the batch builder with its lease (7.6),
the model cache (5.8), the replay gate (13.3) and the tombstone guard (INV-8).

### 5.1 Which path it is

DESIGN 7.6 batch rebuilds flip the head without the replay gate; only a
reorganiser candidate is gated (13.3). A transition is the 7.6 builder run as a
bulk build (BUILD-PLAN Step 9: balanced clustering to `F_TARGET`, level-1 build
calls, upper levels, index-of-indexes rule) plus the 13.3 gate, because a
transition changes the cost and behaviour of every later recall of the pool, not
just the quality of one node. ASSUMPTION: gating transitions is a proposal
beyond DESIGN 7.6; later ordinary rebuilds of an adopted tree are not gated.

### 5.2 Steps, 0 to 1

1. Propose: the evaluator (4.3) writes a candidate manifest, status building,
   with `stage_target = 1`, the fired trigger names (not their values), and the
   pin state; takes the build lease; reads the highest committed searchable
   source sequence `S0` and sets the candidate's `W = S0`. (The pool's `wp` is
   not used as W at stage 0: DESIGN 7.6 step 1 reads P, which stage 0 does not
   advance.)
2. Build: bulk build over every live leaf with `src_seq <= W`. Members carry
   `gen_from = G+1` (rebuild style, so they are invisible until the flip). Build
   calls go through the model port, the pool's tenant fair queue and the cache.
   Nothing the build writes is visible to any reader.
3. Replay gate (section 5.4).
4. Commit under the shared side of the tenant's write gate, in one block: read
   the published tombstone sequence; if any tombstone newer than the one the
   build started with intersects the candidate's lineage, abort (DESIGN 7.6
   step 6); refresh the indices written (searchable publication, DESIGN 5.6);
   compare-and-set `wp` to `W`; compare-and-set `hd:topic` from generation 0 to
   `G+1`. After the flip the pool is at stage 1.
5. The gap. Right after the head flip the committer reads the highest allocated
   source sequence `S_gap` and records it, with `W`, in the generation's
   manifest (`gap = (W, S_gap]`). Leaves in the gap have queue records but no
   member. The placement worker (DESIGN 7.5) consumes only records above
   `S_gap`; it must not consume gap records, because `wp = W` plus a member
   written later with a `src_seq` at or below P would make that member appear
   under a task pinned earlier, breaking DESIGN 13.2's stable view. Gap leaves are
   placed by the first batch rebuild after the flip, which places leaves that
   have no member (DESIGN 7.6 today handles only pending members, so this is an
   edit): members bound by `gen_from`, atomically visible at its flip. Until then
   direct search reaches them, as it reaches any leaf not yet placed. A leaf
   stored after the flip reads the head at step 6 of 7.4, sees stage 1, and gets
   a normal write-path placement job; a leaf allocated before the flip but whose
   step 6 ran after it is in the gap and also queued, so it is placed at most
   twice, harmlessly (members are unique per (parent, child)).
6. Entity and time families: built afterwards by their own builds (2.2).

Transition 1 to 2 is the same shape: the candidate is a new generation of the
same topic family that shares every unchanged node and carries the backfilled
typed edges with `gen_from = G+1`; its commit flips the generation head and then
creates `hd:learn` (the defining flip). One rule for every leaf: the backfill
covers leaves up to its snapshot `W_link`; no write-path link job runs before the
flip, so no typing call and no `src_seq`-bound edge exists outside the
candidate (a rejection therefore leaves nothing behind, and every typing call is
inside `B_TRANS`); leaves stored after `W_link` and before the flip are covered by
the first generation after the flip (edges bound by `gen_from`), never by a late
write-path edge; leaves stored after the flip read stage 2 at step 6 of 7.4 and get
write-path link jobs under P. Until the flip, edges are invisible (13.2).

### 5.3 Cost bound

All figures are formulas over DESIGN's own parameters; nothing here is a
measurement. `N` = live leaves of the pool at `W`; `T` = top-level holders of the
candidate; `r_max = T + 48` = the most routing and organise calls one recall can
make (`B_HOLDERS` plus 12 organise calls, DESIGN 8.2, with `B_HOLDERS = T + 36`).

| Part | Calls, upper bound |
|---|---|
| Build, 0 to 1 | one call per node, summed over levels: at most `sum over l of ceil(N / F_TARGET^l)` which is under `N / (F_TARGET - 1) + L`, `L` the number of levels. Failed calls count (DESIGN 18.1); retries within the transition are capped at the same figure again, so `B_BUILD = 2 x (N / (F_TARGET - 1) + L)` |
| Gate generation and judging | at most 12 + 60 (EXPERIMENT 10.1 figures) |
| Gate replay traversal of the candidate | `Q_REPLAY x r_max`, `Q_REPLAY` at most 80 (60 targeted + 20 general, DESIGN 13.3). The current stage 0 arm costs no calls, so the cold-current-traversal term of EXPERIMENT 10.1 does not arise |
| `B_TRANS` | `B_BUILD + 72 + Q_REPLAY x r_max` |
| Backfill, 1 to 2 | typing at most `ceil(3N / 20)` (3 pairs per leaf, 20 per call, DESIGN 7.7), plus 12 + 60 + `Q_REPLAY x r_max` for the gate, where the stage 1 and the candidate arms share one traversal because expansion adds no routing calls (EXPERIMENT 4, arm A2) |

The replay term dominates the build term whenever `Q_REPLAY x r_max` exceeds
`N / (F_TARGET - 1)`. That is the honest cost of reusing the gate, and it is a
reason `Q_REPLAY`, the minimum effect `m_TRANS` and the cooldown are tuned
(TUNING D7). The spend is enforced, not hoped for:

- The budget counter (DESIGN 18.1) is given `B_TRANS` for the transition; a call
  past it is refused and aborts the candidate.
- Per-tenant hourly model budget and fair queue apply (DESIGN 15). Hitting the
  hourly budget pauses the builder (like a provider throttle, DESIGN 18.1);
  it does not abort. A transition therefore takes as long as the budget allows
  and never exceeds it.
- Transition calls take the lower priority within the tenant's own queue: a
  recall is never held behind a build (ASSUMPTION).
- Cache: build calls are prompt-free and cached per pool (DESIGN 5.8), so a
  retry after an abort re-pays only the nodes whose inputs changed. The saving
  is not claimed: it depends on the clustering being stable when a leaf leaves,
  which is measured (TUNING).

### 5.4 The gate for a first tree (what replaces each part of 13.3)

DESIGN 13.3 gates a delta of a tree against the tree already served. A first
tree has no tree to compare with. Replacements, each an ASSUMPTION:

| 13.3 part | For a transition |
|---|---|
| Primary measure: tree-source recall of judged-relevant items in the top 10 | the fused final-list judged recall@10 of the candidate (tree, direct, and at 1 to 2 linked, merged by DESIGN 8.4) against the current stage's list. The current stage 0 has no tree source, so tree-source recall would let any candidate win trivially |
| Cluster = one per reorganiser operation | one cluster per level-1 node of the candidate (its children's membership and entry are the change); queries sharing a node merge into one cluster, as in 13.3. At 1 to 2: one cluster per leaf's edge set, merged the same way |
| Replay set: held-out logged searches that changed leaves' judged-relevant items | the same held-out rule over the pool's own logged searches, with judged-relevant items taken from the blind judge at replay (13.3 step 3 already judges unjudged items at replay; at stage 0 nothing was judged before), topped up to 60 with gate-only synthetic queries from leaves, judge-filtered and tagged. If fewer than `Q_REAL_MIN` real held-out searches exist the gate may run on synthetic queries only and the transition record says `synthetic_gated`; the same test then decides |
| Decision | unchanged: K >= 8, p < 0.05, general-replay guard, no loss in the rarely-needed stratum, zero class or tenant regressions |
| Added guard | the mean paired difference of fused recall@10 over the targeted set is at least `m_TRANS`. The sign test counts clusters, not size (DESIGN 20), and a tree costs calls on every recall that takes the full path; ASSUMPTION: reuse EXPERIMENT 7.2's minimum +0.05 |

Gate-only synthetic queries are never stored as past searches and never used as
probes (DESIGN 13.3, EXPERIMENT 3.7): unchanged. Gate replays run as the pool's
service principal (DESIGN 5.8). Replay sets are lists of search ids rehydrated
from the log, so tombstones apply (13.3 step 6). A rejected or "no decision"
candidate is deleted with its unshared documents (13.2); the pool stays at its
stage with `cool_until` and `leaf_count_at_reject` set.

### 5.5 What a reader sees

- Before the flip: stage 0, bit for bit. A task started before the flip keeps
  `gen = 0` for its life (DESIGN 13.2), so it never changes mode mid-task and
  its delegates inherit it. `explore` in such a task runs direct search.
- At the flip: new tasks pin `G+1` and the stage-1 path. The candidate's members
  and edges were invisible until the head changed (13.2).
- The digest reports the stage of each readable pool (3.2). It does not report
  that a transition is running (open question Q3).
- Latency of an unrelated recall is unaffected: the build runs in the pool's
  background slot (DESIGN 15).

### 5.6 Failure and abort

| Event | Handling |
|---|---|
| A build call fails (timeout, refusal, malformed) | DESIGN 7.6: the whole generation aborts, unshared documents deleted, counted (INV-11); stage unchanged; backoff (4.4) |
| Per-transition cap reached | abort the candidate, same handling; the per-tenant budget is not reopened |
| Gate rejects, or no decision | delete the candidate; backoff and the growth rule (4.4); counted as `no_benefit`, not as a failure |
| An erasure's boundary publishes mid-transition | the shared guard: the commit takes the shared side of the write gate and checks the published tombstone sequence; an intersecting tombstone aborts the candidate (DESIGN 7.6 step 6, 17 row "Erasure during rebuild"). Retry is allowed after the erasure's closure finishes; `E_ABORT_MAX` such aborts in a row put the pool in cooldown (a pool under constant erasure may stay at its stage; stated limit) |
| Crash, lease expiry | the candidate is `building` with an expired lease: the next evaluator deletes its unshared documents and may re-propose after `cool_until` |
| Head compare-and-set lost | another writer changed the head: discard and re-propose; with the lease this cannot happen between two transitions of one pool |
| Stored-byte cap reached | typed refusal of the candidate's writes (DESIGN 15), abort |
| Revocation or tenant disable | the job runs as a service principal bound to the pool's epoch; a disabled tenant stops its jobs |
| Rollback after adoption | DESIGN 13.3 step 5: compare-and-set back, to generation 0 for a first tree; it also hides the candidate's members and edges |

A failed or rejected transition never degrades readers: they were never moved.

## 6. Read path: the per-query choice

### 6.1 The question

Even in a tree pool, many queries are answered well by the flat path, and a
full fan-out costs `r_max` model calls at most (DESIGN 8.2). Answering easy
queries from the flat path alone is the same adaptivity applied per query.

DESIGN says otherwise today: "Every top-level holder of every pinned family is
asked in parallel (OWNER 'asks all context index shard holders')" (8.2), and 8.1
step 3 "Starts three things in parallel: the tree fan-out (8.2), the direct leaf
search (8.5), and ... bounded link expansion", with AGREED (a) "a direct search
over leaves runs in parallel with the tree walk as a safety net". A
cheap-first path is therefore not in the owner's adaptive sentence, which speaks
of size and complexity of memory and context; reading "context ... demands" per
query is a reading. It is proposed as a DEVIATION candidate (DESIGN 22 would
list it), built behind a switch, and left to the owner (open question Q2).

### 6.2 The rule (proposal)

`adaptive.per_query` is `off` by default until TUNING D4 passes its acceptance.
With it off every recall of a stage 1 or 2 pool takes the full path, exactly as
DESIGN 8.

With it on, for a pool at stage 1 or 2:

1. Run the direct search first, and link expansion at stage 2 (both model-free).
2. Compute three scalars from the lists already fetched:
   - `agree`: size of the intersection of the BM25 top 10 and the kNN top 10,
     divided by 10;
   - `margin`: (s1 - s10) / s1 for the kNN cosine scores of ranks 1 and 10;
   - `spread`: distinct sessions among the fused top 10.
3. The recall is flat-sufficient if `agree >= A_MIN` and `margin >= G_MIN` and
   `spread <= S_MAX`. Otherwise, or if the agent passed `deep = true`, or for
   every `explore`, or if a feature failed, it escalates: the fan-out runs and is
   fused as in DESIGN 8.4 (tree, direct, linked lists by RRF).
4. A flat-sufficient recall is additionally escalated with probability
   `EPS_ESC` (PLACEHOLDER 0.05), drawn from a seeded hash of the search id.
   The sample is what keeps the loop honest (6.3): it gives the tree's
   behaviour on the queries the rule called easy, with a known propensity.
5. Cheap-first is serial (direct, then maybe fan-out), so a full-path recall
   pays the direct search's latency first. That is one Elasticsearch round trip,
   small next to a model call.
6. Nothing is cached across principals; the decision needs no cache.

### 6.3 Logging, so the log stays truthful

Each protocol log row (DESIGN 9) gains: `stage`, `route` (`flat` or `full`),
`tree_consulted` (bool), `esc_reason` (`rule`, `sample`, `forced`, `none`),
`p_esc` (1 for rule and forced, `EPS_ESC` for the sample and for a non-escalated
recall, as the exploration propensity is logged), and the scalars `agree`,
`margin`, `spread`. These are the only way a later reader can tell a tree that
missed an item from a tree that was never asked. Every consumer of
`offered_by_tree` must exclude rows with `tree_consulted = false`:

| Consumer | Rule |
|---|---|
| DESIGN 10 step 3, `tree_missed` | defined only over searches with `tree_consulted = true`. The sample (4) is an unbiased subsample of the flat-answered searches; estimates over them use weights `1 / p_esc` |
| DESIGN 12 steps 3 and 4, the audit's "was it found by the tree" and its miss rate | a flat-answered search is "tree not consulted", never a miss |
| DESIGN 11.1 signals "judge: relevant but never offered", "reverse-audit misses" | same exclusion |
| DESIGN 13.3 replay | replays use `tree_consulted` searches for the tree-source reading; flat-answered searches enter only through the fused reading |
| EXPERIMENT 2.2 (c) source contribution | arms force the full path (section 8), so every row is consulted |
| Exploration propensity and IPS (DESIGN 9, 11.1) | unchanged; `p_esc` is a second logged propensity |

`any_missed` (not offered by any source) is unaffected. The opened-share bounds
(DESIGN 9) are computed over all rows.

## 7. Invariants and the four concerns, in every stage and during a transition

| Invariant or concern | Stage 0 | Stage 1 and 2 | During a transition | Down-move |
|---|---|---|---|---|
| INV-1 tenant on everything | every document, including `ad:<pool>` and candidate manifests, carries `tenant` and its pool and class | same | same; lease, queue and cooldown keys embed the tenant (INV-10) | same |
| INV-2 trim before retrieval | indices chosen from the read set; shared-tier queries are bounded to the class's indices and the pool filter, never to a post-filter | same | the evaluator and builder read only their pool | n/a |
| INV-3 join | log rows and notes take the join class; a pool's evaluator reads only its own pool's rows | edges in the join class's index, as DESIGN 8.7 | candidate documents take the pool's class | n/a |
| INV-4 lineage | every derived document has it; the only stored adaptive state is `ad:<pool>` and manifests, scalars and ids only (4.1) | same | candidate nodes, members and edges carry lineage; the gateway refuses any that lacks it | n/a |
| INV-5 no credential | the evaluator and builders hold none; they call the gateway | same | same | same |
| INV-6 immutability | no stage change edits a version; a stage is a head flip | same | a candidate is new documents; the one sanctioned mutation stays the erasure rewrite | the retired generation is kept, not edited |
| INV-7 pointers | the flat digest has the same field contract (pointer, title, size, reason, confidence, plus `pool` and `origin`, POOLS 3.6) | same | same | same |
| INV-8 tombstones | the guard covers ingest, lookup, restore and replay as today; there is no tree to rewrite | rebuild, replay and restore check it | the commit-time check under the shared gate (5.2 step 4) | n/a |
| INV-9 no cancellation | none | none | an erasure aborts the pool's own background candidate; no principal's request is cancelled | none |
| INV-10 tenant in every key | thresholds are config constants; any tunable stored per pool is keyed by tenant and pool | same | lease, fair queue and budget keys per tenant | same |
| INV-11 typed failures | a failed feature is `hard`, never easy | an escalation failure is typed; fallback as DESIGN 8.2 | every abort in 5.6 is typed and counted | n/a |
| Tenant isolation | no cross-tenant read, write or statistic (below) | same | the transition's model calls go through the tenant's own fair queue and budget | same |
| Access policy | trim before retrieval; one tree per pool, never a mixed summary; sizes over the trimmed set | same | the builder runs as the pool's service principal and reads only that pool; the candidate takes the pool's class | the stage of a pool the session may not read is never reported or inferable |
| Erasure | see 7.1 | see 7.1 | see 7.2 | see 4.4 |
| Versioning | pointers resolve at `gen = 0` for leaves; heads flip by compare-and-set | as DESIGN 13 | the candidate is invisible until the flip; pinned tasks keep their generation | the old generation stays resolvable for `R_GEN` |

No content crosses tenants through shared thresholds or statistics:

- Thresholds are constants in `config.py`, set offline from development and
  synthetic data (TUNING), versioned with the code. They are never fitted from
  live tenants, never adjusted by any aggregate over tenants, and never stored in
  `lc-sys-registry`, which holds no principal, content or count (INV-1).
- A pool's signals are computed from its own indices and log. The platform keeps
  no cross-pool or cross-tenant tally of stages, sizes or hard rates. Typed
  gateway events say which pool changed stage, with no value, as the existing
  logs say typed outcomes (DESIGN 6.5).
- An operator can see that a transition happened in the gateway's typed events;
  it can see no size or rate of a tenant's pool.
- Self-inflicted cost: a writer who inflates a pool to force a transition can
  spend only its own tenant's model budget and slots (DESIGN 15), bounded by the
  stored-byte cap, the per-transition cap and the cooldown.

### 7.1 Erasure in every stage

- Stage 0: the closure of DESIGN 14.2 runs over every index of the tenant and
  finds the pool's leaves, source documents, log rows, stored queries, queue
  records and authz. For a pool that has never left stage 0, the node, member,
  edge, note and cache indices hold zero documents, and a test asserts that (a
  document there is a defect, not a pass). A demoted pool keeps its retired
  generation and edges (4.4) and is excluded from that assertion. The verifier (14.7) plants canaries through the real
  writer in the kinds that exist at that stage and runs its positive control on
  each; for a kind that does not exist it asserts the count of documents is zero.
  It still runs the full-kind positive controls where the kinds do exist (a
  pinned stage 2 pool, and the transition test below), so no kind's control is
  skipped (LAB-R8), and it prints the probe count per stage configuration.
- The physical tiers add no new rule: the closure runs over every physical index
  of the pool (POOLS 9, section 9 here).
- Stage 1 and 2: as DESIGN 14, unchanged, including the walk up `lin_nodes` in
  every retained generation.

### 7.2 Erasure arriving mid-transition

Erasure takes the exclusive side of the tenant's write gate for its closure
(DESIGN 14.3 step 2); the builder's commit (check, refresh, `wp`, head) takes the
shared side, so they cannot interleave. The four cases:

1. Erasure before the build starts: the leaf is already deleted or in the guard;
   the build reads only live leaves and the guard refuses an in-scope input.
2. Erasure during the build: the candidate's nodes already written are documents
   with lineage, so the closure finds and rewrites them (they are in the redact
   set), and the commit-time check also aborts the candidate because a newer
   tombstone intersects its lineage. Nothing derived from the erased leaf can be
   adopted.
3. Erasure while the commit holds the shared gate: the erasure waits for it;
   after the flip its closure covers the new live generation (14.2).
4. Erasure while the builder waits for the shared gate: after the boundary the
   builder re-reads the published sequence, sees the intersection, aborts.

The head flip never happens on a tombstone check older than the last boundary.
The replay gate's statistics may have been computed on content that the erasure
then removed; the commit-time abort makes that moot.

## 8. Interaction with the registered experiment

State plainly: EXPERIMENT's arms force the tree on regardless of size and are not
changed by this amendment. A0 is stage 0 by construction (the same direct search);
A1, A2, LOOP, FROZEN and the rest run `adaptive.mode = pinned:1` or `pinned:2`
from the first write, with `per_query = off`, and C1 and C2 are unaffected. The
S0 fixture (about 300 leaves) therefore pins its stage for its recall gates;
adaptive behaviour on the fixture is exercised by separate tests with lowered
thresholds (section 10). Arms keep their own per-layer switches (edges on or off,
reflection, audit and reorganiser on or off), as registered: A2 uses edges without
the loop, FROZEN and NO-AUDIT use parts of it. The pin only fixes the stage floor
and turns the evaluator off. The pins are recorded in the manifests, and the run
driver refuses an arm whose pin differs from its registration.

What would have to be measured, as a proposal for a later, separately approved
stage, with a new hypothesis HA (nothing below is registered, no cost claim is
made here; TUNING gives the counted costs):

1. Crossover. At a grid of pool sizes built by sub-sampling the same development
   pool, the paired difference of recall@10 between a pinned tree arm and a
   pinned flat arm, with the intervals and outcome words of EXPERIMENT 2.1 and
   the clustered bootstrap of 7.1. `N_UP1` and the other memory thresholds are
   set below the smallest size at which the tree's lower bound is above 0, by the
   transition's own lead time (TUNING D2).
2. Predictiveness of the difficulty features. For each size, the discrimination
   (AUROC with a bootstrap lower bound) of `agree`, `margin`, `spread` and
   `hard` against "flat missed a gold session in its top 10", on development
   only. A feature that does not clear its pre-registered bound is not used
   (TUNING D3, D4).
3. HA. Over a growth stream (an E2-style insertion from empty), arms ADAPT,
   FLAT-ONLY and TREE-ONLY scored at size checkpoints: (a) at sizes below the
   first transition, ADAPT minus FLAT-ONLY has `lo >= -delta` and model calls are
   zero by assertion; (b) at sizes after a transition has completed, ADAPT minus
   TREE-ONLY has `lo >= -delta`; (c) the windows during transitions are reported,
   not decided; (d) the model calls saved against TREE-ONLY are reported. `delta`
   is the non-inferiority margin of the H3 guard, 0.02 (EXPERIMENT 7.2).
4. Thresholds and prompts are frozen before any confirmatory use (EXPERIMENT
   3.7). The fresh set is not touched.

## 9. Physical scaling, without downtime

Everything in this section is PENDING_REAL_TEST until probed on the Basic
cluster (new probes P17 to P20, section 11); fallbacks are stated.

### 9.1 Index layout

Names (DESIGN 6.2 extended): a dedicated pool's indices are
`lc-<t>-<c>-<p>-<kind>`, each an alias to physical indices
`...-p<8 random hex>`; a shared small pool's documents are in
`lc-<t>-<c>-shr-<kind>`. Pool and class ids are lowercase `[a-z0-9]{4,16}`
without hyphen, and `shr` is reserved so it cannot be a pool id. The class key's
pattern `lc-<t>-<c>-*` already covers every pool of the class (DESIGN 6.3).
Which pools a session may touch is the gateway's decision (POOLS 2.4), not an
Elasticsearch role.

### 9.2 S0 to S1: moving a pool out of the shared index

The pool's documents are immutable per version except heads and erasure rewrites,
so the copy is safe to repeat. Steps, per kind:

1. Create the pool's dedicated physical index and an alias pair: a write alias
   (one index) and a search alias.
2. Dual-read: set a pool flag `tier = moving` in the pool's control document by
   compare-and-set. While it is set, the gateway searches the shared index (pool
   filter) and the dedicated index and merges; new writes go to the dedicated
   index.
3. Copy the pool's documents from the shared index to the dedicated index by a
   scroll or reindex filtered on the pool, at a recorded watermark (source-log
   rule, DESIGN 5.1: not a consistent snapshot, so the watermark is the highest
   source sequence copied). Copying reads and writes through the gateway's keys;
   documents of the pool written during the copy already went to the dedicated
   index.
4. Under the shared side of the write gate, verify counts per kind against the
   watermark, refresh, compare-and-set `tier = dedicated`; the shared index stops
   being read for the pool.
5. Delete the pool's documents from the shared index, then purge (DESIGN 14.5)
   the shared index's deleted-document count, because they are copies of content.
   An erasure issued during the move is applied to both indices: the closure
   queries every index in the pool's manifest of physical indices (9.4), and the
   commit-time tombstone check applies to the copy exactly as to a rebuild.

No request waits for the move: reads see both stores, writes go to one.
Fallback if filtered reindex through the gateway is unavailable on Basic: copy by
scroll and bulk in the gateway.

### 9.3 S1 to S2: rollover

Append-only kinds (leaf, src, log, stored queries) roll over: when the current
physical index crosses the size or age bound, a new physical index is created and
the write alias is moved to it in one atomic `_aliases` action (PENDING_REAL_TEST
P8); the search alias gains the new index and keeps the old ones. Reads search the
alias, so nothing is reindexed and nothing pauses. Ids are unique per version
(`leaf_id:lv`), so a document lives in exactly one physical index. Kinds that are
updated in place (heads, the control index, nodes rewritten by erasure) are not
rolled; nodes use path-copied immutable versions, so they roll like leaves, and an
erasure rewrite addresses the physical index that holds the `_id` (found by a
search on the alias, never by writing to a multi-index alias).

If a pool needs a different primary-shard count on an existing index, the
alternatives are the split API (needs the index read-only for the split; PENDING
_REAL_TEST P18) or a copy-and-swap into a new physical index behind the alias
(the same machinery as DESIGN 14.5 and as 9.2). Rollover is preferred: it needs
neither.

### 9.4 Erasure and the verifier across shards and generations

- Each pool keeps a manifest of its physical indices in its control index (ids
  and tier only). The erasure closure, the purge loop, copy-and-swap and the
  verifier enumerate that manifest, not an alias, so no physical index is
  missed, including an index left by a rollover, a retired generation and the
  shared index during a move.
- The purge (DESIGN 14.5) runs per physical index; the receipt reports the time
  to zero per index. `docs.deleted == 0` is required on every physical index of
  the pool before the physical purge is reported.
- The verifier has read-only keys on `lc-<t>-*` and reads each physical index
  directly (14.7). Its positive control plants a canary in each physical index
  (one per rolled index and, during a move, one in the shared copy) and must find
  it before "zero found after" counts.
- Snapshots follow DESIGN 6.5: only source, control and authz indices; the pool
  manifest is in a control index, so a restore rebuilds projections for every
  physical index named in it after the tombstone journal is applied (14.6).

### 9.5 Cost of many small pools

The costs and their mitigations, as designed: a SHARED pool costs no shards; a
DEDICATED pool costs up to 9 shards (fewer when empty kinds are created lazily,
ASSUMPTION: a kind's index is created at first use, so a stage 0 pool at S1
holds only leaf, control, log and stored-query indices, 4 shards (the source log stays one per tenant, DESIGN 5.1), and the
node, member, edge, note and cache indices appear only at stage 1 or 2); a
rollover costs one more index per kind. Provisioning refuses a pool that would
take the node above 80% of `cluster.max_shards_per_node` (DESIGN 6.4), and
reports the refusal as typed. Many tiny indices are a known Elasticsearch cost
(cluster-state size and shard overhead; SOURCE: the shard-size guidance cited in
DESIGN 5.6); the shared tier exists to avoid them.

## 10. Build-plan step

Proposed as BUILD-PLAN Step 24, built after Step 19 and before Step 22's S0 run,
numbered 24 so existing numbers and cross-references keep working. It uses no
model call: the stub and the counter only.

### Step 24: adaptive mode (stage 0 to 2, per-query path, physical tiers)

- `adaptive/modes.py` (stage derived from heads, generation 0, pins),
  `adaptive/signals.py` (the pool signals of 4.1), `adaptive/evaluator.py` (the
  triggers, hysteresis, cooldowns, `ad:<pool>`), `adaptive/transition.py` (candidate,
  gap, commit; reuses `structure/` and the gate), `adaptive/tiers.py` (S0 to S1
  move, rollover, manifest of physical indices), `recall/route.py` (section 6),
  `config.py` (every constant of this file, each with its TUNING id), pins in the
  run driver.
- Failing-first:
  - Stage 0 recall and the A0 arm give the same digest on the same leaves, and a
    write plus a recall at stage 0 leave the model port's counter at 0 (broken: a
    stage 0 write that calls a build or typing task; a stage 0 recall that calls
    `route`).
  - A pool below `N_FLOOR` never gets a candidate, and a pool with one leaf never
    gets a tree (broken: a trigger that fires on size alone with no floor builds a
    tree at one leaf).
  - A pool with `hard_rate` high and size below its memory gate stays flat
    (broken: difficulty alone fires); a pool above its memory gate with
    `hard_rate` low stays flat until `N_BACK` (broken: size alone fires early).
  - A pool at the threshold for one evaluation does not move; at `E_CONSEC`
    consecutive evaluations it does; a signal oscillating around the line moves at
    most once per `D_DWELL` (broken: one threshold with no hysteresis flaps; no
    dwell flaps; consecutive count not reset on a failing evaluation).
  - After a rejected candidate the pool is not proposed again until cooldown
    and growth (broken: a loop that rebuilds at every evaluation).
  - A transition's calls never exceed `B_TRANS` and a call past it aborts
    (broken: no cap).
  - A recall whose features say flat-sufficient answers with zero routing calls;
    a hard one escalates (broken: a rule that never escalates fails the
    needle-needs-the-tree fixture; a rule that always escalates fails the
    zero-call assertion).
  - The log row of a flat-answered recall has `tree_consulted = false`, and
    `tree_missed` counts none of it (broken: `tree_missed` over all rows reports a
    false repair signal); the sampled escalations carry `p_esc = EPS_ESC`.
  - Pinned task across a flip: a task pinned at generation 0 before the flip sees
    flat results to its end; a leaf stored in the gap is found by direct search
    throughout, is a member only after the next rebuild, and never appears under a
    task pinned before (broken: the write path places gap leaves with `src_seq`
    at or below P; an edge or member appears under a pinned task, SEC-V2 red).
  - Erasure at each phase of a transition: before the build, mid-build, after the
    gate, between the check and the compare-and-set (test hook), after the flip.
    No live node, member or cached output derived from the erased canary exists
    after the boundary, and the verifier reports zero residue (broken: tombstone
    checked at build start only; the flip without the shared gate; the closure
    skipping candidate nodes).
  - Cross-tenant and cross-pool statistics: two tenants with identical leaf sets
    and different query histories get identical thresholds and independent stages
    (broken: a global quantile over tenants, or a threshold stored in the
    registry, makes a tenant's transition depend on another's data; SEC-T red); a
    pool's evaluator issues no query that touches another pool's documents or
    another class's log (request-plan assertion; broken: a tenant-wide count).
  - A session with the read set of a lower class never learns the stage of a
    higher-class pool (broken: digest lists all pools).
  - `adaptive.mode` set from a session request is refused (broken: accepted).
  - Down-move: a pool shrunk by erasure demotes only after dwell and the down
    thresholds, keeps the retired generation for `R_GEN`, and a pinned task still
    resolves it (broken: the generation deleted at demotion).
  - Physical tiers: an S0 to S1 move under concurrent writes and an erasure loses
    no document and leaves none in the shared index (verifier, with its positive
    control in the shared copy); a rollover under concurrent writes loses none;
    the closure and the verifier cover every physical index (broken: closure over
    the alias only misses a rolled index; verifier reading the write alias only).
  - The `ad:<pool>` document holds scalars only (a schema assertion; broken: a
    stored signal list).
- Acceptance criteria:
  - AC-1 A new pool is plain retrieval. Verify by: the stage 0 tests above; the
    counter is 0 across 1,000 stub writes and recalls.
  - AC-2 A pool changes stage only on the triggers of 4.3, with hysteresis.
    Verify by: simulated signal sequences (a property test over generated
    sequences) assert at most one move per dwell and no move on a single
    evaluation.
  - AC-3 A transition is a candidate generation adopted by the gate, with the cost
    bounded by `B_TRANS`. Verify by: the stub loop on the fixture with lowered
    thresholds; the counter at the transition's end is at most `B_TRANS`.
  - AC-4 Readers are unaffected during a transition. Verify by: a pinned task
    across the flip (version probe) and recall digests before the flip equal those
    of a pool with no transition.
  - AC-5 Per-query choice is logged truthfully. Verify by: the log rows of a mixed
    run; `tree_missed` computed with and without the exclusion differ, and the
    first matches a hand computation on the fixture.
  - AC-6 Isolation, access, erasure and versioning hold in every stage and during
    a transition. Verify by: SEC-T, SEC-A, SEC-E and SEC-V rerun with the pool at
    stage 0, 1 and 2 and with an erasure at each phase of a transition.
  - AC-7 Physical moves lose nothing and erase everything. Verify by: the tier
    tests and the verifier's positive control on every physical index.
- Negative cases:
  - NEG-1 (AC-1) A pool that never left stage 0 holds a node, member, edge, note or
    cache document: red.
  - NEG-2 (AC-2) A single noisy evaluation, a burst of queries from one task, or a
    bulk import with no recalls moves a pool: red (a bulk import waits for
    `R_MIN` tasks).
  - NEG-3 (AC-3) A transition at `leaf_count` below `N_FLOOR`, a transition with the
    gate bypassed, or one that continues past its cap: red.
  - NEG-4 (AC-4) The flip changes what a task pinned before it sees: red.
  - NEG-5 (AC-5) A flat-answered recall counted as a tree miss: red.
  - NEG-6 (AC-6) A tombstone that intersects the candidate does not abort it, or a
    threshold, count or stage of one tenant or pool is readable from another:
    red.
  - NEG-7 (AC-7) A physical index missing from the manifest: the verifier's check
    that manifest equals the set of indices matching the pool's pattern turns red.

## 11. Probes this adds

| Id | PENDING_REAL_TEST | Fallback |
|---|---|---|
| P17 | filtered copy of one pool from a shared index to a dedicated index preserves ids and counts; an erasure during the move is applied to both | copy by scroll and bulk in the gateway |
| P18 | the split API is available on Basic and needs only a read-only block of the index | rollover only, with copy-and-swap for any primary-shard change |
| P19 | a search alias over several physical indices scores and orders documents as a single index would for BM25 statistics within one physical index, and the cost of the multi-index search at the rollover counts expected | RRF across per-index lists (rank-only, no cross-index scores) |
| P20 | the shared tier depends on P7 (kNN `filter` applied during search) for the `pool` filter and on routing by pool id | every pool at tier S1 (dedicated), at the shard cost of 9.5 |

P8 (atomic `_aliases`) is reused.

All run on the test cluster before the tier
code.

## 12. Limits this adds (for DESIGN 20)

| Limit | Why |
|---|---|
| Shared tier shares term statistics between pools of one tenant and class | Inherent to shared indices with BM25; no document, title or count crosses pools, but a session's own scores can shift with another pool's vocabulary. `pool_isolation = dedicated` removes it at the shard cost |
| The difficulty features are heuristics | They are tuned and validated on development data and can be wrong on other data; a wrong "easy" costs recall, a wrong "hard" costs calls; the sampled escalation measures the error |
| Supersedes and contradicts are absent below stage 2 | Detection shares the typing call (DESIGN 7.7); a backfill at the 1 to 2 transition covers existing leaves |
| A pool under constant erasure may never complete a transition | Each intersecting erasure aborts the candidate (7.6); cooldown applies |
| Aggregate adaptive state holds scalars derived from the pool | Counts and timestamps only; not content; not reached by an erasure because recomputed from live data |
| Statistics from the stage 0 log are query-shaped | They are inside the closure through the log rows (14.2) |

## 13. Conflicts with DESIGN, and the edit that resolves each

| # | DESIGN says | Conflict | Resolution |
|---|---|---|---|
| 1 | 17: "New tenant, no tree yet: recall returns direct search only; digest says 'no index yet'" | it is today a transient state; here it is the normal first stage, and "no index yet" is wrong | rewrite the row as stage 0 (section 3.2) |
| 2 | 5.3: "An index family is one tree over the leaves of one class" | the tree is per pool | edit 5.3: per pool; the class remains the access label |
| 3 | 7.4 step 6, 7.5: enqueue and place every leaf | placement only at stage 1 and above | add the stage condition (3.1) |
| 4 | 7.7: link creation for each new leaf | only at stage 2 | add the stage condition |
| 5 | 8.1 step 3: "Starts three things in parallel"; 8.2 "Every top-level holder ... is asked in parallel"; AGREED (a) | the per-query path serialises and can skip the fan-out | DEVIATION proposal (6.1); off by default; the full path is unchanged |
| 6 | 10 step 3 `tree_missed`; 12 steps 3 and 4; 11.1; 13.3 | they assume the tree was consulted | restrict to `tree_consulted` (6.3) |
| 7 | 13.3 step 3: primary is tree-source recall against the current generation | no tree to compare with at a first transition | 5.4 |
| 8 | 7.6: batch rebuilds flip the head with no gate | a transition is gated | 5.1: only transitions are gated |
| 9 | 5.6: generation 1 is the first generation; P advances with write-path jobs | stage 0 has none | generation 0 implicit; `wp` not advanced at stage 0 and set to `W` at the flip |
| 10 | 6.4: shard budget `(9 x C + 3) x (1 + r)` per tenant | pools add an axis | the formula is per pool for dedicated pools and per (tenant, class) for the shared tier (POOLS 1.2) |
| 11 | 20 "Cost of the owner's routing: 25 to 77 model calls per recall ...; no design change can make agent-per-holder routing cheap without changing the method" | adaptivity pays it only for pools that reached stage 1, and per query only with the switch on | edit the row to say so |
| 12 | 1: the owner's words, and the 1.1 mapping | the new statements are not in it | add the quotes (section 14) |
| 14 | 5.6: "The write-path watermark P (`_id = wp`, one per tenant in `lc-<t>-ctl` ...) ... Because the source sequence is per tenant, one P serves every class" | the watermark and the generations are per pool | `wp`, heads and manifests live in the pool's control index; `src_seq` stays one monotone counter per tenant (DESIGN 5.1), so sequences still compare across pools; in a shared control index their `_id` carries the pool (`hd:<pool>:<family>`, `wp:<pool>`) |
| 15 | 5.7: log rows, stored queries, notes and cache entries live in the index of their join class | index names now carry a pool | a row whose join class equals its pool's class lives in the pool's own index; a row whose join class is higher (a higher-class query over a lower-class pool) lives in the shared small-pool index of the join class, with the offered item's `pool` field, never in the lower class's index (INV-3) |
| 13 | 4.1 POOLED tier: shared indices across tenants, "not built" | the shared small-pool tier is within one tenant and one class | state in 4.1 that the two are different |

DESIGN section 1 is not contradicted: the owner's tree, fan-out, cross-links,
reorganisation and versioning are all kept, and a pool that reaches stage 2
has all of them. The adaptive sentence says the method starts small; it does not
say the method is dropped.

## 14. Edits to make in DESIGN / BUILD-PLAN / EXPERIMENT when this is adopted

Section numbers are those of commit 6002839. Each edit is by section.

DESIGN.md
- 1: add the two quotes of section 1 of this file after the existing quotes, and
  a sentence that adaptivity is an OWNER requirement.
- 1.1: add rows "starts as a fast easy normal rag and auto adapts" to ADAPTIVE 2
  to 6, and "memory pools ... shard/scale" to ADAPTIVE 2.3 and 9.
- 2: INV-7 field list gains `pool` and `origin` (POOLS 3.6). INV-4: add that the
  adaptive control document `ad:<pool>` and candidate manifests are scalars and
  ids only (control documents like `wp`). INV-1: state that stage, size and hard
  rate are never in `lc-sys-registry`.
- 2A: a line under each of the four concerns pointing to ADAPTIVE 7.
- 4.1: state that the shared small-pool tier is not the POOLED tier.
- 5.3: per pool (conflict 2). 5.6: generation 0, `wp` at stage 0, `hd:learn`,
  `ad:<pool>`, the physical-index manifest. 5.7 and 9: the new log fields of 6.3.
- 6.2, 6.4: pool names, reserved `shr`, lazily created kinds, shard formula.
- 7.4 step 6, 7.5, 7.6, 7.7: stage conditions and the bulk-build transition
  (3.1, 5.2). 8.1 to 8.5: stage 0 path, the per-query path behind its switch (6),
  the stage line in the digest, `gen = 0`.
- 10, 11.1, 12, 13.3: the `tree_consulted` exclusions (6.3) and the transition
  gate replacements (5.4).
- 14.2, 14.3, 14.7: closure over every physical index of a pool and the zero-count
  assertions at stage 0 (7.1, 9.4). 15: transition jobs and budgets (5.3).
- 17: rewrite the "New tenant, no tree yet" row; add rows for a transition at a
  tiny pool, erasure mid-transition, bulk import with no recalls, and a pool
  shrunk below threshold.
- 18.1: the budget handle gets a per-transition cap.
- 19: add `adaptive/` and `recall/route.py`.
- 20: the rows of section 12 and the cost-of-routing edit (conflict 11).
- 22: add the per-query path as a DEVIATION candidate; add every PLACEHOLDER
  of this file to the cost-relevant ASSUMPTIONS list.

BUILD-PLAN.md
- Step 6: queue record kept at stage 0 and no placement. Step 9: the bulk build is
  also the transition. Step 10: stage 0 digest and the flat path. Step 11: the new
  log fields. Step 13: gap rule and `wp`. Step 19: the transition gate
  replacements. Step 22: S0 runs pinned stages for its recall gates and the
  adaptive scenarios of Step 24; the verifier's per-stage probe counts.
- Add Step 24 (section 10) after Step 19 in build order; add P17 to P20 to
  section 2.
- 3A: the per-query path's tuned thresholds and the 1-to-2 transition build after
  H1, behind the same interfaces; Step 24's stage 0 and 1 and the pins are needed
  before any arm runs, so that arms can pin.

EXPERIMENT.md (committed alone before the run it affects, with the reason)
- 4: a line that arms pin their stage and `per_query = off`; add arms ADAPT,
  FLAT-ONLY, TREE-ONLY as proposals for the stage of section 8.
- 1: add HA (proposal) with its measure and margin.
- 2.2: HA outcome words; 3.7: thresholds frozen before use; 6: controls for ADAPT
  (a pool that never leaves stage 0 must equal A0).
- 10: a proposed stage for the measurements of section 8, with its count from
  TUNING; 12: add the open questions below as X10 to X15 with their defaults.

## 15. Open questions for the owner (defaults in force until answered)

| Id | Question | Recommended default |
|---|---|---|
| Q1 | Does "segmenting" mean only topic indexes (this file's reading), or also cutting sessions into leaves? | topic indexes only; leaves are still cut at `LEAF_CAP` and at session boundaries, with no model call |
| Q2 | May a recall of a tree pool be answered from the flat path alone when the query looks easy (a deviation from "ask all index holders")? | built behind a switch, off until TUNING D4 passes, then on with 5% sampled escalation |
| Q3 | Should the digest tell the agent each pool's stage, and that a build is running? | the stage of readable pools only; no progress |
| Q4 | Should a pool that shrinks go back to plain retrieval automatically? | yes, one rung at a time, after the dwell, never deleting early |
| Q5 | Shared small-pool indices (cheap, statistics shared inside a tenant and class) or an index per pool from the start (strongest isolation, about 5 to 9 shards each)? | shared for small pools, dedicated past the size trigger; a tenant can force `dedicated` |
| Q6 | What is the most one transition may spend, in model calls? | the formula `B_TRANS` (5.3) within the tenant's hourly budget, automatic, with a typed event; the owner may set an absolute cap |

## 16. Index of ASSUMPTIONS and PLACEHOLDERS introduced here

Each is in TUNING's table (D1 to D8 here; D9 to D13 in POOLS). ASSUMPTIONS
(placeholders carry their TUNING id in brackets below):
pool as the unit (OWNER, not an assumption); three stages and the rung grouping;
leaf cut kept at stage 0; stage derived from heads; physical tiers and the
shared tier; `ad:<pool>` as a scalar control document; first-tree gate
replacements (cluster = level-1 node, fused primary, `m_TRANS`, synthetic-gated
allowance); gating transitions at all; gap rule; one transition per tenant at a
time; lower priority for transition calls; per-query path and its sampled
escalation; down-moves; lazy creation of kinds. PLACEHOLDERS (no measurement):
[D2] `N_UP1` 2,000 leaves, `T_UP1` 2.5 million tokens, `S_UP1` 300 sessions, `E_UP1`
2,000 entities, `N_BACK` 5 x `N_UP1`; [D3] `W_TASKS` 200, `R_MIN` 50, `H_UP1` 0.20,
`TAU_RE` 0.85; [D4] `A_MIN` 0.4, `G_MIN` 0.1, `S_MAX` 6, `EPS_ESC` 0.05; [D5] `E_CONSEC` 3,
`EVAL_PERIOD` 1 hour, `A_EVERY` 500 writes, `D_DWELL` 24 hours, `D_DWELL1` 24
hours, `COOL_BASE` 6 hours, `COOL_MAX` 7 days, `RHO_DOWN` 0.5, `RHO_RETRY` 1.5,
`E_ABORT_MAX` 3, `T_PAR` 1; [D6] `R2_MIN` 200, `H_UP2` 0.15, `M_UP2` 0.25, `G_KEEP`
0.05; [D7] `Q_REPLAY` 80, `Q_REAL_MIN` 20, `m_TRANS` 0.05; [D8] `B_SHARED_MAX` 256 MiB,
`DOC_SHARED_MAX` 50,000, `B_SHARD_TARGET` 10 GiB, `AGE_ROLL` 30 days.
Derived, not tuned: `N_FLOOR = 8 x F_TARGET`, `B_BUILD`, `B_TRANS`, `r_max`.
