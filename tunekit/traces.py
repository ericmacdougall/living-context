"""Traces for the state-machine stages (TUNING Z3, D5 to D7): a trace is a list of ticks, each with the pool's leaf count and the
recall units that arrived in the tick (principal, session, hard). Deterministic from a seed; no model call. The evaluator itself is
product code that does not exist yet (`stages.MachineAdapter` raises ProductCodeMissing); these generators and their invariants are
what it will be run against."""
from __future__ import annotations

import random
from dataclasses import dataclass
from typing import List, Tuple


@dataclass(frozen=True)
class Unit:
    principal: int
    session: int
    hard: bool


@dataclass(frozen=True)
class Tick:
    hour: int
    leaf_count: int
    units: Tuple[Unit, ...]


def _units(rng: random.Random, n: int, hard_rate: float, principals: int, flood_share: float = 0.0) -> Tuple[Unit, ...]:
    out = []
    for _ in range(n):
        p = 0 if rng.random() < flood_share else rng.randrange(1, max(2, principals))
        out.append(Unit(p, p * 1000 + rng.randrange(20), rng.random() < hard_rate))
    return tuple(out)


def growth(seed: int, *, n0=100, n_end=20000, hours=24 * 60, hard_rate=0.3, units_per_hour=4, principals=8) -> List[Tick]:
    """Geometric growth from n0 to n_end, steady demand."""
    rng = random.Random(f"trace|growth|{seed}")
    r = (n_end / n0) ** (1.0 / max(1, hours - 1))
    return [Tick(h, round(n0 * r ** h), _units(rng, units_per_hour, hard_rate, principals)) for h in range(hours)]


def stationary_noise(seed: int, *, n=3000, hours=24 * 30, base_hard=0.10, units_per_hour=4, principals=8) -> List[Tick]:
    """A fixed-size pool whose hard rate wobbles below the gate: any move on it is a false move."""
    rng = random.Random(f"trace|noise|{seed}")
    return [Tick(h, n, _units(rng, units_per_hour, max(0.0, min(0.19, rng.gauss(base_hard, 0.03))), principals)) for h in range(hours)]


def oscillating(seed: int, *, n=3000, hours=24 * 30, period=48, low=0.05, high=0.35, units_per_hour=4, principals=8) -> List[Tick]:
    """Hard rate crossing the gate up and down every `period` hours: tests dwell."""
    rng = random.Random(f"trace|osc|{seed}")
    return [Tick(h, n, _units(rng, units_per_hour, high if (h // period) % 2 else low, principals)) for h in range(hours)]


def burst(seed: int, *, n=3000, hours=24 * 14, at=100, length=6, units_per_hour=4, principals=8) -> List[Tick]:
    """Quiet pool with one short burst of hard recalls."""
    rng = random.Random(f"trace|burst|{seed}")
    return [Tick(h, n, _units(rng, units_per_hour * (10 if at <= h < at + length else 1), 0.9 if at <= h < at + length else 0.05, principals))
            for h in range(hours)]


def bulk_import(seed: int, *, n_end=30000, hours=24 * 7) -> List[Tick]:
    """Everything arrives in one tick and nobody recalls: the memory gate alone must not move the pool before N_BACK."""
    return [Tick(h, n_end if h >= 1 else 0, ()) for h in range(hours)]


def flooding_principal(seed: int, *, n=3000, hours=24 * 14, flood_share=0.9, units_per_hour=20, principals=6) -> List[Tick]:
    """One principal issues most of the (hard) recalls: it must never be decisive on its own (leave-one-principal-out)."""
    rng = random.Random(f"trace|flood|{seed}")
    out = []
    for h in range(hours):
        us = []
        for u in _units(rng, units_per_hour, 0.1, principals, flood_share):
            us.append(Unit(u.principal, u.session, True if u.principal == 0 else u.hard))
        out.append(Tick(h, n, tuple(us)))
    return out


ALL = {"growth": growth, "stationary_noise": stationary_noise, "oscillating": oscillating, "burst": burst,
       "bulk_import": bulk_import, "flooding_principal": flooding_principal}


def hard_rate(ticks: List[Tick], window: int = 200, drop_principal: int = -1) -> float:
    us = [u for t in ticks for u in t.units if u.principal != drop_principal][-window:]
    return sum(u.hard for u in us) / len(us) if us else float("nan")
