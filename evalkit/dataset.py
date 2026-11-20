"""Dataset adapter, field whitelist and the registered split (EXPERIMENT 3.1, 3.2; BUILD-PLAN Step 20).

The adapter keeps, per turn, `role` and `content` only, and per session its content hash. `has_answer`, dates, ids
and question types are never read into a turn, so none of them can reach a write, a document or a prompt. Gold is
the question's `answer_session_ids` mapped to content hashes and lives only on the harness side.

The split code (`split_pilot_test`, `split_assignment`, `full_split_assignment`, `sensitivity_excluded`, the two
cluster rules and the content hash) is vendored from the owner's earlier evaluation harness, whose tree is not a git
checkout, so no source commit exists to record. Source files by sha256 prefix: dataset.py cb6a7b0068f252a4,
content.py 150fde11ff0d652b, corpus.py b008689e7ba07aba. Behaviour is unchanged; names and typing were simplified.

The fresh confirmatory set is PENDING RELEASE (EXPERIMENT 3.2, X1): `development_questions` returns only pilot, dev2
and held_back questions and `confirmatory_questions` refuses. `fresh_questions` returns it for stage S6-H1 only, behind a release
file in the run directory that names the owner's release and the freeze manifest (the release follows the freeze).
"""
from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

DATASET_SHA256 = "d6f21ea9d60a0d56f34a05b609c79c88a451d2ae03597821ea3d5a9678c3a442"  # LongMemEval-S cleaned
SPLIT_SEED = 16096
DEV2_SIZE = 16
OPAQUE_SALT = "lc-eval-v1"
REGISTERED_COUNTS = {"pilot": 102, "dev2": 16, "held_back": 19, "test": 363}   # EXPERIMENT 3.2 (test = fresh + excluded)
REGISTERED_SENSITIVITY_EXCLUDED = 51
WHITELIST_TURN = ("role", "content")


class DatasetError(Exception):
    pass


class Unreleased(DatasetError):
    """The fresh confirmatory set is not released (EXPERIMENT 3.2, X1)."""


@dataclass(frozen=True)
class Question:
    qid: str
    qtype: str
    question: str
    answer: str                      # harness side only; never model-facing
    gold_session_ids: Tuple[str, ...]  # harness side only
    sessions: Tuple[Tuple[str, Tuple[Tuple[str, str], ...]], ...]   # (session id, ((role, content), ...)), first copy kept

    @property
    def is_abstention(self) -> bool:
        return self.qid.endswith("_abs")

    @property
    def haystack_session_ids(self) -> List[str]:
        return [sid for sid, _ in self.sessions]


def parse_record(rec: dict) -> Question:
    sids, sess = rec["haystack_session_ids"], rec["haystack_sessions"]
    if len(sids) != len(sess):
        raise DatasetError("haystack arrays differ in length")
    seen, out = set(), []
    for sid, turns in zip(sids, sess):
        if sid in seen:     # a session id repeated inside one haystack: keep the first
            continue
        seen.add(sid)
        out.append((sid, tuple((str(t["role"]), str(t["content"])) for t in turns)))   # the whitelist: role, content
    return Question(str(rec["question_id"]), str(rec["question_type"]), str(rec["question"]), str(rec["answer"]),
                    tuple(rec.get("answer_session_ids", [])), tuple(out))


def file_sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def load_questions(path: str, *, expect_sha256: Optional[str] = DATASET_SHA256) -> List[Question]:
    if expect_sha256 is not None and file_sha256(path) != expect_sha256:
        raise DatasetError("dataset sha256 differs from the registered one")
    with open(path, "r", encoding="utf-8") as fh:
        return [parse_record(r) for r in json.load(fh)]


# ---- content identity -------------------------------------------------------------------------------------------
def turns_hash(turns: Sequence[Tuple[str, str]]) -> str:
    return hashlib.sha256(json.dumps([[r, t] for r, t in turns], ensure_ascii=False).encode("utf-8")).hexdigest()


def session_hashes(questions: Iterable[Question]) -> Dict[str, str]:
    """session id -> content hash. Raises if an id maps to two contents."""
    out: Dict[str, str] = {}
    for q in questions:
        for sid, turns in q.sessions:
            h = turns_hash(turns)
            if sid in out and out[sid] != h:
                raise DatasetError("session id has two different contents")
            out[sid] = h
    return out


