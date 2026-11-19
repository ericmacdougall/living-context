# living-context: build plan

Ordered steps, each small enough to commit green on its own. Each step names
its failing-first test: a test written before the code, shown to fail against
the previous commit or a named broken variant, then shown to pass. A test that
cannot fail for the stated reason does not count (LAB-R8). Security probes
start at step 5 (the first step with a gateway) and run in every later step
(EXPERIMENT.md section 9); steps 1 to 4 have their own policy and keystore
tests.

Labels as in [DESIGN.md](DESIGN.md) section 0.

## 0. Rules for the whole build

- Nothing secret in the repository: no passwords, API keys, key ids, CA
  private keys, session tokens, host names, absolute home paths or dataset
  files. Configuration is read from a TOML file whose path is given by
  `LC_CONFIG`; the repository holds only `config.example.toml` with
  placeholders. Secrets are read from 0600 files by path, never from argv or
  environment values, never logged (LAB-R7).
- Every step ends with `python -m unittest discover -s tests` green, the
  security probes green for everything built so far, and every broken control
  red.
- No model calls until stage S1 is approved (EXPERIMENT.md section 10). Steps
  1 to 21 use the deterministic stub only.
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
| P2 | API key with role descriptor on `lc-ta-*` only and no cluster privilege, security on: search, get, mget, msearch, count, kNN, percolate, terms lookup, `_cat/indices`, `_field_caps`, `_mapping`, wildcard and comma lists, alias names against `lc-tb-*` (including `lc-tb-authz`) and the look-alike `lc-tbx-*`; plus the cluster APIs `_cluster/state`, `_cat/shards`, `_cat/aliases`, `_tasks?detailed`, `_resolve/index`, `_search_shards`, `_nodes/stats`, `_cluster/stats`. Control: the same with a key holding `cluster: monitor` must leak through at least one cluster API (so the probe can fail) | all refused or empty, no names or query descriptions leaked | this is the hard boundary: if it fails, dedicated tenants move to separate clusters and the gap is reported before any other step |
| P3 | `ELASTIC_PASSWORD_FILE` honoured; the password appears in no container log | yes | operator runs the reset tool with output redirected to a 0600 file |
| P4 | Percolate a stored `knn` query | fails or unsupported | already designed lexical-only; record |
| P5 | Realtime GET by id returns a document version immediately after the index call returns, without refresh (used for sessions, heads and tombstones; epochs are already in the gateway's own table, DESIGN 4.4) | yes | gateway keeps the tombstone table in memory too, updated synchronously by the calls it serves, reloaded on start from the journal |
| P6 | API key invalidation latency on our node | under 1 s | gateway drops its client for a revoked tenant at once (it is the only client); report the measured latency as a limit |
| P7 | kNN `filter` with a restrictive filter returns k hits (filter applied during search) | yes | over-fetch with `num_candidates` up to 10,000 and report recall loss; per-class indices already make the filter mostly unnecessary |
| P8 | One `_aliases` request with add and remove actions is atomic under concurrent search | yes | gateway write gate held across the swap; readers retry once on a missing alias |
| P9 | `index.merge.policy.expunge_deletes_allowed: 0` accepted; `only_expunge_deletes` on a live index brings `docs.deleted` to 0 | yes | copy-and-swap purge (DESIGN 14.5) for every erasure |
| P10 | Time from delete to `docs.deleted == 0` (retention leases on a single node) | measured | the bound in receipts is the measured figure |
| P11 | Index with stale `if_seq_no` returns 409 and writes nothing | yes | lease document per head with the same probe on the lease |
| P12 | `_msearch` with one failing sub-search: others succeed, per-item errors | yes | one request per sub-search |
| P13 | `terms` on `src_id` with 10,000 ids; `index.max_terms_count` default 65,536 | works | batch the ids |
| P14 | Revoke with `lc-ta-authz` blocked (`index.blocks.read_only_allow_delete`) and, separately, with the disk driven to the flood-stage watermark on a scratch volume | revoke takes effect at once from the gateway's table; the Elasticsearch persist is retried and reported | none: revocation must not depend on the write (DESIGN 4.4); a failure here blocks Step 15 |
| P15 | A tenant key owner (native user with index privileges on `lc-ta-*` and `manage_own_api_key` only) creates, rotates and invalidates its own keys; created keys carry at most its privileges; the gateway's keys cannot create or invalidate keys | yes | the operator keeps the bootstrap credential reachable for rotation; the gap is reported as a limit |
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
  slots and queues, per-tenant stored-byte caps, a separate revoke lane),
  sessions minted only by an identity adapter from a proof, session tokens
  carrying the tenant prefix, per-tenant `lc-<t>-authz` with HMAC principal
  tokens, the in-memory epoch table with its local journal, `es.py` reading key
  files by path, `indices.py` naming (including the per-class control index)
  and shard-budget check (DESIGN 6.4 formula), `validator.py` refusing
  documents without tenant, class or lineage; slow logs disabled on every
  index; no body logging.
