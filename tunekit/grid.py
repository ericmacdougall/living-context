"""Grid cells: one cell is (growth mode, complexity level, size, seed, optional knob overrides). Names are stable file stems."""
from __future__ import annotations

import gzip
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Tuple

import numpy as np

from . import synth


SHORT = {"paraphrase": "para", "topic_overlap": "ovl", "entity_reuse": "reuse", "near_dup": "dup", "update_rate": "upd",
         "alias_rate": "alias", "topic_size": "tsize", "session_len": "slen", "one_word": "oneword"}
LONG = {v: k for k, v in SHORT.items()}


@dataclass(frozen=True)
class Cell:
    n: int
    seed: int
    level: float = 0.5
    growth: str = "wide"
    over: Tuple[Tuple[str, float], ...] = ()
    n_questions: int = 400

    @property
    def name(self) -> str:
        o = "".join(f"_{SHORT[k]}{v:g}" for k, v in self.over)
        return f"{self.growth}_c{self.level:g}_n{self.n}_s{self.seed}{o}"

    def knobs(self) -> synth.Knobs:
        return synth.knobs_at(self.n, self.seed, self.level, growth=self.growth, n_questions=self.n_questions, **dict(self.over))

    @staticmethod
    def parse(name: str) -> "Cell":
        parts = name.split("_")
        growth, level, n, seed = parts[0], float(parts[1][1:]), int(parts[2][1:]), int(parts[3][1:])
        over = []
        for p in parts[4:]:
            k = "".join(ch for ch in p if not (ch.isdigit() or ch in ".-"))
            over.append((LONG[k], float(p[len(k):])))
        return Cell(n=n, seed=seed, level=level, growth=growth, over=tuple(over))


def save_corpus(c: synth.Corpus, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(path, "wt") as f:
        json.dump({"knobs": c.manifest["knobs"], "leaves": c.leaves, "questions": c.questions, "topics": c.topics,
                   "manifest": c.manifest}, f)


def load_corpus(path: Path) -> synth.Corpus:
    with gzip.open(path, "rt") as f:
        d = json.load(f)
    return synth.Corpus(synth.Knobs(**d["knobs"]), d["leaves"], d["questions"], d["topics"], d["manifest"])


def save_vectors(path: Path, passages: np.ndarray, queries: np.ndarray) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(path, p=passages.astype(np.float32), q=queries.astype(np.float32))


def load_vectors(path: Path):
    z = np.load(path)
    return z["p"].astype(np.float64), z["q"].astype(np.float64)


SIZES = (64, 256, 1000, 4000, 20000)
LEVELS = (0.0, 0.5, 1.0)
KNOB_HIGH = {"topic_overlap": 0.8, "entity_reuse": 5.0, "near_dup": 0.4, "update_rate": 0.4, "alias_rate": 0.6, "one_word": 1.0}


def main_grid() -> List[Cell]:
    """The grid pre-stated in docs/tuning/RESULTS-Z2.md section 2, smallest cells first."""
    cells: List[Cell] = []
    for n in SIZES:
        seeds = (1, 2, 3) if n <= 4000 else (1, 2)
        for level in LEVELS:
            cells += [Cell(n, s, level) for s in seeds]
        seeds_deep = (1, 2) if n <= 4000 else (1,)
        cells += [Cell(n, s, 0.5, growth="deep") for s in seeds_deep]
    for s in (1, 2):
        for k, v in KNOB_HIGH.items():
            cells.append(Cell(4000, s, 0.5, over=((k, v),)))
        for p in (0.0, 0.5, 1.0):
            cells.append(Cell(4000, s, 0.5, over=(("paraphrase", p),)))
    seen, out = set(), []
    for c in cells:
        if c.name not in seen:
            seen.add(c.name)
            out.append(c)
    return out
