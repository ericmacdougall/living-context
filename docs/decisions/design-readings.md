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
