# Readings of DESIGN passages that disagree (main, ASSUMPTION)

Each entry names the passages, the reading the build follows and the test that keeps the other reading safe.

## Lineage on upper nodes: DESIGN 5.4 and 14.2 against the build

- 5.4: "an upper node carries `lin_nodes` (its child node versions) and reaches leaves transitively, so the root does not
  hold every subject token of the tenant. Upper-node facts copied from children carry the source leaf id, and that leaf's
  subjects are added to the upper node's `lin_subjects`." 14.2: "Upper index nodes carry only `lin_nodes`".
- The two are consistent only if an upper node's `lin_subjects` holds the subjects of the leaves behind the facts it copied.
  The stub build does not keep a leaf id per fact, so it copies the subjects of every descendant upward (the safer reading
  for erasure: a subject query alone reaches the node).
- Erasure walks `lin_nodes` upward whether or not subjects were copied. `tests/security/test_erasure_es.py` runs both
  paths: subjects stripped from upper nodes (only the walk reaches them) and subjects copied (the subject query reaches them).
- Open: the privacy rationale of 5.4 (the root should not name every subject) is not met by the stub build.

## Log rows: DESIGN 5.7 and 9 against `opened`

- 5.7: "Rows are immutable." 9: "One immutable row per offered item per recall ... `opened (bool, ts, by whom)` ... `opened`
  is written by the gateway when `lookup` or `ask` serves the leaf." 14.3 step 5 and DEVIATION 6 already allow an in-place
  rewrite of a row for erasure.
- Followed: a row is written once with `opened = false`; the gateway makes the one-way transitions `opened`, `asked`,
  `used_cited` and the audit's `used_behavioural` in place; nothing else changes except the erasure strip. Lineage fields never
  change on a transition, so erasure and the audit stay correct. The alternative (an append-only event document per transition)
  would also satisfy both passages and is not built.

## Exploration propensity: DESIGN 9 `epsilon / n` against `k / n`

- Slots are drawn without replacement, so with k slots from n eligible leaves each leaf is included with probability k / n.
  `epsilon / n` is the same number when epsilon is the number of exploration slots in the digest, and wrong when epsilon is read
  as the share (0.1) and k is above 1. The code logs k / n; `tests/unit/test_exploration.py` checks the logged value against the
  measured inclusion frequency and shows the share-over-n variant red.

## Edge construction (DESIGN 5.5, 7.7, 8.6, 16; main-accepted reading ASSUMPTION, listed by the Step 12 owner)

- 7.7 types "the top 3 by similarity" but also draws candidates from "leaves sharing an entity". The build ranks entity-sharing
  candidates first for the three typing slots, then by similarity; an edge to a shared-entity leaf of low similarity would
  otherwise never be typed. The A2 gain test (`tests/security/test_edges_es.py`) depends on it and RANDOM-EDGES removes the gain.
- 5.5 says the erased edge version "which the tombstone records" keeps a re-created edge's counter above it. The build keeps the
  counter in its own `ec:<edge_id>` document (ids and one number, kind `counter`, which erasure keeps), written by compare-and-set
  at every new version, so the counter is above every version that ever existed whether or not an erasure deleted the documents.
  Step 14 may mirror the highest erased version into the tombstone; the test deletes the version and re-creates the edge.
- Entities come from code (`graph/entities.py`: a capitalised word of four letters or more that does not start a sentence), the
  stub's stand-in for a model-extracted list; the leaf projection stores them in `entities`.
- 8.6 gives "type weight x weight x decay x specificity": the build reads type weight as the type's initial weight of 16.2 and
  weight as the stored (confirmable) weight; decay is 1.0 until the decay interface is built after H1.
- The degree count and the supersedes-cycle check read searchable edges plus the writer's own unrefreshed writes (one gateway
  process holds the writer); a second writer process would need a realtime source for both.
- 16.3 "set valid_to on the older leaf's head" is not built in Step 12; a `supersedes` edge is written and the expander returns the
  superseding partner with a superseded leaf.

## Versioning and rebuild (DESIGN 5.3, 5.6, 7.5, 7.6, 13.2; main-accepted reading ASSUMPTION, listed by the Step 13 owner)

- Members name their parent by the stable node id, not by a node version: a member placed while node `X:2` was live must still
  find its parent after `X:3` is published. The view queries both spellings.
