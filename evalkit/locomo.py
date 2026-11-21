"""LoCoMo adapter: dataset loader, evidence parsing, the turn to leaf gold map, the seeded subset and the per-question metrics
(docs/LOCOMO-PLAN.md is the registered definition of every rule here).

Pure with respect to the model, Elasticsearch and the embedder. The leaf map comes from the real segmenter run with each turn's index
carried in `Turn.ts` (a label only: the segmenter's time-gap rule fires on gaps over 1,800 and consecutive indices differ by 1), and
`bind` checks it against the leaves a tenant actually stored, so a gold leaf is never a guess.

Only `conversation.session_N` turns (speaker, text, blip_caption, dia_id) and `session_N_date_time` are read; observations, summaries,
image urls and the answers never become memory content. Categories are labelled by number only (the LoCoMo repository does not name
them)."""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import Dict, List, Mapping, Optional, Sequence, Tuple

from living_context.ingest import segment

from .dataset import file_sha256, turns_hash

DATA_SHA256 = "79fa87e90f04081343b8c8debecb80a9a6842b76a7aa537dc9fdf651ea698ff4"
SEED = 16096
CATEGORIES = (1, 2, 3, 4)                      # 5 (adversarial) has no evidence and is excluded
EVIDENCE_RE = re.compile(r"D:?(\d+):0*(\d+)")  # plan section 4
_SESSION_KEY = re.compile(r"session_(\d+)$")


class LocomoError(Exception):
    pass


@dataclass(frozen=True)
class Turn:
    dia_id: str            # normalised "D<n>:<m>"
    role: str
    content: str           # exactly what is ingested (date prefix and image caption included)


@dataclass(frozen=True)
class Session:
    n: int
    turns: Tuple[Turn, ...]

    @property
    def label(self) -> str:
        return "D%d" % self.n

    def role_content(self) -> List[Tuple[str, str]]:
        return [(t.role, t.content) for t in self.turns]


@dataclass(frozen=True)
class LQuestion:
    qid: str
    sample_id: str
    conv: int
    category: int
    question: str
    gold_turns: Tuple[str, ...]      # resolved evidence ids, in list order, without repeats
    n_unresolved: int                # ids parsed from the evidence that name no turn of the conversation (dropped)


@dataclass(frozen=True)
class Conversation:
    idx: int
    sample_id: str
    sessions: Tuple[Session, ...]
    questions: Tuple[LQuestion, ...]
    n_excluded: int = 0              # category 1 to 4 questions with no resolvable evidence

    def tenant(self) -> str:
        return "lcmo%d" % self.idx

    def session_hash(self, s: Session) -> str:
        """Content identity of a session, bound to its conversation so equal text in two conversations stays two sessions."""
        return turns_hash(s.role_content() + [("__id__", "%s:%d" % (self.sample_id, s.n))])


def norm_dia(raw: str) -> Optional[str]:
    m = EVIDENCE_RE.search(raw)
    return "D%d:%d" % (int(m.group(1)), int(m.group(2))) if m else None


def parse_evidence(items: Sequence[str]) -> List[str]:
    """All ids of all strings, normalised, in order, repeats removed (plan section 4)."""
    out: List[str] = []
    for s in items:
        for m in EVIDENCE_RE.finditer(str(s)):
            i = "D%d:%d" % (int(m.group(1)), int(m.group(2)))
            if i not in out:
                out.append(i)
    return out


def turn_content(t: Mapping, *, date: str = "", first: bool = False) -> str:
    text = str(t["text"])
    cap = str(t.get("blip_caption") or "").strip()
    if cap:
        text += " [image: %s]" % cap
    return ("[%s] %s" % (date, text)) if (first and date) else text


