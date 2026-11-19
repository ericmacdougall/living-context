# Running stage S2 (H1 arms)

One page. Nothing here is started without the owner's approval of stage S2 with its call cap (EXPERIMENT 10, X2). The runner
refuses to start without it. Placeholders: `<run-dir>`, `<host-dir>`, `<dataset>`, `<model-dir>`, `<es-url>`, `<es-ca>`,
`<es-pw-file>`, `<claude-binary>`, `<commit>`.

## What it runs

The 30 pilot questions of the registered pick (seeded, non-abstention), each over its own haystack pooled into one tenant: arms A0,
A1, A1-tree-only (A1's tree list alone) and SHUFFLED-SUMMARIES, agent-routed through the headless CLI on the subscription at the
Sonnet tier (X5, X7). Every figure is EXPLORATORY (EXPERIMENT 7.4). The held-out and fresh questions are never loaded.

## Before the start (no model call, safe at any time)

1. The commit under test is pushed, synced to the run host once, and not changed during the run (one stamped run per stage).
2. The Elasticsearch container of this stream is up on loopback, the pinned model files are in `<model-dir>` (the loader checks
   their sha256 sums and refuses anything else), the host has at least 10 GB free disk.
3. Print the call budget: `python3 -I -m evalkit.s2_budget --dataset <dataset>`. On the real pool (30 questions, 1,412 sessions,
   5,988 leaves) it prints 399 build calls (exact), 24 to 72 route and organise calls per recall, 60 recalls (A1 shares one with
   A1-tree-only; SHUFFLED is the second), so 1,839 to 4,719 calls, up to 5,517 with the retry allowance for failed build calls.

## Start

```
python -m evalkit.run_h1 --stage S2 --dataset <dataset> --es-url <es-url> --es-ca <es-ca> --es-pw-file <es-pw-file> \
  --embed-dir <model-dir> --binary <claude-binary> --model <sonnet model id> --model-kind claude \
  --run-dir <run-dir> --n-questions 30 --commit <commit> \
  --approved-cap <CAP> --approval-ref "<the owner's approval: session and message reference>" \
  > <run-dir>.out 2>&1 &
```

- `--approved-cap` is the hard cap: the durable counter is charged before every call and call CAP+1 is refused. It is the owner's
  number. The budget above supports 5,517 as the ceiling; a lower cap ends the run at exactly the cap (exit 4).
- `--approval-ref` is a free-text reference to the owner's approval; the run refuses without one.
- `--a0-fields strong|text` (default `strong`): the BM25 fields of A0 and of A1's direct list. `strong` is text, title and entities
  (the stronger baseline, DESIGN 8.5); `text` is text only. Choose before the start; it is stamped and cannot change on resume.
- Throttling: `--max-retries` (default 3) and `--base-delay` (default 30 s, doubling, capped at 600 s). Each retry is a charged call.

## Watch

- `<run-dir>.out`: one line per completed question with the counter.
- `<run-dir>/counter` (calls charged), `<run-dir>/calls.jsonl` (one row per charged call: task, tokens, latency, validity, throttled),
  `<run-dir>/done.jsonl` (one row per completed question), `<run-dir>/journal.jsonl` (every outcome, used to resume).
- `<run-dir>/STATE.json` at the end of any exit.

## Stop and resume

- Stop: `touch <run-dir>/STOP`. The run ends cleanly before its next charge (exit 3) and keeps everything.
- Resume: remove `<run-dir>/STOP` and repeat the start command with `--resume` added and the SAME commit, cap, question count and
  `--a0-fields`; anything else is refused (a different cap needs a new approval and a new run). A resumed run attaches to the built
  tenant, skips completed questions, answers every already-paid call from the journal and charges only new work. One call that was
  charged but whose answer was lost when the process died is not repeated: it counts as a failed call (`lost_in_flight`).
- Exit codes: 0 complete, 3 stopped, 4 cap reached, 5 a build call failed after its retries, 75 paused by throttling, 2 refused to
  start. 3, 4 and 75 keep a resumable state (4 resumes only under the same cap, so it ends the stage).

## Results

`<run-dir>/results/`: `arm_A0.json`, `arm_A1.json`, `arm_A1-tree-only.json`, `arm_SHUFFLED.json` (registered row schema: commit, stage,
arm, pool, n, metric, interval method and seed, call counter, cache hits, routing; plus per-question rows with opaque references only),
`comparisons.json` (A1 minus A0; the SHUFFLED control; tree-only share and top-10 gold by source), `summaries.json` (calls by task kind,
tokens, latency median and p90, failure rate, throttles, most calls in any 5-hour window). Also `tree.json`, `STATE.json`.

## Done looks like

`STATE.json` says `COMPLETE`; `questions_done` is 30; the counter is at or below the cap; `summaries.json` failure rate is under 5%
(EXPERIMENT 10, S2 gate); the SHUFFLED control row is read next to its registered rule; the amendment that records the measured figures
(EXPERIMENT 14) is written before S3. Then stop the Elasticsearch container but keep its data: C1 and C2 are computed later on this
tenant (BUILD-PLAN Step 21).

## What the S2 row still needs that this run does not do

| Part of the registered S2 row | Status | What it would take |
|---|---|---|
| A2 (typed edges, bounded link expansion) | not built here | the edge-typing calls (20 pairs per call; registered 330 to 1,100, about 36% more on 5,988 leaves) over the built tenant, and the link-expansion arm merged as a third list. H2, not H1. |
| Relevance judge on 3 pools | not built here | 180 `judge_relevance` calls over three pools drawn by the reflection step; belongs to the loop stages (H3, H4). |
| C1 and C2 | not built here | no model call; Step 21 on the stored S2 tenant, so the tenant must be kept. |
| SHUFFLED fails for its stated reason | partly | the drop is computed; the check that holder selections follow the shuffled text needs the holder logs, which this run does not store. |
| A0+LEN | not reported | needs the length-prior beta that S3 fits and freezes. |
| Sustained calls per 5-hour window | measured, not sustained | a run of a few hours reports the peak; wall days per stage come from the amendment before S3. |
