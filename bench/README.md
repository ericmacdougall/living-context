# Benchmark recipe

How to reproduce a living-context benchmark run and compute its metrics. Everything here maps to an entry point that already
exists in `evalkit/`; nothing in `bench/` runs a model or touches Elasticsearch except `bench/metrics.py`'s inputs, which are the
files a run wrote. The registered design, arms and statistics are in `docs/EXPERIMENT.md`; the operator pages are `docs/RUN-S2.md`
(S2 and S3) and `docs/RUN-S6H1.md` (S6-H1). This page is the short path through them.

> **Cost and approval.** S2, S3 and S6-H1 call a real model and refuse to start without the owner's call cap and approval reference
> (`--approved-cap`, `--approval-ref`). The dataset and Elasticsearch must be on the run host (`docs/HOST.md`), not a laptop.
> Placeholders below are in `<angle brackets>`; fill in your own paths and keys, and never put a key on the command line or in a
> file under version control (`.gitignore` already excludes `*.env`, `secrets/`).

## Prerequisites

| What | Pin | Where it comes from |
|---|---|---|
| Dataset | `longmemeval_s_cleaned.json` (LongMemEval-S, cleaned) | `<dataset>`, a local path; its sessions are loaded by `evalkit.dataset` |
| Elasticsearch | 9.5.6, image digest `sha256:79b4f1de4c15e6eca824723086a51e5934406d614d410cc11be77748cafac537` | `docs/HOST.md`; security on, a CA file `<es-ca>`, the elastic password in a mode-0600 file `<es-pw-file>` |
| Embedder | `intfloat/multilingual-e5-small`, revision `614241f622f53c4eeff9890bdc4f31cfecc418b3` | `<model-dir>` holding the six files whose sha256 sums are in `docs/HOST.md` (checked before every load) |
| Python deps | `requirements.lock` plus `requirements-embed.lock` (torch, transformers; Linux x86_64) | `pip install --require-hashes -r requirements.lock -r requirements-embed.lock` on the run host |
| Model | the model named in the stage's approval | `--model-kind claude` (headless CLI, `--binary <claude-binary> --model <model-id>`) or `--model-kind muse` (`docs/MUSE-PORT.md`: `--key-file <key-file>` mode 0600, or a Secret Manager secret) |

The stub embedder and stub model (`--stage DEV-STUB`) are a smoke of the data path only. S2, S3 and S6-H1 **refuse** to run on
the stub embedder, because its hashed bag of words would hand flat search a weakened baseline.

## 0. Sanity check, no model call: `evalkit.a0_sanity`

Ingests the pilot haystacks through the real writer with real E5 vectors and scores arm A0 (flat hybrid search) only. It also
reports leaves per session, ingest and embedding throughput and index sizes, which the later stages' budgets use.

```
python -m evalkit.a0_sanity --dataset <dataset> --es-url <es-url> --es-ca <es-ca> --es-pw-file <es-pw-file> \
  --embed-dir <model-dir> --run-dir <run-dir> [--n-questions N] [--threads 6]
```

Output: the A0 gold-session recall@10 on the pilot questions with gold (EXPLORATORY), printed, with the sizes above. No
`done.jsonl` is written; this stage has no arms to compare.

## 1. Stage S2 (30 pilot questions) and S3 (102 pilot questions): `evalkit.run_h1`

Arms: A0 (flat), A1 (tree plus direct list), A1-tree-only, SHUFFLED-SUMMARIES (the control). S3 adds the A0+LEN fit (its beta is
fitted and frozen here) and runs SHUFFLED on the first 50 questions (`--shuffled-n 50`).

```
# S2
python -m evalkit.run_h1 --stage S2 --dataset <dataset> --es-url <es-url> --es-ca <es-ca> --es-pw-file <es-pw-file> \
  --embed-dir <model-dir> --model-kind claude --binary <claude-binary> --model <model-id> \
  --run-dir <run-dir-s2> --n-questions 30 --commit <git-sha> \
  --approved-cap <CAP> --approval-ref "<the owner's approval>" > <run-dir-s2>.out 2>&1 &

# S3 (same flags; 102 questions, SHUFFLED on 50)
python -m evalkit.run_h1 --stage S3 ... --run-dir <run-dir-s3> --n-questions 102 --shuffled-n 50 --commit <git-sha> \
  --approved-cap <CAP> --approval-ref "<the owner's approval>" > <run-dir-s3>.out 2>&1 &
```

`--a0-fields strong|text` chooses the BM25 fields of A0 and of A1's direct list (default `strong`: text, title, entities); it is
stamped and cannot change on `--resume`. Watch `<run-dir>.out`; stop with `touch <run-dir>/STOP`; resume by repeating the command
with `--resume` and the same commit, cap, question count and `--a0-fields`. Exit codes: 0 complete, 3 stopped, 4 cap reached,
5 a build call failed, 75 paused by throttling, 2 refused to start.

