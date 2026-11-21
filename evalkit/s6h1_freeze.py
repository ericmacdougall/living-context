"""The S6-H1 freeze (EXPERIMENT 3.7): what S3 freezes for S6-H1, written once as a manifest and checked before S6-H1 starts.

  python -I -m evalkit.s6h1_freeze --s3-run-dir <S3 run dir> --commit <code commit> --model <model id> [--out PATH]

writes `evalkit/freeze/S6-H1.json` (no model call, no dataset read). The operator commits that file ALONE (the freeze commit); its tree
equals the code commit's tree plus the manifest, so the code digest below, which leaves `evalkit/freeze/` out, is the same for both.

The manifest holds
  * `code_commit` and `code_sha256`: the commit the values were computed from, and a digest over every source file of `living_context/`
    and `evalkit/` and the two lock files (the prompts, thresholds, merge rule and statistics are code, so a changed byte anywhere in
    them changes the digest);
  * `values`: the registered values S6-H1 uses, named one by one: beta of A0+LEN from S3's `beta.json`, the A0 field set S3 ran with,
    every tree constant, the merge rule (k, window, tie salt), K, the pinned embedder, a digest of the rendered prompts of the H1 task
    kinds and the CLI invocation, the thresholds and statistics settings, SHUFFLED's question count and minimum, and the registered
    fresh-set counts with any DEV-EXPOSED qids an amendment adds;
  * `s3`: S3's commit and the sha256 of its STATE.json, STAMP.json and beta.json (S3 must be COMPLETE).

`check` refuses S6-H1 unless the manifest exists, names stage S6-H1, its `code_commit` equals the commit under test, the code under
test has the frozen digest, every registered value recomputed from that code equals the manifest's, the model is the frozen one, and
S3's run directory still says COMPLETE with the same three files."""
from __future__ import annotations

import argparse
import dataclasses
import hashlib
import json
import sys
from pathlib import Path
from typing import List

STAGE = "S6-H1"
ROOT = Path(__file__).resolve().parent.parent
MANIFEST = Path(__file__).resolve().parent / "freeze" / "S6-H1.json"
DIGEST_DIRS = ("living_context", "evalkit")
DIGEST_FILES = ("requirements.lock", "requirements-embed.lock")
DIGEST_SKIP = ("evalkit/freeze/",)
SHUFFLED_N = 50                     # EXPERIMENT 6: SHUFFLED-SUMMARIES on 50 questions
SHUFFLED_MIN_POINT = 0.10           # EXPERIMENT 6: drop with point at least 0.10 and lower bound above 0
H1_TASKS = ("route", "organise", "build_l1", "build_upper")


class FreezeError(Exception):
    pass


def code_digest(root: Path = ROOT) -> str:
    """sha256 over (relative path, sha256 of content) of every .py file under DIGEST_DIRS and the lock files, in path order;
    `evalkit/freeze/` and caches are left out."""
    root = Path(root)
    files = []
    for d in DIGEST_DIRS:
        for p in sorted((root / d).rglob("*.py")):
            rel = p.relative_to(root).as_posix()
            if "__pycache__" in rel or any(rel.startswith(s) for s in DIGEST_SKIP):
                continue
            files.append(rel)
    files += [f for f in DIGEST_FILES if (root / f).exists()]
    h = hashlib.sha256()
    for rel in sorted(files):
        h.update(rel.encode() + b"\0" + hashlib.sha256((root / rel).read_bytes()).hexdigest().encode() + b"\n")
    return h.hexdigest()


