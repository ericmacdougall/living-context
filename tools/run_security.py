"""Run the security suite and count skips (AC-13): `python tools/run_security.py [DIR ...]`.

Runs `python -m unittest discover` over each directory (default tests/security), parses the summary line, prints one line per
directory with the counts, and exits 1 when anything failed, errored or SKIPPED, or when no test ran at all. A security check
that skips is a check that did not run: a module that needs a cluster fails when none is configured, it never skips. The runner
applies to the S0-load checkpoint as well.
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

SUMMARY = re.compile(r"^(OK|FAILED)(?: \(([^)]*)\))?\s*$", re.M)
RAN = re.compile(r"^Ran (\d+) tests? in", re.M)


def parse_summary(text: str, *, broken_ignore_skips: bool = False) -> dict:
    """Counts from a unittest run's stderr. `broken_ignore_skips` is the BROKEN variant of the tests: skips are not read."""
    ran = RAN.search(text)
    summ = None
    for m in SUMMARY.finditer(text):
        summ = m
    out = {"ran": int(ran.group(1)) if ran else 0, "ok": bool(summ and summ.group(1) == "OK"), "failures": 0, "errors": 0, "skipped": 0}
    if summ and summ.group(2):
        for part in summ.group(2).split(","):
            k, _, v = part.strip().partition("=")
            if k in out and v.isdigit() and not (broken_ignore_skips and k == "skipped"):
                out[k] = int(v)
    out["unparsed"] = summ is None
    return out


def verdict(c: dict) -> list[str]:
    why = []
    if c["unparsed"]:
        why.append("no summary line")
    if c["ran"] == 0:
        why.append("zero checks ran")
    if not c["ok"]:
        why.append(f"failures={c['failures']} errors={c['errors']}")
    if c["skipped"]:
        why.append(f"skipped={c['skipped']}")
    return why


def run(directory: str, root: str = ".", *, broken_ignore_skips: bool = False) -> tuple[dict, list[str], str]:
    p = subprocess.run([sys.executable, "-m", "unittest", "discover", "-s", directory, "-t", "."], cwd=root,
                       capture_output=True, text=True)
    c = parse_summary(p.stderr, broken_ignore_skips=broken_ignore_skips)
    return c, verdict(c), p.stderr


def main(argv: list[str]) -> int:
    dirs = argv or ["tests/security"]
    bad = False
    for d in dirs:
        c, why, err = run(d)
        print(f"{d}: ran={c['ran']} failures={c['failures']} errors={c['errors']} skipped={c['skipped']}" + (f"  REFUSED: {'; '.join(why)}" if why else ""))
        if why:
            bad = True
            print(err[-2000:])
    print("SECURITY SUITE", "FAILED" if bad else "PASSED")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
