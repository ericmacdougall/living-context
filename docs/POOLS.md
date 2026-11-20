# living-context: memory pools, agent tools and scoped authorization (amendment)

Status: design amendment, not yet adopted. Nothing here has been built or run.
This document is in the authorization and security domain: it changes who may
read and write what. It needs two independent reviews before any build starts.
Section numbers of DESIGN.md, BUILD-PLAN.md and EXPERIMENT.md refer to the
branch head this was drafted against (commit 6002839); section 14 lists the
edits to make when it is adopted. Companion files: [ADAPTIVE.md](ADAPTIVE.md)
(how each pool starts plain and grows) and [TUNING.md](TUNING.md) (how every
default here is set; ids D9 to D13). Labels as in DESIGN section 0.

## 0. The requirements

The owner's words, verbatim:

> "it need 'memory pools' that a session has access to/are decided to be queried
> and included as rag and followup injections"

> "memory pools are what shard/scale as their data grows.. and agents use tools
> to update/save to them"

> "proper authorization , scoped tokens etc"

What is OWNER: named memory pools; a session has access to some of them; which
of them are queried is decided; results are included as retrieval and as
follow-up injections; pools scale as they grow; agents write to them through
tools; authorization is proper and tokens are scoped. Everything else is a
design proposal and labelled.

Wording. DESIGN 4.1 already uses "POOLED" for a tenancy tier (shared indices
across tenants, not built). That is a different thing. In these files "pool" means
only the memory collection defined here; the tenancy tier is always written "the
POOLED tier", and the shared small-pool index of ADAPTIVE 2.3 is within one tenant
and one class, never across tenants.

## 1. What a pool is, and what it maps onto

