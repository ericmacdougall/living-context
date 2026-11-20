"""The read path's entry for one reader (DESIGN 8.1; BUILD-PLAN Step 10): the classes come from the policy decision, the
direct leaf search runs on those classes only, the tree fan-out runs over the pinned view of the class, and the digest is the
fusion of the two. A reader of several classes (Step 16, DESIGN 8.1 and 17: one tree per class, the reader fans out over several roots) gets one
digest per class of its read set, each exactly the digest a reader of that class alone would get, merged into one: the pointers
are fused by reciprocal rank over the per-class rankings, the outlines are kept per class and the pins are reported per class.
Nothing is read outside the read set, and a class is never served from a wider class."""
from __future__ import annotations

from typing import Callable

from ..ids import KIND_LEAF, Pointer, encode_pointer
from ..policy import ClassLattice, Principal
from . import digest as dg
from .direct import DirectSearch
from .fanout import Recall
from .fusion import rrf_salted


class EntryError(Exception):
    pass


def direct_pointers(hits, gen: int) -> list:
    out = []
    for h in hits:
        leaf_id, lv = h.doc_id.rsplit(":", 1)
        out.append(dg.make_pointer(encode_pointer(Pointer(KIND_LEAF, gen, leaf_id, int(lv))), h.title, h.size, "direct", "", 0))
    return out


def _one_class(principal, lattice, prompt, cls, direct, view_for, recall_for, size) -> tuple:
    view = view_for(cls)
    hits = direct.search(principal, lattice, prompt, size=size, classes=[cls])
    return recall_for(cls).recall(prompt, view, direct=direct_pointers(hits, view.gen)).digest, view.gen


def merge_digests(parts: dict, gens: dict, salt: str = "lc") -> dict:
    """One digest from the per-class digests (keys are class names, merged in sorted order, so the result does not depend on the
    order the classes were read in)."""
    order = sorted(parts)
    key = lambda p: p["pointer"]
    by_ptr = {key(p): p for c in order for p in parts[c]["pointers"]}
    fused = rrf_salted([[key(p) for p in parts[c]["pointers"]] for c in order], salt)
    pointers = [by_ptr[i] for i, _ in fused]
    d = {"outline": [o for c in order for o in parts[c]["outline"]],
         "pointers": pointers, "sizes": {"total": sum(p["size"] for p in pointers), "count": len(pointers)},
         "node_pointers": [p for c in order for p in parts[c]["node_pointers"]], "pinned": {"gens": {c: gens[c] for c in order}},
         "sources": {s: [p for c in order for p in parts[c]["sources"][s]] for s in ("tree", "direct", "linked")},
         "counts": {k: sum(parts[c]["counts"][k] for c in order) for k in ("holder_calls", "fallbacks", "dropped_ids")}}
    dg.check_digest(d)
    return d


def recall_digest(principal: Principal, lattice: ClassLattice, prompt: str, *, direct: DirectSearch,
                  view_for: Callable[[str], object], recall_for: Callable[[str], Recall], size: int = 10,
                  broken_all_classes: bool = False) -> dict:
    """`broken_all_classes` is the BROKEN variant of the tests: every class of the lattice is read, not the read set."""
    classes = list(lattice.classes) if broken_all_classes else direct.classes_for(principal, lattice)
    if not classes:
        raise EntryError("the reader has no readable class")
    if len(classes) == 1:
        return _one_class(principal, lattice, prompt, classes[0], direct, view_for, recall_for, size)[0]
    parts, gens = {}, {}
    for cls in sorted(classes):
        parts[cls], gens[cls] = _one_class(principal, lattice, prompt, cls, direct, view_for, recall_for, size)
    return merge_digests(parts, gens)
