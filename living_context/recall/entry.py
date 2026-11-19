"""The read path's entry for one reader (DESIGN 8.1; BUILD-PLAN Step 10): the classes come from the policy decision, the
direct leaf search runs on those classes only, the tree fan-out runs over the pinned view of the class, and the digest is the
fusion of the two. A reader of several classes is built in Step 16 (it needs one merged digest over several families); until
then it is refused, never served from a wider class."""
from __future__ import annotations

from typing import Callable

from ..ids import KIND_LEAF, Pointer, encode_pointer
from ..policy import ClassLattice, Principal
from . import digest as dg
from .direct import DirectSearch
from .fanout import Recall


class EntryError(Exception):
    pass


def direct_pointers(hits, gen: int) -> list:
    out = []
    for h in hits:
        leaf_id, lv = h.doc_id.rsplit(":", 1)
        out.append(dg.make_pointer(encode_pointer(Pointer(KIND_LEAF, gen, leaf_id, int(lv))), h.title, h.size, "direct", "", 0))
    return out


def recall_digest(principal: Principal, lattice: ClassLattice, prompt: str, *, direct: DirectSearch,
                  view_for: Callable[[str], object], recall_for: Callable[[str], Recall], size: int = 10) -> dict:
    classes = direct.classes_for(principal, lattice)
    if len(classes) != 1:
        raise EntryError("a read of several classes is built in Step 16")
    cls = classes[0]
    view = view_for(cls)
    hits = direct.search(principal, lattice, prompt, size=size, classes=[cls])
    return recall_for(cls).recall(prompt, view, direct=direct_pointers(hits, view.gen)).digest
