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