def opaque_ref(content_hash: str, salt: str = OPAQUE_SALT) -> str:
    """Model-facing session id: `s` + 12 hex of a salted hash of the content hash."""
    return "s" + hashlib.sha256((salt + ":" + content_hash).encode()).hexdigest()[:12]


_GOLD_ID = re.compile(r"\banswer" r"_[0-9a-f]{6,}(?:_\d+)?\b")


def assert_model_safe(text: str) -> None:
    """The id guard: a model-facing file or prompt may not carry a gold-revealing id (every gold id begins `answer_`)."""
    if _GOLD_ID.search(text):
        raise DatasetError("a gold-revealing id is in model-facing text")


# ---- clusters and the split (vendored) --------------------------------------------------------------------------
def cluster_ids(questions: Iterable[Question]) -> Dict[str, str]:
    """qid -> cluster id (smallest qid): questions that share a gold session id are one unit."""
    qs = list(questions)
    parent = {q.qid: q.qid for q in qs}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    owner: Dict[str, str] = {}
    for q in qs:
        for s in q.gold_session_ids:
            if s in owner:
                a, b = find(owner[s]), find(q.qid)
                if a != b:
                    parent[max(a, b)] = min(a, b)
            else:
                owner[s] = q.qid
    return {q.qid: find(q.qid) for q in qs}


def content_clusters(questions: Sequence[Question], hashes: Dict[str, str]) -> Dict[str, str]:
    """qid -> content cluster id: questions whose gold sessions share a content hash, and each base question with
    its `_abs` twin."""
    qs = list(questions)
    parent = {q.qid: q.qid for q in qs}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[max(ra, rb)] = min(ra, rb)

    owner: Dict[str, str] = {}
    for q in qs:
        for s in q.gold_session_ids:
            h = hashes.get(s)
            if h is None:
                continue
            if h in owner:
                union(owner[h], q.qid)
            else:
                owner[h] = q.qid
    ids = set(parent)
    for q in qs:
        if q.qid.endswith("_abs") and q.qid[:-4] in ids:
            union(q.qid[:-4], q.qid)
    return {q.qid: find(q.qid) for q in qs}