A memory pool is a named collection of memories with: a pool id, a tenant, one
access class, a kind, a card (a short administrator-written description used for
selection, section 4), a state (`active`, `detached`, `deleting`), and its own
leaves, source log, trees, edges, logs and generations. Kinds (labels, no
different mechanism): `project`, `person` (a person's long-term memory), `team`
(shared knowledge), `scratch` (a session's own working memory, expiring).

### 1.1 The two readings of "pool"

| Reading | Meaning | Consequences |
|---|---|---|
| A. Pool is an access class | the existing class lattice (DESIGN 4.2) names the pools | no new machinery; but at most 4 classes per tenant (4.2), the shard budget `(9 x C + 3)` (6.4) forbids many, the lattice joins classes (INV-3), and a per-session scratch pool or a pool per person cannot exist. Fails the owner's "pools ... shard/scale" and many-pools use |
| B. Pool is a new axis inside a class | a pool belongs to exactly one class; a grant names pools; the class still decides sensitivity and the join rule | one new field (`pool`) on documents, a pool id in index names, a pool-scoped grant. Classes stay few; pools can be many |

ASSUMPTION: reading B. The class of a pool is its sensitivity label and what the
existing read and write sets are checked against; the pool is the unit a session
is granted, selects, writes to, adapts and scales. The effective access of a
session to a pool is the conjunction of both (section 2.4).

A derived thing's class remains the join over its inputs (INV-3). A tree, edges,
log rows and notes are per pool and take the pool's class; a log row's class is
the join of the query's class and the offered item's class as today (DESIGN 5.7).
Because a tree is per pool, nothing derived mixes pools, so no cross-pool join
arises (section 9 for copies and edges).

### 1.2 Where documents live

Names (extends DESIGN 6.2; physical layout and tiers are ADAPTIVE 2.3 and 9):
`lc-<t>-<c>-<p>-<kind>` for a dedicated pool and `lc-<t>-<c>-shr-<kind>` for the
shared small-pool tier, with a `pool` field on every document (added to the common
fields of DESIGN 5, with the same lineage rules). The source log stays one per
tenant (DESIGN 5.1) with a `pool` field. Heads, manifests and `wp` live in the
pool's control index; in a shared control index their `_id` carries the pool. Log
rows, stored queries, notes and cache entries live in the index of their join class
(DESIGN 5.7): in the pool's own index when the join class is the pool's class, and
in the shared small-pool index of the join class otherwise (ADAPTIVE 13, rows 14
and 15). The shared tier relies on the kNN `filter` and routing (P7, P20). Pool ids are 16 random lowercase
alphanumerics, never reused, so they cannot be guessed or enumerated (2.6). The
pool's display name lives only in its card.

Shard budget per DESIGN 6.4: shared pools cost nothing; a dedicated pool costs up
to 9 shards with lazily created kinds (ADAPTIVE 9.5).

## 2. Principals and the owner's "scoped tokens"

Nothing here replaces DESIGN 4. The roles, classes, the epoch table, the
two-administrator rule and the no-self-grant rule are reused unchanged. What is
added is that grants and session scopes name pools and operations.

### 2.1 Operations

| Operation | Meaning |
|---|---|
| `query` | retrieve and fetch (`pool_query`, `pool_fetch`, `ask`, `explore`) |
| `save` | add new memory (`pool_save`) |
| `update` | correct or supersede an existing memory (`pool_update`) |
| `forget` | file an item erasure request for a memory (`pool_forget`) |
| `administer` | grant or revoke operations on this pool to other principals; edit its card |

`save` and `update` are the write set restricted to the pool; `query` is the read
set restricted to the pool. No operation implies another.

### 2.2 Grants

A grant is `(principal token, pool, operations)` in the tenant's authz index
(DESIGN 5.10), stored with `lin_subjects` of the principal like every authz
document. Who grants:

- A tenant administrator creates pools (a pool is created by an administrator,
  never by an agent) and grants any operation on any pool to another principal
  (DESIGN 4.3). No self-grant. Revoking an administrator's grant or `administer`
  needs a second, distinct administrator (the two-person rule, unchanged).
- A principal holding `administer` on a pool may grant and revoke `query`,
  `save`, `update` and `forget` on that pool to others, never to itself, never
  `administer` (only an administrator grants `administer`). ASSUMPTION.
- The session's scratch pool is created by the gateway at session start for the
  session's principal, with `query save update forget`, and is deleted by the
  erasure machinery at session end or after `SCRATCH_TTL`.

### 2.3 The token

A scoped token is a session token of DESIGN 5.10 (`<tenant id>.<random 256 bits>`,
stored only as its sha256) whose session record carries:

- `tenant`, `principal`, `session` (an id of the calling session, set at mint),
- `scope`: pool id to the set of operations (never wider than the principal's
  live grants at mint, and re-intersected at every use),
- `default_save_pool` (optional, one pool in `scope` with `save`),
- `exp` (short lifetime, `TOKEN_TTL`) and `max_age` (the latest `exp` can reach),
- `parent`: the parent token's session id for a delegated token, else none,
- `epoch`: the principal's authority epoch at mint (DESIGN 4.4).

The gateway is the only verifier. Tokens are opaque bearer strings resolved by
lookup in the tenant's authz index, so there is no new cryptography and no key to
leak; Elasticsearch keys never leave the gateway (INV-5). Tokens never appear in
logs (the gateway logs only hashed tenant-scoped identifiers, DESIGN 6.5), in
stored memories, in model prompts or in digests; the recall service holds the
caller's token only to forward it (DESIGN 3). A tool call carries the token and
the session id; a token presented with another session id, from another tenant's
connection or after its session closed is refused.

### 2.4 Checks, at every use

Every tool call, every fan-out wave and every send hand-off checks, with no
Elasticsearch read on the hot path (the epoch table and a per-session scope cache
are in gateway memory):

1. the token resolves, its tenant prefix matches, `exp` has not passed;
2. the principal's authority epoch equals the token's `epoch` (DESIGN 4.4: one
   unconditional statement revokes every session of the principal at once, mid-
   session, including a recall in progress at its next wave and its send
   hand-off under the send fence);
3. the parent chain is valid (a delegated token is valid only while its parent
   is, so revoking or letting a parent expire kills its children);
4. for each pool named or implied: the pool is `active`, the pool is in the token
   `scope` with the needed operation, the principal's live grant still holds it,
   and the pool's class is in the principal's read set (for `query`) or write set
   (for the writing operations).

A class-grant removal bumps the epoch, as DESIGN 4.4 already specifies. A pool-grant
removal does not: it updates the gateway's in-memory grant table, which step 4
reads at every use, so the pool drops out of every token of that principal at once
while the principal's other pools and sessions continue (the table is gateway
memory like the other people-operation state, DESIGN 20, so a restart re-reads it
from the authz index and a grant that cannot be re-read is treated as absent). Renewal (`token_renew`) extends `exp` up to `max_age` only
if the epoch is unchanged and every pool in scope is still granted with the same
operations; a renewal never widens scope.

### 2.5 Delegation to a sub-agent

`token_delegate(scope', ttl')` mints a child token only by attenuation:
`scope'` is a subset of the parent's scope, per pool a subset of its operations,
`ttl'` no later than the parent's `exp`, `administer` never delegated, and the
child can delegate only further narrowings. The child record stores `parent`.
Anything wider is refused with the typed error `scope_widened`. A delegated token
is never accepted as a grant source.

### 2.6 Errors never reveal whether a pool exists

Every refusal of a pool-related request (not granted, not in scope, wrong
operation, class not permitted, detached, deleting, nonexistent, guessed id,
revoked) returns the one typed answer `unavailable`, with the same shape and no
distinguishing field, extending DESIGN 8.4 and 8.7. Typed errors that do not
concern a pool (`expired`, `revoked_session`, `scope_widened`, `rate_limited`,
`too_large`, `version_conflict`, `invalid`) are returned only after the pool
checks pass, or for a token that is itself invalid. Timing is not claimed
constant; the pool checks run from memory, so the difference between "not
granted" and "does not exist" is a memory lookup, not a store read (limit).

A call that names several pools is checked whole: if any named pool fails any
check, the whole call returns `unavailable` and no partial result is built, so
the answer does not tell which pool failed.

## 3. Agent tools

All tools are gateway operations over the local socket (DESIGN 3), bound to a
session by its token. They hold no store credential. Every write tool takes an
idempotency key scoped to (tenant, principal, key), with the body hash stored, as
DESIGN 7.1. Tool input is untrusted data.

### 3.1 The tools

| Tool | Arguments | Target pool and how constrained | Returns | Typed errors | Logged |
|---|---|---|---|---|---|
| `pool_save` | `text`, `pool` (optional), `key`, `meta` (optional: tags, `valid_from`) | `pool` if given, else the token's `default_save_pool`, else `pool_required` (typed, to a holder of more than one save pool). Needs `save` on it | the leaf pointer(s) (a long text is cut at `LEAF_CAP`, DESIGN 7.2) and the pool id; no text | `unavailable`, `pool_required`, `too_large`, `invalid`, `held` (quarantine or review hold), `rate_limited` | one gateway event: tool, pool, outcome, sizes, hashed principal; never text. Store step as DESIGN 7.4 with provenance |
| `pool_update` | `pointer`, `text`, `mode` (`correct` or `supersede`), `if_lv` (expected current version), `key` | the pool of the pointer, resolved by the gateway from the leaf head; needs `update` on it | the new pointer; for `supersede` also the old pointer's resolved state | `unavailable`, `version_conflict` (with the current pointer if readable), `too_large`, `invalid` | as above, plus old and new pointers |
| `pool_forget` | `pointers` (up to `FORGET_MAX`), `reason` (short, optional), `key` | each pointer's pool; needs `forget` on every one, else the whole call is `unavailable` | an erasure id; the call returns at the erasure boundary (DESIGN 14.3), with the boundary time | `unavailable`, `held` (section 3.4), `rate_limited` | the erasure request as DESIGN 14.3 step 1 |
| `pool_query` | `query`, `pools` (optional hint), `exclude_origin` (optional), `budget`, `deep` (optional) | the intersection of the hint (or the whole granted set) with the token's `query` pools, narrowed by selection (section 4); never wider | the digest of DESIGN 8.4 with a `pool` label on each item and per-pool stage (ADAPTIVE 3.2) | `unavailable` (any named pool not granted: whole call), `rate_limited`, `invalid` | the protocol log rows (DESIGN 9) plus the selection record (section 4.4) |
| `pool_fetch` | `pointer` | the pool of the pointer, resolved by the gateway; needs `query` | the leaf text in a data envelope (3.5) and its provenance, or the resolution state of DESIGN 8.4 (`superseded`, `moved`, `expired`) | `unavailable` | an `opened` row written by the gateway, as `lookup` today |
| `ask`, `explore` | as DESIGN 8.4, plus the optional `pools` hint | as `pool_query` | as DESIGN 8.4 | as `pool_query` | as DESIGN 8.4 |
| `token_renew`, `token_delegate` | section 2 | the token's own scope | a token | `scope_widened`, `expired` | token mint events, hashed ids |

`pool_fetch` is the existing `lookup` with the pool check in front of it.

### 3.2 `pool_save`

- Size: `text` at most `SAVE_MAX` (PLACEHOLDER 64 KiB; the request line limit of
  DESIGN 7.1 still applies); empty or whitespace is refused (DESIGN 7.2 step 5).
- Quota: at most `SAVE_RATE` saves per session per hour and the pool's stored-byte
  cap (DESIGN 15); past either, a typed refusal.
- Which pool: the owner says agents "update/save to them". A save lands in
  exactly the pool named, or in the token's single default pool, and never in a
  pool the token lacks `save` on. There is no implicit write to a "best matching"
  pool: choosing where to write is not a selection that may be left to a heuristic.
- Provenance (3.6) is added by the gateway from the session, never from the
  arguments.
- The write path is DESIGN 7: tombstone guard, class from the pool, subjects, key,
  source, embed, leaf, head, queue record. Stage and placement follow ADAPTIVE.

### 3.3 `pool_update`

- `correct`: a new version `lv + 1` of the same leaf id (DESIGN 5.2, 13.1), by
  compare-and-set on the leaf head with `if_lv`; the old pointer resolves
  `superseded` with the new pointer (DESIGN 8.4). Provenance of the new version
  names the updater.
- `supersede`: a new leaf in the same pool with `supersedes` set to the old
  leaf, the old head's `valid_to` set, and, at stage 2, a `supersedes` edge
  (DESIGN 16.3). DESIGN 20 records that the head's `valid_to` is not yet written
  by the build; this tool needs that row closed (a dependency, section 14).
- A text that names a subject erased by a tombstone is refused with the same
  typed `invalid` as any policy refusal (DESIGN 7.3).
- An update never changes pool or class. Moving a memory between pools is a copy
  (section 9.1).

### 3.4 `pool_forget`

Not a delete. It files an item-scope erasure (DESIGN 14.1) for the named leaves
and goes through the existing steps: closure under the write gate, one
publication boundary, key destruction, deletion, rewrite, purge, verification,
receipt (DESIGN 14.3). The agent's authority is `forget` on the pool of each
pointer. The closure reaches everything derived from the leaf, including copies
in other pools by lineage (DESIGN 14.2). ASSUMPTION: if the closure contains
documents of a pool the principal does not hold `forget` on, the request is
`held` for a tenant administrator and nothing is erased until released. Subject
erasure ("erase this person") is not an agent tool; it stays with the erasure
requester role (DESIGN 4.3). The result gives totals over the requester's own
read set only (DESIGN 4.3, review F19b).

### 3.5 What the model sees (data, not instruction)

Memory returned to a model is wrapped by the gateway in a delimited data
envelope:

```
<memory pool="<label>" origin="<agent|human|import|system>" ptr="<opaque>" >
...text...
</memory>
```

The gateway escapes the delimiter sequences inside `text` so stored content
cannot close the envelope or open another. The envelope contract, stated to every
harness that builds a prompt from it: contents are data, never instructions; they
go in a user or tool-result position, never in a system prompt. This is a
convention the harness must honour, not something the gateway can enforce on a
model (limit, section 10). Pool label is the card's short label, which is visible
only to a grantee of the pool.

### 3.6 Provenance on every memory

Stored with each leaf version, written by the gateway from the session: `origin`
(`agent` when written through `pool_save` or `pool_update` by an agent token,
`human` for a human principal, `import`, `system`), `writer` (the principal's
HMAC token, DESIGN 5), `writer_session` (an id, hashed), `wrote_at`, and for
copies `copied_from`. They carry the leaf's lineage (`lin_subjects` includes the
writer token), so erasure of the writer reaches them. Provenance is read-only after
write (INV-6). On read, `origin` and the pool label travel with every digest
entry and every envelope; `pool_query` accepts `exclude_origin` (it can only
narrow). INV-7's field list gains `pool` and `origin` (section 14); both are
descriptors of the reader's own pools.

