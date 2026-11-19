"""The H1 arms and their scoring (EXPERIMENT 4, 5.1, 5.2, 6; BUILD-PLAN Step 20).

Arms (each a ranked list of leaves, then the first 10 distinct SESSIONS, scored against gold session content hashes):

  A0                flat hybrid over leaves, BM25 + kNN, client-side RRF k = 60, window 50 (the direct list)
  A0+LEN            A0 re-ranked by RRF score + beta * ln(session tokens); beta is a parameter, fitted on development
                    at S3 and frozen (`fit_beta`); never fitted here on anything else
  A1                RRF(tree list, direct list), each cut at 50, ties broken by a salted hash of the leaf id (5.1)
  A1-tree-only      the tree list alone (what SHUFFLED-SUMMARIES runs on)
  SHUFFLED          A1-tree-only over a view whose descriptors are permuted among the nodes of the same level

Gold never enters this module's inputs except as the `gold` argument of the scorers; the retrievers see the question
text only. A recall call that failed (typed failure, refusal, budget) scores 0 and is counted (EXPERIMENT 5.2).
The scorer's `broken_*` switches are the variants the tests must turn red.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from living_context.config import Constants
from living_context.ids import decode_pointer
from living_context.model.port import PortRefused
from living_context.model.budget import BudgetExceeded
from living_context.recall.fanout import Recall
from living_context.recall.fusion import K_RRF, WINDOW, rrf, rrf_salted

K = 10
ARMS = ("A0", "A0+LEN", "A1", "A1-tree-only", "SHUFFLED")


@dataclass(frozen=True)
class World:
    """Harness-side knowledge about leaves: which session (content hash) each belongs to and each session's size."""
    leaf_session: Mapping[str, str]
    session_tokens: Mapping[str, int]


@dataclass
class TreeOut:
    ids: List[str]                      # leaf ids, tree order (holder confidence, depth, path)
    ok: bool = True
    error: str = ""
    holder_calls: int = 0
    fallbacks: int = 0


@dataclass
class ArmOut:
    leaves: List[str]                   # merged leaf order (cut at WINDOW)
    sessions: List[str]                 # first K distinct sessions
    failed: bool = False


# ---- ranking helpers --------------------------------------------------------------------------------------------
def distinct_sessions(leaf_ids: Sequence[str], world: World, k: int = K, *, broken_leaf_level: bool = False) -> List[str]:
    """The first k distinct sessions in order. `broken_leaf_level` scores the first k LEAVES (one entry per leaf)."""
    out: List[str] = []
    for lid in leaf_ids:
        s = world.leaf_session.get(lid)
        if s is None:
            continue
        if broken_leaf_level:
            out.append(s)
        elif s not in out:
            out.append(s)
        if len(out) == k:
            break
    return out


def recall_at_k(sessions: Sequence[str], gold: Sequence[str], k: int = K) -> float:
    g = set(gold)
    return len(g & set(sessions[:k])) / len(g) if g else float("nan")


def a0_leaves(direct: Sequence[Tuple[str, float]]) -> List[str]:
    return [lid for lid, _ in direct[:WINDOW]]


def a0_len_leaves(direct: Sequence[Tuple[str, float]], world: World, beta: float) -> List[str]:
    """RRF score + beta * ln(session tokens); ties by score then id so the order is deterministic."""
    def key(item):
        lid, score = item
        toks = max(1, world.session_tokens.get(world.leaf_session.get(lid, ""), 1))
        return (-(score + beta * math.log(toks)), lid)
    return [lid for lid, _ in sorted(direct[:WINDOW], key=key)]


def a1_leaves(tree_ids: Sequence[str], direct_ids: Sequence[str], salt: str) -> List[str]:
    return [i for i, _ in rrf_salted([list(tree_ids[:WINDOW]), list(direct_ids[:WINDOW])], salt, K_RRF, WINDOW)]


def fit_beta(rows: Sequence[Tuple[Sequence[Tuple[str, float]], Sequence[str]]], world: World,
             grid: Sequence[float] = (0.0, 0.0005, 0.001, 0.002, 0.004, 0.008, 0.016)) -> float:
    """Pick beta from a declared grid by mean development recall@10 (ties go to the smaller beta). `rows` are
    (A0 scored list, gold sessions) of DEVELOPMENT questions. S3 runs this once and freezes the value (3.7)."""
    best, best_r = grid[0], -1.0
    for b in grid:
        rs = [recall_at_k(distinct_sessions(a0_len_leaves(d, world, b), world), g) for d, g in rows if g]
        r = sum(rs) / len(rs) if rs else 0.0
        if r > best_r + 1e-12:
            best, best_r = b, r
    return best


# ---- one question -----------------------------------------------------------------------------------------------
Direct = Callable[[str], Sequence[Tuple[str, float]]]
Tree = Callable[[str], TreeOut]


