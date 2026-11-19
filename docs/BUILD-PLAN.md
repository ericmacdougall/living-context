# living-context: build plan

Ordered steps, each small enough to commit green on its own. Each step names
its failing-first test: a test written before the code, shown to fail against
the previous commit or a named broken variant, then shown to pass. A test that
cannot fail for the stated reason does not count (LAB-R8). Security probes
start at step 5 (the first step with a gateway) and run in every later step
(EXPERIMENT.md section 9; the T5 floods in the short form of section 0);
steps 1 to 4 have their own policy and keystore tests.

Order (revised after review 2): the gateway, policy, lineage validation,
source and keystore lifecycle, publication and versioning rules, revocation,
erasure, restore and the independent verifier are built from the first
steps. Edges, reflection, the reverse audit and the reorganiser with its gate
are built early as minimal real producers driven by the stub model (Steps 12,
17, 18, 19), so the security track exercises every artefact kind they write
(edges, notes, judge labels, query-shaped members and summaries). Their
refinements (sophisticated audit ranking, IPS-weighted feedback, hub policy,
co-confirmation splits), the comparison arms and the proxy world come after
H1, behind the same interfaces (section 3A); nothing that H1 needs waits for
them.

Labels as in [DESIGN.md](DESIGN.md) section 0.

## 0. Rules for the whole build

- Nothing secret in the repository: no passwords, API keys, key ids, CA
  private keys, session tokens, host names, absolute home paths or dataset
  files. Configuration is read from a TOML file whose path is given by
  `LC_CONFIG`; the repository holds only `config.example.toml` with
  placeholders. Secrets are read from 0600 files by path, never from argv or
  environment values, never logged (LAB-R7).
- Every step ends with `python -m unittest discover -s tests` green, the
  security probes of S0-core green for everything built so far, and every
  broken control red. The T5 floods are not re-run in full at every step:
  their full measurement is the S0-load checkpoint (Step 22); a step that
  touches admission, quotas, the model or embedding port, or byte caps runs
  them in the short form of Step 5.
- No model calls until stage S1 is approved (EXPERIMENT.md section 10). Steps
  1 to 22 use the deterministic stub only.
- Heavy jobs (whole-pool embedding, whole-pool builds) run on a remote host,
  not the owner's laptop.

## 1. Host setup

### 1.1 Python

- Python 3.12, a virtual environment outside version control (`.venv/` is
  already ignored).
- `requirements.in` and a hashed lockfile `requirements.lock` (pip-compile
  `--generate-hashes` or `uv pip compile --generate-hashes`). Core: numpy,
  cryptography. Extra `embed`: torch (CPU wheel), transformers. Exact versions
  pinned in the lockfile; no unpinned installs.
- The Elasticsearch client is the standard library over HTTPS (no extra
  package holds credentials).

### 1.2 Elasticsearch (test cluster)