### 3.7 Injection defences, and what remains

Content saved by an agent is data written by an untrusted party; a later session
that reads the pool may be steered by it. Defences built here: provenance and
labelling on every read; the data envelope with escaped delimiters; the existing
limits on holder calls (no tools, labels and ids validated, reasons and titles
bounded, DESIGN 8.2, 8.3); no body, gist or fact in pointers (INV-7); per-session
save quota; `exclude_origin`; `forget` and administrator release; and a pool
property `agent_saves` of `direct` or `review` (a held queue reusing the
quarantine of DESIGN 7.3, released by `administer`). Not solved, stated as limits:
a model can still obey instruction-like text it reads; labelling reduces but does
not remove that; semantic scanning of saved text is not attempted (it is not
reliable and would be a model call at stage 0).

## 4. Selection: which granted pools are queried

### 4.1 Sets

- `G` = pools in the token's scope with `query`, active, class permitted (2.4).
  A session with no explicit grant has `G` empty and can touch no pool (least
  privilege by default).
- The caller's hint `H` (optional) from `pool_query`'s `pools` or the harness
  configuration.
- `S` = the pools queried this turn.

Hard rule (INV-12, new): `S` is a subset of `G`. The planner takes `S`, then
intersects it with `G` computed from the token at this call. Selection never
widens: a hint naming a pool outside `G` fails the whole call with `unavailable`
(2.6); a selection step (code or model) only ever sees the cards of pools in `G`
and its output is validated against them, like a holder's output against the
presented child ids (DESIGN 8.2). A confused deputy cannot arise because no step
that influences selection holds authority of its own: the authority is the token's.