def run_question(query: str, direct: Direct, tree: Tree, world: World, *, beta: float, salt: str = "lc",
                 shuffled_tree: Optional[Tree] = None, broken_leaf_level: bool = False) -> Dict[str, ArmOut]:
    """All H1 arms for one question. The retrievers get the query text only."""
    d = list(direct(query))[:WINDOW]
    out: Dict[str, ArmOut] = {}
    ds = lambda ids: distinct_sessions(ids, world, K, broken_leaf_level=broken_leaf_level)
    a0 = a0_leaves(d)
    out["A0"] = ArmOut(a0, ds(a0))
    al = a0_len_leaves(d, world, beta)
    out["A0+LEN"] = ArmOut(al, ds(al))
    t = tree(query)
    if t.ok:
        out["A1-tree-only"] = ArmOut(t.ids[:WINDOW], ds(t.ids[:WINDOW]))
        m = a1_leaves(t.ids, a0, salt)
        out["A1"] = ArmOut(m, ds(m))
    else:
        out["A1-tree-only"] = ArmOut([], [], failed=True)
        out["A1"] = ArmOut([], [], failed=True)
    if shuffled_tree is not None:
        s = shuffled_tree(query)
        out["SHUFFLED"] = ArmOut(s.ids[:WINDOW], ds(s.ids[:WINDOW]), failed=not s.ok) if s.ok else ArmOut([], [], failed=True)
    return out


def source_of_gold(a1: ArmOut, tree_ids: Sequence[str], direct_ids: Sequence[str], gold: Sequence[str], world: World) -> Dict[str, str]:
    """For each gold session in A1's top 10: `tree_only` (its leaves are in the tree list and none in the direct list's
    top 50), `direct_only`, or `both`. The linked list does not exist in A1 (EXPERIMENT 2.2 (c))."""
    t_sess = {world.leaf_session[i] for i in tree_ids[:WINDOW] if i in world.leaf_session}
    d_sess = {world.leaf_session[i] for i in direct_ids[:WINDOW] if i in world.leaf_session}
    out = {}
    for s in gold:
        if s not in a1.sessions[:K]:
            continue
        out[s] = "both" if (s in t_sess and s in d_sess) else "tree_only" if s in t_sess else "direct_only" if s in d_sess else "neither"
    return out


# ---- the tree retriever -----------------------------------------------------------------------------------------
class TreeRetriever:
    """Runs the real fan-out (`Recall`) over a pinned view through a model port; returns the tree list only. Any typed
    failure of the whole recall (refusal, budget) is a failed recall (scores 0, counted), never a silent empty list."""

    def __init__(self, port, view, tenant: str, cls: str, principal: str, consts: Optional[Constants] = None, *, salt: str = "lc"):
        self.port, self.view, self.tenant, self.cls, self.principal = port, view, tenant, cls, principal
        self.consts, self.salt = consts, salt
        self.failures = 0

    def __call__(self, query: str) -> TreeOut:
        try:
            res = Recall(self.port, self.tenant, self.cls, self.principal, consts=self.consts, salt=self.salt).recall(query, self.view)
        except (PortRefused, BudgetExceeded) as e:
            self.failures += 1
            return TreeOut([], False, type(e).__name__)
        ids = [c.leaf_id for c in res.leaves]
        return TreeOut(ids, True, "", res.counters.holder_calls, res.counters.fallbacks)


# ---- SHUFFLED-SUMMARIES (EXPERIMENT 6) --------------------------------------------------------------------------
DESCRIPTOR_KEYS = ("title", "gist", "entities", "facts")


class PermutedView:
    """Node summaries and child entries permuted among nodes of the same level: for each level, the descriptors
    (title, gist, entities, facts) of every child entry of every node of that level are rotated across that level's
    entries, so a holder reads text that belongs to another branch while ids, kinds and sizes stay as they are.
    A holder's selection therefore follows the shuffled text and lands on the wrong child (the control's reason)."""

    def __init__(self, inner, *, shift: Optional[int] = None):
        self._inner = inner
        self.gen = inner.gen
        self._entries_cache: Dict[str, list] = {}
        kids: Dict[str, list] = {}
        queue, seen = list(inner.top()), set()
        while queue:
            ref = queue.pop()
            if ref in seen:
                continue
            seen.add(ref)
            ents = list(inner.entries(ref))
            self._entries_cache[ref] = ents
            kids[ref] = [f"{e['child_id']}:{e['child_version']}" for e in ents if not e["is_leaf"] and not e.get("pending")]
            queue += kids[ref]
        level: Dict[str, int] = {}

        def lvl(ref: str) -> int:    # level 1 = a node whose children are leaves
            if ref not in level:
                level[ref] = 1 + max((lvl(k) for k in kids[ref]), default=0)
            return level[ref]

        by_level: Dict[int, List[str]] = {}
        for ref in kids:
            by_level.setdefault(lvl(ref), []).append(ref)
        self._perm: Dict[str, list] = {}
        for _level, refs in by_level.items():
            refs = sorted(refs)
            flat = [(r, i) for r in refs for i in range(len(self._entries_cache[r]))
                    if not self._entries_cache[r][i].get("pending")]
            descs = [{k: self._entries_cache[r][i].get(k) for k in DESCRIPTOR_KEYS} for r, i in flat]
            n = len(descs)
            s = (shift if shift is not None else max(1, n // 2)) % n if n else 0
            rotated = descs[s:] + descs[:s]
            for (r, i), d in zip(flat, rotated):
                self._perm.setdefault(r, {})[i] = d

    def top(self) -> list:
        return self._inner.top()

    def entries(self, ref: str) -> list:
        out = []
        for i, e in enumerate(self._entries_cache.get(ref) or self._inner.entries(ref)):
            d = self._perm.get(ref, {}).get(i)
            out.append(dict(e, **d) if d else dict(e))
        return out
