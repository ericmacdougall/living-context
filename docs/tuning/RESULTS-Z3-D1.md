# Z3, the D1 derivation check: `N_FLOOR`

Status: run once on the product's own bulk build with the stub model backend (no model call, no network). Not a tuning result: D1 is
derived (ADAPTIVE 4.3), and this is the check TUNING Z3 names for it.

Claim checked (ADAPTIVE 4.3): `N_FLOOR = F_MAX x F_TARGET` (24 x 16 = 384 with the shipped constants); at or below it a bulk build
has no upper level, so a "tree" is one flat list of holders; one leaf above it the index-of-indexes rule adds a level.

Pre-stated check: for each size below, `living_context.structure.build.build_tree` over generated leaves (stub embeddings, stub
model backend) returns `levels == 1` with at most `F_MAX` top nodes for every size at or below 384, and `levels >= 2` for 385.

Observed (build calls are stub calls, counted by the build itself):

| Leaves | Levels | Top-level nodes | Build calls |
|---:|---:|---:|---:|
| 8 | 1 | 1 | 1 |
| 100 | 1 | 7 | 7 |
| 384 | 1 | 24 | 24 |
| 385 | 2 | 2 | 27 |
| 400 | 2 | 2 | 27 |
| 768 | 2 | 3 | 51 |
| 769 | 2 | 4 | 53 |
| 2,000 | 2 | 8 | 133 |

Reading: the derivation holds at the boundary, 384 gives one level with 24 top nodes and 385 gives two levels. It is asserted by
`tests/tunekit/test_kit.py` (class `D1`), including the sizes 8 and 100. What it does not show: the build cost in real calls (the
stub's call count is the number of groups, one call per group), the recall effect of the upper level, or anything about when a
tree helps; those need the paid stage P1.
