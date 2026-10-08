"""Calcul des labels multi-horizons et des statistiques de résultat. Fonctions pures.

Convention : le chemin de prix est la liste (ts, prix_spot) des trades
STRICTEMENT postérieurs au moment de la décision. Le prix d'entrée est le prix
effectivement accessible (slippage + frais inclus) pour la taille de référence.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

PricePath = Sequence[tuple[float, float]]


@dataclass(frozen=True)
class Outcome:
    y: int
    max_return: float
    max_drawdown: float
    time_to_peak_s: float
    final_return: float
    rug: bool


def label_hit(path: PricePath, t_dec: float, entry: float, up: float, down: float, horizon_s: float) -> int:
    """1 si le prix atteint entry×(1+up) dans l'horizon SANS avoir touché entry×(1-down) avant."""
    if entry <= 0:
        return 0
    target, stop = entry * (1 + up), entry * (1 - down)
    end = t_dec + horizon_s
    for t, p in path:
        if t <= t_dec:
            continue
        if t > end:
            break
        if p <= stop:
            return 0
        if p >= target:
            return 1
    return 0


def outcome(
    path: PricePath, t_dec: float, entry: float, spot: float, up: float, down: float,
    horizon_s: float, rug_drop: float = 0.9, rug_window_s: float = 600,
) -> Outcome:
    y = label_hit(path, t_dec, entry, up, down, horizon_s)
    end = t_dec + horizon_s
    window = [(t, p) for t, p in path if t_dec < t <= end]
    if not window or entry <= 0:
        return Outcome(y, 0.0, 0.0, 0.0, spot / entry - 1 if entry > 0 else 0.0, False)
    prices = [p for _, p in window]
    pmax = max(prices)
    t_peak = next(t for t, p in window if p == pmax)
    pmin = min(prices)
    return Outcome(
        y=y,
        max_return=pmax / entry - 1,
        max_drawdown=max(0.0, 1 - pmin / entry),
        time_to_peak_s=t_peak - t_dec,
        final_return=prices[-1] / entry - 1,
        rug=is_rug(path, t_dec, spot, rug_drop, rug_window_s),
    )


def is_rug(path: PricePath, t_dec: float, spot: float, drop: float = 0.9, window_s: float = 600) -> bool:
    """Rug : chute de `drop` (90 %) depuis le plus haut atteint, en moins de `window_s` après la décision."""
    peak = spot
    for t, p in path:
        if t <= t_dec:
            continue
        if t > t_dec + window_s:
            break
        peak = max(peak, p)
        if peak > 0 and p <= peak * (1 - drop):
            return True
    return False


def simulated_trade(
    path: PricePath, t_dec: float, entry: float, take_profit: float, stop_loss: float,
    horizon_s: float, sell_fee_bps: float = 0.0,
) -> float:
    """PnL relatif d'un trade simulé : achat au prix d'entrée, sortie au TP, au SL ou
    à la fin de l'horizon. Utilisé comme récompense du bandit et comme coût d'erreur."""
    if entry <= 0:
        return 0.0
    tp, sl = entry * (1 + take_profit), entry * (1 - stop_loss)
    exit_price = None
    last = None
    for t, p in path:
        if t <= t_dec:
            continue
        if t > t_dec + horizon_s:
            break
        last = p
        if p >= tp:
            exit_price = tp
            break
        if p <= sl:
            exit_price = p        # le stop est exécuté au prix observé (gap possible)
            break
    if exit_price is None:
        exit_price = last if last is not None else entry
    return exit_price / entry * (1 - sell_fee_bps / 10_000) - 1
