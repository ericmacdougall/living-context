"""Stage A of a grid run: generate each cell's corpus and embed its leaves and questions with the pinned local embedder, writing
corpus and vectors under the run directory (outside the repository). Skips cells already done. Zero model-port calls.

  python -m tunekit.embed_cells --run DIR --model MODEL_DIR --cells name1,name2 [--threads 4]
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from . import grid, synth


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", required=True)
    ap.add_argument("--model", required=True)
    ap.add_argument("--cells", required=True, help="comma-separated cell names or @file with one per line")
    ap.add_argument("--threads", type=int, default=4)
    a = ap.parse_args(argv)
    names = (Path(a.cells[1:]).read_text().split() if a.cells.startswith("@") else a.cells.split(","))
    run = Path(a.run)
    from .e5 import BatchEmbedder
    emb = BatchEmbedder(a.model, threads=a.threads)
    log = run / "embed_log.jsonl"
    for name in names:
        vp, cp = run / "emb" / (name + ".npz"), run / "corp" / (name + ".json.gz")
        if vp.exists() and cp.exists():
            continue
        cell = grid.Cell.parse(name)
        t0 = time.time()
        c = synth.generate(cell.knobs())
        t1 = time.time()
        P = emb.passages([L["text"] for L in c.leaves])
        Q = emb.queries([q["text"] for q in c.questions])
        t2 = time.time()
        grid.save_corpus(c, cp)
        grid.save_vectors(vp, P, Q)
        rec = {"cell": name, "n": len(c.leaves), "gen_s": round(t1 - t0, 2), "embed_s": round(t2 - t1, 2),
               "texts_per_s": round((len(c.leaves) + len(c.questions)) / max(t2 - t1, 1e-9), 1), "sha": c.manifest["content_sha256"][:16]}
        with open(log, "a") as f:
            f.write(json.dumps(rec) + "\n")
        print(json.dumps(rec), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
