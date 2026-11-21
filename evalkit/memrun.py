"""All H1 arms over an in-memory corpus with the stub model: the stub gate of EXPERIMENT S0 for H1 (BUILD-PLAN Step 10, 20).

`run_fixture` builds the real tree (Step 9 builder) over the fixture's leaves, runs the real fan-out (Step 10) and scores
every arm per question. No Elasticsearch and no real model: the port is the stub's, behind the same call interface."""
from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Dict, Optional

from living_context.config import Constants
from living_context.model.budget import Budget
from living_context.model.cache import DictStore
from living_context.model.port import ModelPort
from living_context.model.stub import StubBackend
from living_context.policy import ClassLattice
from living_context.recall.view import MemView
from living_context.structure import build

from . import arms, corpus
from .fixture import Fixture
from .score import question_scores

LAT = ClassLattice(["base"], [])


def run_fixture(fx: Fixture, *, consts: Optional[Constants] = None, beta: float = 0.0, shuffle_shift: Optional[int] = None,
                cap: int = 10 ** 8) -> dict:
    c = consts or Constants()
    mc = corpus.MemCorpus(fx.sessions)
    with tempfile.TemporaryDirectory() as d:
        bport = ModelPort(StubBackend(), Budget(Path(d) / "build", 10 ** 8), LAT, DictStore())
        tree = build.build_tree(mc.leaves, bport, "t", "base", consts=c)
        port = ModelPort(StubBackend(), Budget(Path(d) / "recall", cap), LAT, DictStore())   # the recall counter has its own cap
        view = MemView(tree)
        direct = corpus.MemDirect(mc.leaves)
        tr = arms.TreeRetriever(port, view, "t", "base", "px", c)
        sh = arms.TreeRetriever(port, arms.PermutedView(view, shift=shuffle_shift), "t", "base", "px", c)
        outs: Dict[str, Dict[str, arms.ArmOut]] = {a: {} for a in arms.ARMS}
        raw: Dict[str, dict] = {}
        for q in fx.questions:
            o = arms.run_question(fx.queries[q.qid], direct, tr, mc.world, beta=beta, shuffled_tree=sh)
            for a, v in o.items():
                outs[a][q.qid] = v
            raw[q.qid] = {"direct": [i for i, _ in direct(fx.queries[q.qid])], "tree": tr(fx.queries[q.qid]).ids}
        scores, failed = {}, {}
        for a in arms.ARMS:
            scores[a], failed[a] = question_scores(outs[a], fx.gold, {q.qid for q in fx.questions if q.is_abstention})
    return {"scores": scores, "failed": failed, "outs": outs, "raw": raw, "world": mc.world, "levels": tree.levels,
            "nodes": len(tree.nodes), "leaves": len(mc.leaves), "tree_failures": tr.failures}