def prompts_digest() -> str:
    """sha256 of the system prompt, the CLI invocation, the prompt versions and the user message of every H1 task kind rendered over a
    fixed input: a changed template, instruction or limit changes it."""
    from living_context.model import port
    from . import claude_cli
    canon = {
        "route": {"query": "Q", "children": [{"label": "c1", "title": "t", "gist": "g"}], "b_fanout": 6},
        "organise": {"pointers": [{"pointer": "p1", "path": "a/b", "score": 1.0}]},
        "build_l1": {"leaves": [{"text": "x", "title": "t"}]},
        "build_upper": {"children": [{"summary": "s", "facts": ["f"], "title": "t"}]},
    }
    parts = [claude_cli.SYSTEM_PROMPT, claude_cli.PROMPT_VERSION, json.dumps(port.PROMPT_VERSION, sort_keys=True),
             json.dumps(claude_cli.argv_for("<binary>", "<model>")), json.dumps(sorted(claude_cli.CHILD_ENV.items()))]
    parts += [t + "\0" + claude_cli.render(t, canon[t]) for t in H1_TASKS]
    return hashlib.sha256("\n\x1e".join(parts).encode()).hexdigest()


def registered_values(*, beta: float, a0_fields: str, dev_exposed_extra=()) -> dict:
    """The values S6-H1 runs with, computed from the code. An amendment that adds DEV-EXPOSED qids lowers the primary count by as many."""
    from living_context.config import Constants
    from living_context.embed import e5
    from living_context.recall.fusion import K_RRF, WINDOW
    from . import arms, dataset as ds, stats
    if a0_fields not in ("strong", "text"):
        raise FreezeError("a0_fields must be strong or text")
    return {
        "beta": float(beta), "a0_fields": a0_fields,
        "constants": dataclasses.asdict(Constants()),
        "merge_rule": {"k_rrf": K_RRF, "window": WINDOW, "tie_salt": "lc", "a1": "RRF(tree, direct), each cut at window"},
        "K": arms.K, "embedder": e5.MODEL_ID, "prompts_sha256": prompts_digest(),
        "thresholds": {"h1_min_effect": stats.H1_MIN_EFFECT, "shuffled_min_point": SHUFFLED_MIN_POINT, "shuffled_n": SHUFFLED_N,
                       "seed": stats.SEED, "resamples": stats.RESAMPLES, "interval": "clustered paired bootstrap, percentile 95%",
                       "wilson": "95%", "split_seed": ds.SPLIT_SEED},
        "registered_fresh": dict(ds.REGISTERED_FRESH, primary=ds.REGISTERED_FRESH["primary"] - len(set(dev_exposed_extra))),
        "dev_exposed_extra": sorted(dev_exposed_extra),
    }


def _sha(p: Path) -> str:
    return hashlib.sha256(Path(p).read_bytes()).hexdigest()


def s3_record(s3_dir) -> dict:
    """S3 must be COMPLETE: its STATE.json says so, its STAMP.json is stage S3, its beta.json holds the frozen beta."""
    d = Path(s3_dir)
    need = [d / "STATE.json", d / "STAMP.json", d / "beta.json"]
    missing = [p.name for p in need if not p.exists()]
    if missing:
        raise FreezeError("S3 run directory lacks %s" % ", ".join(missing))
    state, stamp, beta = (json.loads(p.read_text()) for p in need)
    if state.get("state") != "COMPLETE":
        raise FreezeError("S3 is not COMPLETE (state %r)" % state.get("state"))
    if stamp.get("stage") != "S3":
        raise FreezeError("the S3 run directory's stamp is for stage %r" % stamp.get("stage"))
    if not isinstance(beta.get("beta"), (int, float)) or isinstance(beta.get("beta"), bool):
        raise FreezeError("S3's beta.json holds no beta")
    return {"commit": stamp.get("commit"), "a0_fields": stamp.get("a0_fields"), "beta": float(beta["beta"]),
            "state_sha256": _sha(need[0]), "stamp_sha256": _sha(need[1]), "beta_sha256": _sha(need[2])}


def make_manifest(*, s3_dir, code_commit: str, model: str, root: Path = ROOT, dev_exposed_extra=()) -> dict:
    if not (code_commit or "").strip() or not (model or "").strip():
        raise FreezeError("the freeze needs the code commit and the model id")
    rec = s3_record(s3_dir)
    vals = registered_values(beta=rec["beta"], a0_fields=rec["a0_fields"] or "", dev_exposed_extra=dev_exposed_extra)
    return {"stage": STAGE, "code_commit": code_commit, "code_sha256": code_digest(root), "model": model,
            "s3": {k: rec[k] for k in ("commit", "state_sha256", "stamp_sha256", "beta_sha256")}, "values": vals}