- A builder that finds a node version id it meant to create already present treats the attempt as lost (the other builder owns
  that version), never overwrites it and never deletes it on discard; a node version is created with create-only semantics.
- Placement thresholds by depth (0.0, 0.05, 0.10, 0.20) and the summary refresh rule (4 new children or 25% size change) follow 7.5
  and 7.6; a node that does not refresh keeps its summary and is marked `stale`.
- Step 13 remainder (second pass, main-accepted reading ASSUMPTION): 7.6 step 3 says "merge a node under F_MIN into its most
  similar sibling" and "collapse single-child chains" without saying where. The build merges level-1 nodes among the children of
  a parent the rebuild touches (the top nodes of a one-level tree), only when the union fits F_MAX, and collapses a node left with
  one child by putting the child in its place (the top list of a tree may then mix levels; placement descends such a frontier and
  only level-1 nodes take members). The conflict kept safe: a rebuild must not move a leaf the erasure and lineage walks cannot
  find; every merge writes member documents for the absorbed leaves under the sibling and a forwarding record, and the test
  checks that every leaf is in exactly one level-1 node and the earlier generation is unchanged.
- 7.6 step 6 (the tombstone check at the commit) against 14.3 (the gate): the check reads the in-memory published tombstone
  state under the shared side of the write gate, immediately before the head flip; an erasure published after the plan and before
  the commit is therefore seen, one that arrives during the commit waits at the gate and rewrites the new generation as a retained
  one. The attempt aborts (it does not rebase); the next run rebuilds from the inputs that remain.
- A pending-new level-1 node of a tree taller than one level joins the most similar level-2 node (the next rebuild may split it).
- The evaluation-only pinning hook (BUILD-PLAN 3.6 via Step 13) is read as "a pinned leaf stays with its donor sibling" (EXPERIMENT
  3.6, F3: donor-sibling metadata); only a split could move it, so the hook is applied in the split.