def parse_conversation(idx: int, rec: Mapping) -> Conversation:
    conv, sample_id = rec["conversation"], str(rec["sample_id"])
    keys = sorted((int(m.group(1)), k) for k in conv for m in [_SESSION_KEY.match(k)] if m)
    sessions: List[Session] = []
    seen = set()
    for n, key in keys:
        date = str(conv.get("%s_date_time" % key) or "").strip()
        turns = []
        for j, t in enumerate(conv[key]):
            d = norm_dia(str(t["dia_id"]))
            if d is None or not d.startswith("D%d:" % n):
                raise LocomoError("turn id %r does not belong to session %d" % (t["dia_id"], n))
            if d in seen:
                raise LocomoError("turn id %s appears twice" % d)
            seen.add(d)
            turns.append(Turn(d, str(t["speaker"]), turn_content(t, date=date, first=(j == 0))))
        if turns:
            sessions.append(Session(n, tuple(turns)))
    qs, excluded = [], 0
    for i, q in enumerate(rec["qa"]):
        cat = int(q["category"])
        if cat not in CATEGORIES:
            continue
        ids = parse_evidence(q.get("evidence") or [])
        ok = [x for x in ids if x in seen]
        if not ok:
            excluded += 1
            continue
        qs.append(LQuestion("%s#%d" % (sample_id, i), sample_id, idx, cat, str(q["question"]), tuple(ok), len(ids) - len(ok)))
    return Conversation(idx, sample_id, tuple(sessions), tuple(qs), excluded)


def load(path: str, *, expect_sha256: Optional[str] = DATA_SHA256) -> List[Conversation]:
    if expect_sha256 is not None and file_sha256(path) != expect_sha256:
        raise LocomoError("dataset sha256 differs from the registered one")
    with open(path, "r", encoding="utf-8") as fh:
        recs = json.load(fh)
    return [parse_conversation(i, r) for i, r in enumerate(recs)]


# ---- the turn to leaf map ---------------------------------------------------------------------------------------------------
@dataclass(frozen=True)
class LeafEntry:
    session: str                     # "D<n>"
    text: str                        # the leaf text the tenant must store
    turn_ids: Tuple[str, ...]        # dia ids of the turns whose text is in the leaf (a split turn is in each of its pieces)


def session_leaves(s: Session, cap_tokens: int) -> List[LeafEntry]:
    ts = [segment.Turn(t.role, t.content, float(i)) for i, t in enumerate(s.turns)]
    try:
        leaves, _ = segment.segment([ts], cap_tokens)
    except segment.Empty:
        return []
    return [LeafEntry(s.label, segment.render(l), tuple(s.turns[int(t.ts)].dia_id for t in l if t.content.strip())) for l in leaves]


def conversation_leaves(conv: Conversation, cap_tokens: int) -> List[LeafEntry]:
    """Every leaf of the conversation in ingest order (sessions in order, leaves in order)."""
    out: List[LeafEntry] = []
    for s in conv.sessions:
        out += session_leaves(s, cap_tokens)
    return out


@dataclass
class LeafMap:
    leaf_ids: List[str]                          # ingest order
    entries: List[LeafEntry]
    by_turn: Dict[str, List[str]] = field(default_factory=dict)       # dia id -> leaf ids

    def session_of(self) -> Dict[str, str]:
        return {i: e.session for i, e in zip(self.leaf_ids, self.entries)}

    def to_json(self) -> dict:
        return {"leaves": [{"id": i, "session": e.session, "turns": list(e.turn_ids)} for i, e in zip(self.leaf_ids, self.entries)]}


def bind(conv: Conversation, stored: Sequence[Tuple[str, str]], cap_tokens: int) -> LeafMap:
    """`stored` = (leaf id, stored text) in the order the tenant wrote them. Raises unless every stored text equals the segmented text at
    its position, then unless every evidence turn's text is inside the stored text of its mapped leaves (plan section 3)."""
    entries = conversation_leaves(conv, cap_tokens)
    if len(entries) != len(stored):
        raise LocomoError("the tenant stored %d leaves, the segmenter gives %d" % (len(stored), len(entries)))
    for k, ((lid, text), e) in enumerate(zip(stored, entries)):
        if text != e.text:
            raise LocomoError("stored leaf %d (%s) differs from the segmented text" % (k, lid))
    lm = LeafMap([lid for lid, _ in stored], list(entries))
    for lid, e in zip(lm.leaf_ids, entries):
        for t in e.turn_ids:
            lm.by_turn.setdefault(t, []).append(lid)
    text_of = dict(stored)
    turn = {t.dia_id: t for s in conv.sessions for t in s.turns}
    for q in conv.questions:
        for tid in q.gold_turns:
            leaves = lm.by_turn.get(tid)
            if not leaves:
                raise LocomoError("evidence turn %s is in no leaf" % tid)
            c = turn[tid].content
            if len(leaves) == 1:
                ok = c in text_of[leaves[0]]
            else:                                           # a turn longer than a leaf, split: both ends must be present
                ok = c[:30] in text_of[leaves[0]] and c[-30:] in text_of[leaves[-1]]
            if not ok:
                raise LocomoError("evidence turn %s is not inside its mapped leaf" % tid)
    return lm