### 4.2 The rule, zero model calls by default

Rule R0 (default; code only, no model call):

1. If `H` is given, `S = H`.
2. Else if `|G| <= P_ALL`, `S = G`.
3. Else rank `G` by the cosine between the query embedding and each pool's card
   embedding (the card is administrator-authored text, embedded once at write;
   the query embedding exists already for the kNN), keep the top `P_MAX`
   with cosine at least `TAU_CARD`, and always add the sticky pools: those queried
   in the last `STICKY_TURNS` turns whose results were opened.
4. With probability `EPS_POOL` (seeded hash of the search id) add one pool of
   `G` not in `S`, so the loop can see what selection misses.

Rule R1 (optional, off by default): one `route_pools` model call per turn, whose
input is the query and the cards of `G` only, returning a subset of the presented
labels with reasons bounded as in DESIGN 8.2; cost one call per recall, on a
cheap tier; code validates. A failure falls back to R0 and is counted (INV-11).
R1 earns its place only if TUNING D9 shows R0 loses recall that R1 recovers.

A card carries `lin_subjects` like any administrator text (DESIGN 7.3), so an
erasure reaches a card that names a person.

### 4.3 Cost

R0 costs the query embedding that is computed anyway and a dot product per pool
in `G`; no Elasticsearch request and no model call. Each queried pool then costs
that pool's own path: at stage 0 one direct search (two Elasticsearch requests),
at stage 1 or 2 as ADAPTIVE 6. So a recall costs at most `P_MAX` direct searches
in the zero-model case.

### 4.4 Logging so the log stays truthful

Every protocol log row (DESIGN 9) gains `pool` (the offered item's pool),
`sel_rule` (`hint`, `all`, `card`, `route`), `sel_pools` (the ids queried this
turn), `sel_p` (1 for `hint`, `all`, `card`; `EPS_POOL` for the sampled extra),
and `pool_consulted` (always true for a pool in `sel_pools`). A pool not in
`sel_pools` was not asked, which is not the same as a miss:

- `tree_missed`, the reverse audit's "was it found" and DESIGN 11.1's miss
  signals are defined only over searches that queried the leaf's pool (the same
  exclusion as ADAPTIVE 6.3).
- The sampled extra pool gives an unbiased view of what selection leaves out, with
  propensity `EPS_POOL`, used as the exploration slot is (DESIGN 9).
- The hint and the selection are logged as such, never as the agent's relevance.

## 5. Inclusion: results from several pools

### 5.1 Merge