- `tools/provision_tenant.py` creates the tenant's indices, aliases, its
  registry row in `lc-sys-registry`, its tenant key owner, and API keys with no
  cluster privilege (DESIGN 6.3).
- Failing-first tests (security track begins): SEC-T1 (with the cluster
  APIs), T2, T3 (normalised bodies) on the fixture tenants; T5 flood probes
  including the disk-fill flood against the byte cap; T6 key-space collision;
  the validator refuses a document without `lin_subjects`; an authz document
  of `ta` is unreachable with any `tb` key. Broken controls: `lc-*`
  descriptor; tenant from body; parse before admission; 32-bit lock key; key
  with `cluster: monitor`; no byte cap. Each must turn its probe red.

### Step 6: write path, source log and leaf projection

- `ingest/`: segmentation (session and gap cuts, embedding valleys, hard cap
  with recursive split), subjects (identifier declarations), tombstone guard,
  source append, projection, leaf head compare-and-set, dirty queue.
- `embed/`: hashed stub.
- Failing-first: the longest session in the dataset shape (a 78,000-character
  synthetic session) produces no leaf over the cap (broken variant: split only
  at round boundaries fails); empty input refused; idempotent retry returns
  the same leaf; the same idempotency key with a different body is refused
  (broken: returns the first result); a projection without its source document
  is repaired; a bare-name match of an erased identifier is held, with the same
  outcome a writer sees for any hold.

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
  pointers only, the reason schema (at most 12 words) and 5-gram leak check,
  chunked holder calls above `F_MAX` entries, pending members shown with a
  code-built title, `last_seen` never shown, the three-list RRF merge rule
  (EXPERIMENT 5.1), gateway-logged `opened`, typed resolution answers.
- Failing-first: on the fixture, stub A1-tree-only recall@10 (no direct-search
  fusion) at least 0.80 and SHUFFLED-SUMMARIES at least 0.30 lower (the stub
  gate in EXPERIMENT S0); a digest never contains leaf text (broken: include
  the gist of an unopened leaf's text body); no reason shares a 5-gram with
  its holder's input (broken: reasons quoting child facts, SEC-A8 red); a
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

- `graph/`: candidates (kNN 10, shared entities), stub typing, weights,
  decay, specificity, degree cap and hub nodes, supersedes and contradicts
  rules, edge versions bound by `src_seq` (insert-time) or `gen_from`
  (rebuild hub edges, reorganiser) and a `gen_to` set, expansion with caps and
  the visibility filter of the task's pin (DESIGN 13.2: the pinned write-path
  watermark P for write-path versions, the pinned generation's ancestry for
  the others), version counters of re-created edges above erased versions.
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
- Manifests with ancestry and source watermark W (monotone along ancestry);
  the visibility rule for members and edges (DESIGN 13.2: pinned P for
  write-path versions, ancestry for rebuild and reorganiser versions, `gen_to`
  sets for retirement);
  deletion of a rejected or rebased candidate's unshared nodes, members and
  edges; an evaluation-only pinning hook that keeps planted entries out of
  normal rebuilds (used by `evalkit` only, EXPERIMENT 3.6).
