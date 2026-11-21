"""Offline quickstart: toy sessions -> leaves -> a tiny tree -> a recall, with no network and no model.

    python examples/quickstart.py            (from the repository root, or `make example`)

What it exercises (the real code, driven through the deterministic stand-ins the unit tests use):
  * living_context.ingest.segment      segmentation of sessions into leaves
  * living_context.ingest.title        the code-built leaf title
  * living_context.embed.stub          the hashed bag-of-words embedder (NOT the pinned E5 embedder)
  * living_context.structure.build     balanced clustering and the bottom-up tree build
  * living_context.model               ModelPort + StubBackend (a deterministic extractive "model"), budget and cache
  * living_context.recall.fanout       the owner's fan-out recall over an in-memory pinned view (MemView)

What it does NOT exercise: Elasticsearch (no ingest writer, no BM25/kNN direct search, no gateway, no keystore, no erasure),
the real embedder, or any real model. Numbers printed here say the data path runs; they are not benchmark results.
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:          # run as a plain script from any directory
    sys.path.insert(0, str(REPO))

from living_context.config import Constants                      # noqa: E402
from living_context.embed import stub                            # noqa: E402
from living_context.ingest import title                          # noqa: E402
from living_context.ingest.segment import Turn, segment          # noqa: E402
from living_context.model.budget import Budget                   # noqa: E402
from living_context.model.cache import DictStore                 # noqa: E402
from living_context.model.port import ModelPort                  # noqa: E402
from living_context.model.stub import StubBackend                # noqa: E402
from living_context.policy import ClassLattice                   # noqa: E402
from living_context.recall.fanout import Recall                  # noqa: E402
from living_context.recall.view import MemView                   # noqa: E402
from living_context.structure import build                       # noqa: E402

TENANT, CLS, PRINCIPAL = "demo", "base", "alice"

# (session id, [(role, text), ...]); each session is about one topic, so a question has one obvious gold session.
SESSIONS = {
    "garden": [("user", "I planted tomato seedlings along the south fence and watered them every morning."),
               ("assistant", "Tomato plants like six hours of sun and deep watering, mulch keeps the soil moist."),
               ("user", "The basil next to the tomatoes grew quickly in the sunny garden bed.")],
    "kayak": [("user", "We paddled the kayak across the glacier lake and spotted an osprey."),
              ("assistant", "A kayak paddle stroke should be driven by the torso, keep the blade vertical in the water."),
              ("user", "The lake was calm so the kayak trip along the shore took three hours.")],
    "tax": [("user", "My accountant said the quarterly tax estimate is due in January."),
            ("assistant", "Quarterly estimated tax payments cover income not subject to withholding."),
            ("user", "I filed the tax form early and kept the receipts for the accountant.")],
    "piano": [("user", "I practised the piano sonata slowly, hands separately, with a metronome."),
              ("assistant", "For a piano sonata, slow metronome practice builds even rhythm before adding speed."),
              ("user", "The piano teacher suggested scales before the sonata each evening.")],
    "bread": [("user", "My sourdough bread starter bubbled overnight so I baked a loaf."),
              ("assistant", "A sourdough starter needs regular feeding with flour and water for a good loaf."),
              ("user", "The bread crust turned dark after forty minutes in the oven.")],
    "bike": [("user", "I replaced the bicycle chain and adjusted the rear derailleur."),
             ("assistant", "A worn bicycle chain stretches, so replace the chain before the cassette wears."),
             ("user", "The derailleur cable needed tension, then the bicycle shifted smoothly.")],
}

QUESTIONS = [("what did I plant by the fence", "garden"),
             ("when is the quarterly tax estimate due", "tax"),
             ("how long did the kayak trip on the lake take", "kayak"),
             ("which part of the bicycle did I replace", "bike")]


def make_leaves():
    """Segment the sessions and wrap every leaf as a LeafRec; returns (leaves, leaf id -> session id)."""
    leaves, owner = [], {}
    for sid, turns in SESSIONS.items():
        pieces, _ = segment([[Turn(r, t) for r, t in turns]])
        for piece in pieces:
            text = "\n".join(f"{t.role}: {t.content}" for t in piece)
            leaf_id = f"{len(leaves) + 1:032x}"
            leaves.append(build.LeafRec(leaf_id, 1, text, title.build_title(text), stub.embed(text), max(1, len(text) // 4)))
            owner[leaf_id] = sid
    return leaves, owner


def flat_rank(question, leaves):
    """The offline stand-in for arm A0: cosine of the hashed embeddings over every leaf (no Elasticsearch, no BM25)."""
    qv = stub.embed(question)
    return [l.leaf_id for l in sorted(leaves, key=lambda l: (-stub.cosine(qv, list(l.embedding)), l.leaf_id))]


def first_sessions(leaf_ids, owner, k=3):
    out = []
    for lid in leaf_ids:
        if owner[lid] not in out:
            out.append(owner[lid])
        if len(out) == k:
            break
    return out


def main() -> int:
    leaves, owner = make_leaves()
    print(f"{len(SESSIONS)} sessions -> {len(leaves)} leaves")
    # A small fan-out so a handful of leaves still makes a tree of more than one node (the product defaults are 16/24/4).
    consts = Constants(f_target=4, f_max=6, f_min=2)
    lattice = ClassLattice([CLS], [])
    with tempfile.TemporaryDirectory() as work:
        def port(name):
            return ModelPort(StubBackend(), Budget(Path(work) / name, 1_000_000), lattice, DictStore())

        tree = build.build_tree(leaves, port("build"), TENANT, CLS, consts=consts)
        print(f"tree: {len(tree.nodes)} nodes, {tree.levels} level(s), {tree.calls} build calls (stub model)")
        view = MemView(tree)
        hits_tree = hits_flat = 0
        for question, gold in QUESTIONS:
            res = Recall(port("q"), TENANT, CLS, PRINCIPAL, consts=consts, workers=1).recall(question, view)
            tree_sessions = first_sessions([c.leaf_id for c in res.leaves], owner)
            flat_sessions = first_sessions(flat_rank(question, leaves), owner)
            hits_tree += gold in tree_sessions
            hits_flat += gold in flat_sessions
            print(f"\nQ: {question}\n   gold: {gold}\n   tree recall -> {tree_sessions}   "
                  f"(holder calls {res.counters.holder_calls}, fallbacks {res.counters.fallbacks})\n   flat cosine -> {flat_sessions}")
        print(f"\ngold session in the first 3 sessions: tree {hits_tree}/{len(QUESTIONS)}, flat {hits_flat}/{len(QUESTIONS)}")
        print("(toy data, stub model and stub embedder: a smoke of the data path, not a result)")
    return 0 if hits_tree and hits_flat else 1


if __name__ == "__main__":
    sys.exit(main())