1. Each pool in `S` runs the path its stage provides (ADAPTIVE 3.2, 6), under its
   own pinned generation (DESIGN 8.1), producing a ranked list of at most
   `K_POOL` items.
2. Lists are merged across pools by client-side RRF (k = 60) on ranks only, ties
   by a salted hash of the leaf id (DESIGN 8.4). Raw scores are never compared
   across pools: BM25 statistics differ per index and per pool, and comparing
   them would also let one pool's statistics colour another's ranking.
3. The merged list is cut at `K_TOTAL`, with a per-pool floor of one item for each
   pool that produced any item above `I_RANK` (so a large pool cannot crowd a small
   one out entirely; the floor is a ranking choice, labelled ASSUMPTION).
4. Every digest entry carries its `pool` label and `origin` (3.6) and
   no body, gist or fact (INV-7). Sizes, totals and counts are over what the
   session may read in `S`; there is no "other pools" count anywhere.
5. The digest's per-source sections (tree, direct, linked) are kept per pool.

### 5.2 No leakage across pools

- A session sees titles, sizes, labels and counts only of pools in `S`, which is a
  subset of `G` (4.1).
- Shared-tier term statistics are the stated limit (ADAPTIVE 2.3, 12): a session's
  scores inside a shared index can shift with another pool's vocabulary. No title,
  size, count or document of that other pool is returned.
- Edges and expansion stay inside one pool (section 9.3), so a digest cannot
  surface a neighbour in a pool the session does not hold.
- A pointer to a leaf of a pool outside `G` resolves to `unavailable`.

## 6. Follow-up injections

### 6.1 What it is

After the first retrieval, as the conversation moves, the recall service may offer
memory into the session without the agent asking. An injection is an offer built
by the same planner as a query (selection, per-pool paths, merge) and delivered in
the data envelope of 3.5 with `via = injection`. It is a recall made by the
harness on the agent's behalf, not a new retrieval engine, and it runs under the
session's token: it can touch only `G`.

### 6.2 Triggers (all model-free)

The harness calls `inject_check(turn)` after each turn; the recall service
evaluates, in order:

| Trigger | Condition | Notes |
|---|---|---|
| T1 topic shift | the cosine between this turn's embedding and the running mean of the last `SHIFT_WINDOW` turns is below `TAU_SHIFT`, and the turn has at least `MIN_TURN_TOKENS` | embeddings are computed outside the model port |
| T2 new entity | the code entity extractor (the one used for leaves) finds an entity not seen earlier in the session | the offer comes from a direct search on the entity |
| T3 failed lookup | a `pool_fetch` or `ask` returned `superseded`, `moved` or `expired`, or a `pool_query` returned nothing | offer the superseding or moved pointer, or retry once across the other pools of `G`, once per failed item |
| T4 the actor asks | an explicit `pool_query`, `ask` or `explore` | this is a retrieval, not an injection; listed because the owner's "followup injections" include it |

### 6.3 Bounds

At most `I_PER_TURN` items and `I_TOKENS` descriptor tokens per injection; at least
`I_COOLDOWN` turns between injections; at most `I_SESSION` injected items per
session; only items with merged rank at most `I_RANK`; the same `P_MAX` and
per-pool limits as a query. By default an injection carries descriptors only
(pointer, title, size, pool, origin, reason), consistent with DESIGN INV-7; text
delivery (`deliver_text`, an integer, default 0) is configuration, and delivered
text is logged as `delivered`, never as `opened` (open question Q3).

### 6.4 De-duplication

Against what the session already holds: the set of (leaf id, version) and content
hashes offered or delivered earlier in the session, rebuilt from the session's
protocol log rows, plus any pointers the harness passes as held (a harness claim
can only remove candidates, so trusting it cannot widen anything). An item whose
leaf was superseded since is offered as the new version once, with the old pointer
marked `superseded`.

### 6.5 Marking and trust

An injected item is data. It carries `via = injection`, its `pool` and `origin`,
and travels in the envelope of 3.5; it is never placed where the harness puts
instructions. Injected items from `origin = agent` are marked as such. The limits
of 3.7 apply unchanged; injection widens only what the session is shown from pools
it already holds.

### 6.6 Revocation during a session

Each injection is built under a fresh check (2.4) and again at the send hand-off;
a pool revoked between the build and the hand-off is dropped from the offer at the
hand-off, the same rule that discards a revoked principal's unsent output (DESIGN
4.4).

### 6.7 Logging

Injection offers are log rows with `via = injection`, the trigger, the pools
queried and the propensity. They are excluded from the opened-share bound of
DESIGN 9 (computed over `via = recall` rows), because they are offers the actor
did not ask for and would otherwise deflate it. ASSUMPTION. Their opens (a later
`pool_fetch` of an injected pointer) count as opens, as any open does.

## 7. Each pool adapts on its own

The unit of adaptation is the memory pool, in both files: stage, physical tier,
signals, transitions and cooldowns are per pool (ADAPTIVE 2.1). A small pool stays
plain retrieval; a large one has a tree, edges and a loop; a pool that outgrows
the shared index gets its own indices and rolls over. A session querying a small
and a large pool gets a flat list and a routed list merged by rank (5.1). Pool
creation costs no index and no model call; a new pool is a shared-tier pool at
stage 0.

## 8. Versioning

