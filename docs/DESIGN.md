# living-context: design

Status: design of record for the first build, revised three times after
adversarial reviews (first review: findings F1 to F21, M1 to M5, cited as
"review F<n>"; second review: findings F1 to F14, cited as "review 2, F<n>";
third review: open findings F2, F7, F9, F11, F15, cited as "review 3,
F<n>"; EXPERIMENT.md section 13 maps each to its fix). The second revision
removed mechanism where it could (the evaluation cache, the betting
confidence sequence, gists in returned pointers); the third replaced the
per-principal send fence by one per-tenant fence and model-written titles
by code-built ones. Nothing below has been run.
Every claim about behaviour is a target until the experiment in
[EXPERIMENT.md](EXPERIMENT.md) or a probe in [BUILD-PLAN.md](BUILD-PLAN.md)
records it.

## 0. How to read this document

Every choice carries one of these labels:

| Label | Meaning |
|---|---|
| OWNER | Taken from the owner's own words (quoted in section 1). |
| AGREED | Added in conversation and accepted by the owner ("i agree"). |
| SOURCE | Taken from a cited public source (URL given, read 2026-11-18). |
| LAB | A lesson from the owner's earlier, unpublished lab on this problem; it failed several independent reviews, and those failures are the evidence. Cited as LAB-R1 to LAB-R12 (section 21). |
| ASSUMPTION | My choice, not the owner's and not from a source. It can be changed without changing the method. |
| DEVIATION | A departure from the owner's description, with the reason. |
| COMPARISON | Something built only so the owner's method can be measured against it. It is never the method. |
| PENDING_REAL_TEST | A fact about Elasticsearch or the platform that the design depends on and that has not been observed on our cluster. Each has a probe id (P1 to P16) in BUILD-PLAN.md and a stated fallback. |

## 1. The owner's method (what is being built)

The owner's words, verbatim:

> "knowing one person can only quickly remember so much... wouldnt it make
> sense to layer memory automatically? segment topics to maximum context sizes
> and an index, and when the index itself gets large, segment it and index it.
> you could then do standard fan out tree traversal where an agent asks all
> context index shard holders 'which of your index holders may be relevant to
> this prompt: ___' -- in parallel... and expand the parallelism as each lower
> layer responds... with each parent agent summing and organizing the
> information and providing pure reference pointers and size and total size,
> so in the end the originating agent can have a tight summary to let it know
> what additional useful context/information is available and it can go look
> it up -- or delegate subagent exploration with deeper perspective/specific
> questions, allowing it to both immediately ask against the relevant segment
> data via linked lookup and inference, and pass its own tree-traversing
> additional lookup search, where any new resources it hasnt explored can be
> explored as well. ultimately recursive indexing/segmenting, recursive
> delegated search."

> "there needs to be additional layers of course - intelligent reorganization
> -- and relational cross linking between leaves in a graph... so distance by
> tree branching is not isolation."

> "the evaluation of reorganization: it can reorganize by graph edge strength
> and search hit confirmation, if the protocol includes tracking if the actor
> actually did a lookup to the referenced item, and the memory-management in
> the harness itself inferences during 'reflection' what was relevant - and
> run background searches for anything that may have been relevant - while
> doing memory addition/updates... obviously with everything versioned... it
> would provide strong clustering and comparative recall, precision and
> relevance."

> "an item 'never offered' that is found in reflection [which has deeper and
> looser search] and during pruning [where reverse searching happens: 'what
> searches happened this leaf would have been relevant to, and was it found']
> would allow the 'exposure bias' to be repaired."

> "build it, the way i described [with proper edge handling and research for
> the specifics]... and test it"

> "design tenant isolation, erasure and access policies -- and versioning -
> from the get go"

"Edge handling" is read both ways: graph edges (construction, typing,
weights, hubs, decay; section 16) and edge cases (section 17).

AGREED additions: (a) a direct search over leaves runs in parallel with the
tree walk as a safety net; (b) several overlapping indexes, because topics are
not a partition; (c) stable leaf ids and versioned nodes so pointers survive
reorganisation; (d) a relevance judge in reflection that is separate from the
searcher; (e) evaluate forward in time.

NOT adopted by the owner, so COMPARISON only: routing each level by an
Elasticsearch query instead of an agent per index holder (arm C1). Collapsed
retrieval over all node levels, which RAPTOR and MemTree found better than
traversal (SOURCE: https://arxiv.org/html/2401.18059v1,
https://arxiv.org/html/2410.14052v3), is also COMPARISON only (arm C2).

### 1.1 Mapping of the owner's sentences to components

| Owner's phrase | Component | Section |
|---|---|---|
| segment topics to maximum context sizes | segmenter with a hard leaf cap | 7.2 |
| an index; when the index gets large, segment it and index it | index nodes; index-of-indexes rule | 5.4, 7.6 |
| agent asks all index shard holders "which of your index holders may be relevant to this prompt" in parallel | holder calls, top level asked in full | 8.2 |
| expand the parallelism as each lower layer responds | wave-free asynchronous expansion | 8.2 |
| each parent summing and organising, pure reference pointers, size and total size | organise step and digest | 8.3 |
| tight summary; go look it up; delegate subagent exploration | digest, lookup, ask, explore | 8.4 |
| ask against the segment data via linked lookup and inference | `ask` tool | 8.4 |
| any new resources it hasn't explored can be explored | `explore` with an exclusion set | 8.4 |
| relational cross linking between leaves in a graph | typed weighted edges | 16 |
| reorganise by graph edge strength and search hit confirmation | reorganiser signals | 11 |
| tracking if the actor actually did a lookup | protocol log, opened from the gateway | 9 |
| reflection: inference of what was relevant, background searches | reflection with a separate judge | 10 |
| everything versioned | versioning model | 13 |
| never offered, found in reflection and pruning; reverse search | exposure-bias repair, reverse audit | 10, 12 |

## 2. Invariants (each is tested; see EXPERIMENT.md section 9)

- INV-1 Tenant on everything. Every stored document of every kind carries
  `tenant`, lives in an index whose name contains that tenant, and is reached
  only through a gateway request bound to that tenant by an authenticated
  session (LAB-R10). One exception, holding no principal, content or count:
  the global tenant registry `lc-sys-registry` (tenant id, status, created
  time; section 5.10). Authz records (sessions, grants, epochs) are per tenant.
- INV-2 Trim before retrieval. What a reader may not see is never fetched:
  the gateway chooses the set of indices from the reader's permitted classes
  before any query, and no result is ever filtered by class after retrieval
  (SOURCE: the vendor's own warning that document-level security does not
  restrict global statistics,
  https://www.elastic.co/docs/deploy-manage/users-roles/cluster-or-deployment-auth/controlling-access-at-document-field-level).
- INV-3 Class of a derived thing = join over everything it was derived from,
  including the queries that shaped it. A derived thing whose join is not a
  declared class is not built.
- INV-4 Lineage before visibility. Every derived document carries its lineage
  (source leaf versions and subject tokens) in the same document; the gateway
  refuses to index any document that lacks it (fail closed).
- INV-5 No store credential outside the gateway. Agents, the recall service,
  background workers and the evaluation harness hold no Elasticsearch
  credential (LAB-R2).
- INV-6 Immutable versions. Leaf versions, node versions, edge versions and
  manifests are never edited, with one sanctioned exception: an erasure
  rewrite (section 14.4), which is recorded.
- INV-7 Pointers carry no content field (review 2, F11; review 3, F11). A
  returned pointer, and every entry of the digest, carries the pointer, a
  title, the size, the holder path, a reason and a confidence, nothing else:
  no body (leaf text), gist or fact field is ever copied into it. Every title
  is code-built (5.2, 8.2): the top 4 tf-idf terms of the leaf's text, or of
  a node's summary, leaving out every token that contains a digit
  (ASSUMPTION; so numeric dates, amounts, identifiers and codes never reach a
  title, while digit-free forms, such as a month name, a spelled-out number
  or an alphabetic code, can, 20).
  Reasons and organise outline labels are bounded: at most 12 words, sharing
  no 5-gram with the leaf text, facts or gists in the call's input (8.2,
  8.3). Gists and facts stay inside holders, which see their children's
  entries. Leaf text is served only by a logged `lookup` or `ask`. This is a
  field contract, enforced by code and tested by SEC-A8; it is not a promise
  of semantic non-disclosure: titles, reasons and labels are descriptors
  derived from content, and a descriptor can convey an answer (20). So
  `opened` records lookups, not use: an item that was not opened may still
  have been used, and "not opened" is never counted as evidence of
  irrelevance by itself (9). Reading the owner's "pure reference pointers"
  together with "a tight summary" this way is an ASSUMPTION about the
  owner's intent (EXPERIMENT 12, X9).
- INV-8 Tombstones are honoured by every path that can write, rebuild,
  replay, restore or re-ingest, through one shared guard (LAB-R4). Tombstones
  live in a journal outside the snapshot timeline as well as in the store, so
  no restore can predate them (14.6).
- INV-9 No principal's request is cancelled, blocked or terminated because of
  another principal's request, with three exceptions: revoking a principal
  discards that principal's own unsent output; an erasure's closure and
  publication hold its own tenant's write gate, which delays (never refuses)
  that tenant's commits for at most `B_ERASE` (14.3; never another tenant's);
  and a revoke or an erasure's publication step holds the exclusive side of
  its own tenant's send fence for one in-memory step, which delays (never
  refuses) that tenant's send hand-offs by the gateway's own code time
  (target under 1 ms; 4.4).
  Revocation never waits on, and
  never depends on, any action of the revoked party; it is one fixed statement
  on a table the gateway owns (4.4; LAB-R2, LAB-R3).
- INV-10 Every lock, cache, rate-limit and idempotency key embeds the tenant id
  in a collision-free form (no short hashes) (LAB-R1). Every shared queue (the
  model port, the embedding port, the keystore) is per tenant or fairly shared
  with a reserved share per tenant (15).
- INV-11 Failures are typed and counted. A failed model call is never read as
  "nothing relevant".

## 2A. The four first-class concerns at a glance

These are woven through the data model (5), both paths (7, 8) and the loop
(9 to 13). This section gathers the answers in one place.

### TENANT ISOLATION

- What carries the tenant: every document of every kind (leaf, source, node
  and its summary, member, edge, log row, stored query, query embedding, note,
  judge label, cache entry, manifest, head, tombstone), by the `tenant` field
  and by its index name `lc-<tenant>-...` (5, 6.2). Keys of locks, caches,
  rate limits and idempotency embed the tenant (INV-10).
- How Basic enforces it: no document-level security (not on Basic, and not a
  boundary anyway). Each dedicated tenant has its own index family, reached
  only with API keys whose role descriptors name only that tenant's pattern
  and grant no cluster privilege (6.3); the gateway is the only client and
  binds each request to one tenant from an authenticated session whose token
  carries the tenant (3, 5.10, 7.1). Authz records are per tenant
  (`lc-<t>-authz`). Probe P2 must confirm the key boundary, including the
  cluster-level APIs, on our cluster before anything else.
- What one tenant can never observe of another: content, titles, sizes,
  counts, index names, edges, routing (holders asked), scoring statistics,
  errors (8.7; EXPERIMENT 9.1 T1 to T4, T6, T7).
- What one tenant can never affect: another tenant's writes, erasures,
  revocations and structure; availability is bounded by per-tenant admission
  caps, job slots, fair queues at the model and embedding ports, per-tenant
  stored-byte caps and a keystore file per tenant (15), with shared-node
  latency as a stated limit (20).

### ACCESS POLICY

- Principals and roles: section 4.3 (writer, reader, reorganiser, erasure
  requester, erasure executor, verifier, tenant administrator, revoker,
  tenant key owner, operator), with who may write, read, reorganise, erase,
  administer and rotate keys.
- Classes: a small declared lattice per tenant (4.2); the class of anything
  derived is the join over its inputs, including the queries that shaped it
  (INV-3).
- No leak through summaries, pointers, sizes, titles, edges or timing: trim
  before retrieval by choosing indices from the read set (INV-2); per-class
  trees, never a mixed summary filtered at read; sizes computed over the
  trimmed set; edges in the join class's index; one typed `unavailable` for
  forbidden, erased and missing (8.7).
- Revocation the revoked party cannot block: one unconditional update of an
  authority epoch in a table the gateway owns in memory (`revoke` returns once
  memory denies; durability in a bounded per-tenant local journal is reported
  separately, and a journal failure closes that tenant; persisted to
  Elasticsearch best-effort), checked at every request, every
  fan-out wave and under the tenant's send fence (shared with erasure
  publication) before every hand-off to the socket, on a
  reserved admission lane; an operator break-glass revoke; two administrators to
  revoke an administrator or revoker; no self-grant; class-grant removal bumps
  the epoch; tenant keys invalidated by a tenant key owner for tenant-wide
  revocation (4.4).
- Caches never share prompt-bearing outputs across principals, and a cache
  entry's class is the join of its prompt's and its inputs' classes (5.8).
- The application never holds a store credential (INV-5, 3).

### ERASURE

- Lineage from every derived artefact to its source leaves and subjects is in
  the artefact itself, written atomically with it (INV-4, 5).
- Erasing a subject or item: under the tenant's write gate, compute the full
  closure, then publish the tombstone and the complete redact set together in
  one in-memory step under the tenant's send fence, so no send check can
  straddle it (the erasure boundary, within `B_ERASE` of the request); then key
  destruction, deletion, in-code rewrite of every affected node in every
  retained generation, purge, verification with a positive control, receipt
  (14). "Erased" is promised from the boundary, not from the request. It
  reaches summaries up the tree, edges, the offered/opened/used log,
  stored queries and their embeddings, reflection notes, judge labels, cached
  model outputs, old generations, replay sets, authz records, and every
  artefact shaped by the person's queries or by others' queries that mention
  them (14.2). A class relabel is an erasure of the old-class copy,
  effective when the relabel call returns at its boundary (14.1).
- Restores cannot resurrect: tombstones are kept in a journal outside the
  snapshot timeline; every restore of Elasticsearch or of the keystore applies
  it, replays the erasure closure over every restored index (authz and control
  included), and passes the independent verifier's residue checks before the
  tenant is enabled (14.6).
- Verified by a separate verifier with planted canaries that it must first
  find, including canaries in query text, authz records and plaintext logs
  (14.7; EXPERIMENT 9.4).

### VERSIONING

- What is versioned: leaves, nodes, edges, members, whole structures
  (generations per class and family; each edge and member version carries
  `src_seq` or `gen_from` plus a `gen_to` set, so a pinned reader sees a
  stable tree and a stable graph) (13.1).
- Pinning: a task pins generations at its first recall; leaf pointers never
  depend on the tree; node pointers resolve in their generation, then `moved`
  or `expired`; edges and members are visible by the task's pinned write-path
  watermark (write-path versions) or the pinned generation's ancestry (rebuild
  and reorganiser versions), so a candidate's edges are invisible until the gate adopts it
  (13.2). P and generation heads are published only after the documents they
  cover are searchable, and ancestry ids are kept for the life of the class
  (5.6), so the visible set cannot change under a pinned task.
- Coexistence with erasure: per-version source keys erase all source versions
  and their snapshots; derived history is rebuildable and holds ids, not
  content; the one place where they conflict, erasure wins (14.4,
  DEVIATION 6).
- Replay as the adoption gate for reorganisation (13.3).

## 3. Components and the trust boundary

```
 agents (originating agent, delegates)          no store credential
      |  tool calls: recall / lookup / ask / explore / remember / erase
      v
 recall service + background workers            no store credential; holds the
 (fan-out, organise, reflection, reorganiser,   caller's session token only
  reverse audit, erasure executor)
      |  gateway protocol (local socket, session token per request)
      v
 GATEWAY  ---- the only Elasticsearch client; holds per-tenant, per-role
      |        API keys read from 0600 files outside the repository; owns the
      |        authority-epoch table and the per-tenant tombstone journals
      |---- KEYSTORE service (one file per tenant: per-leaf data keys,
      |        per-tenant HMAC keys)
      v
 Elasticsearch 9.x, Basic licence, security on, TLS
 VERIFIER (separate process, separate read-only keys) reads ES directly for
 erasure and isolation verification; it cannot write.
 MODEL PORT (stub, or a capped model client) is called by the recall service
 and workers with content already trimmed by the gateway.
```

- The gateway resolves tenant, principal, channel and permitted classes from
  the session record, never from the request body; a body that names a
  tenant, principal or class it is not entitled to is refused (LAB-R10).
