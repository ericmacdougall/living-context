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
   `python -I -m evalkit.s6h1_mem --leaves 85000 --cluster-workers <W> --json <host-dir>/mem-proof.json`. The runner refuses a proof
   measured on other code, one below 78,700 leaves, one whose clusterer ran with fewer workers than the run's `--cluster-workers`, a host
   without the proven peak plus one CLI process (256 MB) per `--concurrency` plus 2 GB free, and, after ingest, a pool with more leaves
   than were proven.
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
- `--stamp-registry` is one append-only file per host: a stamped S6-H1 run in any other run directory refuses this one. Use ONE path
  for every start on the host; a different path is not checked against it.
- The run is stamped (STAMP.json and the registry row) only after every no-call gate passed: fresh-set counts, pool count, memory proof
  and the budget at the exact leaf count. A refusal before that leaves the stamp unspent; an ingested tenant is attached, not re-ingested.
- A fresh start deletes the `s6h1` tenant's own indices and its own registry row only; other tenants on the same Elasticsearch are kept.
- `--model` and `--a0-fields` must be the frozen ones; `--commit` must be the manifest's code commit.
- `--concurrency N` (registered 2; above 2 needs `--concurrency-reason`, recorded in the stamp and in `starts.jsonl`): model calls at
  once. Questions and build groups run on N workers; the ledger, the tree and the results are the same at any N (tested at 1 and 4 with
  seeded node ids). The budget multiplies the throttle and refusal allowances by N.
- `--cluster-workers W` (default 1): clusterer threads. The groups are byte-identical at any W (MEASURED on the run host, synthetic
  384-d vectors: 20,000 leaves 172 s at W=1, 21 s at W=16; 40,000 leaves 692 s at W=1, 78 s at W=16, the same groups SHA-256;
  78,700 leaves 297 s and 3.6 GB at W=16). Each worker holds one 16 MB block buffer, so the memory proof must use at least W.
- `--embed-workers P` and `--ingest-writers T` (default 1 each: the one-at-a-time ingest). With either above 1, every leaf text is
  embedded first in P processes of `--threads` torch threads (pinned model, one text at a time, so a vector depends on the text, the
  model and the thread count only: MEASURED on the run host, 60 synthetic texts, 3 workers equal the in-process vectors byte for
  byte), then T threads write the leaves through the real writer with the tenant's `src_seq` range reserved in one compare-and-set,
  so the leaves, their order and their `src_seq` are those of the one-at-a-time ingest. Each embedding process needs about 1 GB
  (MEASURED 761 to 800 MB); the memory gate counts them. Writes cost about 61 ms per leaf one at a time (MEASURED: pilot ingest
  4,813.7 s, of which 3,606.8 s embedding, 19,605 leaves). MEASURED on the H1 Elasticsearch (tenant `s6probe`, 360 synthetic
  leaves, stub vectors): one at a time 21.2 leaves/s, 4 writers 75.5, 8 writers 89.4, 16 writers 87.8, each the same sequence as one
  at a time. Embedding (run host, 480 synthetic texts): in-process at 6 threads 71 to 78 s; 6 processes of 6 threads 59 s; 24
  processes of 1 thread 22 s, byte-equal to the in-process vectors at 6 threads (and 1 thread equals 6 threads on all 480).
- `--reuse-from-tenant T` (default none): before embedding, read tenant T's leaf index (search only, nothing written) and reuse its
  vector for a leaf whose text is the same and which the pinned tokenizer embeds in ONE window (480 text tokens or fewer); the index
  holds float32 values and one window's vector is float32 widened, so the reused vector is exactly what embedding gives. A multi-window
  leaf (a float64 mean) is always embedded. Up to 20 reused texts are re-embedded at the start and must be byte-equal, or the run
  stops before any write. `starts.jsonl` records `reused`, `reuse_checked` and `reuse_from`. MEASURED: S3's index holds 18,120
  leaves, 4,850 of them one window; 50 checked equal.
