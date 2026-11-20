"""Stage B driver: run cells (ingest, query, score) with a few worker processes. Needs the ES url, CA path and the path of a 0600
credential file; the credential is only ever read from that file by the product's EsClient."""
from __future__ import annotations

import argparse
import json
import sys
import traceback
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

from . import baseline, runcell


def _one(args):
    url, ca, pw, run, name = args
    env = baseline.Env(url, ca, pw, Path(run) / "scratch")
    env.work.mkdir(parents=True, exist_ok=True)
    try:
        agg = runcell.run_cell(env, name, Path(run))
        return name, "ok", agg["recall"] if "recall" in agg else agg["hit10"]["p"], None
    except Exception as e:     # recorded, not swallowed: the cell stays undone and is retried by the next invocation
        return name, "error", None, "%s: %s" % (type(e).__name__, str(e)[:300])


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", required=True)
    ap.add_argument("--es-url", required=True)
    ap.add_argument("--ca", required=True)
    ap.add_argument("--pw-file", required=True)
    ap.add_argument("--cells", required=True)
    ap.add_argument("--workers", type=int, default=3)
    a = ap.parse_args(argv)
    names = Path(a.cells[1:]).read_text().split() if a.cells.startswith("@") else a.cells.split(",")
    jobs = [(a.es_url, a.ca, a.pw_file, a.run, n) for n in names]
    log = Path(a.run) / "grid_log.jsonl"
    bad = 0
    with ProcessPoolExecutor(max_workers=a.workers) as ex:
        for name, st, p, err in ex.map(_one, jobs):
            rec = {"cell": name, "status": st, "hit10": p, "error": err}
            print(json.dumps(rec), flush=True)
            with open(log, "a") as f:
                f.write(json.dumps(rec) + "\n")
            bad += st != "ok"
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