Expected outputs in `<run-dir>`:

| File | What it is |
|---|---|
| `STATE.json` | final state (`COMPLETE` when done), `questions_done`, counters |
| `counter` | calls charged (at or below the cap) |
| `calls.jsonl` | one row per charged call: `task`, `input_tokens` (+ cache fields), `output_tokens`, `latency_s`, validity, `throttled`, and `cost_usd` where the port reports one |
| `done.jsonl` | one row per completed question: `qref`, `gold` (opaque session refs), `arms.<arm>.sessions` (top 10) and `.failed`, sources of gold, holder calls |
| `journal.jsonl` | every outcome, used to resume |
| `results/arm_<arm>.json` | per-arm result in the registered row schema, per-question rows included (`arm_A0.json`, `arm_A1.json`, `arm_A1-tree-only.json`, `arm_SHUFFLED.json`; `+` becomes `_plus_`) |
| `results/comparisons.json` | A1 minus A0, the SHUFFLED control, tree-only share, top-10 gold by source |
| `results/summaries.json` | calls by task kind, tokens, latency median and p90, failure rate, throttles |
| `beta.json` | (S3) the frozen A0+LEN beta |

## 2. Stage S6-H1 (confirmatory, fresh set, whole pool): `evalkit.run_s6h1`

Runs only after S3 is COMPLETE, the freeze manifest exists (`evalkit.s6h1_freeze`), the memory proof exists (`evalkit.s6h1_mem`) and
the owner has released the fresh set. Every guard is in `check_prereqs`; the page that lists them is `docs/RUN-S6H1.md`.

```
python -m evalkit.run_s6h1 --dataset <dataset> --es-url <es-url> --es-ca <es-ca> --es-pw-file <es-pw-file> \
  --embed-dir <model-dir> --model-kind claude --binary <claude-binary> --model <model-id> \
  --run-dir <run-dir-s6> --s3-run-dir <run-dir-s3> --rates-from <run-dir-s2> <run-dir-s3> \
  --stamp-registry <host-dir>/stamped-stages.jsonl --mem-proof <host-dir>/mem-proof.json \
  --commit <git-sha> --approved-cap <CAP, at most 35574> --approval-ref "<the owner's approval>" \
  --release-ref "<the owner's release of the fresh set>" --a0-fields <S3's A0 fields> > <run-dir-s6>.out 2>&1 &
```

A separate early build (`--prebuild --amendment-ref <ref>`, same prerequisites) builds the whole-pool tenant and tree before the
freeze; see `docs/RUN-S6H1.md`. Outputs are those of S2, plus `results/arm_A0_plus_LEN.json`, `PREBUILD.json`/`STAMP.json` and the
H1 decision in `results/`.

## 3. Compute the metrics: `bench/metrics.py`

The registered figure is gold-session recall@10 (`results/arm_*.json`, `mean` and `interval`: clustered bootstrap, see
`docs/EXPERIMENT.md`). `bench/metrics.py` recomputes it, and three more, from the run's `done.jsonl` alone (stdlib only, no
Elasticsearch, no model), and summarises cost and latency per task kind from `calls.jsonl`:

```
python -m bench.metrics <run-dir>                  # tables; reads <run-dir>/done.jsonl and, if present, <run-dir>/calls.jsonl
python -m bench.metrics <run-dir> --calls <file>   # a call log elsewhere
python -m bench.metrics <run-dir> --json           # one JSON document
make bench-metrics RUN=<run-dir>
```

| Metric (per arm, over questions with gold) | Definition |
|---|---|
| recall@10 | gold sessions in the top 10 / gold sessions (the registered metric) |
| precision@10 | gold sessions in the top 10 / 10 (the cut-off is the denominator) |
| all-gold@10 | 1 if every gold session is in the top 10, else 0 |
| MRR@10 | 1 / rank of the first gold session in the top 10, else 0 |

Abstention questions (empty `gold`) are skipped, as `evalkit.score` skips them. A recall that failed scores 0 and is counted in the
`failed` column; it is never dropped. Per task kind the call table gives calls, `cost_usd` (summed over the calls that report one; the
table says when only some did), input and output tokens (input includes cache-creation and cache-read tokens, as
`results/summaries.json` does) and latency median and p90.

A check you can run: the `recall@10` column must equal the `mean` in `results/arm_<arm>.json` for the same arm (the result files
count questions the same way). If it does not, one of the two is wrong; trust neither until you know which.

Confidence intervals and the registered H1 decision are not recomputed by `bench/metrics.py`; they come from `evalkit.stats`
(clustered paired bootstrap over content clusters) and are in `results/comparisons.json`.

## What this recipe does not cover

- Running anything: the stages need the owner's approval and a paid model, and are not exercised by `make test`.
- `evalkit.run_h1 --stage DEV-STUB` still needs Elasticsearch for its ingest; the fully offline smoke is `examples/quickstart.py`.