- Failing-first: SEC-V1, V2 (tree and edges), V4 (with edges and rollback), V6
  with broken controls (node id reuse, re-read head per wave, flip without
  compare-and-set, expansion reading the latest edges, candidate edges visible
  at write, rollback leaving the candidate's edges visible); a leaf written
  during a rebuild is found by direct search immediately, by the tree as a
  pending member once P covers it, and with a title and gist after the next
  batch.

### Step 14: erasure

- `erasure/`: tombstones written first to the per-tenant tombstone journal
  (`gateway/journal.py`: hash chain, fsync, replication to the second
  location), fence, key destruction, lineage closure including authz records
  and query-shaped artefacts (DESIGN 14.2), deletes, the log-row strip,
  rewrites and member and edge removal in every retained generation, purge
  (default and copy-and-swap), receipts with a requester copy over its read set;
  relabel as erasure of the old-class copy; restore that applies the newest
  journal to Elasticsearch and to the keystore before serving; `verifier/` as a
  separate entry point with its own keys, canaries (including query-only,
  authz and query-shaped canaries) and the plaintext log grep with its control
  marker.
- Failing-first: SEC-E0 to E6, E8 and E9 with their broken controls (redact
  set not consulted; current generation only; key-only; raw-file grep of
  segments as verifier; string-search closure; closure without authz;
  reseal-before-quarantine; no erased-identifier HMACs; restore before
  tombstones; tombstones only inside the snapshot, which must resurrect content
  when an older snapshot and an older keystore backup are restored; keystore
  restore that keeps keys named by newer tombstones; fence removed; hold
  ignored; receipt without recount; requester receipt with per-class counts;
  subject detection on leaves only).

### Step 15: revocation

- Revoke call on the gateway's in-memory epoch table with its local journal and
  best-effort persistence (DESIGN 4.4); revoke lane; epoch checks at admission
  and at each fan-out wave; the per-principal send fence (non-blocking socket
  writes under the shared side, writer-preferring exclusive side for revoke,
  discard of unsent buffers); a test hook that injects a delay between the
  final check and the hand-off; the two-administrator rule; no self-grant; the
  operator's break-glass revoke; class-grant removal as an epoch bump; tenant
  key owner rotation and invalidation (P15) and the gateway dropping its clients
  for a revoked tenant.
- Failing-first: SEC-R1 to R6 and T7 with their broken controls, including: no
  send fence with the injected delay (must fail deterministically); a socket
  write inside the fence with a slow-reading client; revoke that requires the
  Elasticsearch write (with `lc-ta-authz` blocked, P14); single-administrator
  revoke of a revoker; self-grant; class removal without an epoch bump; rotation
  that needs the bootstrap credential.

### Step 16: access-class non-interference suite

- C0 and C1 fixture corpora; the 300-query byte-identity harness, and its
  structural mode (request plan and set equality) for real-model runs; relabel.
- Failing-first: SEC-A1 to A6 and A8 with all broken controls in DESIGN 8.7 and
  EXPERIMENT 9.2 (including a relabel read immediately after the relabel call
  returns, broken: relabel that waits for the next generation); A7 timing
  harness reports, with the A4 pair included.

### Step 17: reflection and the separate judge (stub)

- `feedback/reflection.py`, `judge.py`: three pools including the deeper,
  looser pool (c); a switch that removes pool (c) for the NO-AUDIT arm; judge
  blind to source; per-source misses (`tree_missed`, `any_missed`); notes with
  lineage (including query subject tokens) and join class; write-time
  background search.
- Failing-first: on the fixture, a planted relevant leaf not offered by the
  tree is labelled "relevant, tree_missed" even when the direct search offered
  it (broken: "never offered by any source" records nothing); the judge input
  contains no source tag (assertion on the prompt).

### Step 18: reverse audit

- `feedback/reverse_audit.py`: model-free candidate generation (batched
  percolation of every leaf against the stored queries added since the last
  cycle, and kNN of those queries' vectors over leaf vectors), the gold-blind
  priority rule, the caps `N_AUDIT` and `K_CAND`, judge confirmation, log check
  against `offered_by_tree`, Wilson miss rate, flagging rule, pruning and
  archiving rules; synthetic queries behind a switch, off by default.
- Failing-first: a planted mis-filed leaf whose past query was logged and not
  offered by the tree gets a miss rate of 1 with its query listed (broken:
  audit against synthetic queries only misses the real query); the audit's
  judge calls per cycle never exceed `N_AUDIT` (broken: uncapped, counted);
  with synthetic queries off, a leaf with no search history is never re-filed
  (broken: synthetic on re-files it); an archived leaf is still directly
  searchable and still erasable.

### Step 19: reorganiser and the replay gate

- `feedback/reorganise.py`, `gate.py`: signals with clipped IPS weights,
  operations producing a candidate generation (members and edges carrying the
  candidate's generation and `lin_queries`), targeted replay with gate-only
  synthetic queries that are never stored, judging of unjudged items at
  replay, the betting confidence sequence (DESIGN 13.3), the general-replay
  count guard, adoption and rollback by head compare-and-set;
  `evalkit/gate_power.py` (the simulation behind EXPERIMENT 7.3).
- Failing-first: on the fixture, the oracle candidate is adopted and the
  random-moves candidate rejected (the gate positive controls; broken: a gate
  that rejects everything must fail the oracle check); on the fixture stream,
  LOOP repairs planted items in strata 2 and 3 and NO-AUDIT does not
  (difference at least 0.25); decoys are repaired less than plants;
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
  metadata and the pinning hook, decoys, strata quotas, QUERY-MEMO,
  AGENTIC and AGENT-A0, the EVAL-CACHE wrapper, `budget.py` (recomputes
  EXPERIMENT 10.3), result-file schema with the required fields. Reused modules
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
  violates the opened-share bound; a result file missing a required field is
  refused.

### Step 21: comparison arms

- `recall/compare/es_routing.py` (C1) and `collapsed.py` (C2), labelled
  COMPARISON in code and outputs; the proxy-world runner that uses C1 routing
  for supporting loop arms, labelled DEVIATION 7 in every output row.
- Failing-first: C1 issues no model calls (counter unchanged) and its results
  are tagged COMPARISON in every output row; proxy rows are tagged DEVIATION 7
  and refused by the decision code (broken: untagged row refused by the result
  writer; a proxy row passed to a decision is refused).

### Step 22: stub-only loop on the fixture (stage S0)

- Run the whole pipeline end to end on the fixture with the stub; all gates
  in EXPERIMENT S0.
- Done when every S0 gate passes in one stamped run.

### Step 23: capped model client (needs S1 approval)

- `model/claude_cli.py`: headless CLI calls from a neutral working directory
  with no hooks, no tools, no session persistence; full pinned model id; the
  budget counter; the prompt leak check before the cache lookup (LAB pattern).
- Failing-first: with the counter at its cap the client refuses without
  spawning a process; a prompt containing a gold-style id or a whitelisted-out
  dataset field marker is refused before any call; a throttle response pauses
  the stage and is recorded, and is not scored as a failure.
- Then the S1 canary (20 to 25 calls), and the amendment replacing per-call
  token assumptions (EXPERIMENT 10.2) with measured figures, committed before
  S2.

## 4. What each later stage needs from the owner

S1 to S7b each need the owner's approval of that stage and its call cap
(EXPERIMENT section 10); S6, S7a and S7b also need the release of the fresh
set. The order of the stages and the stop rule after S6 are the owner's
decision (EXPERIMENT 12, X6).
No step in this plan needs a human to create an account or type a password:
the bootstrap secret and each tenant key owner's credential are generated into
0600 files the same way and never displayed.
