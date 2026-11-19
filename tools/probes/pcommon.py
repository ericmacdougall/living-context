"""Shared helpers for the platform probes (BUILD-PLAN Step 0, AC-15).

Every probe script calls `finish()`, which writes `results/<id>.json` holding
{id, verdict, expected, observed, script, script_sha256, image_digest, n,
commit, date, ...}. The verdict is stated relative to the probe's expected
result (a probe whose expected result is "unsupported" records PASS when the
cluster says unsupported). N is the executed-check count and must equal the
pinned N in `pinned_n.json`; a probe that executed a different count records
verdict ERROR. Editing the script changes its sha256 and so invalidates the
recorded result (the gate compares).

A probe run against a cluster with security off prints
"security off: P2 not established" and exits 2 without a verdict.
"""
from __future__ import annotations

import datetime
import hashlib
import json
import os
import ssl
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
RESULTS = Path(os.environ.get("LC_PROBE_RESULTS", HERE / "results"))
SECURITY_OFF = "security off: P2 not established"


def sha256_file(path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def pinned_n(pid: str) -> int:
    return json.loads((HERE / "pinned_n.json").read_text())[pid]


def image_digest() -> str:
    """Digest of the image the cluster runs, passed by the runner (never guessed here)."""
    return os.environ.get("LC_IMAGE_DIGEST", "unknown")


def git_commit() -> str:
    if os.environ.get("LC_COMMIT"):
        return os.environ["LC_COMMIT"]
    try:
        root = HERE.parent.parent
        sha = subprocess.run(["git", "-C", str(root), "rev-parse", "HEAD"], capture_output=True, text=True,
                             timeout=10).stdout.strip()
        return sha or "no-git"
    except Exception:
        return "no-git"


def unauth_status(url: str, ca: str | None) -> int:
    ctx = ssl.create_default_context(cafile=ca) if url.startswith("https") and ca else None
    try:
        with urllib.request.urlopen(url.rstrip("/") + "/", context=ctx, timeout=15) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code
    except Exception:
        return 0


def require_security(url: str, ca: str | None) -> None:
    """Exit 2 with the fixed phrase when the cluster answers an unauthenticated request."""
    if unauth_status(url, ca) == 200:
        print(json.dumps({"verdict": SECURITY_OFF}))
        sys.exit(2)


def admin_client():
    """Basic-auth client for the secured test cluster; credentials by file path only."""
    from living_context.gateway.es import EsClient
    url, ca, pw = os.environ["LC_TEST_ES_URL"], os.environ["LC_TEST_CA"], os.environ["LC_TEST_PW"]
    return EsClient(url, ca, pw, scheme="basic", user="elastic", timeout=120)


def start(argv_url_env: bool = True):
    """Common start: returns (url, ca) after the security-off check."""
    url, ca = os.environ.get("LC_TEST_ES_URL", ""), os.environ.get("LC_TEST_CA")
    if len(sys.argv) > 1 and sys.argv[1].startswith("http"):
        url = sys.argv[1]
    require_security(url, ca)
    return url, ca


def finish(pid: str, script: str, expected: str, observed: str, ok: bool, n: int, details: dict,
           verdict: str | None = None, results_dir: Path | None = None) -> int:
    pinned = pinned_n(pid)
    if n != pinned:
        verdict, ok = "ERROR", False
        details = dict(details, n_mismatch=f"executed {n}, pinned {pinned}")
    rec = {"id": pid, "verdict": verdict or ("PASS" if ok else "FAIL"), "expected": expected,
           "observed": observed, "script": Path(script).name, "script_sha256": sha256_file(script),
           "image_digest": image_digest(), "n": n, "commit": git_commit(),
           "date": datetime.date.today().isoformat(), "details": details}
    out = Path(results_dir or RESULTS)
    out.mkdir(parents=True, exist_ok=True)
    (out / f"{pid}.json").write_text(json.dumps(rec, indent=2, sort_keys=True) + "\n")
    print(json.dumps({k: rec[k] for k in ("id", "verdict", "n", "script_sha256")}))
    return 0 if rec["verdict"] == "PASS" else 1


# ---- small cluster helpers shared by the probes --------------------------------------------------

def create(es, name, props=None, settings=None, aliases=None):
    es.request("DELETE", f"/{name}")
    body = {"settings": dict({"number_of_shards": 1, "number_of_replicas": 0}, **(settings or {}))}
    if props:
        body["mappings"] = {"properties": props}
    if aliases:
        body["aliases"] = aliases
    st, r = es.request("PUT", f"/{name}", body)
    assert st == 200, (name, st)


def bulk_index(es, index, docs, refresh=True):
    """docs: iterable of (id, source). Returns the number of failed items."""
    lines = []
    for i, d in docs:
        lines += [{"index": {"_index": index, "_id": i}}, d]
    bad = 0
    for k in range(0, len(lines), 4000):
        st, out = es.request("POST", "/_bulk", lines[k:k + 4000], ndjson=True)
        bad += sum(1 for it in out.get("items", []) if it["index"]["status"] not in (200, 201))
    if refresh:
        es.request("POST", f"/{index}/_refresh")
    return bad


def drop(es, *names):
    for n in names:
        es.request("DELETE", f"/{n}")


def licence_error(status, body) -> bool:
    text = json.dumps(body).lower() if not isinstance(body, str) else body.lower()
    return status in (400, 403) and ("license" in text or "licence" in text)


def p4_verdict(status_top_level: int, status_in_bool: int, status_control: int) -> str:
    """P4's verdict relative to its expected result: storing a knn query must be refused
    (unsupported) while the control (a stored match query) is accepted."""
    stored = [s for s in (status_top_level, status_in_bool) if s in (200, 201)]
    return "PASS" if not stored and status_control in (200, 201) else "FAIL"
