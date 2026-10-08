"""Gestionnaire de risque (cœur pur, testé). S'applique aussi en simulation, pour que les
résultats simulés reflètent exactement ce que le trading réel aurait fait."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Limits:
    sol_per_trade: float = 0.1
    max_open_positions: int = 3
    daily_loss_limit_sol: float = 0.3
    max_trades_per_day: int = 20
    min_wallet_reserve_sol: float = 0.05    # frais réseau des ventes : jamais tout engager

    @classmethod
    def from_cfg(cls, c: dict | None) -> "Limits":
        return cls(**{k: v for k, v in (c or {}).items() if k in cls.__dataclass_fields__})


@dataclass(frozen=True)
class Decision:
    ok: bool
    reason: str = ""


def can_open(lim: Limits, *, killed: bool, open_positions: int, realized_today_sol: float,
             trades_today: int, wallet_sol: float | None = None) -> Decision:
    if killed:
        return Decision(False, "arrêt d'urgence actif (/stop)")
    if open_positions >= lim.max_open_positions:
        return Decision(False, f"{open_positions} positions ouvertes (max {lim.max_open_positions})")
    if realized_today_sol <= -lim.daily_loss_limit_sol:
        return Decision(False, f"perte du jour {realized_today_sol:.3f} SOL ≥ limite {lim.daily_loss_limit_sol} SOL")
    if trades_today >= lim.max_trades_per_day:
        return Decision(False, f"{trades_today} trades aujourd'hui (max {lim.max_trades_per_day})")
    if wallet_sol is not None and wallet_sol < lim.sol_per_trade + lim.min_wallet_reserve_sol:
        return Decision(False, f"solde du portefeuille insuffisant ({wallet_sol:.3f} SOL)")
    return Decision(True)