- Generations, heads and `wp` are per pool (they live in the pool's control
  index). A task pins, for each pool in `S`, the pool's live generation and its
  watermark at the first recall that includes the pool, and the pins are returned
  in the digest (DESIGN 8.1). A pool added to `S` by a later turn is pinned then.
- Leaf pointers stay `(gen, leaf_id, lv)` with the pool inside the opaque pointer
  and resolved from the leaf head; node pointers resolve within a pool's
  generation.
- A pool's card, grants and state are versioned by their documents like any
  control document; changing a card never rewrites a leaf.

## 9. Erasure, isolation and access across pools

### 9.1 Items copied between pools

A copy is a new write by a principal who holds `query` on the source pool and
`save` on the target (and whose class rules allow it), creating a new leaf in the
target with `copied_from` set, its lineage including the source's `lin_leaf_ids`
and `lin_subjects`, and the target pool's class, which must dominate the
source's class (a copy up, INV-3: the copy is derived from the source). A copy down
into a lower-class pool is a declassification and is an ordinary new write by a
principal holding both classes (DESIGN 14.1), not a copy; a pool has exactly one
class, so there is no join to take. Because the
lineage names the source leaf, an item erasure of the source and a subject erasure
of its subjects reach the copy (DESIGN 14.2). Deleting a source pool does not
erase copies by itself (9.4). There is no live link: a copy is an independent
memory with a pointer back that resolves `unavailable` if the source is gone.

### 9.2 Erasing a subject across all pools

Subject erasure is tenant-wide already: the closure queries every index of the
tenant, all classes, all kinds, by `lin_subjects`, so every pool, every physical
index of every pool and every retained generation is covered (DESIGN 14.2,
ADAPTIVE 9.4). The pool field adds nothing to the closure; the receipt is totals
over the requester's read set. A scratch pool that held the subject is in the
closure.

### 9.3 Cross-pool edges: not allowed

ASSUMPTION: edges are within one pool in this version. An edge between a leaf of
pool A and one of pool B would reveal to a session that holds A that a related
memory exists in B (an existence and degree channel, DESIGN 8.7), would need an
edge index scoped to the pair, and would join classes. If added later: stored in
an index of the pair's join class, visible only to a session holding `query` on both
pools, carrying no title, deleted when either endpoint is erased. Reading across
pools is done at query time by merging, never by stored cross-pool structure.

### 9.4 Detaching and deleting a pool

- Detach: the pool's state becomes `detached` and every grant on it is removed from the in-memory
  grant table (2.4), with no epoch bump, so no grantee's other access is touched. Every request then returns `unavailable`. Nothing is
  deleted; reattach is an administrator action. A detached pool's documents stay in
  the erasure closure.
- Delete: a new erasure scope `pool` (DESIGN 14.1), requiring a tenant administrator
  and a second administrator (ASSUMPTION; it is destructive and irreversible).
  Closure: every document with the pool id in every kind, every physical index in
  the pool's manifest, every retained generation (ADAPTIVE 9.4). Then the steps of
  DESIGN 14.3 with the pool scope's own tombstone, key destruction for its leaves,
  verification with a positive control in each physical index, receipt. Copies in
  other pools are not erased by a pool delete (they are separate memories); their
  `copied_from` now resolves `unavailable`. A subject or item erasure still reaches
  them (9.1).

### 9.5 Isolation across tenants

Pools exist inside a tenant. A pool id is meaningful only with a token of its
tenant; a token or pointer of another tenant resolves to `unavailable`. Shared
indices never cross tenants (ADAPTIVE 2.3).

## 10. Limits this adds (for DESIGN 20)

| Limit | Why |
|---|---|
| Injection into model prompts is a convention | The gateway wraps and labels memory; it cannot stop a model obeying text it was given |
| Shared small-pool tier shares term statistics inside a tenant and class | ADAPTIVE 2.3 |
| Refusals hide pool existence by shape, not by timing | Checks run from memory; a timing difference is a memory lookup, not a store read |
| A bearer token can be used by whoever holds it for its lifetime | It is bound to the session id and short-lived, not to a device; revocation and the short `TOKEN_TTL` bound the exposure |
| A pool card is administrator text visible to grantees | It is meant for selection; it must not hold secrets |
| `forget` by an agent can be broader than its pool | Held for an administrator when the closure leaves the pools it holds (3.4) |
| An agent with `save` on a shared pool can plant instruction-like text | Provenance, labelling, quota and the `review` mode reduce it; they do not remove it |

## 11. Build-plan step

Proposed as BUILD-PLAN Step 25, after Step 15 (revocation) and Step 24 (adaptive
mode); the token and grant parts depend on Step 5 and Step 15. It uses the stub
only. Because it changes authorization, the build step starts only after two
independent reviews of this file from different models or agents.

### Step 25: memory pools, scoped tokens, agent tools, selection and injection

- `policy/pools.py` (pool, class, grants, effective access, pure),
  `gateway/tokens.py` (scoped tokens, attenuation, renewal, parent chain, scope cache,
  checks of 2.4), `gateway/tools.py` (the tools of section 3, idempotency, quotas,
  provenance, envelope), `recall/select.py` (R0, optional R1 behind the stub,
  the subset rule), `recall/merge.py` (section 5), `recall/inject.py` (triggers,
  bounds, de-duplication), `erasure/pool_scope.py` (pool delete, detach,
  `forget` hold), card handling, log fields.
