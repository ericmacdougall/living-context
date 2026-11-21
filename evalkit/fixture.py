"""Synthetic fixtures for the H1 gates (EXPERIMENT S0 stub gate, BUILD-PLAN Step 20): dataset-shaped questions with planted
gold, so the arms, the scorers and the controls can be shown red and green without the dataset.

Two worlds, same size:

  advantage  every question has one GOLD session: a long note whose topic words are repeated inside a lot of filler, plus a
             cluster of short MATE sessions that carry the same topic words singly. Flat search drowns the long gold under
             the short mates of its own and other questions (length normalisation); the tree finds the gold through the
             branch that holds the topic.
  null       every gold session is short and carries its topic words, like any other session: flat search finds it and the
             tree has nothing to add.

Words are generated syllable strings, deterministic by seed, never stop words and never containing digits.
"""
from __future__ import annotations

import random
from dataclasses import dataclass
from typing import Dict, List, Tuple

from .dataset import Question, turns_hash

_C = "bdfghklmnprstvz"
_V = "aeiou"


def _words(rng: random.Random, n: int, syl: int = 3) -> List[str]:
    out, seen = [], set()
    while len(out) < n:
        w = "".join(rng.choice(_C) + rng.choice(_V) for _ in range(syl))
        if w not in seen:
            seen.add(w)
            out.append(w)
    return out


@dataclass
class Fixture:
    questions: List[Question]
    sessions: List[Tuple[str, tuple]]          # (content hash, turns): every distinct session of the world
    queries: Dict[str, str]                    # qid -> question text
    gold: Dict[str, List[str]]                 # qid -> gold content hashes
    clusters: Dict[str, str]                   # qid -> content cluster


def _session(text_user: str, text_asst: str = "noted") -> tuple:
    return (("user", text_user), ("assistant", text_asst))


def make(kind: str = "advantage", n_questions: int = 40, seed: int = 11, mates: int = 15, noise_words: int = 300,
         reps: int = 3, mate_words: int = 2) -> Fixture:
    if kind not in ("advantage", "null"):
        raise ValueError(kind)
    rng = random.Random(seed)
    noise = _words(rng, 300, 2)
    topics = _words(rng, 3 * n_questions, 3)
    sessions: List[Tuple[str, tuple]] = []
    questions: List[Question] = []
    gold: Dict[str, List[str]] = {}
    queries: Dict[str, str] = {}
    for i in range(n_questions):
        t = topics[3 * i: 3 * i + 3]
        ask = " ".join(t)
        if kind == "advantage":
            ws = [rng.choice(noise) for _ in range(noise_words)]
            filler = " ".join(" ".join(ws[j:j + 10]) + "." for j in range(0, len(ws), 10))
            gold_turns = _session(f"{filler} " + " ".join(" ".join(t * 2) + "." for _ in range(reps)))
        else:
            gold_turns = _session(f"{ask} {rng.choice(noise)}")
        gh = turns_hash(gold_turns)
        sessions.append((gh, gold_turns))
        extra = []
        # mates: short sessions that each carry two of the topic words (advantage) or three unrelated words (null)
        for m in range(mates):
            if kind == "advantage":
                mt = _session(" ".join(rng.sample(t, mate_words) + [rng.choice(noise), rng.choice(noise)]))
            else:
                mt = _session(f"{rng.choice(noise)} {rng.choice(noise)} {rng.choice(noise)}")
            sessions.append((turns_hash(mt), mt))
            extra.append(mt)
        sid = f"s{i}"
        qid = f"q{i:03d}"
        qs = ((sid + "g", gold_turns),) + tuple((f"{sid}m{k}", mt) for k, mt in enumerate(extra))
        questions.append(Question(qid, "multi-session", ask, "n/a", (sid + "g",), qs))
        gold[qid] = [gh]
        queries[qid] = ask
    clusters = {q.qid: q.qid for q in questions}
    return Fixture(questions, sessions, queries, gold, clusters)
