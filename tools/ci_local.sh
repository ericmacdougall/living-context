#!/usr/bin/env bash
# Local CI gate. Runs the same suite as .github/workflows/ci.yml, on this machine, with no network services.
# Usage: tools/ci_local.sh            (uses ./.venv; override with VENV=/path/to/venv)
# Writes the full log to ci-local.log and exits non-zero on any failure.
set -euo pipefail
cd "$(dirname "$0")/.."
VENV="${VENV:-.venv}"
TIMEOUT="${TIMEOUT:-1800}"
PY="$VENV/bin/python"
[ -x "$PY" ] || { echo "no venv at $VENV; run: make venv" >&2; exit 2; }
{
  echo "== ci_local $(date -u +%Y-%m-%dT%H:%M:%SZ) commit $(git rev-parse --short HEAD 2>/dev/null || echo none)"
  "$PY" -m pytest -p no:cacheprovider tests/unit -q --timeout="$TIMEOUT" \
    --cov=living_context --cov=evalkit --cov-report=term --cov-report=json:coverage.json
  "$PY" examples/quickstart.py
  echo "== ci_local PASSED"
} 2>&1 | tee ci-local.log
