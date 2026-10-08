"""Boucle 3 — méta-apprentissage : quelles corrections marchent dans quelle situation.

Table d'efficacité (état, contexte de marché, correction) → succès / échecs / gain.
Le choix de la correction est fait par un bandit contextuel (Thompson sampling
Beta) qui combine les statistiques du contexte exact et, à moindre poids, celles
de l'état tous contextes confondus ; exploration ε d'actions peu essayées.
"""
from __future__ import annotations

import random
from dataclasses import dataclass, field

from .stats import DEGRADATION, IMPROVEMENT, NO_EFFECT


@dataclass
class Cell:
    n: int = 0
    success: int = 0
    fail: int = 0
    gain_sum: float = 0.0

    @property
    def neutral(self) -> int:
        return self.n - self.success - self.fail


@dataclass
class EfficacyTable:
    cells: dict[tuple[str, str, str], Cell] = field(default_factory=dict)
    explore: float = 0.15
    seed: int | None = None

    def __post_init__(self) -> None:
        self.rng = random.Random(self.seed)

    def cell(self, state: str, ctx: str, action: str) -> Cell:
        return self.cells.setdefault((state, ctx, action), Cell())

    def update(self, state: str, ctx: str, action: str, verdict: str, gain: float) -> Cell:
        c = self.cell(state, ctx, action)
        c.n += 1
        c.success += verdict == IMPROVEMENT
        c.fail += verdict == DEGRADATION
        c.gain_sum += gain
        return c

    def _global(self, state: str, action: str) -> Cell:
        g = Cell()
        for (s, _, a), c in self.cells.items():
            if s == state and a == action:
                g.n += c.n
                g.success += c.success
                g.fail += c.fail
                g.gain_sum += c.gain_sum
        return g

    def choose(self, state: str, ctx: str, candidates: list[str]) -> str | None:
        if not candidates:
            return None
        if self.rng.random() < self.explore:
            least = min(candidates, key=lambda a: self.cell(state, ctx, a).n)
            return least
        best, best_v = None, -1.0
        for a in candidates:
            c, g = self.cell(state, ctx, a), self._global(state, a)
            # SANS_EFFET compte pour un demi-échec : une correction inutile a un coût
            alpha = 1 + c.success + 0.5 * g.success
            beta = 1 + c.fail + 0.5 * c.neutral + 0.5 * (g.fail + 0.5 * g.neutral)
            v = self.rng.betavariate(alpha, beta)
            if v > best_v:
                best, best_v = a, v
        return best

    def rows(self) -> list[dict]:
        return [
            {"state": s, "context": ctx, "action": a, "n": c.n, "success": c.success, "fail": c.fail,
             "success_rate": round(c.success / c.n, 3) if c.n else None,
             "mean_gain": round(c.gain_sum / c.n, 4) if c.n else None}
            for (s, ctx, a), c in sorted(self.cells.items())
        ]


def verdict_is_failure(verdict: str) -> bool:
    return verdict in (DEGRADATION, NO_EFFECT)
