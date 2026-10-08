"""Contexte de marché : température, lancements/heure, migrations/heure, prix SOL,
narratifs qui performent sur la dernière heure."""
from __future__ import annotations

from collections import deque


class MarketContext:
    def __init__(self, narrative_window_s: float = 3600):
        self.launches: deque[float] = deque()
        self.migrations: deque[float] = deque()
        self.pumps: deque[tuple[float, frozenset[str], float]] = deque()   # (ts, mots, multiplicateur)
        self.sol_price_usd: float | None = None
        self.window = narrative_window_s
        self._launch_rate_ema: float | None = None

    def _trim(self, now: float) -> None:
        for dq in (self.launches, self.migrations):
            while dq and dq[0] < now - 3600:
                dq.popleft()
        while self.pumps and self.pumps[0][0] < now - self.window:
            self.pumps.popleft()

    def on_launch(self, ts: float) -> None:
        self.launches.append(ts)

    def on_migration(self, ts: float) -> None:
        self.migrations.append(ts)

    def on_pump(self, ts: float, words: set[str], mult: float) -> None:
        """Un token vient de dépasser un multiplicateur notable : ses mots forment un narratif chaud."""
        if words:
            self.pumps.append((ts, frozenset(words), mult))

    def narrative_score(self, words: set[str], now: float) -> float:
        self._trim(now)
        if not words:
            return 0.0
        score = 0.0
        for ts, w, mult in self.pumps:
            if words & w:
                decay = 1 - (now - ts) / self.window
                score += decay * min(mult, 20) / 20
        return score

    def snapshot(self, now: float) -> dict[str, float]:
        self._trim(now)
        lph = float(len(self.launches))
        mph = float(len(self.migrations))
        self._launch_rate_ema = lph if self._launch_rate_ema is None else 0.999 * self._launch_rate_ema + 0.001 * lph
        # température : taux de migration (succès) × activité relative
        rel_activity = lph / self._launch_rate_ema if self._launch_rate_ema else 1.0
        temperature = (mph / lph * 100 if lph else 0.0) * rel_activity
        return {
            "temperature": temperature, "launches_per_h": lph, "migrations_per_h": mph,
            "sol_price_usd": self.sol_price_usd or 0.0,
        }

    def regime(self, now: float) -> str:
        """Bucket de contexte utilisé par le méta-apprentissage (boucle 3)."""
        t = self.snapshot(now)["temperature"]
        return "hot" if t > 1.5 else "cold" if t < 0.5 else "normal"
