"""Offline top-50 direct-list log for a stamped H1 stage, with ZERO model calls.

Why this exists: the S3 ledger keeps each arm's top-10 sessions only, so the registered length control (A0+LEN) and any other
re-ranking of the flat list cannot be recomputed after the run. The S3 run cannot take a code change once it has charged a call
(`run_h1.check_prereqs`), so this tool does not touch the run: the direct list is deterministic (Elasticsearch over the tenant the
run built, plus the pinned local embedder), so it is recomputed here from the same tenant, read-only, for the same questions.

Output: one JSON line per question: qref, gold (opaque session refs), and `direct50`, the top-50 leaves in direct order as
[leaf id, opaque session ref, score, session tokens]. The file stays on the run host (leaf ids are internal ids, not model-facing).
Validity check after the run: `a0_from_row` must equal the ledger's A0 sessions for every question (and `a0_len_from_row` its
A0+LEN sessions with the frozen beta); a mismatch means the index changed and the log must not be used."""
import argparse
import hashlib
import json
from pathlib import Path
from typing import Callable, List, Mapping, Sequence, Tuple

from . import arms, dataset as ds
from .s2_stage import qref

Row = dict


def direct50_rows(questions: Sequence[ds.Question], hashes: Mapping[str, str], direct: Callable[[str], Sequence[Tuple[str, float]]],
                  world: arms.World) -> List[Row]:
    out = []
    for q in questions:
        d = list(direct(q.question))[:arms.WINDOW]
        entries = []
        for lid, score in d:
            sh = world.leaf_session.get(lid)
            entries.append([lid, ds.opaque_ref(sh) if sh is not None else None, float(score),
                            int(world.session_tokens.get(sh, 0)) if sh is not None else 0])
        out.append({"qref": qref(q.qid), "gold": [ds.opaque_ref(x) for x in sorted({hashes[s] for s in q.gold_session_ids})],
                    "direct50": entries})
    return out


def _world_of(row: Row) -> arms.World:
    ls = {e[0]: e[1] for e in row["direct50"] if e[1] is not None}
    st = {e[1]: e[3] for e in row["direct50"] if e[1] is not None}
    return arms.World(leaf_session=ls, session_tokens=st)


def a0_from_row(row: Row, k: int = arms.K) -> List[str]:
    """A0's top-k opaque sessions, rebuilt from the log alone."""
    d = [(e[0], e[2]) for e in row["direct50"]]
    return arms.distinct_sessions(arms.a0_leaves(d), _world_of(row), k)


def a0_len_from_row(row: Row, beta: float, k: int = arms.K) -> List[str]:
    """A0+LEN's top-k opaque sessions for any beta, rebuilt from the log alone (same rule as arms.a0_len_leaves)."""
    w = _world_of(row)
    d = [(e[0], e[2]) for e in row["direct50"]]
    return arms.distinct_sessions(arms.a0_len_leaves(d, w, beta), w, k)


def main(argv=None) -> int:
    from living_context.embed import e5
    from .eval_tenant import EvalTenant
    from .flat import A0_FIELDS_STRONG, A0_FIELDS_TEXT
    from .run_h1 import pick_questions
    from living_context.config import Constants
    ap = argparse.ArgumentParser()
    for f in ("--dataset", "--es-url", "--es-ca", "--es-pw-file", "--tenant", "--run-dir", "--embed-dir", "--out"):
        ap.add_argument(f, required=True)
    ap.add_argument("--n-questions", type=int, required=True)
    ap.add_argument("--splits", nargs="+", default=["pilot"])
    ap.add_argument("--a0-fields", default="strong", choices=["strong", "text"])
    ap.add_argument("--threads", type=int, default=4)
    a = ap.parse_args(argv)
    run_dir, out = Path(a.run_dir), Path(a.out)
    if out.exists():
        print("REFUSED: %s exists; the log is written once" % out)
        return 2
    qs = ds.load_questions(a.dataset)
    dev = pick_questions(ds.development_questions(qs, tuple(a.splits)), a.n_questions)
    hashes = ds.session_hashes(qs)
    emb = e5.E5Embedder(a.embed_dir, threads=a.threads)
    e5.require_real(emb, stage="S3")
    pport, qport = e5.ports(emb, limit=2)
    et = EvalTenant(a.es_url, a.es_ca, a.es_pw_file, a.tenant, run_dir / "tenant", consts=Constants(), embed=pport.for_tenant(a.tenant),
                    query_embed=qport.for_tenant(a.tenant), a0_fields=A0_FIELDS_TEXT if a.a0_fields == "text" else A0_FIELDS_STRONG,
                    attach=True)
    n = et.load_state(run_dir / "tenant_state.json")
    if et.leaf_count_in_es() != n:
        print("REFUSED: the tenant no longer holds the run's leaves")
        return 2
    rows = direct50_rows(dev, hashes, et.direct(), et.world)
    body = "".join(json.dumps(r, sort_keys=True) + "\n" for r in rows)
    out.write_text(body)
    short = sum(len(r["direct50"]) < arms.WINDOW for r in rows)
    print("wrote %d questions, %d with fewer than %d leaves, sha256 %s" % (len(rows), short, arms.WINDOW,
                                                                         hashlib.sha256(body.encode()).hexdigest()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
