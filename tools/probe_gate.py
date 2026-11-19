"""Probe gate (AC-15): no build step is green until its probes have current results.

A result file `tools/probes/results/<id>.json` is current when
  - its `script_sha256` equals the sha256 of the probe script it names (editing
    a probe invalidates its result),
  - its `image_digest` equals the pinned digest in docs/HOST.md,
  - its `n` equals the pinned N in tools/probes/pinned_n.json.
All of P1 to P16 must be current for any step 1 to 22. A verdict other than
PASS (FAIL, LIMITED, ERROR) for a probe the step depends on is accepted only
for FAIL and LIMITED, and only when a decision record exists in docs/decisions/
naming the probe id, the verdict, the mitigation and a `Decided by:` line that
names who decided (the project owner or main).
Verdicts are stated relative to the probe's expected result (P4 returning
"unsupported" is a PASS; see probe_p4_percolate_knn.py).

    python -m tools.probe_gate STEP [--root DIR]     exit 0 = the step may be marked green
"""
from __future__ import annotations

import hashlib
import json
import re
import sys
from pathlib import Path

ALL = [f"P{i}" for i in range(1, 17)]
STEP_NEEDS = {5: ["P2", "P3", "P15", "P16"], 13: ["P5", "P8", "P11"], 14: ["P9", "P10"], 15: ["P6", "P14", "P15"]}
PROBE_SCRIPT = {  # result id -> script that produces it (relative to tools/probes)
    "P1": "probe_p1_licence.py", "P2": "probe_isolation.py", "P3": "probe_isolation.py", "P4": "probe_p4_percolate_knn.py",
    "P5": "probe_refresh.py", "P6": "probe_isolation.py", "P7": "probe_p7_knn_filter.py", "P8": "probe_p8_alias_swap.py",
    "P9": "probe_p9_p10_expunge.py", "P10": "probe_p9_p10_expunge.py", "P11": "probe_p11_seqno.py",
    "P12": "probe_p12_msearch.py", "P13": "probe_p13_terms.py", "P14": "probe_p14_authz_block.py",
    "P15": "probe_p15_key_owner.py", "P16": "probe_p16_slowlogs.py",
}


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def pinned_digest(root: Path) -> str | None:
    m = re.search(r"^ES-image-digest:\s*(sha256:[0-9a-f]{64})\s*$", (root / "docs" / "HOST.md").read_text(), re.M)
    return m.group(1) if m else None


def decision_ok(root: Path, pid: str, verdict: str) -> bool:
    """A decision record is a file in docs/decisions naming the probe, the verdict, a mitigation and a `Decided by:` line
    that names the owner or main. A record with no such line, or one naming anyone else, is refused."""
    d = root / "docs" / "decisions"
    for f in sorted(d.glob("*.md")) if d.is_dir() else []:
        t = f.read_text()
        if (re.search(rf"^Probe:\s*{pid}\s*$", t, re.M) and re.search(rf"^Verdict:\s*{verdict}\s*$", t, re.M)
                and re.search(r"^Mitigation:\s*\S", t, re.M) and re.search(r"^Decided by:.*\b(owner|main)\b", t, re.M | re.I)):
            return True
    return False


def check_result(root: Path, pid: str) -> tuple[dict | None, list[str]]:
    probes = root / "tools" / "probes"
    f = probes / "results" / f"{pid}.json"
    if not f.is_file():
        return None, [f"{pid}: no result file"]
    try:
        rec = json.loads(f.read_text())
    except ValueError:
        return None, [f"{pid}: result file unreadable"]
    out = []
    script = probes / PROBE_SCRIPT[pid]
    if rec.get("id") != pid:
        out.append(f"{pid}: result names another id")
    if not script.is_file() or rec.get("script_sha256") != _sha(script):
        out.append(f"{pid}: script changed since the result (hash differs)")
    if rec.get("image_digest") != pinned_digest(root):
        out.append(f"{pid}: image digest differs from docs/HOST.md")
    try:
        pinned = json.loads((probes / "pinned_n.json").read_text())[pid]
    except (KeyError, ValueError, OSError):
        pinned = None
    if rec.get("n") != pinned:
        out.append(f"{pid}: N {rec.get('n')} differs from pinned {pinned}")
    if rec.get("verdict") not in ("PASS", "FAIL", "LIMITED", "ERROR"):
        out.append(f"{pid}: verdict {rec.get('verdict')!r} not recognised")
    return rec, out


def gate(root: Path, step: int) -> list[str]:
    """Reasons the step may not be marked green (empty = it may)."""
    root = Path(root)
    refusals: list[str] = []
    recs: dict[str, dict] = {}
    for pid in ALL:
        rec, why = check_result(root, pid)
        refusals += why
        if rec:
            recs[pid] = rec
    for pid in STEP_NEEDS.get(step, []):
        rec = recs.get(pid)
        if rec is None:
            continue
        v = rec.get("verdict")
        if v == "PASS":
            continue
        if v in ("FAIL", "LIMITED") and decision_ok(root, pid, v):
            continue
        refusals.append(f"{pid}: verdict {v} needs a decision record in docs/decisions")
    return refusals


def main(argv: list[str]) -> int:
    root = Path(argv[argv.index("--root") + 1]) if "--root" in argv else Path(__file__).resolve().parents[1]
    step = int(argv[0])
    why = gate(root, step)
    for w in why:
        print("REFUSED:", w)
    print("GATE", "REFUSES" if why else "OPEN", f"step {step}")
    return 1 if why else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