- Image `docker.elastic.co/elasticsearch/elasticsearch:9.5.6` (the version the
  earlier lab's Basic findings were made on), pinned by digest; the digest is
  recorded in `docs/HOST.md` at setup.
- Single node, `discovery.type=single-node`, `xpack.security.enabled=true`,
  HTTP TLS with a CA generated at setup into a host directory outside the
  repository, Basic licence (the default; never start a trial).
- Bootstrap credential: generated with a random generator and redirected
  straight into a 0600 file, mounted into the container and passed by file
  (`ELASTIC_PASSWORD_FILE`; PENDING_REAL_TEST P3), so it never appears in a
  command line, environment value, log or terminal. It is used only by
  `tools/provision_tenant.py` to create API keys, which are written to 0600
  files. After provisioning the operator may move the bootstrap file offline.
- Memory: heap 2 GB for the fixture and development pools; whole-pool runs on
  the remote host with at least 8 GB heap (ASSUMPTION; measured at S2).
- Bound to loopback only.

### 1.3 Embedding model

- `intfloat/multilingual-e5-small`, revision
  `614241f622f53c4eeff9890bdc4f31cfecc418b3` (MIT licence), downloaded into a
  cache directory outside the repository; the downloaded files' sha256 sums
  are recorded in `docs/HOST.md`.
- Leaves over 480 E5 tokens are embedded in 480-token windows; the leaf vector
  is the L2-normalised mean of window vectors (ASSUMPTION; ablation:
  max-over-windows via a window index, development only).
- Whole-pool embedding (about 58k leaves, on the order of 150k windows; the
  earlier lab took about 2.1 hours for 150,526 chunks on a loaded 12-thread
  host) runs on the remote host only.

### 1.4 Dataset

LongMemEval-S cleaned, placed outside the repository; its path comes from
the config file; its sha256 is checked at load.

## 2. Step 0: platform probes (PENDING_REAL_TEST, before any design code)

Each probe is a script in `tools/probes/` that prints a machine-readable
verdict and is committed with its result file. Each has a fallback that the
design already names.

| Id | Probe | Expected | Fallback if not |
|---|---|---|---|
| P1 | Basic licence matrix: `rrf` and `linear` retrievers, DLS role (`query`), FLS role (`field_security`), `semantic_text` indexing, `bbq_disk` mapping; lexical percolate; kNN with `int8_hnsw` at 384 dims; aliases; force merge | licence errors for the first five; the rest work (LAB observed) | none needed for refusals; any surprise is recorded and the design re-checked |
| P2 | API key with role descriptor on `lc-tena-*` only and no cluster privilege, security on: search, get, mget, msearch, count, kNN, percolate, terms lookup, `_cat/indices`, `_field_caps`, `_mapping`, wildcard and comma lists, alias names against `lc-tenb-*` (including `lc-tenb-authz`) and the look-alike `lc-tenbx-*`; plus the cluster APIs `_cluster/state`, `_cat/shards`, `_cat/aliases`, `_tasks?detailed`, `_resolve/index`, `_search_shards`, `_nodes/stats`, `_cluster/stats`. Control: the same with a key holding `cluster: monitor` must leak through at least one cluster API (so the probe can fail) | all refused or empty, no names or query descriptions leaked | this is the hard boundary: if it fails, dedicated tenants move to separate clusters and the gap is reported before any other step |
| P3 | `ELASTIC_PASSWORD_FILE` honoured; the password appears in no container log | yes | operator runs the reset tool with output redirected to a 0600 file |
| P4 | Percolate a stored `knn` query | fails or unsupported | already designed lexical-only; record |
| P5 | Realtime GET by id returns a document version immediately after the index call returns, without refresh (used for sessions and heads; tombstones and epochs are in the gateway's own memory, DESIGN 4.4, 5.9); and an explicit `_refresh` that has returned makes every write acknowledged before it searchable (the searchable-publication barrier, DESIGN 5.6; review 2, F4) | yes | for heads: the gateway keeps heads in memory too, reloaded on start; for refresh: none, P and heads are not published, and the gap blocks Step 13 |
| P6 | API key invalidation latency on our node | under 1 s | gateway drops its client for a revoked tenant at once (it is the only client); report the measured latency as a limit |
| P7 | kNN `filter` with a restrictive filter returns k hits (filter applied during search) | yes | over-fetch with `num_candidates` up to 10,000 and report recall loss; per-class indices already make the filter mostly unnecessary |
| P8 | One `_aliases` request with add and remove actions is atomic under concurrent search | yes | gateway write gate held across the swap; readers retry once on a missing alias |
| P9 | `index.merge.policy.expunge_deletes_allowed: 0` accepted; `only_expunge_deletes` on a live index brings `docs.deleted` to 0 | yes | copy-and-swap purge (DESIGN 14.5) for every erasure |
| P10 | Time from delete to `docs.deleted == 0` (retention leases on a single node) | measured | the bound in receipts is the measured figure |
| P11 | Index with stale `if_seq_no` returns 409 and writes nothing | yes | lease document per head with the same probe on the lease |
| P12 | `_msearch` with one failing sub-search: others succeed, per-item errors | yes | one request per sub-search |
| P13 | `terms` on `src_id` with 10,000 ids; `index.max_terms_count` default 65,536 | works | batch the ids |
| P14 | Revoke with `lc-tena-authz` blocked (`index.blocks.read_only_allow_delete`) and, separately, with the disk driven to the flood-stage watermark on a scratch volume | revoke takes effect at once from the gateway's table; the Elasticsearch persist is retried and reported | none: revocation must not depend on the write (DESIGN 4.4); a failure here blocks Step 15 |
| P15 | A tenant key owner (native user with index privileges on `lc-tena-*` and `manage_own_api_key` only) creates, rotates and invalidates its own keys; created keys carry at most its privileges; the gateway's keys cannot create or invalidate keys | yes | the operator keeps the bootstrap credential reachable for rotation; the gap is reported as a limit |
| P16 | Search and indexing slow logs disabled (`-1`) are accepted on every index kind; with default logger levels, no request body or document field reaches the Elasticsearch or container logs; with a scratch index at slow-log threshold 0, a control marker does appear (positive control) | yes | raise logger thresholds further and repeat; report |

Commit: `test(probes): platform probes and recorded results`. Failing-first:
each probe script is run once against a cluster with security off and must
report "security off: P2 not established" rather than a pass (a probe that
passes with security off is broken).

## 3. Build steps

Each step: what, failing-first test, done when.

### Step 1: package skeleton and repository hygiene

- `living_context/` with empty modules per DESIGN section 19; `config.py`
  holding every ASSUMPTION constant with its label in a comment;
  `config.example.toml`.
- `tests/unit/test_repo_hygiene.py`: scans tracked files for absolute home
  paths, private-key headers, strings shaped like Elasticsearch API keys, and
  the dataset's gold-id prefix.
- Failing-first: a planted file containing a fake key-shaped string and a home
  path makes the test red; removing it makes it green.

### Step 2: ids and pointers

- `ids.py`: 128-bit random leaf and node ids, deterministic edge ids, pointer
  codec `(gen, id, version)`, opaque external form.
- Failing-first: a broken generator that reuses ids across a restart (seeded
  from time in seconds) is caught by a uniqueness test over 1,000,000 ids and
  a restart simulation.

### Step 3: policy core (pure)

- `policy/`: principals, roles table (DESIGN 4.3), class lattice with join,
  read and write sets, admission decision function.
- Failing-first: table-driven tests over every (role, operation, class)
  combination; broken variant "reader may read any class of its tenant" and
  "join = the first input's class" must fail.

### Step 4: keystore

- `keystore/`: one SQLite file per tenant with `secure_delete=ON` and
  `journal_mode=WAL` set explicitly, `wal_checkpoint(TRUNCATE)` after every
  destroy batch; AES-256-GCM data keys per leaf version, per-tenant HMAC subject
  and principal keys, identifier canonicalisation (NFKC, case folding,
  confusables skeleton), destroy by subject token or leaf id.
- Failing-first: after destroy, decrypting the source ciphertext fails, and a
  byte scan of the database, `-wal` and `-journal` files finds neither the key
  bytes nor the key id (positive control: before destroy, and before the
  checkpoint, the scan finds both, including in the `-wal` file). Broken
  variants: `secure_delete=OFF` must leave the key bytes in the database file;
  no checkpoint after destroy must leave them in the `-wal` file; one shared
  file for two tenants must let tenant A's long write transaction block tenant
  B's destroy (lock-wait test). N = 1,000 keys. Canonicalisation: a table of
  variant spellings (case, width, accents, homoglyphs) maps to one token;
  broken variant without the confusables map must fail on the homoglyph rows.

### Step 5: gateway foundation and the first security probes

- `gateway/`: local socket server, admission (bytes before parse, structure
  count, one semaphore around parse and dispatch, idle deadline, per-tenant
  slots and queues, a per-tenant open-connection cap (DESIGN 15), per-tenant
  stored-byte caps, a separate revoke lane),
  sessions minted only by an identity adapter from a proof, session tokens
  carrying the tenant prefix, per-tenant `lc-<t>-authz` with HMAC principal
  tokens, the in-memory epoch table with its bounded per-tenant journal
  (DESIGN 4.4; this step builds the table, the journal and its spare file
  only: the durable start record, compaction, the clean stop and break-glass
  recovery that DESIGN 4.4 requires are completed in Step 15, before any
  tenant traffic outside tests), `es.py` reading key
  files by path, `indices.py` naming (including the per-class control index)
  and shard-budget check (DESIGN 6.4 formula), `validator.py` refusing
  documents without tenant, class or lineage; slow logs disabled on every
  index; no body logging.
- `tools/provision_tenant.py` creates the tenant's indices, aliases, its
  registry row in `lc-sys-registry`, its tenant key owner, and API keys with no
  cluster privilege (DESIGN 6.3), preallocates the tenant's journal file and
  spare, and refuses the tenant if an atomic name exchange fails on the
  journal directory (DESIGN 4.4).
- Failing-first tests (security track begins): SEC-T1 (with the cluster
  APIs), T2, T3 (normalised bodies) on the fixture tenants; T5 flood probes
  including the disk-fill flood against the byte cap, in a short form (one
  1-minute run per flood kind with its unloaded baseline, ASSUMPTION: a smoke check that
  each broken control turns it red, never reported as a T5 pass; the full T5
  is S0-load, Step 22); T6 key-space collision;
  the validator refuses a document without `lin_subjects`; an authz document
  of `tena` is unreachable with any `tenb` key. Broken controls: `lc-*`
  descriptor; tenant from body; parse before admission; 32-bit lock key; key
  with `cluster: monitor`; no byte cap. Each must turn its probe red.

### Step 6: write path, source log and leaf projection

- `ingest/`: segmentation (session and gap cuts, embedding valleys, hard cap
  with recursive split), subjects (identifier declarations), tombstone guard,
  source append, projection (with the code-built leaf title, DESIGN 5.2: the
  top 4 tf-idf terms, leaving out every token that contains a digit),
  leaf head compare-and-set, dirty queue.
- `embed/`: hashed stub.
- Failing-first: the longest session in the dataset shape (a 78,000-character
  synthetic session) produces no leaf over the cap (broken variant: split only
  at round boundaries fails); empty input refused; idempotent retry returns
  the same leaf; the same idempotency key with a different body is refused
  (broken: returns the first result); a projection without its source document
  is repaired; a bare-name match of an erased identifier is held, with the same
  outcome a writer sees for any hold; a stored leaf whose top tf-idf terms
  include a numeric date, an amount and an account code written with digits
  gets a title with none of those tokens (broken: a title builder that keeps
  tokens containing a digit); digit-free forms such as a month name may
  remain (DESIGN INV-7, 20).

### Step 7: direct leaf search

- `recall/direct.py`, `recall/fusion.py`: BM25 plus kNN per class index,
  client-side RRF (k 60, window 50).
- Failing-first: on the fixture, a needle reachable only lexically and one
  reachable only semantically are both in the fused top 10 (broken: BM25 only
  fails the semantic needle); a reader with `base` only never touches a
  `restricted` index (request plan assertion; broken: query all class indices
  and filter after).

### Step 8: stub model port and budget counter

- `model/port.py`, `model/stub.py`, `model/cache.py`, `model/budget.py`,
  `model/fairq.py`: task schemas, deterministic content-dependent stub (DESIGN
  18.2) with local child labels c1 to cN, persistent counter incremented before
  the call, cache keyed with tenant, join class, task, prompt version, pinned
  model id and scope (DESIGN 5.8), per-tenant fair queue in front of one global
  concurrency limit; the same fair queue on the embedding port.
- Failing-first: a stub router given shuffled child entries selects different
  children (the stub depends on content); a cache lookup with a different class
  misses (broken: key without class hits); a route over a base node from a
  restricted channel is stored in the restricted cache (broken: cache class
  equals node class); a route cached for principal X misses for principal Y
  (broken: prompt-bearing cache shared across principals); the counter refuses
  at its cap and counts a failed call; with one tenant flooding, another
  tenant's call waits at most its fair share (broken: one FIFO queue).

### Step 9: tree build (one family, one class)

- `structure/`: bulk build by balanced clustering to `F_TARGET`; level-1 build
  calls produce child entries and summaries; upper levels; index-of-indexes
  rule; node versions; manifest; head compare-and-set.
- Failing-first: a fixture of 2,000 leaves produces no node over `F_MAX`, no
  node under `F_MIN` except the last, and a top level of at most `F_MAX`
  (broken: no index-of-indexes rule gives a 125-node top level); sizes and
  totals equal the sum of leaf sizes.

### Step 10: the owner's fan-out read path with the stub

- `recall/fanout.py`, `holder.py`, `organise.py`, `digest.py`, `lookup.py`,
  `fusion.py`: all top holders asked in parallel, asynchronous expansion,
  budget, fallback on failure, organise step, digest with sizes and totals,
  pointers only (pointer, title, size, path, reason, confidence; no body,
  gist or fact field, DESIGN INV-7, a field contract since review 3, F11),
  code-built titles for every leaf and node (top 4 tf-idf terms, no token
  containing a digit), the reason schema (at most 12 words) and the 5-gram
  check on reasons and outline labels,
  chunked holder calls above `F_MAX` entries, pending members shown with a
  code-built title, `last_seen` never shown, the three-list RRF merge rule
  (EXPERIMENT 5.1), gateway-logged `opened`, typed resolution answers.
- Failing-first: on the fixture, stub A1-tree-only recall@10 (no direct-search
  fusion) at least 0.80 and SHUFFLED-SUMMARIES at least 0.30 lower (the stub
  gate in EXPERIMENT S0); a digest never contains a body, gist or fact
  field, including for a one-sentence leaf (broken: return the gist with the
  pointer, which hands over the whole one-sentence leaf; SEC-A8 red); every
  title in a digest equals the code-built title of its leaf or node (broken:
  a model-written title copied from the leaf's first sentence; SEC-A8 red);
  a leaf whose top tf-idf terms include a date, an amount and an account
  code written with digits gets a digest title with none of those tokens
  (digit-free forms such as a month name may remain, DESIGN INV-7, 20;
  broken: a title that keeps tokens containing a digit; SEC-A8 red); no reason or outline label shares a
  5-gram with the leaf text, facts or gists in its call's input (broken:
  reasons quoting child facts; SEC-A8 red); a
  holder prompt never contains `last_seen` (assertion on the captured
  prompt); a node with 3 x `F_MAX` entries is asked in 3 chunks; a holder
  failure produces a flagged fallback, not an empty branch; an id not
  presented to a holder is dropped and counted; SEC-A1, A3, A4 green with their
  broken controls red.

### Step 11: protocol log and exploration slots

- `feedback/log.py`: one row per offered item with propensity, the set of
  sources and `offered_by_tree`, rank; `opened` from the gateway; `used_cited`
  separate; exploration slots placed after the ranking and excluding leaves of
  the last `L` insert batches; subject detection on the query text, whose
  tokens go into `lin_subjects` of the row and the stored query.
- Failing-first: an agent that claims use without a lookup leaves `opened`
  false (broken: take `opened` from the agent's claim); every row carries its
  join class (broken: item class only, SEC-A5 red); a query that mentions a
  declared subject carries that subject's token in its row and stored query
  (broken: detection on leaves only, SEC-E9 red later in Step 14).

### Step 12: edges and bounded expansion

- `graph/` as a minimal real producer (review 2): candidates (kNN 10, shared
  entities), stub typing, initial weights, specificity, supersedes and
  contradicts rules, edge versions bound by `src_seq` (insert-time) or
  `gen_from` (rebuild, reorganiser) and a `gen_to` set, expansion with caps and
  the visibility filter of the task's pin (DESIGN 13.2: the pinned write-path
  watermark P for write-path versions, the pinned generation's ancestry for
  the others), version counters of re-created edges above erased versions.
  Decay, the degree cap and hub nodes (DESIGN 16.4) come after H1 behind the
  same interface (3A); until then the degree cap only refuses edges past it,
  counted.
- Failing-first: on fixture questions whose evidence spans two leaves linked
  only by an entity, A2 finds both and A1 does not; RANDOM-EDGES removes the
  gain; an edge between a `base` and a `restricted` leaf lives only in the
  restricted edge index (SEC-A2, broken: store in the source leaf's class);
  a supersedes cycle becomes contradicts; an edge written on insert is
  invisible to a task pinned before P covered it, for that task's whole life,
  and visible to a task pinned after (broken: expansion reads the latest
  edges, SEC-V2 red); an insert-time edge and a placement member written while
  a reorganiser candidate is open stay visible to new tasks after that
  candidate is rejected (broken: stamp them with the next generation number,
  which the candidate takes, so the rejection strands them; SEC-V7 red, 100
  cycles); a link job held past `D_WP` is cancelled, its edges deleted before P
  moves, and no edge of it appears afterwards (broken: advance P without
  joining the job; a late edge appears under a pinned task, SEC-V2 red).

### Step 13: incremental insert, rebuild, concurrency and versions

- Per-tenant source sequence and the write-path watermark P with its
  `D_WP` deadline (DESIGN 5.6); placement, pending members (above the pinned
  W, at most the pinned P), batch rebuild with lease, split, merge, collapse
  (old members retired by a `gen_to` entry, new ones written with `gen_from`),
  forwarding records, rebase on a lost head compare-and-set, retention and
  retirement of generations.
- Manifests with the full ancestry (kept for the life of the class; retention
  removes content, never ancestry ids; DESIGN 5.6) and source watermark W
  (monotone along ancestry); the visibility rule for members and edges (DESIGN
  13.2: pinned P for write-path versions, ancestry for rebuild and reorganiser
  versions, `gen_to` sets for retirement); the searchable-publication barrier
  (P and every head flip only after an explicit refresh of the touched member,
  edge and node indices has returned; DESIGN 5.6); a rebuild that aborts the
  whole generation when a build call fails for a node with pending members
  (DESIGN 7.6); deletion of a rejected or rebased candidate's unshared nodes,
  members and edges; an evaluation-only pinning hook that keeps planted
  entries out of normal rebuilds (used by `evalkit` only, EXPERIMENT 3.6).
- Failing-first: SEC-V1, V2 (tree and edges), V4 (with edges and rollback), V6,
  V7 with broken controls (node id reuse, re-read head per wave, flip without
  compare-and-set, expansion reading the latest edges, candidate edges visible
  at write, rollback leaving the candidate's edges visible); V2 with index
  refresh paused by a test hook independently of write acknowledgement, tasks
  started without waiting for refresh (broken: publish P or a head on
  acknowledgement; an edge appears mid-task); V2 and V4 across a real
  retention expiry with `R_GEN` shortened, including a retired edge (broken:
  ancestry truncated to retention; the adopted edge vanishes and the retired
  one returns); V7 with one failing build call in a rebuild (broken: publish
  the advanced W with the failed node at its old version; the pending leaf
  drops out of tree-only recall); a leaf written during a rebuild is found by
  direct search immediately, by the tree as a pending member once P covers it,
  with its code-built title from the start, and, for holders, with a gist
  after the next batch.

### Step 14: erasure

- `erasure/`, following DESIGN 14.3 (review 2, F7): the request; the
  per-tenant write gate in the gateway (commits take the shared side, the
  executor the exclusive side); under the gate a refresh and the full lineage
  closure, including ancestors through transitive `lin_nodes` lineage, authz
  records and query-shaped artefacts (DESIGN 14.2), written as a redact set
  tagged with the new sequence; the boundary: tombstone appended to the
  per-tenant tombstone journal (`gateway/journal.py`: hash chain, fsync,
  replication to the second location), recorded in `ctl`, refreshed, and made
  effective with its redact set in one in-memory step; the per-tenant send
  fence (DESIGN 4.4; review 3, F7), built here because erasure needs it first:
  every final send check (tombstone sequence and epoch) and its non-blocking
  socket write under the shared side, each accepted connection's send buffer
  requested at 32 KiB and every hand-off capped at 64 KiB minus the
  connection's queued byte count (`SIOCOUTQ`, read under the shared side;
  measured on loopback only, ASSUMPTION after probe LC-PB), the publication step under the
  exclusive side, no journal, disk or Elasticsearch I/O under either side,
  the fence as the innermost lock (taken under the write gate, never the
  reverse), the in-memory scopes and redact-set ids that the check compares
  against, and the test hook that pauses a sender between its final check
  and its socket write (Step 15 adds revoke's use of the exclusive side); a
  response read before the boundary is aborted at its next hand-off; then
  key destruction,
  deletes, the log-row strip, rewrites and member and edge removal in every
  retained generation, purge (default and copy-and-swap), receipts with a
  requester copy over its read set; relabel as erasure of the old-class copy,
  returning at the boundary; restore (DESIGN 14.6, review 2, F8) that applies
  the newest journal to Elasticsearch and to the keystore, replays the closure
  over every restored index (authz and control included), rebuilds
  projections and enables the tenant only after the verifier's residue checks
  pass; `verifier/` as a separate entry point with its own keys, canaries
  (including query-only, authz and query-shaped canaries) and the plaintext
  log grep with its control marker.
- Failing-first: SEC-E0 to E6, E8 and E9 with their broken controls (redact
  set not consulted; tombstone published before the closure completes, shown
  by pausing the closure walk before an upper node reached only through
  transitive lineage; commits not held at the gate during the closure; a send
  hand-off without the tombstone re-check (a lookup read before the boundary
  sent in full after it); a send hand-off that does not hold the send fence,
  with 8 concurrent senders paused after their final check (E8 (d): a paused
  sender writes covered bytes after the boundary); a hand-off that writes
  without the queue-capped count (E8 (d) measures each paused sender's queue
  and it exceeds 64 KiB); a model-port wrapper keeping outputs in a file
  outside the indices (E1's data-root grep must find it); current
  generation only; key-only; raw-file grep of segments as verifier;
  string-search closure; closure without authz; reseal-before-quarantine; no
  erased-identifier HMACs; restore before tombstones; restore that re-applies
  tombstones but does not replay the closure over restored authz records;
  tombstones only inside the snapshot, which must resurrect content when an
  older snapshot and an older keystore backup are restored; keystore restore
  that keeps keys named by newer tombstones; job commit that does not check
  the published tombstones; hold ignored; receipt without recount; requester
  receipt with per-class counts; subject detection on leaves only); E7 records
  request-to-boundary time against `B_ERASE`.

### Step 15: revocation

- Revoke call on the gateway's in-memory epoch table (DESIGN 4.4, review 2,
  F9): denial in memory under the fence with no I/O inside it, return of
  `denied`, then the append and fsync to the tenant's bounded, preallocated
  journal with `durable_at` reported separately; a durable start record
  before any of the tenant's requests is admitted (fsync of the journal file
  and of its directory; review 3, F9); compaction in the order write the
  spare file, fsync, exchange the two names atomically (tested at
  provisioning, Step 5), fsync directory, with the new file carrying the
  current start record and the old one left as the next spare; the clean
  stop (admission and the revoke lane closed first, then the drain, then the
  shutdown record, never written for a closed tenant); the fail-closed rule
  (journal fault, `D_FSYNC` exceeded, or an incomplete journal at start
  closes the tenant, across restarts, until explicit break-glass recovery:
  the operator's call carries the principals of every revoke receipt without
  `durable_at` and the administrators to reopen; the gateway journals and
  fsyncs those revokes first, then admits grant calls only from the named
  administrators, who re-grant, and writes a recovery record and a new
  durable start record); the journal's I/O behind a thin layer that a
  test can replace by the power-loss simulation of EXPERIMENT R7; and
  best-effort persistence to
  `lc-<t>-authz`; revoke lane; epoch checks at admission
  and at each fan-out wave; revoke on the per-tenant send fence of Step 14
  (its writer-preferring exclusive side for the epoch flip and the discard of
  unsent buffers; one fence for revocation and erasure publication, DESIGN
  4.4); Step 14's pause hook between the final check and the hand-off; the
  two-administrator rule; no self-grant; the
  operator's break-glass revoke; class-grant removal as an epoch bump; tenant
  key owner rotation and invalidation (P15) and the gateway dropping its clients
  for a revoked tenant.
- Failing-first: SEC-R1 to R7 and T7 with their broken controls, including: no
  send fence with the injected delay (must fail deterministically); a
  blocking socket write inside the fence with a slow-reading client; revoke that requires the
  Elasticsearch write (with `lc-tena-authz` blocked, P14); revoke that waits for
  the journal fsync (over the bound with the fsync delayed); a journal error
  that does not close the tenant; a start that accepts a journal without a
  shutdown record; compaction in place instead of into the spare; one
  journal shared by all tenants; a shutdown record written before pending
  appends drain; a revoke lane left open during the clean-stop drain;
  admission before the start record's fsync returns; a start record without
  fsync (re-admission after a simulated power loss that follows a previous
  clean shutdown); compaction that exchanges names before the file fsync,
  skips the directory fsync, or drops the start record (the first two go red
  only through R7's check of the journal-rebuilt table); a shutdown record
  written for a fault-closed tenant; break-glass recovery that reopens every
  administrator before re-applying the receipts' revokes;
  single-administrator revoke of a revoker; self-grant; class
  removal without an epoch bump; rotation that needs the bootstrap credential.
  R7 runs on a small scratch filesystem for the out-of-space case, stops the
  gateway abruptly at each persistence boundary, and replays each simulated
  power-loss point through the test I/O layer.

### Step 16: access-class non-interference suite

- C0 and C1 fixture corpora; the 300-query byte-identity harness, and its
  structural mode (request plan and set equality) for real-model runs; relabel.
- Failing-first: SEC-A1 to A6 and A8 with all broken controls in DESIGN 8.7 and
  EXPERIMENT 9.2 (including a relabel read immediately after the relabel call
  returns, broken: relabel that waits for the next generation); A7 timing
  harness reports, with the A4 pair included.

### Step 17: reflection and the separate judge (stub)

- Minimal real producer (review 2): `feedback/reflection.py`, `judge.py`:
  three pools including the deeper, looser pool (c); a switch that removes pool (c) for the NO-AUDIT arm; judge
  blind to source; per-source misses (`tree_missed`, `any_missed`); notes with
  lineage (including query subject tokens) and join class; write-time
  background search.
- Failing-first: on the fixture, a planted relevant leaf not offered by the
  tree is labelled "relevant, tree_missed" even when the direct search offered
  it (broken: "never offered by any source" records nothing); the judge input
  contains no source tag (assertion on the prompt).

### Step 18: reverse audit

- Minimal real producer (review 2): `feedback/reverse_audit.py`: model-free
  candidate generation (batched percolation of every leaf against the stored
  queries added since the last cycle, and kNN of those queries' vectors over
  leaf vectors), candidate searches and judge labels accumulated per leaf
  across cycles, the gold-blind priority rule of DESIGN 12 (a sort), the caps
  `N_AUDIT` and `K_CAND`, judge confirmation, log check against
  `offered_by_tree`, Wilson miss rate, the flagging rule (`S_MIN = 2` missed
  relevant searches and miss rate at least 0.5; review 2, F3), pruning and
  archiving rules; synthetic queries behind a switch, off by default. Any
  ranking beyond that sort comes after H1 (3A).
- Failing-first: a planted mis-filed leaf whose two past queries were logged
  in different cycles and not offered by the tree gets a miss rate of 1 with
  both queries listed and is flagged (broken: candidates from the current
  cycle only, so support never reaches 2; broken: support threshold 5; broken:
  audit against synthetic queries only misses the real query); the audit's
  judge calls per cycle never exceed `N_AUDIT` (broken: uncapped, counted);
  with synthetic queries off, a leaf with no search history is never re-filed
  (broken: synthetic on re-files it); an archived leaf is still directly
  searchable and still erasable.

### Step 19: reorganiser and the replay gate

- Minimal real producer (review 2): `feedback/reorganise.py`, `gate.py`:
  signals (confirmed hits, judge misses, audit misses) without IPS weighting;
  the operations add membership, move and re-summarise, producing a candidate
  generation per learning cycle (members and edges carrying the candidate's
  generation and `lin_queries`); targeted replay with gate-only synthetic
  queries that are never stored, grouped into clusters by operation and
  spread over as many clusters as possible; judging of unjudged items at
  replay; the exact cluster sign-flip test (DESIGN 13.3, review 2, F12); the
  general-replay count guard; adoption and rollback by head compare-and-set;
  `evalkit/gate_power.py` (the simulation behind EXPERIMENT 7.3, already
  written and run). IPS-weighted signals, co-confirmation splits and merges
  and the hub operation come after H1 behind the same interface (3A).
- Failing-first: the exact p-value matches brute-force enumeration over all
  sign vectors for K up to 12; a candidate with fewer than 8 clusters is never
  adopted (broken: per-query test, which adopts one fully correlated cluster
  of 60 paraphrases); on the fixture (at least 16 plants), the oracle
  candidate is adopted and the random-moves candidate rejected (the gate
  positive controls; broken: a gate that rejects everything must fail the
  oracle check); on the fixture stream,
  LOOP repairs planted items in strata 2 and 3 at least 0.25 more often
  than NO-AUDIT (a fixture prediction; NO-AUDIT may still repair plants it
  saw through pool (b) or an open, EXPERIMENT 6); decoys are repaired less than plants;
  RANDOM-FEEDBACK and SHUFFLED-LOG candidates are rejected; rollback restores
  the previous tree and edges exactly; a gate-only synthetic query never
  appears in the stored-query index.

### Step 20: evaluation kit

- `evalkit/`: dataset adapter with the field whitelist (role, content, opaque
  session reference; EXPERIMENT 3.1), opaque ids and the id guard, split rule,
  the DEV-EXPOSED stratum, pools, metrics (including tree-only recall and
  per-source shares), clustered bootstrap, Tango and Newcombe score intervals,
  the outcome words of EXPERIMENT 2.1 and Holm, size-matched controls, A0+LEN,
  the merge rule, replay construction with lag `L` and interleaving,
  RECENCY-ONLY, the stream reader driver (EXPERIMENT 3.8) with opened-share
  bounds, recurring workload and probe generation from question and answer
  only with the rank filter, the exposed flag, planting with donor-sibling
  metadata and the pinning hook, decoys, strata from A0 ranks at each
  scheduled search's corpus prefix and their quotas, fixed before any arm
  runs, the descriptive offer audit and the eligibility report (EXPERIMENT
  3.6), fused final-list and tree-only recall,
  bootstrap over recalls for the reader's count measures (EXPERIMENT 3.8),
  QUERY-MEMO, AGENTIC and AGENT-A0, `budget.py` (already written and run; it
  prints EXPERIMENT 10.3), result-file schema with the required fields. No
  evaluation cache (review 2, F10). Reused modules
  are vendored from the owner's earlier evaluation harness with the source
  commit recorded in each file's header, and checked by the hygiene test.
- Data probes, written to `docs/DATA.md`: every record and turn key with
  counts (MEASURED before this build: `has_answer` on 10,960 turns, 896 true);
  pool counts (DEV-BASE 6,018, fresh new 11,016, excluded-block new 1,430,
  whole 18,464); the DEV-EXPOSED questions; the exposure audit of fresh gold
  contents against the earlier lab's committed prompt and result files, by
  content hash (EXPERIMENT 3.2). Any difference from EXPERIMENT's figures is
  an amendment before S2.
- Failing-first: the split rule reproduces the registered counts (test 363,
  pilot 102, held_back 19, dev2 16) and the 51-question sensitivity exclusion;
  the id guard refuses a model-facing file containing a gold id; a record
  carrying every original field plus a marker in each non-whitelisted field
  leaves no marker in any Elasticsearch document or captured prompt across all
  eleven task kinds (broken: adapter copying `has_answer` into `source_ref`);
  RECENCY-ONLY on a replay built without lag exceeds its margin (so the control
  can fail) and passes with lag; a stub opener that opens every pointer
  violates the opened-share bound; a plant ranked fourth at its source
  question's prefix and beyond 20 in the end pool lands in stratum 1 (broken:
  end-pool ranks put it in stratum 2); the confirmatory H4 population (each
  plant and its stratum) is identical in LOOP, FROZEN and NO-AUDIT and equals
  the prefix selection written before any arm ran, also when one arm's logs
  show a plant offered through an exploration slot or the direct section
  (review 3, F2; broken: a population builder that reads arm logs and drops
  exposed plants from one arm's population); the descriptive offer audit
  reports a plant offered only through an exploration slot (broken: audit
  over scheduled searches only); a result file missing a
  required field is refused; on the fixture, with a perfect memo list (the
  gold session first) while the tree, direct and linked lists all miss the
  gold and share no item with each other (so no item outranks the memo's
  first under RRF k = 60), QUERY-MEMO's fused final-list recall@10 is 1 and FROZEN's is 0, and
  the H3 "beyond memo" comparison reads fused final-list recall (broken: a
  comparison scored on tree-only recall gives both 0 and must fail);
  the interval code refuses a Wilson interval for a measure not declared
  binary, and opens per recall on 100 recalls whose opens vary from 0 to 6
  (seeded) gets the bootstrap interval of a reference computation that
  resamples recalls with the same seed (broken: Wilson on the total opens
  over 100 recalls must be refused); the run driver refuses a confirmatory stage
  whose `L`, `c` or another registered value differs from its freeze commit
  and a second stamped run of a confirmatory stage, and allows resuming the
  same run on the same commit (EXPERIMENT 3.7; broken: a driver that reads
  `L` from the working configuration).

### Step 21: comparison arms (not needed for H1)

Not needed for H1, so it may follow S6-H1 (3A). C1 and C2 make no model
calls, so their S2 and S3 rows can be computed later on the stored S2 and S3
structures; the proxy world is needed only by S4a and S4b.

- `recall/compare/es_routing.py` (C1) and `collapsed.py` (C2), labelled
  COMPARISON in code and outputs; the proxy-world runner that uses C1 routing
  for supporting loop arms, labelled DEVIATION 7 in every output row.
- Failing-first: C1 issues no model calls (counter unchanged) and its results
  are tagged COMPARISON in every output row; proxy rows are tagged DEVIATION 7
  and refused by the decision code (broken: untagged row refused by the result
  writer; a proxy row passed to a decision is refused).

### Step 22: stub-only loop on the fixture (stage S0)

- Run the whole pipeline end to end on the fixture (at least 16 plants) with
  the stub; all gates in EXPERIMENT S0, in two named, resumable checkpoints
  (review 2, F13), each a stamped run on the same commit:
  - S0-core: every probe and gate except the T5 floods. Estimated wall time
    5 to 8 hours (ASSUMPTION until the first run measures it), dominated by
    about 190 erasures with purge (E1 to E4 and E9, 3 runs each, plus E8),
    each waiting 22 to 30 s for retention leases (LAB), about 1.5 hours; the
    E7 scale test (a 1,000,000-document tenant with stub embeddings), about 1
    to 2 hours; R1 to R7 with their trials, restarts and simulated power
    losses, about 1 to 1.5 hours; the rest
    (T1 to T4, T6, T7, A1 to A8, V1 to V7, the stub loop gates) about 1 to 2
    hours. Resumable per probe: a probe that has passed on this commit is not
    re-run after an interruption.
  - S0-load: the T5 floods only: 6 flood kinds x 3 runs x 10 minutes = 180
    minutes of loaded measurement, plus one 10-minute unloaded baseline per
    flood kind (60 minutes) and a reset between runs (about 5 minutes each,
    90 minutes): estimated 5.5 hours of wall time (ASSUMPTION). Floods run one
    at a time on the single node, because parallel floods would change each
    flood's baseline. Resumable per flood kind (each about 55 minutes).
- Done when both checkpoints pass on the same commit. A partial set is
  reported as S0 INCOMPLETE with the list of probes not yet run, never as a
  pass.
- Failing-first: the S0 report, given S0-core results alone, both
  checkpoints with one probe missing or skipped, or the two checkpoints on
  different commits, reports S0 INCOMPLETE and names what is missing
  (broken: a report that passes S0 on S0-core alone must fail).
- After the deferred parts of 3A land, both checkpoints are run again on that
  commit before S4b or S6-H2 starts.

### Step 23: capped model client (needs S1 approval)

- `model/claude_cli.py`: headless CLI calls from a neutral working directory
  with no hooks, no tools, no session persistence; full pinned model id; the
  budget counter; the prompt leak check before the product cache lookup
  (DESIGN 5.8; LAB pattern).
- Failing-first: with the counter at its cap the client refuses without
  spawning a process; a prompt containing a gold-style id or a whitelisted-out
  dataset field marker is refused before any call; a throttle response pauses
  the stage and is recorded, and is not scored as a failure.
- Then the S1 canary (20 to 25 calls), and the amendment replacing per-call
  token assumptions (EXPERIMENT 10.2) with measured figures, committed before
  S2.

## 3A. Built after H1, behind the same interfaces

Not needed for S0 to S6-H1; built before S4b or S6-H2 starts, followed by a
fresh S0 run (Step 22):

- Graph refinements: decay, the degree cap with hub summary nodes (DESIGN
  16.4), the personalised PageRank ablation.
- Feedback refinements: IPS-weighted signals with clipped propensities
  (DESIGN 11.1); split, merge and collapse driven by co-confirmation clusters
  and the hub operation (DESIGN 11.2); any audit ranking beyond the sort of
  DESIGN 12.
- Step 21 (comparison arms C1 and C2, and the proxy world).
- Development ablations (EXPERIMENT 4), each under its own approval and call
  cap; they cannot change anything already frozen (EXPERIMENT 3.7).

A2 and RANDOM-EDGES rows from S2 and S3 are exploratory; if the graph
refinements change expansion, those rows are recomputed on the stored S2 and
S3 structures before the freeze that precedes S6-H2 (EXPERIMENT 3.7).

Each refinement enters behind the interface its minimal producer already
uses, with its own failing-first test; the security probes that cover the
artefacts it writes are re-run.

## 4. What each later stage needs from the owner

S1 to S7b each need the owner's approval of that stage and its call cap
(EXPERIMENT section 10); S6, S7a and S7b also need the release of the fresh
set. The default order is S0, S1, S2, S3, S6-H1, then S6-H2, S4b and S7b,
S4a and S7a (EXPERIMENT 10.3); any reduction or the stop rule after S6-H1 is
the owner's decision (EXPERIMENT 12, X4 and X6).
No step in this plan needs a human to create an account or type a password:
the bootstrap secret and each tenant key owner's credential are generated into
0600 files the same way and never displayed.