- Failing-first:
  - A token for pool A is refused on pool B, with `unavailable` identical to a pool
    that does not exist (broken: a distinct error code; a timing-distinct path).
  - A `query`-only token cannot `pool_save`, `pool_update` or `pool_forget`
    (broken: operations not checked per pool).
  - An expired token and a token of a revoked principal are refused, including mid-
    recall at the next wave and at the send hand-off (broken: scope checked once at
    the start; epoch not re-checked per wave).
  - A delegated token with a wider pool set, a wider operation set, a longer
    lifetime, or `administer` is refused `scope_widened`; revoking the parent kills
    the child (broken: attenuation not enforced; child outlives parent).
  - Random and sequential pool ids and ids of another tenant return the same
    `unavailable` as a real ungranted pool (broken: nonexistent returns another
    code).
  - A token replayed with another session id, on another tenant's connection or
    after session close is refused (broken: token not bound to session).
  - A call naming two pools where one is not granted fails whole with no partial
    result (broken: returns the granted pool's results).
  - A selection that would widen (a hint outside `G`, a model suggestion outside the
    presented cards, a card-ranking over all pools) never reaches an ungranted pool
    (broken: selection computed over all tenant pools, or intersected after
    retrieval; request-plan assertion that no index of an ungranted pool is touched).
  - A save lands only in the named pool or the token's single default; with two save
    pools and none named it is `pool_required`; never in another pool (broken:
    "best matching pool" heuristic).
  - A revoked pool is never injected: revoke between the build of an injection and
    its send hand-off and the item is absent (broken: no hand-off check).
  - An injection never repeats what the session holds, including a superseded
    version offered once (broken: no de-duplication; a held set kept only in the
    caller).
  - An injection obeys `I_PER_TURN`, `I_TOKENS`, `I_COOLDOWN`, `I_SESSION`
    (broken: unbounded).
  - A title, size, count or label of a pool outside `S` never appears in a digest;
    the merge uses ranks, not cross-pool scores (broken: merge by raw score leaks
    statistics; a digest "N more in other pools").
  - Injected and fetched text is inside the envelope, with delimiters in the stored
    text escaped (broken: raw text can close the envelope).
  - Provenance is set from the session, not the arguments (broken: `origin` taken
    from the argument).
  - The token never appears in a log line, a stored document, a digest or a model
    prompt capture (a scan with a planted token value; broken: a debug log of the
    request).
  - A `pool_update` with a stale `if_lv` is `version_conflict`; a retried save with
    the same key returns the first result; the same key with another body is refused
    (DESIGN 7.1).
  - `pool_forget` runs the erasure steps (a canary in the forgotten leaf and its
    copy in another pool is gone, with zero residue); a closure outside the held
    pools is `held` (broken: raw delete; erases the copy without the hold).
  - Pool delete covers every physical index of the pool; pool detach returns
    `unavailable` to every request; a subject erasure reaches a scratch pool and
    every pool of the tenant (verifier with positive control).
  - A pool adapts on its own: one pool at stage 1 and one at stage 0 in the same
    recall, merged by rank (ADAPTIVE tests).
- Acceptance criteria:
  - AC-1 A session with no grant touches no pool. Verify by: the empty-scope token
    gets `unavailable` on every tool.
  - AC-2 Authorization is per pool and operation and re-checked at every call, wave
    and hand-off. Verify by: the token tests above, with a mid-recall revoke.
  - AC-3 Selection only narrows. Verify by: the request-plan assertion and the
    widening variants.
  - AC-4 Results from several pools merge by rank with labels and no leakage.
    Verify by: SEC-A rerun with pools, and the digest scan.
  - AC-5 Injections are bounded, de-duplicated, labelled as data and revocable.
    Verify by: the injection tests on a scripted conversation with planted topic
    shifts (TUNING 2.1, G7) and a revoke in the middle.
  - AC-6 Erasure, `forget`, detach and delete behave across pools and physical
    indices. Verify by: SEC-E with canaries in two pools, a scratch pool, a copy
    and a rolled index.
  - AC-7 Tokens never leak. Verify by: the planted-token scan.
- Negative cases:
  - NEG-1 (AC-1) A principal with grants but a token minted with an empty scope reads
    a pool: red.
  - NEG-2 (AC-2) A token still works after its principal's epoch bump, after its
    parent expired, or after its pool grant was removed: red.
  - NEG-3 (AC-3) A hint, a card ranking or a routing output reaches a pool outside
    `G`: red.
  - NEG-4 (AC-4) A digest shows a title, count or score of a pool outside `S`: red.
  - NEG-5 (AC-5) An injection repeats a held item, exceeds a bound or appears for a
    revoked pool: red.
  - NEG-6 (AC-6) A forgotten leaf, its copy, or a deleted pool's document survives
    in any physical index, or the verifier skips a physical index: red.
  - NEG-7 (AC-7) A token string in any log, document or prompt: red.
  - NEG-8 (AC-2) A token minted for one tenant works against another: red.

## 12. Open questions for the owner (defaults in force until answered)

| Id | Question | Recommended default |
|---|---|---|
| Q1 | Reading B: pools as a new axis inside a class, with the class as sensitivity label (reading A would limit pools to about four per tenant) | reading B |
| Q2 | May an agent's saves into a pool shared by several principals go live at once, or wait for review? | `review` for pools shared by more than one principal, `direct` for scratch and single-owner pools |
| Q3 | Should a follow-up injection carry text, or only descriptors the agent can open? | descriptors only for injections; the first query may deliver the top one text with `deliver_text = 1` |
| Q4 | Is a model routing step for choosing pools wanted, or is the zero-call card rule enough? | the card rule; the model step stays off until TUNING shows it recovers recall |
| Q5 | Should an agent's `forget` of a memory also erase copies in pools it cannot forget in? | no: hold for an administrator |

Also see ADAPTIVE 15 (Q5 there: shared small-pool indices or an index per pool).

## 13. Index of ASSUMPTIONS and PLACEHOLDERS introduced here

ASSUMPTIONS: reading B for pools (Q1); administrators create pools and `administer`
holders grant the other operations; scratch pool per session; the token as a session
record with attenuation; the intersection rule for effective access; `unavailable`
as the one pool refusal; selection R0 with cards; the sampled extra pool; the
per-pool floor in the merge; cross-pool edges not allowed; `forget` held when the
closure leaves the pools held; `agent_saves` review mode; two administrators to
delete a pool; injections excluded from the opened-share bound. PLACEHOLDERS (no
measurement): `P_ALL` 4, `P_MAX` 8, `TAU_CARD` 0.2, `STICKY_TURNS` 3, `EPS_POOL`
0.05 (D9); `K_POOL` 10, `K_TOTAL` 30 (D10); `TAU_SHIFT` 0.5, `SHIFT_WINDOW` 3,
`MIN_TURN_TOKENS` 8, `I_RANK` 5 (D11); `I_PER_TURN` 3, `I_TOKENS` 400, `I_COOLDOWN`
2 turns, `I_SESSION` 30 (D12); `TOKEN_TTL` 1 hour, `max_age` 12 hours, `SAVE_MAX` 64
KiB, `SAVE_RATE` 200 per hour, `FORGET_MAX` 20, `SCRATCH_TTL` 24 hours (D13; set by
security review and cost, not tuned against recall).

## 14. Conflicts with DESIGN, and edits to make when adopted

Conflicts (quote, then the resolving edit):

- DESIGN 4.2 "Per-person private memory: modelled as a separate tenant when there
  are many persons, or as a class when there are few" and "A class per person for
  thousands of persons would blow the shard budget". Pools make a per-person
  memory a pool (a kind `person`) in the shared tier; edit 4.2 to say so and keep
  the tenant option for hard isolation.
- DESIGN 4.3 gives the write set and read set per principal. Pools add a grant per
  pool; edit the table (section 2.2).
- DESIGN 8.1 "Gets the reader's read set from the session ... Pins, for each class
  in the read set and each family, the live generation": the pin is per pool in `S`.
- DESIGN 8.2 "Every top-level holder of every pinned family is asked": per queried
  pool, and per ADAPTIVE 6.
- DESIGN 8.7 "a reader walks only the trees of classes in its read set": per pools in
  `S`; add the shared-tier statistics row.
- DESIGN 6.3 read keys per class: a class key now also covers all pools of the class;
  pool access is the gateway's decision, as class access is for combinations of
  classes today.
- DESIGN 20 "The leaf head's `valid_to` is not set": `pool_update supersede` needs it
  set; close that row when the head is made writable.

Edits to make by section (commit 6002839):

DESIGN.md: 1 and 1.1 (the quotes; mapping rows to POOLS 1 to 6); 2 (INV-12 selection
only narrows; INV-7 field list gains `pool`, `origin`); 2A (a line on pools under
each concern); 3 (tools are gateway operations); 4.1 (the POOLED tier is not a pool);
4.2 and 4.3 (grants per pool); 4.4 (tokens, attenuation, parent chain, renewal; the
epoch check at every tool call); 5 (`pool` common field; provenance fields on
leaves; pool, card and grant documents); 5.9 and 14.1 (the `pool` scope); 5.10
(session records carry scope, parent, session id, `exp`); 6.2, 6.4 (names, shard
formula, shared tier); 7 (the tools' store paths; provenance); 8.1 to 8.5 (selection,
merge, labels, stage line); 9 (the new log fields of 4.4 and 6.7); 14.2, 14.3, 14.7
(pool scope, held forget, physical indices); 15 (per-session quotas); 17 (rows for
the negative cases of Step 25); 19 (`policy/pools.py`, `gateway/tokens.py`,
`gateway/tools.py`, `recall/select.py`, `recall/merge.py`, `recall/inject.py`);
20 (section 10 here); 22 (the placeholders).

BUILD-PLAN.md: add Step 25 (section 11) after Step 24; Step 5 and 15 note the token
extension; Step 14 note the pool scope; Step 16 reruns the access suite with pools.

EXPERIMENT.md (committed alone before the run it affects): 9.2 SEC-A rows for pools
and tokens; 9.3 revocation mid-session for scoped tokens and delegated tokens; 9.4
pool delete and forget; a proposed measurement stage for selection and injection
(TUNING Z4, P4); 12 the open questions above.