def write_manifest(m: dict, path: Path = MANIFEST) -> str:
    path = Path(path)
    if path.exists():
        raise FreezeError("%s exists: the freeze is written once" % path)
    path.parent.mkdir(parents=True, exist_ok=True)
    body = json.dumps(m, indent=1, sort_keys=True) + "\n"
    path.write_text(body)
    return hashlib.sha256(body.encode()).hexdigest()


def manifest_sha256(path: Path = MANIFEST) -> str:
    return _sha(path)


def load(path: Path = MANIFEST) -> dict:
    return json.loads(Path(path).read_text())


def check(*, commit: str, s3_dir, model: str, a0_fields: str, path: Path = MANIFEST, root: Path = ROOT) -> List[str]:
    """Reasons S6-H1 must not start; empty when the code, values and S3 are the frozen ones."""
    path = Path(path)
    if not path.exists():
        return ["no freeze manifest (%s): S3 freezes what S6-H1 uses, in a commit, before S6-H1 (EXPERIMENT 3.7)" % path.name]
    try:
        m = load(path)
    except ValueError:
        return ["the freeze manifest is not valid JSON"]
    why: List[str] = []
    if m.get("stage") != STAGE:
        why.append("the freeze manifest is for stage %r" % m.get("stage"))
    if not (commit or "").strip() or m.get("code_commit") != commit:
        why.append("the freeze manifest's commit %r is not the commit under test %r" % (m.get("code_commit"), commit))
    if m.get("code_sha256") != code_digest(root):
        why.append("the code under test differs from the frozen code (digest of living_context/, evalkit/ and the lock files)")
    v = m.get("values") or {}
    try:
        now = registered_values(beta=v.get("beta", float("nan")), a0_fields=v.get("a0_fields", ""),
                                dev_exposed_extra=v.get("dev_exposed_extra", ()))
    except (FreezeError, TypeError, ValueError) as e:
        now, _ = None, why.append("the frozen values cannot be recomputed: %s" % e)
    if now is not None:
        for k in sorted(set(now) | set(v)):
            if now.get(k) != v.get(k):
                why.append("registered value %r differs from its freeze" % k)
    if m.get("model") != model:
        why.append("the model %r is not the frozen one %r" % (model, m.get("model")))
    if v.get("a0_fields") != a0_fields:
        why.append("--a0-fields %r is not the frozen %r" % (a0_fields, v.get("a0_fields")))
    try:
        rec = s3_record(s3_dir)
    except FreezeError as e:
        why.append(str(e))
    else:
        for k in ("commit", "state_sha256", "stamp_sha256", "beta_sha256"):
            if rec[k] != (m.get("s3") or {}).get(k):
                why.append("S3's %s differs from the freeze" % k)
        if rec["beta"] != v.get("beta"):
            why.append("S3's beta differs from the frozen beta")
    return why


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--s3-run-dir", required=True)
    ap.add_argument("--commit", required=True, help="the code commit the values are computed from (the commit under test)")
    ap.add_argument("--model", required=True)
    ap.add_argument("--dev-exposed-extra", nargs="*", default=[], help="qids an exposure-audit amendment adds to DEV-EXPOSED")
    ap.add_argument("--out", default=str(MANIFEST))
    a = ap.parse_args(argv)
    try:
        m = make_manifest(s3_dir=a.s3_run_dir, code_commit=a.commit, model=a.model, dev_exposed_extra=a.dev_exposed_extra)
        sha = write_manifest(m, Path(a.out))
    except FreezeError as e:
        print("REFUSED: %s" % e)
        return 2
    print("wrote %s sha256 %s; commit it ALONE, then name this sha256 in the run directory's RELEASE.json" % (a.out, sha))
    return 0


if __name__ == "__main__":
    sys.exit(main())