- Adaptive concurrency (default above `--concurrency 1`; `--fixed-concurrency` turns it off): model calls start 2 at once, halve on a
  throttle or an allowance refusal, and double after a clean window of max(8, 2 x the limit) answered calls, never above
  `--concurrency`. An allowance refusal is backed off and asked again; the stage stops (LIMIT_REACHED) when one call's refusals,
  or the refusals across all workers since the last answered call, pass `--max-retries`; at a fixed concurrency it stops at once. A
  throttle is retried per call (`--max-retries` each), so a burst of throttles in flight together halves the limit and goes on;
  five non-zero CLI exits in a row (throttles included) still stop the stage. Every call row and journal `pending` row records the concurrency at the
  time; STATE records `limited`, `concurrency_now` and `concurrency_changes`. Outputs do not depend on the concurrency.

## Early build (EXPERIMENT 14.5)

The whole-pool ingest and the tree build can run before S3 completes, the freeze and the release, in the run directory the stamped
run will use:

```
python -m evalkit.run_s6h1 --prebuild --amendment-ref DEV-14.5 --dataset <dataset> --es-url <es-url> --es-ca <es-ca> \
  --es-pw-file <es-pw-file> --embed-dir <model-dir> --binary <claude-binary> --model <model-id> --run-dir <run-dir> \
  --rates-from <s2-run-dir> <s3-run-dir> --stamp-registry <host-dir>/stamped-stages.jsonl --mem-proof <host-dir>/mem-proof.json \
  --commit <C> --approved-cap 35574 --approval-ref "<the owner's approval of S6-H1>" --a0-fields <S3's A0 fields> \
  [--threads 1 --embed-workers 24 --ingest-writers 8 --reuse-from-tenant <S3's tenant> --cluster-workers 16 \
   --concurrency 7 --concurrency-reason "..."] > <run-dir>.prebuild.out 2>&1 &
```

- It reads the pool contents only (`dataset.pool_contents`: the haystack arrays and each turn's role and content; every other field,
  the question, the answer and the gold ids included, is dropped by the parser). No release, freeze manifest or fresh set is read;
  `--release-ref` and `--s3-run-dir` are not needed.
- The same gates as the stamped start apply (cap, approval, real embedder and model, stop file, concurrency, memory proof and the
  budget), plus: no STAMP.json in the run directory, no S6-H1 run or early build of another run directory in the registry.