def split_pilot_test(qs: Sequence[Tuple[str, str]], seed: int = SPLIT_SEED, pilot_fraction: float = 0.2,
                     clusters: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    """Deterministic stratified split. qs = [(qid, stratum)] -> {qid: 'pilot'|'test'}. Units are clusters; within each
    stratum, units are ordered by sha256("seed:cluster id") and added to the pilot until it holds at least
    ceil(fraction*n) questions (at least one unit when the stratum has 2 or more units, never all)."""
    clusters = clusters or {}
    by: Dict[str, Dict[str, List[str]]] = {}
    for qid, stratum in qs:
        by.setdefault(stratum, {}).setdefault(clusters.get(qid, qid), []).append(qid)
    out: Dict[str, str] = {}
    for stratum, units in by.items():
        n = sum(len(v) for v in units.values())
        want = math.ceil(pilot_fraction * n)
        order = sorted(units, key=lambda c: hashlib.sha256(("%d:%s" % (seed, c)).encode()).hexdigest())
        got = 0
        for i, c in enumerate(order):
            take = len(order) >= 2 and got < want and i < len(order) - 1
            for q in units[c]:
                out[q] = "pilot" if take else "test"
            got += len(units[c]) if take else 0
    return out


def split_assignment(questions: Sequence[Question]) -> Dict[str, str]:
    """qid -> 'pilot' | 'dev2' | 'test'; dev2 takes the first DEV2_SIZE knowledge-update ids (sorted) from the test
    split that are not abstention and whose cluster holds exactly one question."""
    clusters = cluster_ids(questions)
    sp = split_pilot_test([(q.qid, q.qtype) for q in questions], clusters=clusters)
    size: Dict[str, int] = {}
    for c in clusters.values():
        size[c] = size.get(c, 0) + 1
    cand = sorted(q.qid for q in questions if sp[q.qid] == "test" and q.qtype == "knowledge-update"
                  and not q.is_abstention and size[clusters[q.qid]] == 1)
    for qid in cand[:DEV2_SIZE]:
        sp[qid] = "dev2"
    return sp


def full_split_assignment(questions: Sequence[Question]) -> Dict[str, str]:
    """qid -> 'pilot' | 'dev2' | 'test' | 'held_back': every test question whose content cluster touches a pilot or
    dev2 question moves to held_back."""
    sp = split_assignment(questions)
    cc = content_clusters(questions, session_hashes(questions))
    touched = {cc[q] for q, v in sp.items() if v in ("pilot", "dev2")}
    return {q: ("held_back" if v == "test" and cc[q] in touched else v) for q, v in sp.items()}


def sensitivity_excluded(questions: Sequence[Question], assignment: Dict[str, str], hashes: Dict[str, str]) -> List[str]:
    """Non-abstention test questions whose gold content appears verbatim inside a pilot or dev2 haystack."""
    seen = set()
    for q in questions:
        if assignment.get(q.qid) in ("pilot", "dev2"):
            seen |= {hashes[s] for s in q.haystack_session_ids}
    return sorted(q.qid for q in questions if assignment.get(q.qid) == "test" and not q.is_abstention
                  and any(hashes.get(s) in seen for s in q.gold_session_ids))


def split_report(questions: Sequence[Question]) -> dict:
    """Counts only (no content): what Step 20 compares with EXPERIMENT 3.2."""
    sp = full_split_assignment(questions)
    hashes = session_hashes(questions)
    counts: Dict[str, int] = {}
    for v in sp.values():
        counts[v] = counts.get(v, 0) + 1
    return {"counts": counts, "sensitivity_excluded": len(sensitivity_excluded(questions, sp, hashes)),
            "distinct_contents": len(set(hashes.values())), "n_questions": len(sp)}


DEVELOPMENT_SPLITS = ("pilot", "dev2", "held_back")


def development_questions(questions: Sequence[Question], splits: Sequence[str] = ("pilot",)) -> List[Question]:
    """Questions of development splits only. The fresh `test` split is never returned from here."""
    bad = set(splits) - set(DEVELOPMENT_SPLITS)
    if bad:
        raise Unreleased("split %s is not a development split" % sorted(bad))
    sp = full_split_assignment(questions)
    return [q for q in questions if sp[q.qid] in splits]


def confirmatory_questions(*_a, **_k):
    raise Unreleased("the fresh confirmatory set is PENDING RELEASE by the owner (EXPERIMENT 3.2, X1); stage S6-H1 loads it only through "
                     "`fresh_questions` with a release file")


# ---- the fresh set, released for stage S6-H1 only (EXPERIMENT 3.2, X1) ---------------------------------------------------------------
RELEASED_STAGE = "S6-H1"
RELEASE_FILE = "RELEASE.json"
RELEASE_KEYS = ("stage", "release_ref", "source", "freeze_manifest_sha256")
# MEASURED counts of EXPERIMENT 3.2: 363 test = 312 fresh + 51 sensitivity-excluded; 312 = 299 with gold + 13 abstention; of the 299,
# 6 have gold content in a held_back haystack (the DEV-EXPOSED stratum), so the confirmatory H1 denominator is 293
REGISTERED_FRESH = {"test": 363, "excluded": 51, "fresh": 312, "non_abstention": 299, "abstention": 13, "dev_exposed": 6, "primary": 293}


@dataclass(frozen=True)
class FreshSet:
    """The released fresh set. `questions` holds all fresh questions (abstention included) in dataset order; the rest are qid tuples.
    primary: non-abstention, with gold, not DEV-EXPOSED (the H1 denominator); sensitivity: non-abstention with gold (DEV-EXPOSED
    included); abstention: reported as their own stratum, never in a recall mean (EXPERIMENT 3.1)."""
    questions: Tuple[Question, ...]
    primary: Tuple[str, ...]
    sensitivity: Tuple[str, ...]
    dev_exposed: Tuple[str, ...]
    abstention: Tuple[str, ...]
    excluded_n: int

    def counts(self) -> Dict[str, int]:
        return {"fresh": len(self.questions), "non_abstention": len(self.questions) - len(self.abstention),
                "abstention": len(self.abstention), "dev_exposed": len(self.dev_exposed), "primary": len(self.primary),
                "excluded": self.excluded_n}


def read_release(run_dir) -> dict:
    """The release record the operator writes into the run directory once the owner has released the fresh set for S6-H1 and the
    freeze manifest exists (its sha256 is part of the record, so a release cannot precede the freeze)."""
    import os
    path = os.path.join(str(run_dir), RELEASE_FILE)
    if not os.path.exists(path):
        raise Unreleased("no release file %s in the run directory: the fresh set stays unreleased" % RELEASE_FILE)
    try:
        with open(path, "r", encoding="utf-8") as fh:
            rec = json.load(fh)
    except ValueError:
        raise Unreleased("the release file is not valid JSON")
    if not isinstance(rec, dict) or any(not str(rec.get(k) or "").strip() for k in RELEASE_KEYS):
        raise Unreleased("the release file must name %s" % ", ".join(RELEASE_KEYS))
    return rec


def dev_exposed(questions: Sequence[Question], assignment: Dict[str, str], hashes: Dict[str, str], fresh: Iterable[str]) -> List[str]:
    """Fresh non-abstention questions with a gold content inside a held_back haystack (EXPERIMENT 3.2, review M2): development runs show
    that content to models, so these form the DEV-EXPOSED stratum and leave the H1 denominator."""
    shown = set()
    for q in questions:
        if assignment.get(q.qid) == "held_back":
            shown |= {hashes[s] for s in q.haystack_session_ids}
    by = {q.qid: q for q in questions}
    return sorted(qid for qid in fresh if not by[qid].is_abstention and any(hashes.get(s) in shown for s in by[qid].gold_session_ids))


def fresh_questions(questions: Sequence[Question], *, stage: str, run_dir, release_ref: str, freeze_sha256: str,
                    registered: Optional[Dict[str, int]] = None, extra_dev_exposed: Sequence[str] = ()) -> FreshSet:
    """The fresh set, for stage S6-H1 only and only with a release file in the run directory whose stage, reference and freeze manifest
    sha256 match the ones the run was started with. Every other stage, and any missing or mismatched release, raises `Unreleased`.
    The 51-question sensitivity exclusion is applied (`sensitivity_excluded`), abstention questions are kept apart, the DEV-EXPOSED
    stratum (computed, plus any qids an amendment adds through the freeze manifest) leaves the primary set, and every count must equal
    the registered one (counts only; nothing of the content is reported)."""
    if stage != RELEASED_STAGE:
        raise Unreleased("the fresh set is released for stage %s only, not %s" % (RELEASED_STAGE, stage))
    rec = read_release(run_dir)
    if rec["stage"] != RELEASED_STAGE:
        raise Unreleased("the release file is for stage %r" % rec["stage"])
    if not (release_ref or "").strip() or rec["release_ref"] != release_ref:
        raise Unreleased("the release reference differs from the release file")
    if not (freeze_sha256 or "").strip() or rec["freeze_manifest_sha256"] != freeze_sha256:
        raise Unreleased("the release file names a different freeze manifest: the release follows the freeze")
    reg = dict(REGISTERED_FRESH if registered is None else registered)
    sp = full_split_assignment(questions)
    hashes = session_hashes(questions)
    test = [q for q in questions if sp[q.qid] == "test"]
    excl = set(sensitivity_excluded(questions, sp, hashes))
    fresh = [q for q in test if q.qid not in excl]
    abst = tuple(q.qid for q in fresh if q.is_abstention)
    exposed_calc = dev_exposed(questions, sp, hashes, [q.qid for q in fresh])
    extra = sorted(set(extra_dev_exposed) - set(exposed_calc))
    fresh_ids = {q.qid for q in fresh}
    if any(x not in fresh_ids for x in extra):
        raise DatasetError("an amended DEV-EXPOSED question is not in the fresh set")
    exposed = tuple(sorted(set(exposed_calc) | set(extra)))
    sens = tuple(q.qid for q in fresh if not q.is_abstention and q.gold_session_ids)
    primary = tuple(x for x in sens if x not in set(exposed))
    got = {"test": len(test), "excluded": len(excl), "fresh": len(fresh), "non_abstention": len(fresh) - len(abst), "abstention": len(abst),
           "dev_exposed": len(exposed_calc), "primary": len(primary)}   # an amendment that adds DEV-EXPOSED qids restates `primary`
    bad = {k: (got[k], reg[k]) for k in reg if got.get(k) != reg[k]}
    if bad:
        raise DatasetError("fresh-set counts differ from the registered ones (got, registered): %s" % bad)
    return FreshSet(tuple(fresh), primary, sens, exposed, abst, len(excl))