- 16.3 `valid_to` on the older leaf's head: a stated limit in DESIGN 20 (see there).
- 5.3 ("members of the old nodes get G+1 added to their `gen_to`") against 13.2 / V7 ("placement members written while a candidate is
  open stay visible"): a member written after the batch's P under a node the batch splits or merges away is in no new node. The
  test with a split in the cycle showed it vanishing under every pinned task. The build retires only members the batch covered
  (src_seq at most P, or no src_seq), leaves later members alone, and reads them through the forwarding record: the view shows a
  pending member under the first successor of its old parent, and the next rebuild groups it under that successor. The earlier
  generation's pinned tasks still see it under the old node.
- Node pointers: a retained node document answers `ok`; once retention removed it an `xp:` record (ids only, class) gives `moved`
  with the forwarding ids of the first later generation that replaced it, else `expired`.

## Verifier canary match threshold (DESIGN 14.7, 19; main-accepted reading ASSUMPTION, listed by the Step 14 owner)

- The kNN probe of the independent verifier counts a hit as residue when the index's score is at least `(1 + 0.98) / 2 = 0.99`, that
  is cosine 0.98 or more. The first draft required cosine 0.999. Measured on the test cluster (int8-quantised HNSW, cosine
  similarity), a stored copy of the canary vector scored 1.0 in a fresh index and 0.9978 after merges and deletions had run, so
  the strict threshold missed a leftover copy: a false "clean". Unrelated leaves of similar wording scored 0.50 to 0.57.
- The threshold is a trade: lower catches more quantisation drift, higher misses fewer copies as clean but never reports an unrelated
  leaf. The probe is one of six (text, kNN, get by id, lineage, authz, logs); the text, id and lineage probes do not depend on it.
- Test: `test_a_near_miss_vector_below_the_threshold_is_not_residue_but_a_copy_is` (a vector at cosine 0.90 is not counted, the copy is).

## Erasure scopes, holds and the guard (DESIGN 14.1, 14.2, 14.3, 14.6; ASSUMPTION, listed by the owner of the Step 14 close-out)

- 14.1 says "legal hold wins: the erasure is queued and the delay is written in the receipt" and names no one who may place a hold, and 4.3
  has no role for it. The build keeps holds in a durable registry (`erasure/hold.py`: an atomically replaced, fsynced file) with
  `place`, `release` and a queue, and does not expose them through a gateway operation: placing a hold is an operator or
  tenant-administrator action outside the people operations of 4.3. A hold names a subject token or a leaf id. While one covers a
  request nothing is published, deleted or rewritten (not even the guard changes). The receipt's `hold_delay_s` is request to the start of
  the released erasure. Test: `tests/security/test_erasure_scopes_es.py` (E5; the broken variant that ignores holds runs the erasure at once).
- 14.1 lists three scopes (subject, item, class relabel) and the build had `erase_subject` and `relabel_erase` only. An item
  erasure is the same closure started from the named leaf ids (`Executor.erase_item`); its journal record carries an empty subject and
  restore replays it through the same path. The keys of other leaves of the same person are untouched.
- 14.6 and EXPERIMENT E3 ("no erased-identifier HMACs"): the journal now records, per tombstone, the erased identifier tokens and bare-name
  tokens, and `Guard.load_journal` rebuilds the in-memory guard from them at a restart or a restore. Without it a re-ingest of an erased
  identifier was accepted again after the process restarted. The subject token of an identifier erasure is itself that identifier's HMAC.
- EXPERIMENT E3 "reseal-before-quarantine" has no definition beyond its name. Followed reading: a held (quarantined) blob is sealed under a
  data key that the subject tokens found in the text name at the moment of sealing, so destroying a subject destroys the held blob too;
  sealing first and linking later (or never) leaves a key that no erasure by subject reaches. The broken variant seals under an unlinked key.
  The quarantine document is reached by its lineage either way; the key is what the variant leaves behind.
- 14.2 "second net" for query-shaped artefacts: the closure also collects the search ids of the rows and stored queries that are the
  subject's own (the actor, or a query that mentions the subject) and deletes whatever names them in `lin_queries`, an edge's `evidence` or
  a member's `shaped_by`. A row of another actor that only offered an item of the subject is that actor's history: it is stripped, and what
  its own search shaped stays. The 14.2 steps "a leaf left with no member in a generation gets its placement-rule parent back" and
  "node versions re-summarised from an erased search return to the placeholder" apply to a reorganiser that is built in Step 19;
  the lineage walk already rewrites a node that names the search in `lin_queries`.
- E1 "erase only the current generation": the closure walks every retained node version by lineage, so the broken variant filters the
  closure to the newest version of each node id. The test makes the older version by copying a node document to the next version number
  (what a rebuild leaves behind for pinned readers), because the stub rebuild does not always touch the node that holds the planted leaves.

## People operations through the gateway (DESIGN 4.3, 4.4; ASSUMPTION, listed by the owner of the Step 15 close-out)

- 4.4 requires two distinct administrators to revoke an administrator or a revoker and does not say how the second one signs. The
  build records the first administrator's request as pending for `PENDING_S` (900 s, ASSUMPTION) and executes on the second call from a
  different administrator, judged by `policy.decide(REVOKE_PRIVILEGED, co_signer)`. The first call answers `pending`, which only an
  administrator can see (every other caller gets `refused`). The pending table is in memory: a restart drops it and the first
  administrator asks again, which is the safe direction.
- The body fields `tenant`, `principal`, `roles`, `read_set`, `write_set` are refused in every request (INV-1), so the people operations
  name the target as `who` (an identifier, mapped to its per-tenant token by the gateway), the granted roles as `grant_roles` and the
  operator's target tenant as `target_tenant`.
- The operator's break-glass is a request from the operator's own session in the platform tenant, judged by `decide(BREAK_GLASS_REVOKE)`.
  It reads no grant of the tenant, so no tenant role can remove or delay it; a tenant that holds a copy of the operator's principal
  can revoke the copy and the break-glass still works (test with a broken variant that derives the authority from the tenant's grant).
  A closed tenant (a journal fault) answers `unavailable` to every people operation; the operator's recovery call (`break_glass`) reopens it.
- Class-grant removal removes the class and every class that dominates it from the read set (a read set must stay down-closed), takes the
  class from the write set, journals an epoch bump and ends the principal's sessions at their next check.
- Grants are held in the gateway's memory and persisted best-effort to the authz index (4.4); a restart without that persistence
  loses grants made since, which fails closed (a principal without a grant cannot mint a session).
- 4.4 says the tenant key owner rotates and invalidates the tenant's keys. An owner can only list and invalidate the keys it created, so
  provisioning takes the owner's client (`provision_tenant(key_owner=...)`) and creates the tenant's keys through it. Keys created by the
  bootstrap credential instead expire at their 7-day expiry and can be invalidated only with that credential.
- The gateway drops a tenant's clients for good (`EsGate.drop_tenant` refuses to build a new client until `reload` names new keys). The
  earlier behaviour forgot the cached clients and rebuilt them from the old key files at the next request, which did not stop any
  gateway path.

## Verifier kNN probe rescoring (DESIGN 14.7; owner of the Step 14 close-out)

- The kNN probe asks for `rescore_vector` with an oversample of 3, so candidates are scored on the full-precision vectors. Without it,
  2 of 30 fresh positive-control runs on the test cluster saw the best score of the canary's own copy at about 0.95, under the 0.99 threshold
  (int8-quantised HNSW over very small segments), and the control raised `knn found nothing`; with it, 40 of 40 runs found the copy (the rescored best score printed in a run was 1.0000).
  A miss of that kind in a residue run would be a false "clean", so the probe must not depend on the quantisation of a small segment.
  The control raises loudly when it is blind; the residue run is valid only against a control of the same shape.

## Holds against the closure, and what a signature binds (DESIGN 14.1, 4.4; ASSUMPTION, listed by the Step 14 and 15 close-out)

- A hold is matched against the closure under the write gate, before the journal append: a hold on a leaf the closure reaches, or on a
  subject token of a leaf it reaches, queues the request (nothing is published, deleted or rewritten). A hold on a person therefore also
  stops the erasure of one of their items, and a hold on one leaf stops the erasure of a subject whose closure contains it. The
  erasure of another person whose closure merely contains a held person's mention is queued too (the safe direction: a hold wins).
- A first administrator's signature on a two-administrator operation counts for the same action on the same target only (revoke,
  removal of one class, or a re-grant with those roles and sets); a re-grant that takes a role or a class from an administrator or a
  revoker is such an operation, and one that takes something from anyone else ends that principal's sessions (an epoch bump).
- Removing a class grant is a tenant administrator's call; a revoker has the single revoke call (4.3).

## Holds at every entry point, the revoke lane and the start-up path (DESIGN 14.1, 4.4, 15, 20; ASSUMPTION, listed by the Step 15 owner)

- 14.1 says "legal hold wins" for erasure and names the subject and item scopes; it is silent on the class relabel, which 14.1 itself
  defines as an erasure of the old-class copy. Reading kept: a hold covers all three scopes. The hold registry is a required argument of
  the `Executor`, and `erase_subject`, `erase_item` and `relabel_erase` all check it under the write gate before the journal append;
  there is no argument that skips it. A relabel under a hold raises `Held`, is queued (`request_relabel`) and runs on release; the
  caller writes the new-class version only after the released relabel returns its receipt. The old copy stays readable to its
  former readers while the hold lasts (a hold means the content is kept); a restore's replay re-applies a tombstone already
  published, so a hold does not apply to it.
- A queued request leaves the queue only after it has run to its receipt (`HoldRegistry.complete`). A crash or failure between the two
  leaves it queued and it runs again; the repeat finds an empty closure and appends a second tombstone (idempotent).
- A hold placed while an erasure is between its check and its boundary is not seen by that erasure (the registry is not under the
  write gate): a stated limit (DESIGN 20).
- 4.4 and 15 say the revoke lane exists so a flood cannot delay a revoke, and that one tenant cannot starve another. Reading kept:
  the lane is granted only after the session resolves and the principal's role admits the operation (`admin.lane_allowed`), and it is a
  semaphore per tenant. A request merely shaped like a revoke goes to the tenant's general lane, where the handler refuses it.
  An authorised administrator can still fill its own tenant's lane; that bounds the harm to the administrator's own tenant.
- DESIGN 20 says grants "return from the best-effort authz persistence". The build now has that path: `gateway/boot.py` persists each
  grant to `lc-<t>-authz` (lineage names the principal token, so erasing the person deletes it) and reads them back at start-up,
  replays the epoch journal, loads the guard from the tombstone journal and wires the operator's `offboard_tenant` to
  `EsGate.drop_tenant`. Grants read at start-up are capped at 10,000 per tenant (stated limit). BUILD-PLAN step 5 owns the
  wire-up of the server process (listen socket, identity adapters, per-tenant directories); `boot.start_tenant` is the part that
  Step 15's claims need, and the process entry point that calls it for every provisioned tenant is not built.

## Step 16 readings: the multi-class reader, A7 and A8 (DESIGN 8.1, 17; EXPERIMENT 9.2; ASSUMPTION, listed by the Step 15 owner)

- 8.1 says the read path pins a generation per class and family, and 17 says a reader of several classes fans out over several roots;
  neither says how the digests combine. Reading kept: one digest per class of the read set, each exactly the digest the reader of that
  class alone gets, merged by reciprocal rank over the per-class pointer rankings (salted like the single-class fusion), the outlines
  kept per class, the counts summed and the pins reported per class (`pinned.gens`). The merge is independent of the order the classes
  are read in. A read set that is not down-closed is refused by the policy, so there is no reader of a dominating class alone: the
  merged digest of `base` and `restricted` is checked against the `base` reader's digest for its base part.
- A7 is reported and never gates (EXPERIMENT 9.2). The harness is `tools/a7_timing.py` (two-sample KS statistic and relative median
  difference, interleaved). The ES test runs interleaved C0 and C1 requests for at most 150 s and states the N reached against the
  registered 2,000; the A4 pair is compared the same way. A result at or above either limit is a disclosed timing channel.
- A8 runs over the registered 300 queries on a fixture with one-sentence leaves and leaves whose terms are dates, amounts and codes
  with digits. A title is checked against the code-built title and against the words of the leaf's tokens that contain no digit,
  so a build that keeps the letters of a token such as `quokka7` is red; the same tests run under the no-content title mode (AC-16).
  The 5-gram check on reasons and outline labels is run against leaf text, gists and facts of the tree.

## Step 18 judge-call cap and the judge score bands (DESIGN 10, 12; BUILD-PLAN Steps 17, 18; ASSUMPTION, listed by the pools owner)

- DESIGN 12 says at most `N_AUDIT = 100` leaves are audited per cycle and class, and at most `K_CAND = 5` candidate searches per
  leaf; read literally that allows up to 500 judge calls per cycle. Step 18 reads as if the cap were on judge calls. Reading kept
  (the stricter one, bounding model cost): both bounds hold, and in addition the judge calls of one cycle never exceed `N_AUDIT`.
  Leaves that are cut off by the call cap stay in the ledger and are ranked again next cycle. The `broken_uncapped` variant in the
  tests shows the difference.
- DESIGN 10 says relevance labels are 0 to 3 with "relevant" meaning at least 2, and says nothing about how a judge score becomes a
  label. In the stub the score is a 4-gram Jaccard overlap and the bands (`BANDS` in `feedback/judge.py`) are placeholder values,
  not tuned. They are ASSUMPTIONS and are validated (EXPERIMENT 8.2) before any judged label drives a decision; an unlabelled pair
  (a failed call) is unknown, never irrelevant.

## Pools foundation: readings and deviations at stage 0 (POOLS.md, ADAPTIVE.md; ASSUMPTION, listed by the pools owner)

- POOLS 2.2 asks for a second administrator on a `query` or `review` grant to a tenant administrator, and for two administrators to remove an
  administrator's grant. Neither two-signature path is built for pool grants: such a grant, and such a removal, is refused (fail closed), so
  no administrator reads content through a pool grant. The pool delete does use two distinct administrators (first call `pending`).
- POOLS 1.2 says a pool's own indices are created with the pool, and DESIGN 4.4 gives no tenant key a privilege to create an index. Reading
  kept: creating and dropping a pool's indices is an index-management callable handed to the pool service and the pool eraser (as
  provisioning is operator-side); the stored mappings are those of provisioning.
