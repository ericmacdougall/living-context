"""The behavioural-use audit (DESIGN 9 `used_behavioural`; BUILD-PLAN Step 11): a sampled counterfactual. The answer step is
re-run with one offered item removed; if the answer changes, the item was used. The sample is drawn from every offered row of
the sampled recalls, opened or not. Sampling rate is an ASSUMPTION. Stub only; no model call beyond the stub port."""
from __future__ import annotations

import random
from typing import Callable, Sequence

from ..model.port import CallContext, ModelPort, Task


def audit(port: ModelPort, tenant: str, cls: str, principal: str, question: str, items: Sequence[dict], *, rate: float,
          rng: random.Random, write: Callable[[dict, bool], object], broken_only_opened: bool = False) -> int:
    """items: [{"row": ..., "text": ..., "opened": bool}]. Returns the number of rows audited."""
    def answer(texts):
        ctx = CallContext(tenant, cls, principal, (cls,))
        res = port.call(Task.READER_ANSWER, {"question": question, "context": list(texts)}, ctx=ctx)
        return res.output["answer"] if res.ok else None

    full = answer([i["text"] for i in items])
    n = 0
    for k, it in enumerate(items):
        if broken_only_opened and not it["opened"]:
            continue                      # BROKEN: an item used only through its descriptor can never be seen
        if rng.random() >= rate:
            continue
        without = answer([j["text"] for m, j in enumerate(items) if m != k])
        write(it["row"], without is not None and without != full)
        n += 1
    return n
