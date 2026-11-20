# Tuning kit: how to run it

Files in this directory:

- `STAGES.md`: stages Z1 to Z6, generators, the independence evidence, what generated text cannot show.
- `RESULTS-Z2.md`: plain retrieval by size and complexity and the signal tables (pre-statement first, results after).
- `RESULTS-Z3-D1.md`, `RESULTS-Z4-D9.md`: the `N_FLOOR` derivation check and the pool-selection measurement.
- `DATASETS.md`: what must be verified before any public benchmark is downloaded. Nothing is downloaded.
- `data/`: aggregates only (CSV and JSON). No generated corpus text is committed; a generated corpus is reproduced from the knobs
  and seed in its manifest.

Everything runs on a test host with its own virtual environment (the project lockfile plus `tunekit/requirements-embed.lock` for the
local embedder) and its own Elasticsearch container set up as BUILD-PLAN 1.2 and `tools/setup_es.py` describe (security on, TLS,
Basic licence, loopback, 2 GB heap). Credentials are read from a 0600 file by path and are never printed or put in an argument.

```
# 1. generate corpora and embed them (pinned local E5 model; no model-port call)
python -m tunekit.embed_cells --run RUN_DIR --model MODEL_DIR --cells @cells.txt --threads 4
# 2. ingest through the product write path, query, score
python -m tunekit.run_grid --run RUN_DIR --es-url https://127.0.0.1:PORT --ca CA_PEM --pw-file PW_FILE --cells @cells.txt --workers 3
#    a quiet latency run: --workers 1 --passes 3
# 3. aggregate (curves, fall points, signal AUC tables)
python -m tunekit.analyze --run RUN_DIR --out OUT_DIR --signals
# 4. pool selection with the reference rule
python -m tunekit.run_pools --model MODEL_DIR --out FILE --seeds 1,2,3
# tests (no Elasticsearch, no model)
python -m unittest discover -s tests/tunekit -t .
```

`tunekit.grid.main_grid()` lists the cells of the pre-stated grid; a cell name encodes its growth mode, complexity level, size, seed
and any knob override (`wide_c0.5_n4000_s1_para0.5`).

Rerunning is safe: a finished cell is skipped, and a cell killed part-way clears its own leftovers before it provisions again.