- It writes PREBUILD.json (commit, full code digest, cap, approval and amendment references, A0 fields, model, `--threads`, the
  pool's content count and ordered-hash digest, leaves) and an `S6-H1-prebuild` registry row, charges the build to the run's
  counter and cap, and ends with STATE `PREBUILT` (exit 0). A rerun continues through the journal (stop: `touch <run-dir>/STOP`;
  the concurrency may change between reruns); every other recorded value must be the same.
- The CODE IS FROZEN at `<C>` from here on: the memory proof is measured on it, the freeze must name it, and the stamped run refuses
  another commit, code digest, A0 fields, model, `--threads`, cap or approval than PREBUILD.json's, and a whole pool whose ordered
  content hashes differ. A change anywhere in `living_context/` or `evalkit/` after `<C>` voids the early build: the stamped run
  refuses it, and the registry refuses an early build or a stamped run in another run directory, so a second build needs the
  owner's decision on the calls already charged (they count against the cap) and a recorded amendment.
- Sync the code with `git archive <C>` only, never a copy of a working tree (an untracked file under `evalkit/` would enter the code
  digest), and write the memory proof (Preconditions, step 3) on that code before the early build.
- The stamped run (Start, below; same run directory, after the freeze and RELEASE.json) attaches to the prebuilt tenant and tree,
  builds nothing again, and copies PREBUILD.json into STAMP.json. A run directory with PREBUILD.json but no `tenant_state.json` is
  refused: a prebuilt tenant is never reset.

## Model kind (EXPERIMENT 14.6)

S3 (restarted) and S6-H1 run on Muse Spark: in every command above, and in S3's `evalkit.run_h1` command, replace
`--binary <claude-binary> --model <model-id>` with

```
--model-kind muse --provider openrouter --model meta/muse-spark-1.3-contributor \
  --reasoning-effort medium --max-tokens 16384 --rpm <R> \
  ( --key-file <key-file> | --key-secret-project <project> --key-secret-name <secret> )
```

- Exactly one key source; it is given at run time only and recorded nowhere. A key file must be a regular file of the user running
  the stage, mode 0600; only that is checked here, and the port reads it. A secret is fetched by the port inside the run process
  (`gcloud` on the PATH). The port is built before anything is stamped: a key it cannot use refuses the start (exit 2), nothing
  written.
- `--reasoning-effort` and `--max-tokens` are required and registered (14.6: medium and 16,384). They are sent with every request
  and are part of the model's identity: a resume, an early-build rerun or a stamped run with another value is refused.
- `--rpm` (at most 140, default 140): request starts per minute for THIS run. Runs at the same time on the same upstream limit
  split it (S3 and the early build: their `--rpm` sum to at most 140). `--api-timeout` (default 240 s) is per request.
- A 429, a 503 or OpenRouter's in-flight 402 backs off for at least Retry-After (per call, `--max-retries`) and is not a failure
  of the five-failure streak. Only the exponential back-off is capped; a Retry-After above 3,600 s stops the stage resumably
  (exit 75, STATE `PAUSED_THROTTLE` with the reason and `retry_after_s`): resume after the time STATE names. A stop file ends a
  back-off or a pacer wait at once, before any further charge. Retry-After counts in seconds or as an HTTP date; no new call is
  charged while another call backs off (calls already charged finish). A billing or budget refusal (402, OpenRouter's 403 with a limit
  source) or a reported model other than `--model` stops the stage at once (LIMIT_REACHED, exit 76): no further call is charged
  or sent, and the refused call is asked again on the resume. A 504, a timeout, any other error, and an answer cut short by
  `--max-tokens` (finish reason `length`) are failed calls, never answers.
- `calls.jsonl` records the HTTP status, the prompt, completion, reasoning and cached token counts, the reported model, the upstream
  provider, the finish reason and the cost; STATE.json records `models_reported` and `providers_reported`; every `done.jsonl` row
  records the model identity and, per arm, the recall's `port` timeouts, errors and invalid answers.
- A run directory stays on the model it started with: a resume, or a stamped run over an early build, on another kind or setting
  is refused.

## Watch, stop, resume

As S2 (docs/RUN-S2.md): `<run-dir>.out`, `counter`, `calls.jsonl`, `done.jsonl`, `journal.jsonl`, `budget.json`, `STATE.json`. Stop:
`touch <run-dir>/STOP`. Resume: remove STOP, repeat the start command with `--resume` and the same commit, cap, references, model and
fields. The journal is scoped per question and per build group at every `--concurrency`, so a resume replays every answered call
even with another `--concurrency`; a journal with unscoped keys (an older commit at one worker) is refused. Exit codes: 0 complete,
2 refused (before the start, or after ingest for memory or budget, with STATE `REFUSED` and no call charged), 3 stopped, 4 cap reached,
5 a build call failed, 75 throttled, 76 allowance limit.

## Results

`<run-dir>/results/`: `arm_A0.json`, `arm_A0_plus_LEN.json`, `arm_A1.json`, `arm_A1-tree-only.json`, `arm_SHUFFLED.json` (registered
row schema, per-question rows, opaque references), `h1_decision.json` (the word, (a), (b), (c), the SHUFFLED control, BCa, the 299
sensitivity, DEV-EXPOSED, abstention and per-type strata, top-10 gold by source), `summaries.json`.

## Wall time (floor, serial: `--concurrency 1 --cluster-workers 1`)

Ingest about 5 h (S2: 1,344 s for 5,988 leaves), clustering of 78,700 leaves (about 33 to 45 min at W=1, est.; 5 min at W=16, measured on synthetic vectors), build about 5,250 calls at 17.4 s
(about 25 h), recalls 362 at about 67 calls and 2.2 s per call (about 15 h): roughly two days.

## Not covered

- The check that SHUFFLED fails for its stated reason (holder selections following the shuffled text) needs holder logs; not stored.
- H2, H1b and C (S6-H2) are not part of this stage.
