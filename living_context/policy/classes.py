"""Access classes: a small declared lattice per tenant (DESIGN 4.2).

`dominates(a, b)` is the declared partial order, reflexive and transitive.
Example: `base` < `restricted`; `hr` incomparable with `restricted`, both
above `base`. The class of anything derived is the join over its inputs
(INV-3): the least declared class that dominates all of them. If there is no
declared class that dominates them, or no single least one, the thing is not
built (fail closed).
"""
from __future__ import annotations

import re
from itertools import product
from typing import Iterable, Mapping

NAME = re.compile(r"^[a-z0-9]{4,16}$")  # DESIGN 6.2: no hyphen, no prefix tricks


class ClassError(ValueError):
    pass


class NoJoin(ClassError):
    """No declared class is the least upper bound of the inputs: do not build."""


class ClassLattice:
    def __init__(self, classes: Iterable[str], dominates: Iterable[tuple[str, str]] = (),
                 max_classes: int = 4):
        """`dominates` holds pairs (higher, lower): `higher` dominates `lower`."""
        self.classes = tuple(sorted(set(classes)))
        if not self.classes:
            raise ClassError("a tenant declares at least one class")
        if len(self.classes) > max_classes:
            raise ClassError("too many classes")
        for c in self.classes:
            if not isinstance(c, str) or not NAME.match(c):
                raise ClassError("class names are lowercase [a-z0-9]{4,16}")
        order = {c: {c} for c in self.classes}  # c -> set of classes c dominates
        for hi, lo in dominates:
            if hi not in order or lo not in order:
                raise ClassError("order names an undeclared class")
            order[hi].add(lo)
        changed = True
        while changed:  # transitive closure
            changed = False
            for c in self.classes:
                new = set().union(*(order[d] for d in order[c]))
                if not new <= order[c]:
                    order[c] |= new
                    changed = True
        for a, b in product(self.classes, repeat=2):
            if a != b and b in order[a] and a in order[b]:
                raise ClassError("the order has a cycle")
        self._dom: Mapping[str, frozenset[str]] = {c: frozenset(v) for c, v in order.items()}

    def declared(self, c: str) -> bool:
        return c in self._dom

    def dominates(self, a: str, b: str) -> bool:
        if a not in self._dom or b not in self._dom:
            return False
        return b in self._dom[a]

    def down_closure(self, classes: Iterable[str]) -> frozenset[str]:
        out: set[str] = set()
        for c in classes:
            if c not in self._dom:
                raise ClassError("undeclared class")
            out |= self._dom[c]
        return frozenset(out)

    def is_down_closed(self, classes: Iterable[str]) -> bool:
        s = frozenset(classes)
        return all(c in self._dom for c in s) and self.down_closure(s) == s

    def join(self, *inputs: str) -> str:
        """Least declared class that dominates every input; NoJoin if none."""
        if not inputs:
            raise ClassError("the join of nothing is undefined")
        for c in inputs:
            if c not in self._dom:
                raise NoJoin("input class is not declared")
        uppers = [u for u in self.classes if all(self.dominates(u, c) for c in inputs)]
        least = [u for u in uppers if all(self.dominates(v, u) for v in uppers)]
        if len(least) != 1:
            raise NoJoin("no least declared class dominates the inputs")
        return least[0]