def gold_of(q: LQuestion, lm: LeafMap) -> Tuple[List[str], List[str]]:
    """(gold leaf ids in ingest order, gold session labels in session order)."""
    leaves = {l for t in q.gold_turns for l in lm.by_turn[t]}
    order = [l for l in lm.leaf_ids if l in leaves]
    sess = sorted({int(t.split(":")[0][1:]) for t in q.gold_turns})
    return order, ["D%d" % n for n in sess]


def first_session(q: LQuestion) -> str:
    return q.gold_turns[0].split(":")[0]


# ---- the seeded stratified subset -------------------------------------------------------------------------------------------
def hash_key(qid: str, seed: int = SEED) -> str:
    return hashlib.sha256(("%d:locomo:%s" % (seed, qid)).encode()).hexdigest()


def allocate(counts: Mapping[int, int], n: int) -> Dict[int, int]:
    """Proportional allocation of n over the categories, largest remainder, ties to the lower category."""
    total = sum(counts.values())
    if n >= total:
        return dict(counts)
    base = {c: (n * k) // total for c, k in counts.items()}
    rem = sorted(counts, key=lambda c: (-((n * counts[c]) % total), c))
    for c in rem[: n - sum(base.values())]:
        base[c] += 1
    return base


def select_subset(questions: Sequence[LQuestion], n: int, *, seed: int = SEED, exclude: Sequence[str] = ()) -> List[LQuestion]:
    """n questions stratified by category, in hash order (the order is global, so a prefix mixes categories and conversations)."""
    ex = set(exclude)
    pool = [q for q in questions if q.qid not in ex]
    by: Dict[int, List[LQuestion]] = {}
    for q in pool:
        by.setdefault(q.category, []).append(q)
    alloc = allocate({c: len(v) for c, v in by.items()}, n)
    chosen: List[LQuestion] = []
    for c, v in by.items():
        chosen += sorted(v, key=lambda q: hash_key(q.qid, seed))[: alloc[c]]
    return sorted(chosen, key=lambda q: hash_key(q.qid, seed))


# ---- metrics ----------------------------------------------------------------------------------------------------------------
METRICS = ("leaf_r5", "leaf_r10", "sess_r5", "sess_r10", "mrr10", "allgold10", "allgold10_sess")


def distinct_sessions(leaves: Sequence[str], leaf_session: Mapping[str, str], k: int) -> List[str]:
    out: List[str] = []
    for l in leaves:
        s = leaf_session.get(l)
        if s is not None and s not in out:
            out.append(s)
            if len(out) == k:
                break
    return out


def question_metrics(leaves: Sequence[str], gold_leaves: Sequence[str], gold_sessions: Sequence[str], leaf_session: Mapping[str, str],
                     *, failed: bool = False) -> Dict[str, Optional[float]]:
    """The plan's metrics for one question and one arm. The list is scored as returned (never padded); a failed recall scores 0.
    `allgold10` is None unless the question has at least 2 gold leaves, `allgold10_sess` unless it has at least 2 gold sessions."""
    gl, gs = set(gold_leaves), set(gold_sessions)
    if failed:
        leaves = []
    top5, top10 = list(leaves[:5]), list(leaves[:10])
    mrr = next((1.0 / (i + 1) for i, l in enumerate(top10) if l in gl), 0.0)
    return {"leaf_r5": len(gl & set(top5)) / len(gl), "leaf_r10": len(gl & set(top10)) / len(gl),
            "sess_r5": len(gs & set(distinct_sessions(leaves, leaf_session, 5))) / len(gs),
            "sess_r10": len(gs & set(distinct_sessions(leaves, leaf_session, 10))) / len(gs), "mrr10": mrr,
            "allgold10": (float(gl <= set(top10)) if len(gl) >= 2 else None),
            "allgold10_sess": (float(gs <= set(distinct_sessions(leaves, leaf_session, 10))) if len(gs) >= 2 else None)}


def chance_leaf_recall(pool_leaves: int, k: int) -> float:
    """Expected gold-leaf recall@k of a uniformly random ranking of a pool of N leaves: min(k, N) / N."""
    return min(k, pool_leaves) / pool_leaves
