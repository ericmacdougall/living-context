"""The verifier as its own entry point (DESIGN 14.7; BUILD-PLAN Step 14): `python -m living_context.verifier`.

It runs as a separate process with its own read-only credential (the tenant's `verify` role: read and view_index_metadata on the
tenant's indices, no write, no delete, no cluster privilege), takes the canaries from a file the test or the operator wrote
(never from the executor), and imports nothing of the executor, the receipt or the gateway's handler: only the verifier module and
the HTTP transport. Phases:

  control   run every probe before the erasure; every method must find its canary; the report is written to --out
  residue   run the same probes after the erasure against the control report; exit 1 when anything is found

Exit status: 0 clean (control found everything / residue is zero), 1 residue found, 2 an error (a step that cannot run is an error,
never a skip). The report is JSON on stdout (steps, probes, found); no document content is ever printed."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from ..gateway.es import EsClient
from .check import Canaries, Report, Verifier, VerifierError


def _canaries(d: dict) -> Canaries:
    return Canaries(short=d.get("short", ""), long=d.get("long", ""), vector=d.get("vector", ()),
                    subject_token=d.get("subject_token", ""), leaf_ids=d.get("leaf_ids", ()), principal=d.get("principal", ""),
                    log_marker=d.get("log_marker", ""), data_marker=d.get("data_marker", ""))


def _dump(rep: Report) -> dict:
    return {"steps": [list(s) for s in rep.steps], "probes": rep.probes, "found": dict(rep.found), "total": rep.total,
            "log_control_found": rep.log_control_found, "data_control_found": rep.data_control_found}


def _load(d: dict) -> Report:
    return Report(steps=[tuple(s) for s in d["steps"]], found=dict(d["found"]), log_control_found=bool(d.get("log_control_found")),
                  data_control_found=bool(d.get("data_control_found")))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m living_context.verifier")
    ap.add_argument("phase", choices=("control", "residue"))
    ap.add_argument("--url", required=True)
    ap.add_argument("--ca", required=True)
    ap.add_argument("--key-file", required=True, help="the verifier's own API key file (apikey, read only)")
    ap.add_argument("--tenant", required=True)
    ap.add_argument("--canaries", required=True, help="JSON file of the planted canaries")
    ap.add_argument("--log", action="append", default=[], help="a plaintext log file to grep (repeatable)")
    ap.add_argument("--data-root", action="append", default=[],
                    help="a directory the gateway, workers or model port write under (outside Elasticsearch's data directory): raw grep (repeatable)")
    ap.add_argument("--control", help="residue: the control report written by the control phase")
    ap.add_argument("--out", help="control: where to write the report")
    a = ap.parse_args(argv)
    try:
        es = EsClient(a.url, a.ca, a.key_file, scheme="apikey")
        v = Verifier(es, a.tenant, log_paths=[Path(p) for p in a.log], data_roots=[Path(p) for p in a.data_root])
        canaries = _canaries(json.loads(Path(a.canaries).read_text()))
        if a.phase == "control":
            rep = v.positive_control(canaries)
            if a.out:
                Path(a.out).write_text(json.dumps(_dump(rep)))
            print(json.dumps(_dump(rep)))
            return 0
        if not a.control:
            raise VerifierError("residue needs the control report")
        before = _load(json.loads(Path(a.control).read_text()))
        rep = v.residue(canaries, before)
        print(json.dumps(_dump(rep)))
        return 0 if rep.total == 0 else 1
    except (VerifierError, OSError, ValueError, KeyError) as e:
        print(json.dumps({"error": f"{type(e).__name__}: {e}"}))
        return 2


if __name__ == "__main__":
    sys.exit(main())
