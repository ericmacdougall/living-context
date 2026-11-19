"""Bulk tree build for one family of one class (DESIGN 5.4, 7.6; BUILD-PLAN Step 9).

Level 1 groups leaves by balanced clustering to `F_TARGET` and asks the model port (stub only) for child entries and
a summary per group; upper levels group node summaries the same way; the index-of-indexes rule adds levels until the
top has at most `F_MAX` nodes whose summaries fit the holder context `H_CTX`, and drops a single upper node. Sizes
and totals are computed by code from the leaves, never by the model; every title is code-built (INV-7). Pure: no
Elasticsearch here (`publish.py` writes the result).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Optional, Sequence

from ..config import Constants
from ..embed import stub as embed_stub
from ..ids import new_node_id
from ..ingest import segment, title
from ..model.port import CallContext, ModelPort, Task
from . import cluster


class BuildFailed(Exception):
    """A build call failed (timeout, refusal, malformed output): the generation is aborted (DESIGN 7.6)."""


@dataclass(frozen=True)
class LeafRec:
    leaf_id: str
    lv: int
    text: str
    title: str
    embedding: Sequence[float]
    size_tokens: int
    subjects: tuple = ()
    src_seq: int = 0


@dataclass
class Node:
    node_id: str
    nv: int
    level: int
    children: list
    summary: str
    summary_embedding: list
    facts: list
    title: str
    total_tokens: int
    lin_leaf_ids: list
    lin_nodes: list
    lin_subjects: list
    summary_status: str = "ok"

    @property
    def ref(self) -> str:
        return f"{self.node_id}:{self.nv}"

    @property
    def child_count(self) -> int:
        return len(self.children)


@dataclass
class Tree:
    nodes: dict = field(default_factory=dict)      # "id:nv" -> Node
    top: list = field(default_factory=list)        # refs of the top level
    levels: int = 0
    calls: int = 0
    members: list = field(default_factory=list)    # (parent ref, leaf id, lv, score)


def _fail(res) -> None:
    if not res.ok:
        raise BuildFailed(res.error or "build call failed")


def build_tree(leaves: Sequence[LeafRec], port: ModelPort, tenant: str, cls: str, *, consts: Optional[Constants] = None,
               principal: str = "service", index_of_indexes: bool = True, grouper: Callable = cluster.balanced_groups) -> Tree:
    c = consts or Constants()
    tree = Tree()
    if not leaves:
        return tree
    groups = grouper([l.embedding for l in leaves], c.f_target)
    level: list[Node] = []
    for g in groups:
        members = [leaves[i] for i in g]
        ctx = CallContext(tenant, cls, principal, (cls,), lin_leaf_ids=tuple(m.leaf_id for m in members),
                          lin_subjects=tuple(sorted({s for m in members for s in m.subjects})))
        res = port.call(Task.BUILD_L1, {"leaves": [{"text": m.text, "title": m.title} for m in members]}, ctx=ctx)
        tree.calls += 1
        _fail(res)
        out = res.output
        if len(out["entries"]) != len(members):
            raise BuildFailed("entry count")
        children = []
        for m, e in zip(members, out["entries"]):  # local labels c1..cN map back to ids by position, in code
            children.append({"child_id": m.leaf_id, "child_version": m.lv, "is_leaf": True, "title": m.title,
                             "gist": e["gist"], "facts": e["facts"], "entities": e["entities"],
                             "size_tokens": m.size_tokens, "total_tokens": m.size_tokens})
        node = Node(new_node_id(), 1, 1, children, out["summary"], embed_stub.embed(out["summary"]),
                    [f for e in out["entries"] for f in e["facts"]][:12],
                    title.build_title(out["summary"], n=c.title_terms), sum(m.size_tokens for m in members),
                    [m.leaf_id for m in members], [], sorted({s for m in members for s in m.subjects}))
        tree.nodes[node.ref] = node
        tree.members += [(node.ref, m.leaf_id, m.lv, 1.0) for m in members]
        level.append(node)
    depth = 1

    def too_big(nodes: list[Node]) -> bool:
        return len(nodes) > c.f_max or sum(segment.tokens_of(n.summary, c.chars_per_token_bound) for n in nodes) > c.h_ctx

    while index_of_indexes and too_big(level) and len(level) > 1:
        groups = grouper([n.summary_embedding for n in level], c.f_target)
        up: list[Node] = []
        for g in groups:
            kids = [level[i] for i in g]
            ctx = CallContext(tenant, cls, principal, (cls,), lin_nodes=tuple(k.ref for k in kids),
                              lin_subjects=tuple(sorted({s for k in kids for s in k.lin_subjects})))
            res = port.call(Task.BUILD_UPPER, {"children": [{"summary": k.summary, "facts": k.facts, "title": k.title}
                                                            for k in kids]}, ctx=ctx)
            tree.calls += 1
            _fail(res)
            summary = res.output["summary"]
            children = [{"child_id": k.node_id, "child_version": k.nv, "is_leaf": False, "title": k.title,
                         "gist": k.summary, "facts": k.facts[:3], "entities": [], "size_tokens": 0,
                         "total_tokens": k.total_tokens} for k in kids]
            node = Node(new_node_id(), 1, depth + 1, children, summary, embed_stub.embed(summary),
                        [f for k in kids for f in k.facts][:12], title.build_title(summary, n=c.title_terms),
                        sum(k.total_tokens for k in kids), [], [k.ref for k in kids],
                        sorted({s for k in kids for s in k.lin_subjects}))
            tree.nodes[node.ref] = node
            up.append(node)
        level, depth = up, depth + 1
    if index_of_indexes and len(level) == 1 and level[0].level >= 2:  # a single upper node is dropped
        only = level[0]
        level = [tree.nodes[f"{ch['child_id']}:{ch['child_version']}"] for ch in only.children]
        del tree.nodes[only.ref]
        depth -= 1
    tree.top, tree.levels = [n.ref for n in level], depth
    return tree