- DEVIATION 1 (storage base): the keystore is not Elasticsearch. It is one
  small SQLite file per tenant (so one tenant's ingest or bulk erasure cannot
  hold another tenant's write lock; review F20a), with `PRAGMA
  secure_delete=ON` and `PRAGMA journal_mode=WAL`, owned by the keystore
  process. After every destroy batch the keystore runs `PRAGMA
  wal_checkpoint(TRUNCATE)`, so no page image of a destroyed key stays in the
  `-wal` file; Step 4 of BUILD-PLAN scans the database, `-wal` and `-journal`
  files with a positive control. The keystore directory is excluded from host
  snapshots and backups other than the keystore's own encrypted backups, which
  are counted under `R_KEYS` (14.6). Freed filesystem blocks and SSD wear
  levelling can still hold old bytes: a limit (20). Reason for the deviation:
  deleting a document in Elasticsearch only marks it deleted until a merge
  (SOURCE: https://www.elastic.co/blog/lucenes-handling-of-deleted-documents),
  so a key kept there could not be destroyed within a stated bound.
- Also outside Elasticsearch, both owned by the gateway: the authority-epoch
  table (authoritative in memory, with a bounded, preallocated local journal
  per tenant; 4.4) and
  the per-tenant tombstone journal (append-only, hash-chained, replicated to a
  second location outside the snapshot timeline; 14.6). Everything else,
  including the encrypted source log, sessions, grants, a best-effort copy of
  the epochs, tombstones and manifests, lives in Elasticsearch.
- The model provider receives leaf text in holder, build, judge and reader
  calls. It is treated as a processor inside the trust boundary; this is a
  limit (section 20).

## 4. Principals, roles, access classes and tenants

### 4.1 Tenants and tiers

- A tenant is the unit of hard isolation. Two tiers, stated per tenant:
  - DEDICATED (built first, used in all tests): a family of indices per
    tenant, reached with API keys whose role descriptors name only that
    tenant's index pattern. Elasticsearch enforces it.
  - POOLED (designed, not built in the first version; ASSUMPTION): shared
    indices with a `tenant` field and a gateway-enforced filter. It leaks term
    statistics (IDF, doc counts) across tenants through scoring (SOURCE: vendor
    DLS page above) and is therefore labelled "gateway-enforced, statistics
    shared" wherever offered. Reason for deferring: it is only needed past
    about 60 dedicated tenants per node (section 6.4), and the data model
    already carries `tenant` on every document, so adding it later does not
    change any document.
- A separate cluster per tenant is the strongest tier and needs no code.

### 4.2 Access classes

- Each tenant declares a small set of classes (ASSUMPTION: at most 4) and a
  partial order "dominates". Example: `base` < `restricted`; `hr` incomparable
  with `restricted`, both above `base`.
- A principal holds a read set (a down-closed set of classes) and a write set.
- A leaf version has exactly one class, chosen at write from the channel's
  policy; a writer can write only into its write set.
- Join: the class of a derived thing is the least declared class that
  dominates all inputs (INV-3). If no declared class dominates them, the thing
  is not built (an edge between `hr` and `restricted` leaves is never created
  unless the tenant declares a class above both). ASSUMPTION; limit in 20.
- Per-person private memory: modelled as a separate tenant when there are many
  persons, or as a class when there are few (ASSUMPTION). A class per person
  for thousands of persons would blow the shard budget (section 6.4).

### 4.3 Roles and operations

| Role | May | May not |
|---|---|---|
| writer | `remember` into classes in its write set | read anything it did not write unless also a reader |
| reader | `recall`, `lookup`, `ask`, `explore` over its read set | write; see any count, size, title, edge or timing that depends on classes outside its read set |
| reorganiser (service principal, per tenant and class) | read plaintext of the classes it rebuilds; write nodes, edges, manifests, notes of exactly that class | erase; read classes it does not rebuild; write into a lower class anything derived from a higher one |
| erasure requester | file an erasure for a subject or item in its tenant; read its receipt as totals over its own read set | read content; see per-class counts outside its read set (the full receipt goes to the verifier and the tenant administrator; review F19b) |
| erasure executor (service) | compute lineage closure, delete, rewrite, purge | read content beyond what a rebuild needs (rebuilds run as the reorganiser of that class) |
| verifier (separate process) | read-only search of every index of the tenant, including deleted-document statistics (index-level `monitor` on the tenant's indices only) | write, delete, call the gateway write paths, use any cluster-level API |
| tenant administrator | grant and revoke roles and classes of other principals inside its tenant; revoke an administrator or a revoker only together with a second, distinct administrator (two-person rule) | grant anything to itself (no self-grant, review F14d); read content by default (ASSUMPTION: separation of duties) |
| revoker | the single `revoke` call on non-administrator principals | anything else |
| tenant key owner (service user per tenant, held on the operator host, not by the gateway) | create, rotate and invalidate the tenant's own API keys (index privileges on `lc-<t>-*` only, cluster privilege `manage_own_api_key`; PENDING_REAL_TEST P15) | read through the gateway; touch another tenant |
| operator (platform) | provision tenants and tenant key owners with an offline bootstrap credential; the break-glass revoke of any principal of any tenant, which no tenant role can remove or delay | read tenant content through the gateway |

### 4.4 Revocation that the revoked party cannot block

Revised after review F14: the first version made revocation an Elasticsearch
write to a shared index, so anything that made that index unwritable (for
example another tenant filling the disk to the flood-stage watermark, which
blocks writes) blocked revocation, and its check just before sending was not
atomic with the send.

- The epoch table is the gateway's own. Each (tenant, principal) has an
  authority epoch held in gateway memory. `revoke` is one gateway call that,
  under the exclusive side of the tenant's send fence (below), increments the epoch,
  sets status revoked and discards the principal's gateway-owned unsent
  buffers, all in memory; it then releases the fence and returns `denied`
  (review 2, F9). No revocation or send fence is held across any disk or
  network I/O. Durability is reported separately: after the return, outside
  any fence, the change is appended to the tenant's epoch journal and fsynced;
  the revoke receipt records `denied_at` (set before the call returns) and
  `durable_at` (set when the fsync completes), and the revoking administrator
  or tool reads both. A revoke without `durable_at` is re-issued (revoke is
  idempotent). Persisting the epoch to `lc-<t>-authz` is best-effort and
  retried; no step of `revoke` waits on Elasticsearch. No lock that the
  revoked principal can hold, no compare-and-set it could contend, no eviction
  protocol (LAB-R2, LAB-R3). PENDING_REAL_TEST P14: revoke still takes effect
  at once with `lc-<t>-authz` blocked read-only and with the disk at flood
  stage.
- The epoch journal is bounded (review 2, F9). The epoch state is a small map
  (principal token to epoch and status; at most `N_PRINCIPALS = 10,000` per
  tenant, ASSUMPTION), so each tenant has a preallocated journal file of
  fixed size and a spare of the same size for compaction (`J_SIZE = 4 MiB`
  each, ASSUMPTION, both allocated at provisioning, so another tenant filling
  the disk cannot take their space): a snapshot of the map followed by
  checksummed change records. Start (review 3, F9): before
  admitting any request of the tenant, the gateway appends a start record and
  makes it durable, by an fsync of the journal file and then of its
  directory; no request of that tenant is admitted until both have returned,
  and a failure is a journal fault. Compaction (review 3, F9): when the
  records fill half the file, the gateway writes a fresh snapshot of the map,
  followed by a copy of the current start record, into the tenant's second
  preallocated file (the spare), fsyncs that file, swaps the two file names
  by one atomic exchange (`renameat2` with `RENAME_EXCHANGE` on Linux,
  `renamex_np` with `RENAME_SWAP` on macOS; ASSUMPTION: the journal's
  filesystem supports it; provisioning tests it on the journal directory and
  refuses the tenant otherwise), and fsyncs the
  directory, in that order; appends wait in memory meanwhile and count
  against `D_FSYNC`; a failure at any step is a journal fault. The exchange
  leaves the old file in place as the next spare, so after provisioning no
  journal space is ever freed or allocated (a rename over the first file
  would free its space, which another tenant's growth could take before a
  new spare was allocated). The files never grow. Clean stop: the gateway
  first closes the tenant's admission, the revoke lane included (a revoke
  arriving after that is refused with a typed retryable failure, never
  answered `denied`, so its revoker re-issues it after the restart), then
  drains every pending append and fsync, and only then writes and fsyncs a
  shutdown record, and only for a tenant that is not closed; a stop that
  cannot drain within `D_FSYNC` writes no shutdown record, so the tenant is
  closed on start.
- Fail closed. A tenant is closed (every principal of the tenant denied) when
  an append or fsync of its journal fails (no space, I/O error) or does not
  complete within `D_FSYNC = 2 s` (ASSUMPTION), or when on start its journal is
  incomplete: a torn or checksum-failing record, a half-written compaction, or
  no shutdown record after the last start record, which is what any crash
  leaves. Because the start record is durable before any request is
  admitted, this holds after a power loss too: a run that admitted anything
  always leaves a durable start record with no shutdown after it, so a power
  loss between a denial in memory and its fsync cannot leave the previous
  run's clean shutdown as the last durable record (review 3, F9). So a crash
  or power loss between denial in memory and the append leaves the tenant
  closed instead of quietly re-admitting a revoked principal. A closed tenant
  stays closed, across any number of restarts, until explicit break-glass
  recovery: no shutdown record is ever written for a closed tenant, so its
  journal never ends clean. A revoke that was denied in memory but never
  became durable is known only from its receipt, which the revoking
  administrator or tool keeps until `durable_at` (above). So the operator's
  break-glass recovery call carries (a) the principals of every receipt that
  lacks `durable_at` and (b) the administrators to reopen, none of whom may
  be in (a) or revoked in the rebuilt table. The gateway rebuilds the table
  from the durable journal and `lc-<t>-authz` (higher epoch wins), appends
  and fsyncs the revokes of (a) before anything else, and only then reopens
  grant calls, to the administrators of (b) and to no other principal; they
  re-grant or confirm every other principal; the gateway then appends a
  recovery record and a new durable start record and admits the tenant's
  other principals. A power loss at any point of recovery leaves the tenant
  closed (no shutdown record follows its last start record), and recovery
  starts again. A revoke whose receipt was lost as well cannot be re-applied:
  a stated limit (20). Other tenants are unaffected (their journals are
  separate files). The operational cost, every tenant closed
  after an unclean gateway restart, is a stated limit (20). On a complete
  journal, the gateway rebuilds the table from the journal and from
  `lc-<t>-authz`, taking the higher epoch of the two.
- Send fence, one per tenant, serving both revocation and erasure publication
  (review F14b; review 3, F7: the previous revision fenced sends per principal
  while erasure published under the write gate, which senders do not hold, so
  a sender that had checked the tombstone sequence could write after the
  boundary). The fence is a readers-writer lock in gateway memory. Every
  hand-off of response bytes to the kernel takes its shared side and, while
  holding it, re-checks the principal's epoch and, for a response that read
  tenant content, the tenant's published tombstone sequence: if a boundary
  published after the response's reads covers any item the response carries
  (an in-memory comparison of the leaf ids, node versions and subject tokens
  the response recorded at read time with the scopes and redact-set ids of
  the tombstones published since; 5.9, 14.3), the response is aborted: its
  unsent bytes are discarded and the request ends with a typed failure,
  counted, INV-11, which the client may re-issue; a boundary that covers
  nothing in the response, such as one in a class outside the reader's read
  set, changes nothing. Still holding the shared side, it makes one
  non-blocking socket write; whatever the kernel does not accept stays in a
  gateway-owned buffer and is sent by later hand-offs, each with the same
  check under the fence. The exclusive side (writer-preferring, so new
  hand-offs cannot starve it) is taken for two in-memory steps only: `revoke`
  (flip the epoch, discard that principal's gateway-owned unsent buffers;
  its connections are closed after the fence is released) and an erasure's
  publication step (advance the published tombstone sequence, 14.3 step 3).
  No journal, disk, Elasticsearch or blocking socket I/O is done while either
  side is held, so a slow or stalled reader cannot hold the fence; its
  duration is bounded by the gateway's own code (target under 1 ms), not by
  any peer. Lock order: the fence is the innermost lock. A thread may take it
  while holding the tenant's write gate (only the erasure publication step
  does); no thread takes the write gate, or any other lock, while holding the
  fence. Because one fence serves both uses, there is no second fence to
  order. Consequence: a hand-off's check and its write are atomic with
  respect to an epoch flip and to an erasure boundary. After either, no byte
  of a revoked principal's response or of a covered response is handed to
  the kernel; only bytes handed to the kernel before it may still reach the
  peer. Those the kernel still holds for the connection at the flip or
  boundary (its output queue, which the gateway reads) are at most the
  at most 64 KiB per connection, enforced by the hand-off itself (main's
  decision after probe LC-PB, ASSUMPTION): under the shared side of the fence
  the hand-off reads the connection's queued byte count (`SIOCOUTQ`) and makes
  one non-blocking write of at most 64 KiB minus that count, and none when the
  count is 64 KiB or more. The send buffer size alone does not bound it:
  Linux doubles the value given to `SO_SNDBUF`, and with an effective 64 KiB
  the kernel queue was measured above 64 KiB (106,112 bytes on TCP loopback,
  68,096 on a unix stream socket; docs/PROBES.md). The gateway still requests a
  32 KiB send buffer on every accepted connection (an effective 64 KiB on
  Linux), so at most the tenant's
  open-connection cap (15) times 64 KiB in aggregate. The bound is measured on
  loopback (TCP and unix) only and is not established for remote peers, TLS or
  peer-side autotuning (20). On a TCP connection,
  bytes already moved into the peer's receive buffer before the flip or
  boundary are outside this bound and the peer may read them later, so the
  bound is measured at the gateway (its hand-off trace and each connection's
  output queue at the flip or boundary), not at the peer. A test hook pauses a
  sender after its final check and before its socket write, so the broken
  variant whose hand-off does not hold the fence fails deterministically
  (EXPERIMENT 9.3 R1, 9.4 E8).
- Checks: every gateway request at admission, every fan-out wave, and every
  hand-off under the fence read the epoch from the in-memory table.
- The revoked principal has no write path to the epoch table or to
  `lc-<t>-authz`; only the gateway's authz key for that tenant can write it,
  and only on a call by an administrator, a revoker or the operator.
- Admission control reserves a separate lane for `revoke` (its own semaphore
  and queue), so a flood from any principal cannot delay it (LAB-R5).
- Who can revoke whom (review F14c, F14d): a revoker revokes non-administrator
  principals; revoking an administrator or a revoker needs two distinct tenant
  administrators; no principal can grant anything to itself; the operator's
  break-glass revoke reaches any principal of any tenant and cannot be removed
  by any tenant role. A compromised administrator therefore cannot revoke the
  revokers alone, cannot raise its own rights, and can always be revoked by two
  other administrators or by the operator.
- Removing one class grant (review F14e) is a revocation of that class: it bumps
  the principal's epoch under the same fence, so every in-flight fan-out stops
  at its next check, and the next request re-derives the read set without that
  class.
- Bound (target, measured in EXPERIMENT 9.3): after `revoke` returns, the
  revoked principal is handed no further recall result, lookup content or
  digest; a write admitted before the revoke may still commit within the
  write timeout (ASSUMPTION: 30 s); in-flight model calls finish but their
  output is discarded at the next check.
- Tenant-wide revocation (offboarding, key compromise): the gateway drops its
  clients for that tenant at once (it is the only client, so this alone stops
  every gateway path), and the tenant key owner (4.3) invalidates the tenant's
  Elasticsearch API keys (SOURCE: "Invalidated API keys fail authentication",
  https://www.elastic.co/docs/api/doc/elasticsearch/operation/operation-security-invalidate-api-key;
  latency not documented: PENDING_REAL_TEST P6). Rotation and invalidation use
  the tenant key owner, not the offline bootstrap credential (review F14f;
  PENDING_REAL_TEST P15: a user with index privileges on `lc-<t>-*` and
  `manage_own_api_key` can create, rotate and invalidate its own keys; keys
  created by it carry at most its own privileges; fallback: the operator keeps
  the bootstrap credential reachable for rotation, and the gap is reported).
  All keys get a short expiry (ASSUMPTION: 7 days, rotated daily), since keys
  never expire by default (SOURCE: create-API-key reference).

## 5. Data model

Every document in every index carries these common fields (INV-1, INV-4):

| Field | Type | Meaning |
|---|---|---|
| `tenant` | keyword | tenant id (also in the index name) |
| `cls` | keyword | access class (also in the index name) |
| `kind` | keyword | leaf, src, node, edge, log, sq, note, cache, manifest, head, tomb, member |
| `lin_leaves` | keyword[] | source leaf versions `leaf_id:lv` (empty for a leaf itself) |
| `lin_leaf_ids` | keyword[] | the same leaves without version, so an item erasure matches all versions |
| `lin_nodes` | keyword[] | child node versions it was derived from (upper index nodes only; their leaf lineage is transitive through these) |
| `lin_subjects` | keyword[] | subject tokens of every author and mentioned person in the sources, plus, for anything a query shaped, the asker's token and the token of every person the query text mentions (review F17) |
| `lin_queries` | keyword[] | search ids whose logs shaped it (for reorganised artefacts, `co_used` edges, notes, stored queries and log rows) |
| `derivation` | keyword | job id and build id that produced it |
| `erasure_rewrite` | long | 0, or the tombstone sequence that forced a rewrite |
| `created_at` | date | UTC |
| `schema_v` | short | schema version |

Subject tokens are `HMAC-SHA256(tenant_subject_key, canonical_subject_id)`,
truncated to 128 bits, with a per-tenant key held in the keystore, so tokens
do not link a person across tenants (LAB-R10: deterministic tokens linked a
subject across authors). Canonicalisation before the HMAC (review M1): Unicode
NFKC, then full case folding, then a confusables map (Unicode TR39 skeleton;
SOURCE https://www.unicode.org/reports/tr39/, read 2026-11-19), then whitespace collapsed, so a
variant spelling of an erased identifier maps to the same token.

Subject detection runs on every text the system stores or acts on: leaf text
at write (7.3) and query text at log time (9). A query's subject tokens (the
asker plus every person it mentions) are written into `lin_subjects` of its log
rows, its stored query, its cache entries, its notes, and every artefact it
later shapes (an artefact whose `lin_queries` or `evidence` names it).

### 5.1 Source log (`lc-<t>-src`)

- Append-only. One document per leaf version: `_id = leaf_id:lv`, ciphertext
  of the leaf text (AES-256-GCM), nonce, key reference, `cls`, `lin_subjects`,
  `valid_from`, `ingested_at`, `source_ref` (channel, session, turn range),
  and `src_seq`, the source sequence: one monotone counter per tenant,
  allocated at store (7.4) and shared by all classes, so any two leaves of a
  tenant compare.
- The data key for each leaf version is a row in the keystore keyed by
  (tenant, leaf_id, lv) and listing its subject tokens. Destroying the row
  makes that version unreadable in the source log and in every snapshot of it.
- This is the source of truth. Every projection below is rebuildable from it
  at a recorded watermark (SOURCE recommendation: build from source at a
  watermark, because reindex is not a consistent snapshot,
  https://www.elastic.co/docs/reference/elasticsearch/rest-apis/reindex-indices).

### 5.2 Leaves (`lc-<t>-<c>-leaf`)

- A leaf is a contiguous segment of one source session, at most
  `LEAF_CAP = 1,536` tokens (ASSUMPTION; research suggests 600 to 1,500,
  GraphRAG used 600 with 100 overlap, SOURCE https://arxiv.org/html/2404.16130v2).
  The cap is never exceeded (section 7.2).
- `_id = leaf_id:lv`. `leaf_id` is 128 random bits, stable for life, never
  reused. `lv` is an integer version.
- Fields: plaintext `text` (english analyser), `title` (the code-built title
  of INV-7: the leaf's top 4 tf-idf terms, leaving out every token that
  contains a digit, set at store, 7.4), `entities` keyword[],
  `size_tokens`, `embedding` dense_vector (384 dims, cosine, `index_options`
  pinned to `int8_hnsw`; SOURCE: dense-vector reference says `bbq_disk`
  needs Enterprise, so never rely on the default), `embed_model` (id and
  revision), `valid_from`, `valid_to`, `supersedes`, `session_ref`,
  `content_hash`.
- The projection holds plaintext and a vector. Key destruction therefore does
  not erase it; erasure deletes and purges it (section 14). Vectors are
  treated as content (SOURCE: embeddings can be inverted to near-exact text,
  https://arxiv.org/pdf/2310.06816.pdf, read via summary).
- A leaf head document in the class's control index (`lc-<t>-<c>-ctl`,
  `_id = lh:leaf_id`; per class since review M3, so a key of a lower class
  cannot see that a higher-class leaf exists)
  holds `current_lv`, `cls`, `status` (live, superseded, tombstoned) and is
  updated with `if_seq_no` and `if_primary_term`
  (SOURCE: https://www.elastic.co/docs/reference/elasticsearch/rest-apis/optimistic-concurrency-control).

### 5.3 Index families

- An index family is one tree over the leaves of one class. Several families
  overlap over the same leaves (AGREED):
  - `topic`: model-summarised clustering tree; the primary family.
  - The tree of class c may also take, as members added by c's own
    reorganiser, leaves of classes that c dominates (section 12); it never
    takes leaves of a class c does not dominate.
  - `entity`: entity groups to leaves; node summaries are deterministic lists
    of entity names and counts, built without model calls (ASSUMPTION).
  - `time`: day, week, month, year buckets (SOURCE design: H-Mem,
    https://arxiv.org/html/2605.15701). Built in the product; excluded from
    the LongMemEval experiment because that dataset's dates are synthetic per
    question (EXPERIMENT 3.4).
- Within the topic family a leaf may belong to up to `M_MAX = 2` parents
  (soft membership; SOURCE: RAPTOR's soft clustering, whose measured gain was
  0.8 points on one dataset, so it is capped; the cap is ASSUMPTION).
- Membership is stored as member documents (`kind=member`, parent node id,
  child id, score, `shaped_by` placement or a reorganiser job with its
  `lin_queries`, a binding to generations, and `gen_to`), not inside the leaf,
  so moving a leaf never rewrites the leaf. The binding is `src_seq` for a
  member written by placement (7.5; the source sequence of the placed leaf) and
  `gen_from` for a member written by a batch rebuild (7.6) or a reorganiser
  (12, 13.3; the generation that wrote it). `gen_to` is the set of generation
  numbers that retire the member (a move, split, merge or collapse); a number
  of a rejected candidate in that set has no effect. Visibility follows 13.2.

### 5.4 Index nodes (`lc-<t>-<c>-node`)

- `_id = node_id:nv`. `node_id` is 128 random bits; `nv` is an integer
  version. A changed node is a new version; unchanged nodes are shared between
  generations (path copying).
- Fields: `family`, `level` (1 = directly above leaves), `children` (list of
  `{child_id, child_version, is_leaf, title, gist, entities, size_tokens,
  total_tokens, last_seen}`), `summary` text, `summary_embedding`,
  `facts` (short extracted verbatim sentences with their leaf id; SOURCE:
  Extract-Support keeps source sentences in merges, 72.7% vs 59.1% claim
  correctness, https://arxiv.org/html/2502.00977v1), `child_count`,
  `total_tokens` (sum over the subtree), `summary_status` (ok, stale,
  failed), plus the common lineage fields: a level-1 node carries
  `lin_leaves`, `lin_leaf_ids` and `lin_subjects` of its child leaves; an
  upper node carries `lin_nodes` (its child node versions) and reaches leaves
  transitively, so the root does not hold every subject token of the tenant.
  Upper-node facts copied from children carry the source leaf id, and that
  leaf's subjects are added to the upper node's `lin_subjects`.
- The child entries are the "index"; the summary is what the node's parent
  shows for it. Sizes and totals are computed by code, never by the model
  (ASSUMPTION). `last_seen` is stored for the reorganiser only and is never
  shown to a holder (review F5: it would let recency steer routing).
- Fan-out limits: `F_TARGET = 16`, `F_MAX = 24`, `F_MIN = 4` children per
  node (ASSUMPTION; research synthesis suggests 8 to 20 so that a node's child
  entries fit one holder context; GraphRAG found an 8k context best of
  8k to 64k, SOURCE https://arxiv.org/html/2404.16130v2).
- Index-of-indexes rule (OWNER "when the index itself gets large, segment it
  and index it"): when a family's top level has more than `F_MAX` nodes, or
  their summaries together exceed the holder context budget
  `H_CTX = 12,000` tokens (ASSUMPTION), group them under a new level. When the
  top level has one node, drop it. Depth is therefore an outcome, not a
  constant (contrast MemTree, which reached depth 13 with branching 2.1 from
  thresholds alone, SOURCE https://arxiv.org/html/2410.14052v3).

### 5.5 Edges (`lc-<t>-<join class>-edge`)

- One document per directed edge version: `_id = edge_id:ev`,
  `edge_id = sha256(tenant, src_id, dst_id, type)` truncated to 128 bits, so
  a re-created edge gets the same id and a new version. After an erasure, a
  re-created edge's version counter starts above the highest erased version,
  which the tombstone records (review M1), so an old pointer to an erased edge
  version never resolves to new content.
- Fields: `src_id`, `dst_id` (stable leaf ids, or node ids for hub edges),
  `src_lv`, `dst_lv` at creation, `type`, `weight`, `confidence`, `model`,
  `hit_confirmations`, `last_confirmed`, `half_life_days`, `evidence`
  (search ids, judge ids), `valid_from`, `valid_to`, the generation binding
  (`src_seq` or `gen_from`, and `gen_to`; review F18), plus lineage. Stored in the edge index of
  the join class of both endpoints and the evidence (INV-3). Section 16 has the
  rules.
- Edge versions are bound to generations of their class's topic family, like
  members, in one of two ways. An edge written on insert (7.7) carries
  `src_seq`, the source sequence of the leaf that caused it, and becomes
  visible to tasks pinned after the write-path watermark P (5.6) covers that
  sequence. It is not given a generation number, so a candidate that takes the
  next number and is then rejected cannot strand it. An edge written by a batch
  rebuild (hub edges) or a reorganiser gets that generation's number as
  `gen_from` and becomes visible only once that generation is in the reader's
  ancestry (a reorganiser's only if the gate adopts it). Retirement of any edge
  (decay, supersession) adds the retiring generation to its `gen_to` set.
  Readers filter edges by the rule of 13.2, so link expansion never reads "the
  latest edges".

### 5.6 Generations, manifests and heads (`lc-<t>-<c>-ctl`)

- A generation is one consistent version of one family of one class. Its
  manifest (`_id = mf:<family>:<gen>`, in the class's control index) lists the
  top-level node versions, the source watermark W (highest source sequence
  whose placement the build summarised; a batch rebuild sets it to the
  write-path watermark P read at its start, and a reorganiser candidate copies
  its parent's W, also on rebase, so W never decreases along an ancestry), the
  parent generation, its ancestry (every adopted generation number of that
  class and family since its first generation), status (building, candidate,
  live, rejected, retired), the builder job, and the tombstone sequence it
  honours. Generation numbers are allocated by a per-class counter and never
  reused.
- Ancestry ids are kept for the life of the class, independently of content
  retention (review 2, F5): they are a list of integers, one per adopted
  generation, and the visibility rule (13.2) needs all of them, because an
  adopted edge or member can outlive its `gen_from` generation and a retired
  one must stay retired after its `gen_to` generation expires. Retention expiry
  (13.2) removes the content of expired generations (their unshared node
  documents), never ancestry ids.
- The head (`_id = hd:<family>`, in the class's control index) names the live
  generation and is flipped only by compare-and-set on
  `if_seq_no`/`if_primary_term`.
- The write-path watermark P (`_id = wp`, one per tenant in `lc-<t>-ctl`,
  advanced only by compare-and-set) is the highest source sequence s such that
  every write-path job (placement 7.5, link creation 7.7) for every sequence up
  to s has finished: written, or failed and counted (INV-11). A job still open
  after `D_WP` (10 minutes, ASSUMPTION) is cancelled in process and joined, so it can write
  nothing more; then the documents it wrote are deleted, it counts as
  failed, the searchable-publication refresh below runs after those deletes,
  and only then does P move past it. P never decreases. Because the
  source sequence is per tenant, one P serves every class, including edges
  stored in a join class.
- Searchable publication (review 2, F4). Members and edges are found by search
  (terms queries), which sees a write only after a refresh. So P is advanced,
  and a generation head is flipped (by a build, an adoption or a rollback),
  only after an explicit `_refresh` of every member, edge and node index that
  the covered writes touched (including `gen_to` retirement updates) has
  returned. Acknowledgement of a write is never taken as visibility. With this
  barrier, a task that pins P and a head sees the same members and edges for
  its whole life (13.2). PENDING_REAL_TEST P5 (extended): a refresh that has
  returned makes every write acknowledged before it searchable.
- Readers pin a generation by reading the head once per task, and read P once
  at the same time; everything below the roots is reached by explicit
  versioned ids, so a pinned reader sees an unchanging tree regardless of later
  flips and later writes.
- DEVIATION from the research reader's suggestion of one physical index per
  structure version: a physical index set per generation multiplies shards by
  retained generations (tenants x classes x kinds x generations against the
  1,000-shard per-node limit the earlier lab hit; SOURCE limit:
  https://www.elastic.co/docs/deploy-manage/production-guidance/optimize-performance/size-shards).
  Path-copied immutable documents in one index per kind cost no extra shards
  per generation. Section 6.4 has the numbers.

### 5.7 Protocol log (`lc-<t>-<c>-log`) and stored queries (`lc-<t>-<c>-sq`)

- Section 9 gives the row. Rows are immutable. The log row's class is the join
  of the query's class (the class of the channel the reader acts in) and the
  offered item's class (INV-3).
- Stored queries: one document per past search, with a `percolator` field
  holding a lexical bool query built from the search text, the query
  embedding as a dense_vector, the actor's subject token, the tokens of the
  persons the query mentions, and lineage. Evaluation probes held back for
  scoring and gate-only synthetic queries are never stored here.
  Percolation is lexical only (SOURCE and LAB: the percolator pages do not
  mention kNN, and the earlier lab found vector search inside the percolator
  did not work); the semantic half of reverse search is kNN over the stored
  query vectors (section 12).

### 5.8 Reflection notes, judge outputs and model cache

- `lc-<t>-<c>-note`: reflection notes and judge labels, each with lineage
  (leaf versions judged, search ids, subject tokens of query authors).
- `lc-<t>-<c>-cache`: cached model outputs keyed by
  `sha256(tenant, cls, task, prompt_version, model_id_pinned, scope, inputs)`.
  The model id is pinned to a full model identifier, never a floating alias
  (LAB-R10: floating tags made a pin meaningless). Review F19a:
  - `cls` is the join of the classes of everything in the call: the prompt's
    class (the channel's), the node's class and every input's class. The entry
    is stored in that class's cache index, so a restricted-channel route over a
    base node is never readable by a base reader.
  - `scope`: prompt-free tasks (`build_l1`, `build_upper`, `type_edges`) are
    cached per (tenant, class) and shared across principals. Prompt-bearing
    tasks (`route`, `organise`, `ask`, `reader_answer`, `query_variants`) are
    cached only per principal (scope = the principal's token), so one reader's
    cache hits cannot reveal another reader's queries through timing. Judge,
    synthetic-query and gate calls run as the class's service principal and are
    scoped to it.
  - Prompt-bearing entries carry the `lin_subjects` of their query, so erasure
    reaches them (14.2).
  - Children are presented to the model as local labels (c1 to cN) mapped back
    to ids by code, so prompts carry no ids and identical content gives an
    identical key.

### 5.9 Tombstones (`lc-<t>-ctl`, `kind=tomb`, and the tombstone journal)

- `_id = tb:<seq>`, monotonically increasing per tenant (allocated by a
  compare-and-set on a counter document). Scope: subject token, leaf id, leaf
  version, or (leaf id, old class) for a relabel. Also holds erased identifier
  HMACs so a later re-scan of new content can still detect the erased subject
  (LAB-R4; the trade-off is that keeping an HMAC lets the erasure role confirm
  that a given identifier was erased; open question O-5), and the highest
  erased version of each erased edge id (5.5). Tombstones stay in the tenant's
  `lc-<t>-ctl` (they hold ids and HMACs, no class and no content), are
  appended to the tenant's tombstone journal at the erasure boundary (14.3,
  14.6), and are held in gateway memory for the guard (loaded from the journal
  at start), together with the redact-set node ids of each tombstone until
  they are cleared (14.3 step 7), so the send check of 4.4 needs no I/O.

### 5.10 Authz indices (`lc-<t>-authz`) and the tenant registry (`lc-sys-registry`)

Revised after review F15 (the first version had one `lc-sys-authz` index for
all tenants, outside the erasure closure).

- Per tenant, `lc-<t>-authz` holds sessions (minted only by an identity
  adapter from an authentication proof, never from a bare principal id;
  LAB-R10), grants, and the best-effort copy of authority epochs (4.4).
  Principals are stored as per-tenant HMAC tokens (`HMAC(tenant_principal_key,
  canonical principal id)`), never as raw identifiers; session tokens are
  stored only as their sha256. Every authz document carries `lin_subjects`
  (the principal's subject token), so it is inside the erasure closure (14.2).
  Readable and writable only with that tenant's gateway authz key.
- A session token is `<tenant id>.<random 256 bits>`, so the gateway knows
  which tenant's authz index to read before it reads anything; a token whose
  tenant prefix does not match the record it resolves to is refused.
- `lc-sys-registry` is the one global index: tenant id, status, tier, created
  time. No principal, session, grant or count. Writable only by the operator.

## 6. Elasticsearch layout on the Basic tier

### 6.1 What Basic gives and does not give

| Feature | Basic? | Status |
|---|---|---|
| BM25, kNN with filter, quantised HNSW, rescore, lexical percolator, aliases, force merge | yes | LAB observed on a Basic node; re-checked by P1 |
| RRF and linear retrievers | no (licence error) | LAB observed; SOURCE forum post https://discuss.elastic.co/t/use-of-retrievers-in-the-search-api/379145. Fusion is client-side RRF, k = 60 |
| semantic_text, inference endpoints, ELSER, `bbq_disk` | no | LAB observed. Embeddings are computed outside Elasticsearch |
| Document- and field-level security | no | LAB observed licence error; SOURCE pricing text lists it as paid. Not used |
| Role-based access with index-name privileges, API keys | yes per vendor blog (https://www.elastic.co/blog/security-for-elasticsearch-is-now-free) | PENDING_REAL_TEST P2: never run with security on in the earlier lab. This is the hard tenant boundary |
| Graph explore API | tier unknown | not used: it infers co-occurrence, the design needs explicit typed edges |
| Atomic multi-action `_aliases` | unverified | PENDING_REAL_TEST P8 |
| kNN `filter` applied during search | unverified | PENDING_REAL_TEST P7 |

### 6.2 Index names

`lc-<tenant>-<class>-<kind>` for class-scoped kinds (leaf, node, edge, member,
log, sq, note, cache, ctl) and `lc-<tenant>-<kind>` for src, ctl (tombstones,
counters) and authz; `lc-sys-registry` is the only index without a tenant
(5.10). The class control index holds leaf heads, generation heads,
manifests, forwarding records, redact sets and dirty queues of that class
(review M3). Tenant and
class ids are lowercase `[a-z0-9]{4,16}` with no hyphen, so no name can be a
prefix of another tenant's pattern (ASSUMPTION; probe P2 includes a
look-alike tenant name). Each name is an alias to a physical index
`...-p<8 random hex>` so the copy-and-swap purge (14.5) can replace it. The suffix
is random and never a counter: probe P2 found that `_mget` on an alias name
returns the concrete index behind it to a key that may not read it, and a
sequential suffix would then disclose how often an index was swapped (main's
mitigation under the P2 "accept with mitigations" ASSUMPTION; the gateway also
never forwards an Elasticsearch error body, sub-response or index name, 8.7).

### 6.3 Keys and roles

- Per tenant, the gateway holds separate API keys for: read, write-ingest
  (src, leaf, class ctl), structure (node, edge, member, class ctl), log (log,
  sq, note, cache), erase (delete on all of the tenant's indices), and authz
  (`lc-<t>-authz` only). Each role descriptor names only `lc-<tenant>-*`
  patterns (never `lc-*`) and grants no cluster privilege at all, so the
  cluster-level APIs (`_cluster/state`, `_cat/shards`, `_tasks`,
  `_resolve/index`, `_search_shards`, `_nodes/stats`) are refused to every
  gateway and tenant key (review F19e; checked by EXPERIMENT 9.1 T1 with a
  broken control holding `cluster: monitor`). The gateway reads its tenants'
  index statistics with index-level `monitor` on `lc-<t>-*` only.
- Per class, read keys are further restricted to that class's indices and
  that class's control index, plus the tenant's `lc-<t>-ctl` (tombstones: ids
  and HMACs, no class, no content). They never reach `src` (ciphertext; only
  write-ingest and rebuild keys need it). The gateway selects the key from the
  reader's read set. Restated claim (review M3): a gateway bug that built the
  wrong query with a base read key cannot read restricted content, summaries,
  edges, leaf heads, generation heads, manifests or redact sets; it can read
  the tenant-wide tombstone records, which carry no class and no content
  (defence in depth; ASSUMPTION; key count per tenant = roles x classes, at
  most about 30).
- The verifier has its own read-only key per tenant, created by the operator,
  never readable by the gateway's OS user (LAB: per-role secrets and a
  separate verifier service).
- Key files are 0600, outside the repository, read by path (never argv or
  environment values) (LAB pattern).

### 6.4 Shard budget

Per dedicated tenant: kinds per class = 9 (leaf, node, edge, member, log, sq,
note, cache, ctl), per tenant = 3 (src, ctl, authz). With one primary and `r`
replicas:

`shards_per_tenant = (9 x C + 3) x (1 + r)`

| Setting | C | r | shards per tenant | tenants per node at 1,000 shards (minus about 20 system shards) |
|---|---|---|---|---|
| Test cluster (single node) | 2 | 0 | 21 | 46 |
| Recall experiment tenant | 1 | 0 | 12 | 81 |
| Production example | 3 | 1 | 60 | 16 |

Retained generations add no shards (5.6). A copy-and-swap purge adds one
shard per swapped index for its duration. The test cluster holds the three
security fixtures `ta`, `tb` and the look-alike `tbx` (each C = 2): 63 shards,
plus `lc-sys-registry` and system indices. The experiment runs on the remote
host, one tenant per arm (EXPERIMENT 10.1), at most 12 experiment tenants at
a time: 144 shards. Provisioning refuses a tenant that would take the node
above 80% of `cluster.max_shards_per_node` (ASSUMPTION threshold).

### 6.5 Settings

- `refresh_interval` 1 s; `number_of_replicas` 0 on the single-node test
  cluster; dense vectors 384 dims, cosine, `int8_hnsw`. Correctness never
  relies on the periodic refresh: publication of P and heads (5.6) and the
  erasure boundary (14.3) refresh explicitly first.
- `index.merge.policy.expunge_deletes_allowed` set to 0 on erasable indices if
  accepted (PENDING_REAL_TEST P9; fallback copy-and-swap).
- `search.allow_expensive_queries` must stay true for percolate (SOURCE:
  percolate query page); the gateway is the only client, so this does not open
  expensive queries to tenants.
- Snapshots include only `src`, the tenant and class `ctl` indices, `authz`
  and `lc-sys-registry`. Projections are excluded: they are rebuilt from
  source on restore, after the tombstone journal is applied (section 14.6).
- Logs (review F19d): search and indexing slow logs are disabled explicitly on
  every index (thresholds `-1`); Elasticsearch logger levels stay at levels
  that do not print request bodies; the gateway logs ids, typed outcomes and
  hashed tenant-scoped identifiers, never request or response bodies, prompts
  or leaf text. Elasticsearch, container and gateway log files are plaintext,
  so the verifier greps them for canaries (14.7). Because they hold no content,
  what an erasure leaves in them is at most a subject or principal token; log
  files are rotated and deleted after `R_LOGS = 7 days` (ASSUMPTION), a bound
  stated in every receipt.
- Per-tenant stored-byte caps (15) keep the sum of caps below the disk's
  flood-stage watermark, so one tenant cannot drive the node read-only.

## 7. Write path

All steps run inside one gateway session bound to (tenant, principal,
channel). Each step names the isolation, access, erasure and versioning check
it performs.

### 7.1 Admission (every request)

1. Read at most `MAX_LINE = 1 MiB` (ASSUMPTION) before parsing; count JSON
   structures before parsing and refuse past a limit; one semaphore around
   parse and dispatch; idle deadline; bounded per-tenant queue and slots
   (LAB-R5).
2. Resolve the session (realtime GET in `lc-<t>-authz`, the tenant taken from
   the token's prefix), check the authority epoch in the gateway's table (4.4), derive
   tenant, principal, channel, write set and read set. Refuse any body field
   that names a tenant, principal or class.
3. Idempotency key scope = (tenant, principal, key) (LAB ASSUMPTION carried
   over); a retried `remember` with the same key and the same body hash returns
   the first result; the same key with a different body hash is refused with a
   typed error (review M1).

### 7.2 Segmentation (OWNER "segment topics to maximum context sizes")

1. Cut first at session boundaries and time gaps over 30 minutes (free;
   ASSUMPTION threshold).
2. Within a session, cut at embedding-similarity valleys between adjacent
   round windows (TextTiling-style; SOURCE: no segmentation method wins on all
   metrics, https://arxiv.org/html/2601.12061v2; method choice ASSUMPTION and an
   ablation).
3. If a segment is still over `LEAF_CAP`, split at the round boundary nearest
   the middle, then at a turn boundary, then at a sentence boundary; a single
   sentence over the cap is split at a token boundary. Pieces of a split are
   linked by `continues` edges.
4. Token counts use a conservative bound of `ceil(chars / 2.4)` until the
   real tokenizer is wired (LAB measured 2.47 characters per token where
   chars/4 was 13% wrong; ASSUMPTION).
5. Empty or whitespace-only segments are refused with a typed error.

### 7.3 Subjects and class

- Subjects of a leaf = its authors plus persons mentioned, found by the
  tenant's identifier declarations (ASSUMPTION carried from the earlier lab:
  minimum length, stop-list, review) and by the entity list from the build
  call. Mention detection misses spellings it was never told about; this is a
  limit (section 20).
- Before writing, the shared tombstone guard (INV-8) checks the leaf's
  subject tokens and erased identifier HMACs (after canonicalisation, 5);
  content naming an erased subject is refused with the same typed error as any
  other policy refusal, which does not say that a name was erased.
- Name collisions (review M1): an identifier declaration names a subject by
  identifier plus context (for example an email address, a handle, or a name
  scoped to an organisation), never by a bare common name. Erasing one "John
  Smith" therefore tombstones that declaration's identifiers, not the string
  "John Smith". Content that matches an erased identifier only on a bare name
  without the declared context is held in a quarantine queue for review by a
  principal of the tenant who holds the leaf's class, instead of being
  silently refused; the writer sees the same typed "held" outcome as any other
  hold (ASSUMPTION; the queue is tenant- and class-scoped and inside the
  erasure closure).
- Class = the channel's class; the writer must hold it in its write set.

### 7.4 Store

1. Allocate the leaf's `src_seq` (per-tenant counter in `lc-<t>-ctl`,
   compare-and-set). Generate a data key; keystore stores it with (tenant,
   leaf_id, lv, subject tokens).
2. Append the ciphertext source document (`lc-<t>-src`).
3. Embed outside Elasticsearch (stub or E5; section 18.3).
4. Index the plaintext leaf projection with lineage and its code-built title
   (5.2) (`lc-<t>-<c>-leaf`).
5. Create or advance the leaf head with compare-and-set.
6. Enqueue the leaf for placement and linking (a dirty-queue document in ctl).

A partial failure leaves an orphan that the next step can detect: a source
document without a projection is re-projected; a projection without a source
document is deleted (the projection is never the source of truth). Bulk
responses are checked item by item.

### 7.5 Placement (incremental)

- A new leaf is visible to the direct leaf search as soon as step 7.4 is
  refreshed (1 s), before it is placed in any tree. This is the second job of
  the safety net (AGREED).
- Placement descends the topic family of its class by cosine similarity
  between the leaf embedding and node summary embeddings, with thresholds that
  rise with depth (SOURCE design: MemTree theta(d) rising with depth; H-Mem
  0.8, 0.7, 0.6 per level; values ASSUMPTION, tuned on development only). It
  joins up to `M_MAX` level-1 nodes whose similarity clears the threshold; if
  none does, it starts a pending new node.
- Placement writes member documents bound by the leaf's `src_seq` (5.3); it
  changes no node. A member is pending for a reader pinned at G when its
  `src_seq` is above W(G), that is, placed but not yet summarised by a batch
  rebuild; it is visible to holders as soon as P covers it (8.2, 13.2).

### 7.6 Batch rebuild (summary update, split, merge, move)

Runs per (tenant, class, family) under a lease document (compare-and-set,
with expiry) so one builder runs at a time.

1. Read the head generation G and the write-path watermark P; the new
   generation's W is that P (5.6).
2. For each level-1 node with pending members: one build call over the full
   text of its new children (at most `F_MAX` leaves, about 18k to 37k tokens)
   returns, for each new child, a one- or two-line gist, entities and up to
   three verbatim facts (holder input only, INV-7); the child's title is its
   code-built leaf title (5.2), never written by the model (review 3, F11);
   the node summary is refreshed when it has 4 or
   more new children or its size changed by 25% or more (ASSUMPTION; SOURCE
   pattern: update only the ancestor path, HLTM,
   https://arxiv.org/html/2604.26197v1). Otherwise the node gets a new version
   with the new entries and `summary_status=stale`.
3. Split a node over `F_MAX` children (2-way k-means on child embeddings,
   balanced); merge a node under `F_MIN` into its most similar sibling;
   collapse single-child chains. Each produces new node ids; a forwarding
   record maps old node id to new node ids for G+1. Members of the old nodes
   get G+1 added to their `gen_to`; members of the new nodes are written with
   `gen_from` = G+1 (5.3).
4. Propagate new versions up the ancestor path (path copying), each new
   node version's title code-built from its summary (INV-7, 8.2); apply the
   index-of-indexes rule at the top.
5. Write the manifest for G+1 (status building), refresh the indices written
   (searchable publication, 5.6), then compare-and-set the head from G to
   G+1. If the compare-and-set fails, another builder won: this builder
   discards its unshared node versions and re-runs from the new head
   (rebase). Leaves written after W stay pending for the next batch.
6. The commit (the writes of steps 3 to 5) runs under the shared side of the
   tenant's write gate (14.3). Before the flip, the builder checks the
   tenant's published tombstone sequence: if any tombstone newer than the one
   it started with intersects its inputs' lineage, it aborts.

A generation never publishes a W that it does not represent (review 2, F6).
If the build call for any level-1 node with pending members up to W fails
(timeout, refusal, malformed output), the whole generation is aborted: no
head flip, its unshared documents are deleted, and the batch is retried
later. The members stay pending (their `src_seq` is above the live W), so
holders still see them as pending entries and tree-only recall still reaches
them; the direct search finds them as before. Failures are counted (INV-11).
A node whose build keeps failing therefore holds W back for its class: the
tree stays complete but its gists and summaries stay stale (limit, 20).

### 7.7 Link creation on write (OWNER "relational cross linking";
"run background searches ... while doing memory addition/updates")

For each new leaf, in the background: candidates are its top-10 kNN neighbours
within the leaves its class may link to, plus leaves sharing an entity
(SOURCE: A-MEM's k = 10, https://arxiv.org/html/2502.12110v8); the top 3 by
similarity are typed by one model call per 20 pairs (the call sees each
pair's titles, gists and facts, not full text); the rest may get an
untyped `related` edge from similarity alone (ASSUMPTION). Supersedes and
contradicts candidates are checked in the same call (section 16.3). Never
rewrite a neighbour in place (A-MEM does; that conflicts with INV-6). Edges
written here carry `src_seq` and become visible to tasks pinned after the
write-path watermark P covers them (5.5, 5.6, 13.2).

## 8. Read path (the owner's fan-out)

### 8.1 Entry

`recall(prompt, budget)` from the originating agent. The recall service:

1. Gets the reader's read set from the session (gateway).
2. Pins, for each class in the read set and each family, the live generation,
   and the tenant's write-path watermark P (each read once; the pin is
   returned in the digest so delegates use the same structure).
3. Starts three things in parallel: the tree fan-out (8.2), the direct leaf
   search (8.5), and, as soon as either yields leaves, bounded link expansion
   (8.6).

All three touch only indices of classes in the read set (INV-2).

### 8.2 Fan-out over index holders

- An index holder is one index node. Asking a holder is one fresh, stateless
  model call (ASSUMPTION: no long-lived holder sessions, because a shared
  session would carry other readers' prompts; LAB lesson) whose input is the
  prompt and the holder's child entries (title, gist, entities, size, total
  size, facts; never `last_seen`), each presented under a local label c1 to cN,
  and whose output is the subset of labels that may be relevant, each with a
  short reason and a confidence 0 to 3. Code maps labels back to child ids.
- Reasons (review F11): at most 12 words by schema. A reason that shares any
  5-gram (after lowercasing and tokenising) with the facts, gists or leaf text
  in that holder's input is dropped and counted as a typed reason failure; the
  selection itself stands. Titles are never written by a model (review 3,
  F11): every title in a child entry, a pointer or the digest is code-built
  (INV-7): for a leaf, its stored `title` (5.2); for a node, the top 4 tf-idf
  terms of its summary, set when the node version is built (7.6); in both
  cases every token that contains a digit is left out, so a title has fewer
  than 4 terms, or none, when the text has fewer such terms (ASSUMPTION).
  Gists and facts are holder input only; they are never copied into what a
  holder returns. So no body, gist or fact field travels up the tree; titles
  and reasons are descriptors, and a descriptor can still convey an answer
  (INV-7, 20).
- Pending members (placed since the last batch: `src_seq` above W of the
  pinned generation and at most the pinned P, so without a gist yet;
  review M1) are shown to holders as entries with their stored code-built
  title (5.2), their size, and the flag `pending`; no gist and no facts.
- A node with more child entries than fit `H_CTX` (for example many pending
  members between rebuilds, or a node over `F_MAX` awaiting a split) is asked
  in chunks of at most `F_MAX` entries, in parallel; the chunks' answers are
  merged by code (review M1, replacing "the prompt grows").
- The question is the owner's: "which of your index holders may be relevant to
  this prompt: ___". Every top-level holder of every pinned family is asked in
  parallel (OWNER "asks all context index shard holders").
- Expansion is asynchronous, not level by level: the moment a holder answers,
  each named child that is an index node is dispatched as a new holder call
  (OWNER "expand the parallelism as each lower layer responds"). Named leaves
  become pointers.
- Output is validated against the presented child ids. An id that was not
  presented is dropped and counted as a hallucination; a malformed answer is a
  typed failure. On failure, that holder falls back to embedding routing over
  its children (cosine of prompt and child gist embeddings, top 3), and the
  fallback is marked in the log so it is never mistaken for the owner's
  method (INV-11).
- Budget (ASSUMPTION; the owner gave none): every top-level holder is always
  asked; below the top, expansion stops at `B_HOLDERS = (number of top-level
  holders) + 36` holder calls per recall, at most `B_FANOUT = 6` children
  named per holder, at most 12 organise calls, per-call timeout 45 s, and a
  total deadline. Expansion order is by holder confidence. When the budget is reached, unexpanded
  named nodes are returned as node pointers ("branch not opened") so the
  originating agent can explore them later. This is a safety budget, not a
  change to the method; reaching it is logged. Cost estimates in
  EXPERIMENT.md use 25 to 60 model calls per recall on development pools
  (about 10 top-level holders), 35 to 70 on the E2b pools and 50 to 77 on
  the whole pool (about 29) (EXPERIMENT 10.1).
- Prompt injection inside a child entry can only bias selection among the
  children shown, all of which the reader may already see; the holder has no
  tool and cannot fetch anything.

### 8.3 Parents sum and organise (OWNER)

- When all named children of a holder have answered (or timed out), that
  holder's parent step runs: code merges the returned pointers (dedupe by
  leaf id, keep the best path), sums `size_tokens` and `total_tokens`
  (OWNER "size and total size"), and, if two or more children returned
  non-empty results, one organise call groups the pointers into a short
  labelled outline with one-line reasons (OWNER "summing and organizing").
  Outline labels and reasons pass the same 5-gram check as holder reasons
  (8.2) against the facts, gists and leaf text in the organise call's input;
  a failing label is replaced by the holder path and counted.
  A holder with a single non-empty child passes its result up unchanged
  (ASSUMPTION, to save calls; ablation "organise at every parent").
- What travels up is the pointer fields of INV-7: pointer, code-built title,
  size, the path of holder ids, reasons (bounded as in 8.2) and confidences.
  No body, gist or fact field leaves the holder (review 2, F11: a
  one-sentence leaf's gist is the whole leaf, so returning gists would hand
  over content without a lookup). Descriptors can still convey an answer
  (INV-7, 20).
- Sizes, totals and counts are computed over what was presented to that
  holder, which is already trimmed to the reader's classes (INV-2). No "N
  hidden items" figure exists anywhere.

### 8.4 The digest and what the originating agent can do

The digest (the owner's "tight summary") holds: the organised outline, the
pointer list in rank order (each pointer with the fields of INV-7 only:
pointer, title, size, path, reason, confidence), sizes and the total, node
pointers for unopened branches (with the node's title from its parent's
child entry, its size and total), the pinned generations, and a section per
source (tree, direct search, linked). Every leaf pointer carries the leaf's
stored code-built title (INV-7), whichever source offered it, never its
text. Rank order (pre-registered in EXPERIMENT 5.1; review F9):
the tree list (holder confidence, then shallower depth, then path order), the
direct list (8.5) and the linked list (8.6, by edge score) are each cut at 50
and fused by client-side RRF with k = 60, ties broken by a salted hash of the
leaf id (ASSUMPTION). Exploration slots (9) are placed after this ranking.
Tools available to the originating agent:

- `lookup(pointer)`: returns the leaf text if still permitted; logged as
  opened by the gateway (not by the agent's claim).
- `ask(pointers, question)`: OWNER "immediately ask against the relevant
  segment data via linked lookup and inference": a delegate model call that
  receives those leaves (each logged as opened, actor = delegate) and returns
  an answer with the pointers it relied on.
- `explore(question, seeds, exclude)`: OWNER "delegate subagent exploration
  with deeper perspective/specific questions ... pass its own tree-traversing
  additional lookup search, where any new resources it hasn't explored can be
  explored": a recursive recall that starts at the given node pointers (or at
  the top), uses the delegate's own question, and skips pointers already in
  `exclude`, so only new resources come back. Recursion depth at most 2 and a
  shared call budget per task (ASSUMPTION).

Pointer format: `(gen, leaf_id, lv)` for leaves and `(gen, node_id, nv)` for
nodes, opaque to agents. Resolution returns one of: `ok`; `superseded`
(a newer version exists and the reader may read it; the newer pointer is
included); `moved` (node pointer of a retired generation, with its forwarding
node ids); `expired` (a retired generation with no forwarding); or
`unavailable`, which is the single answer for erased, forbidden and
nonexistent (no distinction is ever visible to a reader). A pointer to a leaf
version that was relabelled to a class outside the reader's read set returns
`unavailable` from the moment the relabel returns, never `ok` and never
`superseded` (review F19c; 14.1).

### 8.5 Safety net: direct leaf search (AGREED)

In parallel with the fan-out: BM25 over `text`, `title`, `entities` and kNN
over `embedding` on the leaf indices of the reader's classes, fused
client-side with RRF (k = 60, window 50 per list; SOURCE: Elasticsearch's own
RRF defaults, https://www.elastic.co/docs/reference/elasticsearch/rest-apis/retrievers/rrf-retriever,
copied client-side because the retriever is Enterprise-only on our tier). The
top 10 not already offered by the tree are added to the digest in their own
section, tagged with source `direct` for the log. In the final ranked list the
direct list is one of the RRF inputs of 8.4 (ASSUMPTION; EXPERIMENT also
reports the tree-only list).

### 8.6 Bounded link expansion (OWNER "distance by tree branching is not isolation")

From the leaves in the digest: at most 2 hops, at most 10 neighbours per node
per hop, at most 50 expanded leaves per recall, edge score = type weight x
weight x decay x specificity (SOURCE: HippoRAG's node specificity damping and
the 2-hop, 10-neighbour caps from the research synthesis,
https://arxiv.org/html/2405.14831v3; numbers ASSUMPTION). Two Elasticsearch
round trips per hop: a terms query on `src_id` over the edge indices of the
reader's classes, filtered by the visibility rule (13.2) of the reader's pinned
generation (13.2; never "the latest edges"), then a multi-get of neighbour
leaves. Superseded leaves are
returned with their `supersedes` partner. Expanded leaves enter the digest in
the `linked` section, tagged `edge`.

### 8.7 Leak analysis of the read path

| Channel | Why it cannot carry another tenant's or a higher class's data |
|---|---|
| Content of summaries and gists | the tree of class c holds leaves of class c and, as members added by c's own reorganiser, leaves of classes c dominates; a reader walks only the trees of classes in its read set, so every summary it sees derives only from classes it may read (construction A of the research; never filtered at read) |
| Pointers | pointer lists are built only from children presented to the holder, which are in the reader's classes; forged pointers resolve to `unavailable` |
| Sizes, totals, counts | computed by code over the presented (trimmed) set |
| Titles and entities | part of the leaf's class; shown only where the leaf is |
| Edge existence and degree | edges live in the join class's index; a reader queries only its classes' edge indices; specificity uses degree within those indices |
| Scoring statistics | per class and per tenant indices, so BM25 statistics never include hidden documents (the vendor's DLS warning does not apply because no hidden document shares an index) |
| Fan-out shape and timing | the holders asked and the number of waves depend only on the reader's pinned trees; hidden trees are never touched |
| Load on shared nodes | NOT closed: another tenant's or class's load can slow this reader on a shared node. Gateway quotas bound it; a separate cluster removes it (limit, section 20) |
| Errors | one typed `unavailable` for forbidden, erased, relabelled away and missing; no Elasticsearch error body, sub-response (for example an `_mget` per-document error) or index name is ever forwarded (probe P2, T3) |
| Logs and notes | log rows and notes take the join class (INV-3), so a low-class reorganiser never sees a high-class query |
| Model cache | entry class = join of prompt and inputs; prompt-bearing entries scoped per principal, so neither content nor cache-hit timing crosses classes or principals (5.8) |
| Holder reasons, titles, outline labels | field contract (INV-7): no body, gist or fact field is returned; titles are code-built (4 tf-idf terms, no token containing a digit); reasons and labels at most 12 words and sharing no 5-gram with the call's input (8.2, 8.3); these are descriptors of the reader's own classes and can still convey an answer about them (20) |
| Receipts | an erasure requester sees totals over its own read set only (4.3) |
| Cluster-level APIs | no gateway or tenant key has a cluster privilege (6.3) |
| Elasticsearch and gateway log files | no request bodies, slow logs off, grepped by the verifier (6.5, 14.7) |
| Edges of an unadopted candidate | invisible until the gate adopts the generation (5.5, 13.2) |

## 9. Protocol log (OWNER "tracking if the actor actually did a lookup")

One immutable row per offered item per recall, in the log index of the join
class (5.7):

`search_id, ts, tenant, cls, actor (subject token), actor_model (pinned id),
query_class, prompt_hash, generation pins, pointer (leaf_id, lv), sources (the
set of sources that offered it: tree with its path of holder ids, direct,
edge), offered_by_tree (bool), rank, offer_set_size, propensity, exploration
flag, holder confidence, fallback flag, opened (bool, ts, by whom), asked
(bool), used_cited (the agent's own claim), used_behavioural (set only by the
counterfactual audit), lin_subjects (the actor, every person the query text
mentions, and the offered item's subjects; 5), lin_leaves`.

- Subject detection runs on the query text when the row is written (review
  F17), with the same identifier declarations and canonicalisation as leaves.

- `opened` is written by the gateway when `lookup` or `ask` serves the leaf.
  The agent's claim of use is kept in `used_cited` and never trusted alone
  (SOURCE: up to 57% of RAG citations are not faithful,
  https://arxiv.org/abs/2412.18004; LAB: told to use tools, only 55% of agents
  did). The signal needs an actor that looks things up: in production the
  originating agent; in the experiment one reader session per stream question
  (EXPERIMENT 3.8; review F1). The opened share per recall is monitored: a
  share near 0 leaves the strongest signal empty, and a share near 1 (an actor
  that opens or `ask`s everything) turns `opened` back into the exposure-biased
  co-offer signal of 11.1. EXPERIMENT 3.8 pre-registers bounds of 0.05 to
  0.60. `opened` records lookups, not use (review 3, F11): a title or reason
  can convey an answer (INV-7, 20), so an offered item that was not opened
  may still have been used, and "not opened" is never counted as evidence
  of irrelevance by itself; `used_behavioural` is the signal that can see use
  without a lookup.
- `used_behavioural` comes from a sampled counterfactual: re-run the answer
  step with the item removed; if the answer or action changes, the item was
  used (SOURCE idea: Auto-Dreamer's masking reward,
  https://www.alphaxiv.org/abs/2605.20616.md; sampling rate ASSUMPTION). The
  sample is drawn from every offered row of the sampled recalls, opened or
  not (removing an item removes its pointer and descriptor too), so an item
  used only through its descriptor can be seen.
- `propensity` is the probability that the item was shown under the logging
  policy: 1 for deterministic offers, `epsilon / n` for exploration slots
  (SOURCE: IPS needs logged propensities, https://arxiv.org/html/1608.04468).
  This is the one field that cannot be reconstructed later.
- Exploration: a small share of digest slots (ASSUMPTION: 1 slot in 10, after
  the ranked list, pre-registered) is filled at random from low-exposure and
  recently reorganised leaves of the reader's classes, excluding leaves
  inserted in the last `L` insert batches (so new leaves are not favoured by
  being new; review F5), so the offer propensity of every slot is known and
  how often each slot is opened can be measured instead of assumed (an open
  is a lookup; it does not measure whether a descriptor was read or used).
- A row is written for every digest returned, but the digest never waits for
  it: if the write fails, or the gateway stops before it, the digest stands
  and the failure is counted (ASSUMPTION: availability over completeness of
  the log; the miss is visible). The row write is a commit, so it takes the
  shared side of the tenant's write gate (14.3): while an erasure holds the
  gate the row waits in gateway memory (at most `B_ERASE`, so the backlog is
  bounded by the tenant's request rate over that time) and, when written, is
  checked against the published tombstones like every commit that waited.

## 10. Reflection (OWNER; AGREED separate judge)

Runs off the hot path, per (tenant, class), after recalls and after memory
additions.

1. Pools per recent search: (a) what the tree offered, (b) what the direct
   search offered in the digest (its top 10), (c) a deeper and looser pool
   (OWNER): query variants generated by one call, direct search to depth 50,
   2-hop expansion with relaxed caps, lower placement thresholds. Pools (a) and
   (b) cost no search; pool (c) is the owner's "deeper and looser search".
2. A relevance judge that is a different model or a different prompt family
   from the holder and organise calls labels (query, leaf) pairs 0 to 3 with a
   rationale. It never sees which source offered the item, the holder's
   reasons, or any gold label (SOURCE: judge independence and circularity
   warnings, https://arxiv.org/pdf/2412.17156v2; prompt sensitivity,
   https://arxiv.org/html/2309.10621v3). Unjudged items are unknown, not
   irrelevant (SOURCE: pooling bias).
3. Output: notes "relevant (label >= 2) and tree_missed" with the reason the
   structure missed it (wrong branch, holder declined, not reached within
   budget), reinforcement for offered-and-opened items, and candidate edges.
   An offered item that was not opened gets no negative note: its descriptor
   may have answered the question (INV-7, 9).
   Notes carry lineage (including the query's subject tokens, 9) and the join
   class.
   - "Never offered" is defined per source (review F2): `tree_missed` = judged
     relevant and not offered by the tree source for that search, whatever the
     direct search or link expansion did; `any_missed` = not offered by any
     source. The repair signal is `tree_missed` (ASSUMPTION, the reading of the
     owner's "never offered": the structure being reorganised is the tree, and
     an offer by the safety net does not repair the tree's exposure);
     `any_missed` is logged and reported.
4. Write-time background search: when a leaf is added or updated, reflection
   runs the new leaf as a query against past stored queries (section 12) and
   its neighbourhood, so "anything that may have been relevant" is found at
   addition time (OWNER).

The judge is validated before its labels drive anything (EXPERIMENT 8.2).

## 11. Reorganisation (OWNER "reorganize by graph edge strength and search hit confirmation")

### 11.1 Signals

| Signal | Source | Weight in decisions |
|---|---|---|
| Confirmed hits | `opened` and `used_behavioural` in the log (void when the opened share leaves its bounds, 9) | strong |
| Judge: relevant but never offered | reflection notes | strong; this is the exposure-bias repair signal |
| Reverse-audit misses | section 12 | strong |
| Edge strength | typed weights, refreshed by confirmations | medium |
| Co-retrieval without opening | log | not used for strengthening (it is the exposure-biased signal; SOURCE note on HeLa-Mem's co-activation) |
| Offered but not opened | log | never a negative signal by itself: a title or reason can convey an answer, so not opening is not irrelevance (INV-7, 9) |
| Agent's cited use | `used_cited` | not used alone |

Offer counts are IPS-weighted with clipped propensities (floor 0.05,
ASSUMPTION) so heavily offered items do not dominate (SOURCE: clipping,
https://arxiv.org/html/1608.04468). This reweighting is an ASSUMPTION for
agent logs: no source measures it on memory lookups.

### 11.2 Operations

- Add membership: a leaf judged relevant to queries routed to node N but filed
  elsewhere joins N (up to `M_MAX`).
- Move: a leaf whose current parent never leads to its confirmed hits moves to
  the parent that does.
- Re-summarise: a node whose summary failed to route to its confirmed or judged
  children gets a new summary built with the missed child's facts surfaced.
- Split, merge, collapse: as in 7.6, but driven by co-confirmation clusters
  (leaves confirmed together for the same queries) as well as embeddings.
- Edges: add or strengthen edges between leaves confirmed together; weaken by
  decay only.
- Hub: a leaf or node whose degree passes the cap gets a hub summary node
  (section 16.4).

Every operation produces a candidate generation; nothing is edited in place.
Members and edges it writes carry the candidate's generation number (5.3,
5.5) and its `lin_queries` and the subject tokens of those queries (5), so
they are invisible until adoption and reachable by erasure. The candidate is
adopted only by the gate in section 13.3.

## 12. Pruning and the reverse audit (OWNER)

For each leaf considered for pruning, archiving or re-filing, and for a
capped sample of leaves each cycle chosen as follows (review F13, F3d):

- Candidate generation costs no model call: every cycle, every leaf of the
  class is percolated against the stored queries added since the last cycle
  (batched percolate requests) and every such stored query's vector is used in
  a kNN over leaf vectors to depth 50 (the semantic half). This gives, per
  leaf, the past searches it may have been relevant to, with scores. A
  leaf's candidate searches and their judge labels accumulate across cycles
  (they are stored as notes), so support can come from searches made in
  different cycles.
- Judge sample: at most `N_AUDIT = 100` leaves per cycle per class, and at
  most `K_CAND = 5` candidate searches per leaf (ASSUMPTION). Leaves are
  ranked by (number of candidate searches for which the tree did not offer
  them, then best candidate score), so leaves the tree keeps missing for
  searches they match are audited first; this rule sees no gold and no
  planting list.

The steps per audited leaf:

1. Which past searches would this leaf have been relevant to? The audit job
   runs as the reorganiser of the leaf's class and reads only that class's
   stored queries (INV-3: a lower-class job never reads higher-class queries;
   searches made under a higher-class channel are audited by that class's
   job, which can see the leaf because higher classes dominate it, and which
   repairs a miss only by adding a membership or edge of its own class that
   points at the lower leaf, never by changing the lower class's tree).
   (a) Percolate the leaf document against the stored-query index. (b) kNN of the leaf embedding against the
   stored query vectors of that class (SOURCE: the percolator is lexical only;
   the semantic half is a separate kNN, research recommendation). (c)
   ASSUMPTION addition, off by default: synthetic queries generated from the
   leaf, filtered by the judge and tagged synthetic (SOURCE: doc2query and
   doc2query--, https://arxiv.org/html/1904.08375,
   https://arxiv.org/pdf/2301.03266). The owner's reverse search asks "what
   searches happened"; synthetic queries are not searches that happened and
   can re-file a leaf with no search history, so they are measured apart
   (EXPERIMENT arm LOOP+SYNTH) and never mixed into the owner's audit.
2. The judge confirms which candidates are truly relevant.
3. Was it found? For real past searches, the log answers directly: was this
   leaf version offered by the tree (`offered_by_tree`), at what rank, opened,
   used. For synthetic queries (variant only), replay them against the served
   generation.
4. Per-leaf miss rate = 1 - offered by the tree / relevant, over all
   accumulated judged-relevant past searches, reported with a Wilson interval
   (INFERRED design; no published estimator was found). A leaf is flagged for
   re-filing when at least `S_MIN = 2` distinct past searches judged relevant
   to it were missed by the tree and its miss rate is at least 0.5
   (ASSUMPTION thresholds; review 2, F3: the earlier support of 5 is withdrawn,
   because the registered H4 workload gives a planted leaf only two in-stream
   searches, so 5 made repair impossible by construction). A leaf with fewer
   than 2 missed relevant past searches is not eligible; decoys are built to
   have no constructed relevant search (EXPERIMENT 3.6), so they are
   ineligible unless unrelated searches happen to be judged relevant.

Pruning rules (SOURCE: research recommendation; never delete for low usage,
because low exposure is the bias being repaired): archive a leaf (cold tier,
still directly searchable and still erasable) only if it was offered at a fair
rate to queries it was relevant to, was not opened, and the counterfactual
use sample (`used_behavioural`, 9) included it and found it unused, or if it
is superseded; not being opened alone never archives a leaf, because its
title or reason may have been what the reader used (INV-7, 20); never archive a leaf with high judged relevance and low offers,
unique content, or a legal hold. Archiving changes the tree in a new
generation; it never deletes the source.

## 13. Versioning, replay and the adoption gate

### 13.1 What is versioned

| Thing | Version | Identity that survives |
|---|---|---|
| Leaf | `lv` per correction or relabel; immutable documents | `leaf_id` for life |
| Index node | `nv` per change; immutable; shared across generations | `node_id` until split or merge; then forwarding |
| Edge | `ev` per change; each version is bound to generations by `src_seq` (insert-time) or `gen_from` (rebuild, reorganiser), and `gen_to` (review F18; 13.2) | `edge_id` (deterministic from endpoints and type); versions after an erasure start above the erased ones |
| Member | bound by `src_seq` (placement) or `gen_from` (rebuild, reorganiser), retired by its `gen_to` set (13.2) | (parent, child) |
| Structure | generation per (class, family); manifest written last; head flipped by compare-and-set | generation number |
| Log rows, stored queries, notes | immutable, append-only | their ids |

### 13.2 Pinning and pointer validity

- A task pins generations at its first recall; delegates inherit the pin.
- Visibility rule for members and edges, for a task pinned at G with
  write-path watermark P (8.1), where anc(G) is G plus the adopted chain listed
  in G's manifest. There are three writers:
  - write-path versions (placement members, insert-time edges; they carry
    `src_seq`) are visible if `src_seq` <= P;
  - batch-rebuild versions and reorganiser versions (they carry `gen_from`)
    are visible if `gen_from` is in anc(G);
  - in every case, not if any number in the version's `gen_to` set is in
    anc(G).
  Write-path versions never take a generation number, so a rejected candidate
  cannot strand them (review F18, second round). P and anc(G) are fixed for
  the task, and the visible set cannot change under it because P and heads
  are published only after the documents they cover are searchable (5.6;
  review 2, F4) and anc(G) is never truncated (5.6; review 2, F5). A rejected
  or rebased candidate's unshared documents (nodes, members, edges) are
  deleted when it is rejected. Consequences: edges of a candidate are invisible before the gate
  adopts it (no gate bypass), a pinned reader sees a stable graph as well as a
  stable tree, and rollback (13.3 step 5) removes the candidate's edges along
  with its nodes.
- Leaf pointers never depend on the tree: they resolve by `leaf_id` and `lv`
  in the leaf index, so reorganisation never breaks them.
- Node pointers resolve within their generation while it is retained
  (`R_GEN` = last 5 generations or 14 days, whichever is longer; ASSUMPTION);
  after that they return `moved` (with forwarding) or `expired`. Retention
  expiry deletes an expired generation's unshared node documents; it keeps
  the ancestry ids (5.6).
- Erasure is the one thing that can change what a pinned generation shows: a
  rewritten node (14.4) keeps its id, so a pinned reader may see a redacted
  summary mid-task. Erased leaves return `unavailable`. This is intended.

### 13.3 Replay as the gate on adopting a reorganisation (OWNER "everything versioned"; research recommendation)

Revised after review F4 and F12d: a replay of 20 searches unrelated to the
change has a mean difference near 0 and could never adopt anything, so every
"the loop changed nothing" result would have been produced by the gate, not by
the method.

1. The candidate generation is built from evidence up to time T only.
2. Targeted replay set (primary): held-out logged searches from before T that
   did not shape the candidate (ASSUMPTION: the most recent 20% of the shaping
   window is held out) and whose judged-relevant items include a leaf the
   candidate changed, topped up to at most 60 with gate-only synthetic queries
   generated from the changed leaves, judge-filtered and tagged synthetic.
   Gate-only queries are never stored as past searches and never used as
   evaluation probes. Each is re-run against the candidate and against the
   current generation. Clusters, the statistical unit (review 2, F12): one
   cluster per operation of the candidate (a move, an added membership, a
   re-summary, a split or merge, an edge change), holding every replay query
   generated from a leaf that operation changed (its membership, its child
   entry, or the summary of a node on its path) and every held-out search whose
   judged-relevant items include such a leaf; operations that share a replay
   query are merged into one cluster, so dependent queries stay together.
   The 60 queries are spread over as many clusters as possible (one query per
   cluster in turn, held-out searches first, before any cluster gets a second),
   and a cluster holds at most 6 (ASSUMPTION). A candidate is the whole batch
   of operations of one learning cycle.
3. Primary: tree-source recall of judged-relevant items within the top 10.
   Items the candidate offers that were never judged are judged at replay by
   the same blind judge, inside the stage budget, so new items are neither
   counted as irrelevant nor as relevant by default. Guards: offered set size,
   model calls, tokens, latency, no loss on a rarely-needed stratum (leaves
   with fewer than 3 relevant queries), zero access-class or tenant
   regressions, and a general replay of the 20 most recent held-out searches in
   which at most 2 searches lose a judged-relevant item from the top 10 and
   losses do not exceed gains (ASSUMPTION).
4. Decision (review 2, F12; it replaces the betting confidence sequence of the
   previous revision, whose per-search unit treated correlated paraphrases as
   independent): one fixed-horizon test per candidate, evaluated once, with no
   repeated looks. Per replay query q, d_q = sign(r_cand(q) - r_curr(q)), in
   {-1, 0, +1}, where r is the primary above. With clusters k = 1..K of n_k
   queries:

   ```
   D_k = (1/n_k) * sum over q in k of d_q
   T   = (1/K)   * sum over k of D_k
   p   = #{ e in {-1,+1}^K : (1/K) * sum_k e_k * D_k >= T } / 2^K
   ```

   Adopt only if K >= 8, p < 0.05 and the general-replay guard of step 3
   passes; otherwise "no decision, keep current". p is computed exactly by
   counting signed sums (every D_k is a multiple of 1/60 because n_k <= 6, so
   there are at most 120 K + 1 sums; under 0.1 s at K = 60, depending on the
   machine). The test is
   exact when cluster means are symmetric about 0 under no change and clusters
   are independent; any dependence inside a cluster is allowed. When symmetry
   fails it can be anti-conservative: with frequent small gains and rare large
   losses of mean 0 it adopts 0.17 at K = 8 and about 0.07 at K = 60
   (EXPERIMENT 7.3; limit, 20). Scoring each
   query by the sign of its difference keeps the exact count small; raw
   fractional differences made one exact evaluation take up to 100 s in a
   trial. Null coverage and power are simulated in EXPERIMENT 7.3
   (`evalkit/gate_power.py`). What the gate is (review 3): an inspectable
   heuristic, not a 5% false-adoption guarantee; neither its p-value nor the
   RANDOM-FEEDBACK control is ever cited as proof of a bounded
   false-adoption rate. Sign scoring measures how often clusters improve,
   not by how much, so frequent tiny gains can outweigh rare large losses in
   the gate while mean recall falls; the experiment's H3 and H4 outcomes
   therefore rest only on their independent forward and held-back measures
   (EXPERIMENT 2.2, 7.3), never on the gate's statistics.
5. Adoption = compare-and-set of the head; rollback = compare-and-set back to
   the previous generation, which is retained; by the visibility rule (13.2) the
   rollback also hides the candidate's members and edges.
6. Replay sets are lists of search ids, rehydrated from the log at replay time,
   so tombstones apply (a search whose actor or offered items were erased is
   dropped or stripped).
7. Positive controls (EXPERIMENT 7.3): a known-good candidate must be adopted
   and a known-bad one rejected, in the stub stage and again in development;
   otherwise the loop's results are void.

## 14. Erasure (owner's instruction; LAB-R4, LAB-R6, LAB-R8, LAB-R9, LAB-R11)

### 14.1 Scopes

Erase a subject (a person: everything they authored, every mention of them,
every query they asked and every query that mentions them, and their authz
records), an item (a leaf, all versions), or a class relabel. Erasure includes
what others said about the person (LAB decision carried over; ASSUMPTION for
this repository).

Upward relabel (review F19c): moving a leaf to a class that a former reader
may not read is an erasure of the old-class copy: a tombstone with scope
(leaf id, old class) published with its redact set at the erasure boundary
(14.3), then the usual delete, rewrite and purge of every old-class artefact
derived from it. The relabel call returns at that boundary, so old pointers
return `unavailable` to readers without the new class from the moment the
relabel returns. The new version is written in the new class with a
new `lv`. Downward relabel (declassification) is an ordinary new write into
the lower class by a principal holding both classes.
Legal hold wins: the erasure is queued and the delay is written in the
receipt.

### 14.2 Lineage closure

Every leaf-derived document carries `lin_subjects` and `lin_leaf_ids`, so the
first part of the closure is a set of queries over every index of the tenant
(all classes, all kinds): `terms lin_subjects: [token]` or
`terms lin_leaf_ids: [leaf_id]`. Upper index nodes carry only `lin_nodes`, so
the second part walks upward: for each affected node version, find the node
versions whose `lin_nodes` contain it (in every retained generation), until
the top. The walk uses no model call. The closure includes leaves, source documents,
nodes in every retained generation, members, edges, log rows, stored
queries, notes, judge labels, cache entries, manifests (ids only), replay
sets (ids only), tombstone-guarded queues, quarantine queues and the tenant's
authz records of the person (`lc-<t>-authz`, review F15). A summary that
paraphrases the person without naming them is found by lineage, not by string
search.

Query-shaped artefacts (review F17). Because query subject tokens are carried
into `lin_subjects` of everything a query shapes (5, 9), the first part of the
closure already finds: the person's own log rows, stored queries and cache
entries; other people's log rows, stored queries, cache entries and notes whose
query text mentions the person; and summaries, members and edges that a
reorganisation built from those queries. As a second net, the closure also
collects the erased search ids and queries `lin_queries` and `evidence` for
them. Handling:

- log rows, stored queries, cache entries, notes, judge labels: deleted (log
  rows of other actors that merely offered an erased leaf are stripped, step
  5);
- `co_used` edges and any edge whose `evidence` names an erased search:
  deleted in every retained generation;
- members moved or added by a reorganisation shaped by an erased search:
  deleted in every retained generation; a leaf left with no member in a
  generation gets its placement-rule parent back, computed by code without a
  model call;
- node versions re-summarised from an erased search: returned to the
  code-built placeholder of step 3 in every retained generation (no model
  call).

### 14.3 Steps

Revised after review 2, F7: the previous revision published the tombstone
first and computed the closure afterwards, so a concurrent recall could read
an upper node (reached only through transitive `lin_nodes` lineage) before its
redact-set entry existed, while the design promised invisibility from the
tombstone. There is now one publication boundary, and "erased" is promised
from it, not from the request.

1. Request: `erase` records the request under an idempotent erasure id. If the
   subject is also a principal, it is revoked first (4.4). Under a legal hold
   the request is queued (14.1). Nothing is promised to readers yet: until the
   boundary, old content may still be served.
2. Closure under the write gate, without model calls. The executor takes the
   exclusive side of the tenant's write gate (a readers-writer gate in the
   gateway, one per tenant; the copy-and-swap purge, 14.5, pauses writes to
   one index the same way). Every commit of the tenant
   (a `remember`, a placement or link job, a rebuild, reorganiser or
   reflection commit, a log row, a publication of P or of a head) takes the
   shared side, so commits in progress finish and new ones wait; none is
   refused. Under the gate the executor refreshes every index of the tenant,
   computes the full closure (14.2), including every ancestor reached through
   transitive `lin_nodes` lineage in every retained generation, and writes
   the closure's node versions as a redact set into the class control indices,
   tagged with the new tombstone sequence s (not yet consulted, because s is
   above the published sequence).
3. Boundary, one publication step: append tombstone s (scope, erased
   identifier HMACs, erased edge versions) to the tenant's tombstone journal
   (fsync, replicated; 14.6), record it in `lc-<t>-ctl`, refresh, and load
   its scope and redact-set node ids into gateway memory (inactive while
   above the published sequence); then, holding the exclusive side of the
   tenant's send fence (4.4) for this in-memory step only, advance the
   gateway's in-memory published tombstone sequence to s. That single step
   makes the tombstone and its complete redact set effective together, and
   no send check and its write can straddle it (review 3, F7); the fence and
   then the gate are released. From the boundary: the shared guard
   refuses re-ingest, lookup, rebuild, restore and replay of the scope (INV-8);
   search results are post-checked against the gateway's in-memory tombstone
   table (a post-check for erasure only; access is still trimmed before
   retrieval, INV-2); every commit that waited at the gate checks the
   published tombstones when it commits, so a background job whose inputs
   intersect s aborts (7.6 step 6) and a log row naming an erased item is
   stripped as in step 5; and whenever the gateway hands a node to a holder,
   an organise call, a reflection pass or a reader, it replaces any node in the
   redact set by a code-built placeholder: the erased child entries and facts
   removed, the summary replaced by the remaining children's titles, sizes and
   totals recomputed; a response whose reads preceded the boundary and that
   carries an item the boundary covers is aborted at its next send hand-off
   (4.4, 14.5). `erase` (and a relabel) returns at
   the boundary; the
   receipt states the request and boundary times. Bound: request to boundary
   at most `B_ERASE = 60 s` (ASSUMPTION; measured by EXPERIMENT E7 and E8),
   which is also the longest delay the gate imposes on the tenant's commits
   (INV-9). Crash: if the gateway stops after the journal append and before
   the in-memory step, the journal is authoritative: on start, every journal
   tombstone is published (closure recomputed, redact set rewritten) before
   the tenant is served. A crash before the append leaves no tombstone, and
   the request is retried from step 1.
4. Destroy keys: delete the keystore rows of every leaf version in scope
   (source log and its snapshots become unreadable).
5. Delete: leaves, source documents, members, edges, log rows, stored
   queries, notes, cache entries, authz records, and the query-shaped
   artefacts of 14.2. A log row authored by someone else whose only link to the
   scope is an offered erased item is not deleted (it is that actor's history):
   its erased pointer and the item's subject tokens are stripped by an in-place
   rewrite of that row, recorded with `erasure_rewrite = s`. That rewrite
   mutates a row declared immutable, so it is covered by DEVIATION 6 (review
   M4).
6. Rewrite, in code, no model calls: every node version in the redact set, in
   every retained generation, is rewritten under the same `_id` with the
   placeholder content of step 3 and `erasure_rewrite = s`,
   `summary_status = stale` (14.4). Generations past retention are retired and
   their unshared node documents deleted. A model re-summary of the stale
   nodes happens lazily, only in the live generation, as part of the next
   normal batch rebuild (7.6), so erasure time and erasure cost do not depend
   on model calls; retained older generations keep the placeholder (they
   serve pinned readers and rollback, not quality).
7. Purge (14.5), then verify (14.7), then write the receipt: counts by kind
   and class (no id lists), residue of the closure (must be 0), the retention
   bounds for keystore backups (`R_KEYS`) and log files (`R_LOGS`), the delay
   if a hold applied. The full receipt goes to the verifier and the tenant
   administrator; the requester's copy shows only totals over the requester's
   read set (review F19b). Clear the redact set entries once verified.

### 14.4 How versioning and erasure coexist

- The source of truth is per-version ciphertext under per-version keys:
  destroying the keys erases every version and every snapshot copy of the
  source at once.
- Derived structures are rebuildable, so old generations do not need old
  content: manifests hold ids only. An erasure rewrite is the single sanctioned
  mutation of an immutable document (INV-6; DEVIATION 6 in section 22); it
  changes content, not identity, so pointers stay valid and pinned readers see
  the redacted version.
- Replay sets and stored queries are ids or are themselves subject to closure,
  so replay cannot resurrect content.
- Restore from snapshot: restore src, ctl and authz, re-apply every tombstone
  from the newest tombstone journal (not from the snapshot's own `ctl`, which
  may predate an erasure), destroy every keystore row named by any tombstone,
  replay the erasure closure over every restored index, rebuild projections,
  pass the verifier's residue checks, and only then enable the tenant (14.6).

### 14.5 Purge on live indices

Force merge is advised only for read-only indices (SOURCE:
https://www.elastic.co/guide/en/elasticsearch/reference/8.19/indices-forcemerge.html).
Erasable indices stay live, so:

- Default: delete, refresh, flush, then `_forcemerge?only_expunge_deletes=true`
  with `expunge_deletes_allowed = 0`, then check `_stats` that
  `docs.deleted == 0` and `_segments` shows no deletions
  (PENDING_REAL_TEST P9 and P10: that this setting is accepted, and how long
  soft-delete retention leases hold deleted documents; the earlier lab saw
  22 to 30 s).
- Fallback, and for any index where the default leaves deleted documents:
  copy-and-swap. The gateway pauses writes to that (tenant, class, kind) for
  the swap, reindexes the alias's physical index into a new physical index
  (the erased documents are already deleted, so they are not copied), flips
  the alias, and deletes the old physical index (deleting an index removes
  its files).
- Bounds (targets, measured in EXPERIMENT 9.4): nothing is promised between
  the request and the boundary; the boundary follows the request within
  `B_ERASE = 60 s` (14.3); from the boundary, erased content is served by no
  read path (guard, in-memory tombstone post-check, redact set), and a
  response whose reads preceded the boundary and that carries an item the
  boundary covers is aborted at its next hand-off (4.4), whose check and
  write are atomic with the boundary under the tenant's send fence, so only
  bytes handed to the kernel before the boundary may still reach the peer;
  those the kernel still holds for a connection at the boundary are at most
  64 KiB per connection (the hand-off writes at most 64 KiB minus the queued
  byte count, 4.4), and the tenant's open-connection cap
  times 64 KiB in aggregate, measured at the gateway (4.4); physical purge
  within `P_PURGE = 1 hour` of the boundary (ASSUMPTION); keystore backup copies of destroyed keys age out
  within `R_KEYS = 35 days` (ASSUMPTION), stated in every receipt.

### 14.6 Backups and snapshots

Snapshots hold only ciphertext source, control and authz indices. Projections
(which hold plaintext and vectors) are never snapshotted (ASSUMPTION; this is
what makes crypto-shredding cover backups). Keystore backups are encrypted
and retained at most `R_KEYS`. No regulator text endorsing key deletion for
backups was found (research), so the legal sufficiency is a limit.

The tombstone journal (review F16). The first version kept tombstones only in
`ctl`, which is snapshotted: restoring a snapshot taken before an erasure,
together with a keystore backup taken before it, would have returned both the
ciphertext and its keys with no tombstone to re-apply.

- Each tenant has an append-only, hash-chained tombstone journal owned by the
  gateway: tombstone sequence, scope (subject token, leaf id, leaf version, or
  leaf id and old class), erased identifier HMACs, erased edge versions, and
  the keystore row ids destroyed. No content.
- It lives outside the snapshot timeline: written locally with fsync at the
  erasure boundary (14.3 step 3), and replicated to a second location (a separate
  host or bucket with object versioning, ASSUMPTION) that no snapshot restore
  touches. Only the newest journal is ever restored; there is no older copy to
  choose.
- Every restore of Elasticsearch or of the keystore, of any age, runs these
  steps with the tenant disabled, and enables it only at the end (review 2,
  F8):
  1. every journal tombstone is re-recorded in `ctl`, and every keystore row
     it names is destroyed (a restored keystore backup therefore loses every
     key erased after it was taken);
  2. the erasure closure of every journal tombstone (14.2) is replayed over
     every restored index, including `lc-<t>-authz` (sessions and grants of an
     erased principal), the tenant and class control indices and the source
     log, and every match is deleted or stripped as in 14.3 steps 5 and 6;
  3. projections are rebuilt from source past the guard;
  4. the independent verifier (14.7) runs its residue checks for every journal
     tombstone, with its own keys and by direct reads of every restored index,
     authz included ("content was not served" is not enough);
  5. only when it reports zero residue is the tenant enabled. The gateway also
     refuses to serve a tenant whose `ctl` tombstone sequence is below the
     journal's newest.
  The replay and checks cost no model call; their time is part of the restore
  and is reported.
- If the journal is unavailable, the tenant is not served (fail closed) until
  it is restored. The journal is kept for the life of the tenant, which carries
  the same trade-off as erased identifier HMACs (open question O-5).
- Tested by EXPERIMENT 9.4 E3 and 9.5 V5: erase, then restore an older
  snapshot together with an older keystore backup; zero residue is required.

### 14.7 Verification with a positive control (LAB-R6, LAB-R8, LAB-R9)

- The verifier is a separate process with its own read-only keys. It does not
  use the gateway's code paths for its reads.
- Before an erasure under test, canaries are planted through the real writer
  (never inserted directly; LAB-R9): a short unique string, a long one (over
  2 KB, so it is stored compressed; LAB-R6), and a canary vector, in a leaf
  that will reach every derived kind (node summaries, facts, edges, log rows,
  stored queries, notes, cache) and a retained old generation. Also (review
  F17, F15, F19d): a canary subject that appears only in query text (another
  person's query mentions it) and in a summary, edge and membership shaped by
  that query; a canary principal with authz records; and a canary line in the
  plaintext logs.
- Positive control: the verifier must find every canary by text search,
  kNN with the canary vector, percolation of a canary document, get by id, and
  lineage query, and must see `docs.deleted > 0` after the delete step and
  before purge. Only then does "zero found after" count.
- A raw-file grep of segment files is not used as evidence: stored fields are
  compressed in segment files, so a grep is blind (LAB-R6). Plaintext log
  files (Elasticsearch, container, gateway) are different: they are not
  compressed, so the verifier greps them for the erased canaries, with a
  positive control: a separate control marker (never an erased canary)
  deliberately logged through a scratch index with the slow log enabled at
  threshold 0, which the same grep must find. Since no body is ever logged
  (6.5), a canary in a log file is a defect.
- The verifier asserts counts (number of probes run, no skips) and states its
  step list in the receipt (LAB-R8).

## 15. Availability isolation

- Gateway admission caps per tenant: open connections (which also bounds the
  aggregate of 4.4's per-connection send-buffer residual), concurrent
  requests, bytes in flight, queue length, idle deadline, model-call
  concurrency and a model-call budget per tenant per hour (ASSUMPTION values
  in config).
- Background jobs run in per-tenant slots; one tenant's rebuild cannot take
  another tenant's slot.
- Shared ports (review F20c): the model port has one global concurrency (2 on
  the subscription, ASSUMPTION) and one provider rate limit, and the E5
  embedder is one process. Both take requests through a per-tenant fair queue
  (deficit round robin) with a reserved share per tenant, so one tenant's
  flood waits behind its own queue, not in front of another tenant's.
- Per-tenant stored-byte caps, read from index statistics of the tenant's
  indices; a write past the cap is refused with a typed error. The sum of
  caps stays below the disk's flood-stage watermark, so no tenant can drive
  the node read-only (which would also block other tenants' writes).
- The keystore is one file per tenant (3), so SQLite's single-writer lock is
  per tenant.
- Elasticsearch thread pools and nodes are shared; a heavy tenant can still
  raise another tenant's latency. Bounded, not removed (limit, section 20).
  EXPERIMENT 9.1 T5 tests these bounds with a latency-injecting model stub that
  enforces the global concurrency, a shared embedder, and a disk-fill flood.

## 16. Graph edges ("edge handling", meaning 1)

### 16.1 Construction

- On write: kNN top-10 and shared entities (7.7).
- From use: leaves opened together for the same search and both judged
  relevant get a `co_used` candidate (only confirmed, never mere co-offer).
  It carries the search ids in `evidence` and their subject tokens in
  `lin_subjects`, so erasing a person who asked or was mentioned in those
  searches deletes it (14.2).
- From reflection: a judged-relevant-never-offered leaf gets a candidate edge
  to the leaves that were offered and opened for that search.
- Self-loops are refused; an edge to a leaf in another tenant cannot be
  expressed (the edge writer resolves endpoints in the tenant's indices only).

### 16.2 Types and initial weights (ASSUMPTION values; SOURCE for the type set:
research synthesis from Graphiti, A-MEM, H-Mem)

| Type | Directed | Initial weight | Decays |
|---|---|---|---|
| same_entity | no (two directed docs) | 0.6 | yes |
| same_event | no | 0.8 | yes |
| continues | yes | 0.9 | no |
| supersedes | yes | 1.0 | no |
| contradicts | no | 1.0 | no |
| causes | yes | 0.7 | yes |
| related (untyped) | no | 0.3 x similarity | yes |
| co_used | no | 0.5 | yes |

Each typed edge records the model id, prompt version and confidence; a type
assigned below confidence 0.5 is stored as `related`.

### 16.3 Superseded and contradictory facts

Following Graphiti (SOURCE: https://arxiv.org/html/2501.13956v1): never
delete; set `valid_to` on the older leaf's head and write a `supersedes` edge.
Unlike Graphiti, newest does not win by default: when the newer statement is
unverified (single source, low confidence), the pair is written as
`contradicts`, both stay current, and reflection is asked to resolve it. The
read path returns both with their validity times. A cycle of `supersedes`
edges is converted to `contradicts` and flagged.

### 16.4 Weights, hubs and decay

- Weight update on confirmation: `w = min(1, w + 0.1)`; decay:
  `w x 0.5^(days since last confirmation / half_life)`, half-life 30 days
  (ASSUMPTION); floor 0.05, below which the edge is retired in the next
  generation (never for supersedes, contradicts, continues). A confirmation
  is an open judged relevant or a `used_behavioural` use (11.1), so an edge
  whose use comes only through descriptors, never opened and not in the
  behavioural sample, gets no refresh and decays to retirement: absence of a
  positive signal, not "not opened" counted against it (9; INV-7, 20).
- Specificity: score is divided by `log2(2 + degree)` of the target within the
  reader's visible edge indices (SOURCE idea: HippoRAG node specificity).
- Degree cap 50 per type per leaf (ASSUMPTION): overflow edges are replaced by
  one edge to a hub summary node that lists the overflow targets; the hub is
  an index node of the join class.
- Personalised PageRank (damping 0.5, SOURCE HippoRAG) is an ablation against
  bounded BFS, not the default; HippoRAG lost to ColBERTv2 on HotpotQA.

### 16.5 Dangling and stale edges

- Endpoint erased: the edge is in the erasure closure and is deleted; the
  tombstone records the highest erased version, and a later re-creation of
  the same edge id starts above it (5.5).
- Evidence erased: an edge whose `evidence` names an erased search is deleted
  in every retained generation (14.2).
- Candidate never adopted: its edges are invisible (13.2) and deleted on
  rejection.
- Endpoint superseded: the edge stays, pointing at the old version; resolution
  says `superseded`.
- Endpoint relabelled to a higher class: edges whose join class changed are
  rebuilt in the new join class and deleted from the old one.

## 17. Edge cases ("edge handling", meaning 2)

| Case | Handling | Test (BUILD-PLAN step or EXPERIMENT 9 probe) |
|---|---|---|
| Empty or whitespace input | refused, typed error | unit |
| Single turn over the leaf cap | split at sentence, then token boundary; `continues` edges | unit |
| Session far over cap (78k characters seen in data) | recursive split per 7.2 | unit on the longest dataset session |
| Node over `F_MAX` children, or many pending members | split in next batch; until then the holder is asked in chunks of at most `F_MAX` entries, merged by code (8.2) | unit |
| Pending members shown to holders | code-built title, size, `pending` flag; no gist or facts (8.2) | unit |
| Node under `F_MIN` | merge into most similar sibling | unit |
| Single-child chain | collapsed | unit |
| Top level too large | index-of-indexes rule adds a level | unit |
| New tenant, no tree yet | recall returns direct search only; digest says "no index yet" | stub loop |
| Leaf belongs to several topics | soft membership up to 2 per family; several families; edges | E1 ablation |
| Exact duplicate content with different authors | separate leaves (lineage differs), `same_event` edge; dedupe only within one author and class | unit |
| Contradiction | `contradicts` edge, both kept, flagged for reflection | unit, stub loop |
| Superseded fact | `supersedes`, `valid_to`; newest not assumed right when unverified | unit |
| Correction of a leaf | new `lv`; parents marked dirty; old version pointer resolves `superseded` | version probe |
| Concurrent writes to one leaf head | compare-and-set; loser retries with fresh head | concurrency test |
| Two reorganisers | lease plus head compare-and-set; loser discards and rebases | version probe (d) |
| Write during a rebuild | watermark; leaf stays pending, reachable by direct search | stub loop |
| Insert-time edges and placement members written while a reorganiser candidate is open, then the candidate is rejected | bound by `src_seq`, not by a generation number, so they stay visible to every task pinned after P covers them (13.2) | stub loop |
| Link typing for one leaf stalls (model slow) | P waits at most `D_WP`; then the job is cancelled and joined, its documents deleted, the failure counted, and P moves on; later leaves' members become visible again | unit |
| Two candidates retire the same member; one is rejected | `gen_to` is a set; the rejected number is in no ancestry, so only the adopted retirement hides it | unit |
| Failed model call (timeout, refusal, error) | typed failure; a failed build call for a node with pending members aborts that generation (no W is published that it does not represent; members stay pending and reachable by tree-only recall; 7.6); holder falls back to embedding routing, flagged; counted in denominators | fault-injection test; SEC-V7 |
| Malformed model output | schema validation; treated as failure | fault-injection test |
| Model names a child it was not shown | dropped, counted | fault-injection test |
| Prompt injection in leaf text | holder output limited to presented ids; no tools in holder calls | injection test |
| Model drift | full model id pinned in cache key and log | unit |
| Embedding model change | `embed_model` stored; vectors of different models are never compared; re-embed creates a new field | unit |
| Dangling leaf pointer after reorganisation | cannot happen: leaf pointers do not depend on the tree | version probe (a) |
| Node pointer after reorganisation | resolves in its pinned generation; after retention, `moved` or `expired` | version probe (a) |
| Pointer after erasure | `unavailable` | version probe (c) |
| Forged pointer (other tenant or class) | `unavailable`, same as missing | isolation probe |
| Holder timeout | partial result marked; parent organises what arrived | fault-injection test |
| Hot query, very large fan-out | `B_HOLDERS`, `B_FANOUT`, deadline; unopened branches returned as node pointers | stub loop |
| Very large digest | truncated with "more available: explore node X"; counts only over visible set | unit |
| Retry of `remember` after lost response | idempotency key per (tenant, principal, key) | unit |
| Idempotency key reused with a different body | refused with a typed error (body hash stored with the key) | unit |
| Double-submitted erasure | erasure id idempotent; second call returns the first receipt | erasure probe |
| Elasticsearch unavailable | reads fail closed with a typed error; writes refused as retryable; no local plaintext queue | fault-injection test |
| Partial bulk failure | per-item status; orphans repaired (7.4) | fault-injection test |
| Near-real-time window (1 s) | heads by realtime GET; tombstones and epochs from the gateway's in-memory tables; P, heads and the erasure boundary published only after an explicit refresh (5.6, 14.3); search results post-checked against published tombstones | erasure probe; SEC-V2 |
| Shard limit | provisioning refuses at 80% | provisioning test |
| Time zones | UTC stored plus original offset; day buckets in the tenant's declared zone | unit |
| Clock skew | order by source sequence, not wall clock | unit |
| Legal hold during erasure | hold wins; queued; delay in receipt | erasure probe (g) |
| Erasure during rebuild or reflection | the job's commit waits at the tenant's write gate and, checked against the published tombstones when it commits (the commit-time tombstone check of the shared guard), aborts (7.6 step 6, 14.3) | E4 |
| Hand-off with the kernel queue already near 64 KiB | the hand-off reads `SIOCOUTQ` under the shared fence and writes at most 64 KiB minus the queued count, none when full; the rest stays in the gateway's buffer (4.4) | E8 (d), R1 |
| Erasure boundary while several senders of the tenant are between their final check and their socket write | the publication step waits on the tenant's send fence until they have written; every later hand-off of a covered response is aborted; only bytes handed to the kernel before the boundary can still reach the peer, and those the kernel still holds at the boundary are at most 64 KiB per connection, measured at the gateway (4.4, 14.3) | E8 (d) |
| Restore from snapshot after erasure | tombstones re-applied, erasure closure replayed over every restored index (authz included), verifier residue checks pass, then the tenant is enabled (14.6) | E3, V5 |
| Erased subject mentioned again in new content | erased identifier HMACs refuse it | erasure probe (a) |
| Erased identifier under a variant spelling (case, width, accents, homoglyphs) | NFKC, case folding and the confusables skeleton before the HMAC (5) | unit with a variant table; E3 |
| Two people with the same name, one erased | identifiers are declared with context; a bare-name match is held for review, and the writer's outcome does not reveal an erasure (7.3) | unit |
| Mention under an undeclared spelling | missed by detection: limit (section 20) | documented |
| Erased person mentioned only in other people's queries | query subject detection; query-shaped artefacts in the closure (9, 14.2) | E9 |
| Re-created edge after erasure | version counter starts above the erased versions (5.5) | unit |
| Restore of a snapshot older than an erasure | newest tombstone journal applied first; keystore rows it names destroyed (14.6) | E3, V5 |
| Authz index unwritable (disk at flood stage, read-only block) | revoke works from the gateway's own epoch table and journal (4.4) | R3, P14 |
| Epoch journal write fails (no space, stalled fsync) or a restart finds it incomplete | `revoke` has already denied in memory; that tenant is closed (all its principals denied), across restarts, until break-glass recovery, which first re-applies every revoke whose receipt lacks `durable_at` and then reopens grant calls only to the administrators the operator names; other tenants unaffected (4.4) | R7 |
| Power loss before the start record is durable, between a denial and its fsync, or during compaction | no request is admitted before the start record and its directory are fsynced; compaction is write, fsync, atomic exchange of the two file names, directory fsync, carrying the start record; the table rebuilt from the durable journal holds every revoke reported durable, and the tenant comes up closed whenever a run was not stopped cleanly (4.4) | R7 (simulated power loss) |
| Revoke arriving during a clean stop | admission and the revoke lane close before the drain; a late revoke is refused with a typed retryable failure, never answered `denied`; the shutdown record follows the drain (4.4) | R7 |
| Retention expiry of a generation named by an adopted edge's `gen_from` or a retired edge's `gen_to` | ancestry ids are kept for the life of the class, so the adopted edge stays visible and the retired one stays hidden (5.6) | SEC-V2, V4 |
| Descriptor that conveys an answer (a very short leaf, a decisive term in a title, a reason that paraphrases a fact) | field contract only (INV-7): no body, gist or fact field returned; titles code-built with no token containing a digit; reasons bounded; "not opened" is never counted as irrelevance (9); limit (20) | SEC-A8 (field contract and digit rule) |
| Edge between incomparable classes | not created unless a dominating class is declared | access probe |
| Class relabel of a leaf, upward | erasure of the old-class copy, returning at its erasure boundary; old pointers `unavailable`; new version in the new class (14.1) | A6 |
| Class relabel of a leaf, downward | ordinary new write in the lower class by a principal holding both | access probe |

## 18. Model-call interface and the deterministic stub

### 18.1 Port

```python
class ModelPort(Protocol):
    def call(self, task: Task, inputs: Mapping[str, Any], *,
             ctx: CallContext) -> CallResult: ...
```

- `Task` is one of: `build_l1` (child entries and node summary from leaf
  text), `build_upper` (summary from child summaries and facts), `route`
  (holder selection), `organise`, `type_edges`, `judge_relevance`,
  `query_variants`, `synthetic_queries`, `ask`, `reader_answer`,
  `answer_judge`. Each has a versioned prompt and a JSON schema for its
  output; output that fails the schema is a typed failure.
- `CallContext` carries tenant, class, task id, budget handle and the pinned
  model id. The budget handle increments a persistent per-stage counter before
  the call (a failed call still counts) and refuses at the cap (LAB pattern).
- Cache: key, class and scope as in 5.8; the cache is an Elasticsearch index
  of the class, so erasure reaches it. It is the only model-output cache:
  the evaluation adds none (an evaluation cache outside the product's erasure
  closure was withdrawn, review 2, F10). Prompt-bearing outputs are never
  shared across principals.
- Calls pass through the per-tenant fair queue of 15 before the global
  concurrency limit.
- No retries during evaluation runs (failures count); a provider throttle
  response pauses the stage and is recorded, it is not a scored failure; in
  product use, one retry then fallback (ASSUMPTION).

### 18.2 Deterministic stub

The stub makes the whole loop run with no model and must depend on content,
so that the shuffled-summary and random-edge controls can fail at the stub
gate:

- `route`: scores each presented child by BM25 of the prompt over the child's
  title, gist and entities plus cosine of hashed bag-of-words vectors; returns
  children over a threshold, at most `B_FANOUT`.
- `build_l1`, `build_upper`: extractive. No title (titles are code-built,
  INV-7); gist = the highest-scoring sentence; facts = top 3 sentences;
  summary = concatenated child titles and top terms. Gists and facts go into child entries for
  holders only; a holder's returned pointer never carries them (INV-7), so a
  one-sentence leaf's body field, which equals its gist, is not returned
  without a lookup. That is the field contract only: a leaf of a few words is
  nearly its own title, so its content can still reach the reader (20).
- `organise`: groups pointers by holder path and sorts by score; outline
  labels are holder paths and pass the 5-gram check (8.3).
- `type_edges`: rules (shared entity: same_entity; negation or number change
  over the same entity and attribute: contradicts; later date and same entity:
  supersedes candidate).
- `judge_relevance`: a different scorer from `route` (character 4-gram overlap
  with a separate tokenizer), so judge and router are not the same function.
- `query_variants`, `synthetic_queries`: template rewrites over top terms.
- `ask`, `reader_answer`: extractive answer from the best sentence. As the
  stream reader (EXPERIMENT 3.8), the stub sees only the digest and looks up
  the pointers whose title scores in its own top 3 under a scorer separate
  from `route` and from `judge_relevance`; it never sees gold or the planting
  list.
- Holder reasons: the stub writes reasons from its own matched terms, at most
  12 words; broken variants for the A8 probe quote child facts in reasons,
  return the gist with the pointer, put a model-written title copied from the
  leaf's first sentence into the pointer, and build titles that keep tokens
  containing a digit.

All stub outputs are deterministic given inputs and a seed. A latency-injecting
wrapper with one global concurrency limit is used for the availability probe
(EXPERIMENT 9.1 T5).

### 18.3 Embedding port

- Stub: 384-dim hashed bag-of-words, L2-normalised (deterministic).
- Real: `intfloat/multilingual-e5-small` at a pinned revision, with `query: `
  and `passage: ` prefixes, mean pooling, L2-normalised (LAB: without prefixes,
  top-10 overlap fell to 0.718), run outside Elasticsearch. E5 reads at most
  512 tokens, so a leaf is embedded in 480-token windows and its vector is the
  L2-normalised mean of the window vectors (ASSUMPTION; development ablation:
  max over windows).

## 19. Module layout

```
living_context/
  ids.py                 leaf, node, edge ids; pointer codec
  config.py              typed config; every ASSUMPTION constant lives here
  policy/                principals, roles, class lattice, joins, admission decision (pure)
  gateway/
    server.py            local socket server; the only ES client
    admission.py         byte, structure, connection, concurrency, idle, per-tenant caps and byte caps; revoke lane
    sessions.py          session resolution, authority-epoch table, bounded epoch journal with durable start record, compaction and fail-closed start, per-tenant send fence (revocation and erasure publication)
    journal.py           per-tenant tombstone journal (hash chain, replication, restore apply)
    validator.py         refuses documents without tenant, class, lineage (fail closed)
    indices.py           naming, aliases, provisioning checks, shard budget
    es.py                stdlib HTTPS client; reads key files by path
    guard.py             shared tombstone guard used by every write, rebuild, replay, restore
  keystore/              one SQLite secure_delete file per tenant (WAL, checkpoint after destroy), AES-GCM, HMAC subject and principal tokens, canonicalisation
  model/                 port, stub, capped CLI client, cache, budget counter, per-tenant fair queue
  embed/                 port, hashed stub, E5
  ingest/                segmentation, subjects, write path
  structure/             families, build, insert, split/merge, manifests, heads, generations, forwarding
  graph/                 candidates, typing, weights and decay, hubs, expansion
  recall/                fan-out, holder, organise, digest, direct search, fusion, lookup, ask, explore
    compare/             es_routing.py (C1), collapsed.py (C2): COMPARISON only
  feedback/              protocol log, reflection, judge, reverse audit, reorganiser, gate
  erasure/               closure, executor, purge, receipts
  verifier/              separate entry point and keys; canaries; isolation and erasure checks
  evalkit/               dataset adapter (field whitelist), splits, metrics, bootstrap, score intervals, replay with lag, planting, reader driver, controls, budget.py, gate_power.py
tools/                   provision_tenant.py (operator), probes/ (P1 to P16)
tests/                   unit/, stubloop/, security/, faults/
```

Dependencies (pinned with a lockfile): Python 3.12, numpy, cryptography; for
real embeddings only, torch CPU and transformers. The Elasticsearch client is
the standard library over HTTPS (LAB pattern), so no extra dependency holds
credentials.

## 20. Limits that remain

| Limit | Why it cannot be designed in now |
|---|---|
| Model provider sees leaf text | Holder, build, judge and reader calls need content. Removing it needs a local model of adequate quality; out of scope for the first build |
| Noisy-neighbour latency across tenants and classes on a shared cluster | Elasticsearch thread pools and nodes are shared; gateway quotas bound it, only a separate cluster removes it |
| The 64 KiB kernel-held bound is measured on loopback only | Probe LC-PB measured TCP and unix sockets on loopback with a peer that does not read. Remote peers, TLS records and peer-side receive-buffer autotuning are not measured; the bound there is a target, not a result |
| Tenant boundary: one names-only channel | Probe P2 failed as worded: `_mget` on another tenant's alias name returns the concrete index name to a tenant key. Tenant keys never leave the gateway, no Elasticsearch response is forwarded, and physical suffixes are random; whether that suffices or tenants move to separate clusters is the owner's decision (ASSUMPTION until then). The cluster boundary sits behind the provisioning interface so one cluster per tenant is a configuration choice |
| Canonicalisation over-matches by design | The confusables skeleton can map two different people (`clark` and `dark`) to one token. Registration of an identifier whose token equals an existing identity's is refused (fail closed); two existing identities are never merged and an erasure of one token reaches only the identifiers declared under it. Held matches go to the review queue (7.3) |
| Mentions under undeclared spellings or paraphrase in source leaves | Detection cannot find what it was not told about; lineage closure covers derived artefacts, not new leaves that mention the person in a new way |
| Legal sufficiency of crypto-shredding and the backup bound | No primary regulator text was found; needs counsel |
| Embedding inversion | Vectors are treated as content and erased with it; a model that inverts vectors held by an attacker before erasure is out of scope |
| Pooled tier leaks term statistics | Inherent to shared indices with BM25; offered only with that label |
| API key invalidation latency on multi-node clusters | Undocumented; measured on our single node only (P6) |
| Classes are a small declared lattice | Per-person classes at scale would exceed the shard budget; per-person privacy at scale needs per-person tenants |
| Exposure-bias correction assumes a measurable examination model | No source measures IPS on agent memory logs; propensities are measured by exploration, and the correction is labelled ASSUMPTION |
| Cost of the owner's routing | 25 to 77 model calls per recall (estimate); measured by the canary; no design change can make agent-per-holder routing cheap without changing the method |
| Persistent holder agents | Not used (stateless calls); a persistent holder would see other readers' prompts |
| Filesystem and SSD remanence of destroyed keys | The keystore overwrites and checkpoints, but freed blocks and flash wear levelling are below the file layer; removing this needs a KMS or HSM, out of scope for the first build |
| Tokens left in log files and the tombstone journal | Logs hold no content but may hold subject or principal tokens until `R_LOGS`; the journal keeps erased tokens and identifier HMACs for the tenant's life (O-5) |
| Tombstone journal replica | Its second location is an operator choice; if it is lost together with the primary, the tenant is not served until restored (fail closed) |
| Unclean gateway restart closes tenants | `revoke` returns before its journal write is durable (4.4), so a crash or power loss cannot be told apart from a lost revoke; every tenant whose journal lacks a clean shutdown record after its durable start record is closed until break-glass recovery and the named administrators' re-grants. Recovery can re-apply only the revokes whose receipts survive: a revoke denied in memory whose receipt was lost with the gateway (a revoking tool that crashed too) is unknown to the operator and is not re-applied, so its principal is re-admitted if an administrator re-grants it. Copy-on-write filesystems can still report no space inside a preallocated file; that also closes the tenant. Compaction needs an atomic exchange of two names (`RENAME_EXCHANGE` or `RENAME_SWAP`, ASSUMPTION); provisioning refuses a tenant whose journal filesystem lacks it. The power-loss cases are tested by simulation of the filesystem's durability rules (EXPERIMENT R7), not by cutting power to a real device |
| A node whose build keeps failing holds W back | Publishing a W the tree does not represent would hide pending leaves (review 2, F6); the members stay pending and reachable, but gists and summaries for that class stop advancing until the call succeeds; failures are counted |
| The gate's test assumes symmetric cluster means | The exact sign-flip test (13.3) is valid when cluster means are symmetric about 0 under no change; a candidate with frequent small gains and rare large losses can be adopted (simulated 0.17 at K = 8, about 0.07 at K = 60, EXPERIMENT 7.3). A test valid without the symmetry assumption (a Hoeffding bound) would need a mean of at least 0.87 at K = 8 and would almost never adopt. So the gate is an inspectable heuristic, not a false-adoption guarantee: the general replay guard limits visible losses on recent searches and RANDOM-FEEDBACK shows what random adoptions do to recall, but neither, nor the p-value, bounds the false-adoption rate; sign scoring sees how often clusters improve, not by how much. H3 and H4 are decided only on their independent forward and held-back measures (review 3) |
| Descriptors can convey an answer without a lookup (review 3, F11) | INV-7 is a field contract: no body, gist or fact field reaches a pointer or the digest, titles are code-built without digit tokens, reasons and labels are bounded. It is not semantic non-disclosure: a 4-term title or a 12-word reason can still name a decisive fact (a name, a place, a topic; a date as a month name, a spelled-out amount or an alphabetic code, since the digit rule drops only tokens containing a digit and reasons and outline labels have no digit rule; a leaf of a few words is nearly its own title). So `opened` measures lookups, not use: an unopened pointer may have been used, and "not opened" is never evidence of irrelevance by itself (9, 10, 11, 12); an edge used only through descriptors gets no confirmation and decays (16.4). Non-content descriptors would remove this, but the owner asks for "a tight summary" as well as "pure reference pointers"; the reading is an ASSUMPTION about the owner's intent (EXPERIMENT 12, X9) |
| Erasure delays the tenant's own writes | The write gate of the erasure boundary delays that tenant's commits for at most `B_ERASE` (14.3, INV-9); other tenants are not affected |

## 21. Lessons from the earlier lab (LAB-R1 to LAB-R12)

| Id | Lesson | Where applied |
|---|---|---|
| LAB-R1 | A shared 32-bit lock key let one tenant evict another's sessions via a collision | INV-10 |
| LAB-R2 | Revocation that depends on evicting the revoked party was defeated three times; no design bounds revocation if the application holds a direct store credential | INV-5, INV-9, 4.4 |
| LAB-R3 | Nothing is cancelled because of another request; revocation is one statement | 4.4 |
| LAB-R4 | Every side path that stores content needs the main path's erasure checks; re-scans against current identifiers cannot see erased subjects | INV-8, 5.9, 7.3 |
| LAB-R5 | Parse after admission; bound bytes, structures, concurrency, idle time and per-tenant slots | 7.1, 15 |
| LAB-R6 | Verifiers need a positive control in the encoding the secret actually has; compressed values are invisible to grep | 14.7 |
| LAB-R7 | Ids that grant access are secrets everywhere; one policy | 6.3 (keys never logged) |
| LAB-R8 | A verdict must establish the requirement it names: count steps, no skips, mutants that must turn it red | 14.7, EXPERIMENT 9 |
| LAB-R9 | Tests must drive the real writer, not insert the first object directly | 14.7, EXPERIMENT 9 |
| LAB-R10 | Identity comes from an authenticated session, never the caller; deterministic subject tokens linked people across authors; floating model tags break pins | 3, 5, 5.8 |
| LAB-R11 | Erasure at scale: a 100,000-event subject must finish; every erasure paid a 22 to 30 s lease wait; a test node hit the 1,000-shard limit | 6.4, 14.5 |
| LAB-R12 | Document-level security is not a tenant boundary (term statistics leak, reproduced) | 4.1, 6.1 |

## 22. Index of DEVIATIONS and cost-relevant ASSUMPTIONS

DEVIATIONS from the owner's description (numbered in order; review M4):

1. Keystore outside Elasticsearch (section 3): Elasticsearch deletes are soft
   until merge, so keys could not be destroyed within a bound. The epoch table
   and the tombstone journal are also outside Elasticsearch (3, 4.4, 14.6),
   for the same kind of reason: revocation and erasure must not depend on a
   store another tenant can make unwritable, or on a snapshot that predates
   them.
2. A safety budget on holder calls (`B_HOLDERS`, `B_FANOUT`, deadline;
   8.2): the owner set no limit; without one a broad prompt can ask every
   holder. Unopened branches are returned as pointers, so nothing is hidden,
   only deferred. (Labelled DEVIATION because it can stop an expansion the
   owner's text would continue.)
3. Organise call skipped when only one child returned results (8.3): saves
   calls; ablation "organise at every parent" restores the literal reading.
4. Holder failure falls back to embedding routing for that holder (8.2),
   flagged in the log: the alternative, returning nothing, would silently
   drop branches.
5. Recall is restricted by access class before the fan-out (INV-2): the owner
   described one tree; with classes there is one tree per class, and a reader
   with several classes fans out over several roots.
6. Erasure rewrites stored documents in place (sections 14.2, 14.3, 14.4): the
   owner asked for everything versioned and also for erasure designed in from
   the start. Where they conflict, erasure wins: the content of an affected
   node version in a retained generation is replaced (identity kept, rewrite
   recorded) instead of being kept as history; query-shaped members and edges
   are removed from retained generations; and a log row of another actor that
   offered an erased item has that pointer and its subject tokens stripped
   (identity and the rest of the row kept, rewrite recorded). Keeping the old
   versions would keep the erased content.
7. The proxy world (EXPERIMENT 4.2; review F8): for cost, supporting runs of
   the loop's controls use Elasticsearch-query routing (C1, the coordinator's
   suggestion the owner did not adopt) instead of agent routing. Every deciding
   comparison and control runs agent-routed; proxy results are labelled and
   decide nothing.

ASSUMPTIONS that change cost or results: `LEAF_CAP` 1,536; fan-out 16/24/4;
`M_MAX` 2; `B_HOLDERS` = top-level holders + 36, `B_FANOUT` 6, at most 12
organise calls; organise threshold; L1 summary refresh trigger (4 new
children or 25%); top-3 typed edges per leaf; 2-hop, 10-neighbour, 50-leaf
expansion caps; edge weights and 30-day half-life; exploration share 1 in 10;
IPS clip 0.05; gate: exact cluster sign-flip test, alpha 0.05, at least 8
clusters, at most 60 targeted queries in clusters of at most 6, per-query
sign scoring, one evaluation per candidate, general-replay count guard
(13.3); reverse audit `N_AUDIT` 100 leaves and `K_CAND` 5 searches per cycle,
re-filing support `S_MIN` 2 missed relevant searches and miss rate at least
0.5 (12); reasons and outline labels at most 12 words with the 5-gram rule;
titles code-built from 4 tf-idf terms with no token containing a digit
(8.2); INV-7 read as a field contract, not semantic non-disclosure
(review 3, F11); retention 5 generations or 14
days, ancestry ids kept for the class's life (5.6); `P_PURGE` 1 hour;
`R_KEYS` 35 days; `R_LOGS` 7 days; erasure boundary `B_ERASE` 60 s (14.3);
write-path deadline `D_WP` 10 minutes (5.6); send buffer requested at
32 KiB and each hand-off capped at 64 KiB minus the queued byte count, per
connection, and an open-connection cap per tenant, under
one per-tenant send fence (4.4, 15); epoch journal `J_SIZE` 4 MiB per file,
two files per tenant (journal and spare), compaction by `RENAME_EXCHANGE`, `N_PRINCIPALS` 10,000, `D_FSYNC` 2 s
(4.4); final-list fusion by RRF of the tree, direct and linked lists
(k = 60, cut 50); reading of "never offered" as `tree_missed` (10);
reverse-audit synthetic queries off by default (12).