- POOLS 1.2 puts a pool's stage 0 heads, control rows, log rows and stored queries in class-level indices read by one reader. Built:
  `gateway/class_reader.py`, with a test that enumerates its read paths. The idempotency claim of a pool save is stored as `idem:<pool>:<h>`
  in the class control index (so the reader can read it); the dirty queue is not written for a pool at stage 0 (no placement before stage 1).
  The erasure scan and the verifier read every index of the tenant with their own keys and are not sessions; the reader's rule is for
  what a session can cause.
- POOLS 3.2 cuts a long text at `LEAF_CAP`; here a text over the leaf cap is `too_large` (no segmentation of pool saves yet). `meta.tags`
  are validated and not stored. Update idempotency (`pool_update` with a key) is held in gateway memory only, not durable.
- POOLS 4.1 says an empty scope touches no pool and AC-1 says every tool answers `unavailable`: a `pool_query` whose grant set is empty is
  `unavailable`. Without a hint, selection is every non-private pool of the grant set (taint is not stored yet, so a private pool is
  queried only when named, and `taint_raised` lists it); the card ranking above `P_ALL` is not built.
- POOLS 3.6 seals a pointer with AES-256-GCM-SIV; the nonce is derived from the content (keyed hash of tenant and body), so the same leaf
  version is always the same string. The key file of a tenant holds versions; retiring one makes its pointers `unavailable`.
