"""The separate relevance judge (DESIGN 10; BUILD-PLAN Step 17, stub only).

The judge sees the query and the leaf text and nothing else: never which source offered the item, the holder's reasons or any gold
label (`input_for` builds the whole input and is the only place it is built). It is a different task, and in the stub a different
function, from route and organise. A call that fails leaves the pair UNKNOWN (never "irrelevant"): `label` returns None.
Labels run 0 to 3; "relevant" means label >= 2 (DESIGN 10). The score bands are ASSUMPTION values for the stub's 4-gram Jaccard
scale and are validated before any label drives a decision (EXPERIMENT 8.2)."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Optional

from ..model.port import CallContext, ModelPort, Task

BANDS = (0.10, 0.20, 0.40)   # score thresholds for labels 1, 2, 3 (ASSUMPTION)
INPUT_FIELDS = frozenset({"query", "text"})


@dataclass(frozen=True)
class Label:
    score: float
    label: int

    @property
    def relevant(self) -> bool:
        return self.label >= 2


def band(score: float) -> int:
    return sum(1 for b in BANDS if score >= b)


class Judge:
    def __init__(self, port: ModelPort, tenant: str, cls: str, principal: str):
        self._port, self.tenant, self.cls, self.principal = port, tenant, cls, principal

    @staticmethod
    def input_for(query: str, text: str) -> dict:
        """The whole judge input: the query and the leaf text. No source tag, no reasons, no gold."""
        return {"query": query, "text": text}

    def label(self, query: str, leaf_id: str, text: str, *, lin_subjects: Iterable[str] = (), lin_queries: Iterable[str] = ()) -> Optional[Label]:
        ctx = CallContext(self.tenant, self.cls, self.principal, (self.cls,), lin_leaf_ids=(leaf_id,),
                          lin_subjects=tuple(lin_subjects), lin_queries=tuple(lin_queries))
        res = self._port.call(Task.JUDGE_RELEVANCE, self.input_for(query, text), ctx=ctx)
        if not res.ok or not isinstance(res.output, dict):
            return None
        s = float(res.output["score"])
        return Label(s, band(s))
