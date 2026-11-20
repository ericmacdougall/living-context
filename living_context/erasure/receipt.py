"""Erasure receipts (DESIGN 14.3 step 7, 14.7; BUILD-PLAN Step 14).

The full receipt (verifier and tenant administrator) holds counts by kind and class (no id lists), the residue of the closure from a
RECOUNT of the tenant's indices by lineage (must be 0 for `done`), the verifier's step list and probe count, the key and log
retention bounds, the request-to-boundary time against `B_ERASE` and the delay if a hold applied. The requester's copy holds only
totals over the requester's own read set: no per-class count, no class name and no size outside it (review 3, F19b).
`done` needs a residue of zero from the recount; the BROKEN variant `trust_closure` says done from the closure's own numbers."""
from __future__ import annotations

from typing import Callable, Iterable, Optional

from ..config import Constants
from ..gateway.es import EsClient
from . import scan
from .executor import KEEP_KINDS, REWRITE_KINDS, Receipt

B_ERASE_S = 60.0


def recount(es: EsClient, tenant: str, subject: str, leaf_ids: Iterable[str]) -> int:
    """Documents that still name the subject or an erased leaf id (the erased node placeholders and bookkeeping kinds aside)."""
    return len([h for h in scan.find(es, tenant, subjects=[subject], leaf_ids=set(leaf_ids))
                if h.kind not in KEEP_KINDS | REWRITE_KINDS])


def full_receipt(rc: Receipt, *, residue: int, verifier_steps: list, consts: Optional[Constants] = None, hold_delay_s: Optional[float] = None,
                 trust_closure: bool = False) -> dict:
    c = consts or Constants()
    if trust_closure:      # BROKEN variant: the receipt says what the executor did, not what is left (no recount)
        residue = 0
    by_class: dict = {}
    for (kind, cls), n in sorted(rc.by_class.items()):
        by_class.setdefault(cls, {})[kind] = n
    return {"seq": rc.seq, "done": residue == 0 and bool(verifier_steps), "residue": residue,
            "counts_by_class": by_class, "counts_by_kind": dict(sorted(rc.closure.items())),
            "keys_destroyed": rc.keys_destroyed, "boundary_s": round(rc.boundary_s, 3), "b_erase_s": B_ERASE_S,
            "within_b_erase": rc.boundary_s <= B_ERASE_S, "purge_ok": rc.purge_ok,
            "verifier": {"steps": [list(s) for s in verifier_steps], "probes": sum(s[1] for s in verifier_steps)},
            "retention": {"r_keys_days": c.r_keys_days, "r_logs_days": c.r_logs_days}, "hold_delay_s": rc.hold_delay_s if hold_delay_s is None else hold_delay_s}


def requester_copy(full: dict, read_set: Iterable[str], *, broken_per_class: bool = False) -> dict:
    """Totals over the requester's read set only. The BROKEN variant also hands over the per-class counts (review 3, F19b)."""
    allowed = set(read_set)
    total = sum(n for cls, kinds in full["counts_by_class"].items() if cls in allowed for n in kinds.values())
    out = {"seq": full["seq"], "done": full["done"], "erased_in_your_read_set": total}
    if broken_per_class:
        out["counts_by_class"] = full["counts_by_class"]
    return out