- The writer's principal token is in `lin_subjects` of every pool leaf and its source document (POOLS 3.8), so erasing the person reaches
  the leaf. Token records are stored in `lc-<t>-authz` as hashes with their scope (best effort) and read back at start-up before the
  journal is applied on top.
- Not built (open): `principal_closed` and `J_SOFT` pressure, the journal `flock`, `J_SIZE` 16 MiB, the paused-sender hook for pool grants at
  every wave and hand-off, conversation taint and held saves, review mode, retraction, copy, selection by cards, the merge floors, injection,
  and the harness conformance checks (tool list, prompt capture). The pool-scope tombstone is appended to the tombstone journal; restore
  replay of it is not built.
- Taint (POOLS 2.8): the channel class is the single lowest class of the lattice (none when there is no single lowest class: every pool is then
  non-neutral until named). The durable copy is read lazily at first use, not at mint (a read failure answers `unavailable`). A held update is stored
  at the taint class but cannot be released yet; only a held save is releasable. A hint that raises the taint stores it before the search runs.
- Retraction (POOLS 3.5): built are the direct retraction (agent of an agent-origin leaf, human of any), the held request for an agent retracting a
  human or import leaf, the append-only records, the rate bounds and the `retracted` resolution for a token that could read the item. Not built: the
  review queue listing of requests, restore, release to erasure, `RETRACT_POOL_RATE` persistence across restart (counters are in gateway memory).

