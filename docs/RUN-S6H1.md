# Running stage S6-H1 (the confirmatory H1 stage)

One page. S6-H1 decides H1 (EXPERIMENT 2.2) on the fresh set over the whole pool. The runner refuses to start until every precondition
below holds; nothing here is started without them. Placeholders: `<run-dir>`, `<s3-run-dir>`, `<s2-run-dir>`, `<host-dir>`,
`<dataset>`, `<model-dir>`, `<es-url>`, `<es-ca>`, `<es-pw-file>`, `<claude-binary>`, `<model-id>`, `<C>` (the code commit).

## What it runs

- Pool: one tenant over the WHOLE POOL (EXPERIMENT 3.3): every haystack session of all 500 questions, deduplicated by content hash,
  18,464 registered contents (the runner refuses another count). Expected about 78,700 leaves (counts only: 4.262 leaves per session
  measured on S3's dev static pool).
- Questions: the fresh set through `dataset.fresh_questions` (release gate): 312 = 299 with gold + 13 abstention, after the 51-question
  sensitivity exclusion; the 6 DEV-EXPOSED questions leave the primary set, so H1 is decided on 293; the 299 is a sensitivity; the 13
  abstention questions are their own stratum (no recall).
- Arms: A0, A0+LEN (the beta S3 froze), A1 and A1-tree-only on all 312; SHUFFLED-SUMMARIES on the first 50 primary questions in the
  seeded order. Each ledger row logs the top-50 direct list and the top-50 tree list (opaque session refs), so every arm and the source
  check can be recomputed from rows.
- Decision (`evalkit/s6h1_stage.decide`): (a) A1 minus A0 and (b) A1 minus A0+LEN, clustered paired bootstrap over content clusters,
  percentile 95%, 10,000 resamples, seed 16096; (c) Wilson lower bound of the tree-only share above 0; outcome words of 2.1; the SHUFFLED
  control must drop by at least 0.10 with lower bound above 0, else H1 reads NOT ESTABLISHED (void; reading R3, the rule's word is kept
  beside it).

## Preconditions, in this order (no model call in any of them)

1. S3 is COMPLETE (`<s3-run-dir>/STATE.json`), and its top-50 log validity check passed (direct50_offline: A0 rebuilt from the log equals
   the ledger's A0 for every question).
2. The code commit `<C>` is pushed and synced to the run host once.
3. Memory proof on the run host, at `<C>`, in a memory-capped scope, only when no other paid stage runs there and the host has about
   13 GB available (about 11 GB peak expected at 85,000 leaves; the measurement has not been made yet):
   `python -I -m evalkit.s6h1_mem --leaves 85000 --json <host-dir>/mem-proof.json`. The runner refuses a proof measured on other code,
   one below 78,700 leaves, a host without the proven peak plus 2 GB free, and, after ingest, a pool with more leaves than were proven.
4. Amendments the registration requires before S6 (EXPERIMENT 3.2, 7.2): the H1 power amendment from S3's measured discordance and
   cluster count, and the exposure audit; any question it adds to DEV-EXPOSED is passed to the freeze (`--dev-exposed-extra`).
5. Freeze: `python -I -m evalkit.s6h1_freeze --s3-run-dir <s3-run-dir> --commit <C> --model <model-id>` writes
   `evalkit/freeze/S6-H1.json` and prints its sha256. Commit that file ALONE (the freeze commit), push, sync it to the run host. The code
   digest leaves `evalkit/freeze/` out, so the freeze commit's tree passes as `<C>`.
6. Release record: write `<run-dir>/RELEASE.json` with `stage` "S6-H1", `release_ref` (the owner's release), `source` (where the owner
   said it) and `freeze_manifest_sha256` (printed in step 5).
7. Budget print: `python3 -I -m evalkit.s6h1_budget --leaves 78700 --rates-from <s2-run-dir> <s3-run-dir>`. The runner repeats it at the
   start (expected pool) and after ingest (exact leaf count) and refuses when the top exceeds the cap.
8. Elasticsearch up on loopback, tenant `s6h1` unused; disk for the whole-pool index (several GB) and the run directory.

## Start

```
python -m evalkit.run_s6h1 --dataset <dataset> --es-url <es-url> --es-ca <es-ca> --es-pw-file <es-pw-file> \
  --embed-dir <model-dir> --binary <claude-binary> --model <model-id> --run-dir <run-dir> --s3-run-dir <s3-run-dir> \
  --rates-from <s2-run-dir> <s3-run-dir> --stamp-registry <host-dir>/stamped-stages.jsonl --mem-proof <host-dir>/mem-proof.json \
  --commit <C> --approved-cap 35574 --approval-ref "<the owner's approval of S6-H1>" --release-ref "<the owner's release of X1>" \
  --a0-fields <S3's A0 fields> > <run-dir>.out 2>&1 &
```

- `--approved-cap` is the hard cap (at most 35,574, the owner's number); the durable counter is charged before every call.
- `--stamp-registry` is one append-only file per host: a stamped S6-H1 run in any other run directory refuses this one.
- `--model` and `--a0-fields` must be the frozen ones; `--commit` must be the manifest's code commit.

## Watch, stop, resume

As S2 (docs/RUN-S2.md): `<run-dir>.out`, `counter`, `calls.jsonl`, `done.jsonl`, `journal.jsonl`, `budget.json`, `STATE.json`. Stop:
`touch <run-dir>/STOP`. Resume: remove STOP, repeat the start command with `--resume` and the same commit, cap, references, model and
fields. The journal is scoped per question, so a resume replays only the in-flight question's own calls. Exit codes: 0 complete,
2 refused (before the start, or after ingest for memory or budget, with STATE `REFUSED` and no call charged), 3 stopped, 4 cap reached,
5 a build call failed, 75 throttled, 76 allowance limit.

## Results

`<run-dir>/results/`: `arm_A0.json`, `arm_A0_plus_LEN.json`, `arm_A1.json`, `arm_A1-tree-only.json`, `arm_SHUFFLED.json` (registered
row schema, per-question rows, opaque references), `h1_decision.json` (the word, (a), (b), (c), the SHUFFLED control, BCa, the 299
sensitivity, DEV-EXPOSED, abstention and per-type strata, top-10 gold by source), `summaries.json`.

## Wall time (floor, serial)

Ingest about 5 h (S2: 1,344 s for 5,988 leaves), clustering of 78,700 leaves (measured by step 3), build about 5,250 calls at 17.4 s
(about 25 h), recalls 362 at about 67 calls and 2.2 s per call (about 15 h): roughly two days.

## Not covered

- The check that SHUFFLED fails for its stated reason (holder selections following the shuffled text) needs holder logs; not stored.
- H2, H1b and C (S6-H2) are not part of this stage.