## Review fixes of the pools foundation: readings (ASSUMPTION, listed by the pools owner)

- POOLS 2.6 says delegation is attenuation only and 3.1 says tools are pool scoped, but the pre-existing handler routed every operation for any
  resolved token. Reading kept: a token with a scope reaches only the pool tools, `pool_list`, `pool_describe`, `ping`, `token_renew`,
  `token_delegate` and its own `token_revoke`; everything else is `unavailable` before any handler runs (default-deny with an allow-list).
- POOLS 2.8 and 3.1 ask for a gateway event on a hold release. The `log` indices are written with the log key, which the writer does not hold.
  Reading kept: an append-only `event` document in the class control index (hashes and ids only, lineage by pool and subjects), reached by the
  pool delete and by every erasure; it is not a recall row and no adaptation signal counts it.
- POOLS 9.4 says a commit sees the pool-scope tombstone and aborts. Reading kept: the pool state flips to `deleting` (journalled) before the
  eraser takes the write gate, and every pool commit, hold, retraction and head update checks that state under the shared side of the gate; the
  eraser removes the pool's own authz record and manifest last, so a crash at any step leaves a pool in `deleting` that a second delete finishes.
  Restore replay of the pool-scope tombstone is still not built.
- DESIGN 20 names one gateway process per tenant. Reading kept: the epoch journal takes a non-blocking POSIX lock (`lockf`) beside it, so a
  second process stays closed; a restart inside one process is not refused. Updates of one pool leaf are serialized inside the process for the
  same reason (a stated single-process limit); a crash between the two writes of a `supersede` is an open window.
- The receipts of ungrant, detach and token revoke say `denied` for new calls and carry `in_flight_may_complete`: the hand-off check is
  epoch-only, and a response already handed off for that pool or token can still go out (the paused-sender hook for pool grants is not built).
