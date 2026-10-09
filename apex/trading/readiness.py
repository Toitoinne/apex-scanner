"""Aptitude au trading réel (cœur pur, testé).

Le bot ne trade qu'après avoir PROUVÉ sa rentabilité en paper trading, s'arrête s'il régresse
(tout en continuant d'apprendre), et reprend quand il a de nouveau fait ses preuves.

États :
  APPRENTISSAGE → PRET (critères atteints : alerte, en attente de l'activation par l'utilisateur)
  PRET/ACTIF    → SUSPENDU (régression : arrêt du trading, l'apprentissage continue)
  SUSPENDU      → PRET / ACTIF (critères de reprise atteints sur les positions récentes)
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Sequence

LEARNING, READY, LIVE, SUSPENDED = "APPRENTISSAGE", "PRET", "ACTIF", "SUSPENDU"


@dataclass(frozen=True)
class Closed:
    t: float          # date de clôture (epoch)
    pnl: float        # PnL en fraction de la mise (0,5 = +50 %)


@dataclass
class Stats:
    n: int = 0
    days: float = 0.0
    total_return: float = 0.0        # somme des PnL / nombre de positions (rendement moyen par mise)
    profit_factor: float = 0.0       # gains bruts / pertes brutes
    win_rate: float = 0.0
    max_drawdown_stakes: float = 0.0 # pire repli de la courbe cumulée, en nombre de mises
    positive_days_ratio: float = 0.0
    recent_return: float = 0.0       # rendement moyen des 30 dernières positions

    def to_dict(self) -> dict:
        return {k: round(v, 4) if isinstance(v, float) else v for k, v in self.__dict__.items()}


def compute_stats(closed: Sequence[Closed], recent_n: int = 30) -> Stats:
    s = Stats()
    if not closed:
        return s
    xs = sorted(closed, key=lambda c: c.t)
    s.n = len(xs)
    s.days = (xs[-1].t - xs[0].t) / 86400
    pnls = [c.pnl for c in xs]
    s.total_return = sum(pnls) / s.n
    gains = sum(p for p in pnls if p > 0)
    losses = -sum(p for p in pnls if p < 0)
    s.profit_factor = gains / losses if losses > 0 else (float("inf") if gains > 0 else 0.0)
    s.win_rate = sum(p > 0 for p in pnls) / s.n
    eq = peak = dd = 0.0
    for p in pnls:
        eq += p
        peak = max(peak, eq)
        dd = max(dd, peak - eq)
    s.max_drawdown_stakes = dd
    by_day: dict[int, list[float]] = {}
    for c in xs:
        by_day.setdefault(int(c.t // 86400), []).append(c.pnl)
    days = [sum(v) for v in by_day.values() if len(v) >= 3]
    s.positive_days_ratio = (sum(d > 0 for d in days) / len(days)) if days else 0.0
    recent = pnls[-recent_n:]
    s.recent_return = sum(recent) / len(recent)
    return s


@dataclass
class Criteria:
    min_positions: int = 100
    min_days: float = 7.0
    min_return: float = 0.10            # +10 % par mise en moyenne, frais et slippage compris
    min_profit_factor: float = 1.3
    min_positive_days: float = 0.55
    max_drawdown_stakes: float = 15.0
    min_recent_return: float = 0.0
    # régression (suspension)
    suspend_recent_return: float = -0.15
    suspend_drawdown_stakes: float = 10.0
    # reprise après suspension : à prouver sur les positions postérieures à la suspension
    resume_positions: int = 50

    @classmethod
    def from_cfg(cls, c: dict | None) -> "Criteria":
        return cls(**{k: v for k, v in (c or {}).items() if k in cls.__dataclass_fields__})


@dataclass
class Verdict:
    ready: bool
    regress: bool
    checks: dict[str, tuple[bool, str]] = field(default_factory=dict)

    def failed(self) -> list[str]:
        return [txt for ok, txt in self.checks.values() if not ok]


def evaluate(s: Stats, c: Criteria, health_ok: bool = True, recent_drawdown: float = 0.0) -> Verdict:
    checks = {
        "positions": (s.n >= c.min_positions, f"{s.n}/{c.min_positions} positions clôturées"),
        "duree": (s.days >= c.min_days, f"{s.days:.1f}/{c.min_days:g} jours de recul"),
        "rendement": (s.total_return >= c.min_return, f"rendement moyen {s.total_return:+.1%} (cible ≥ {c.min_return:+.0%})"),
        "profit_factor": (s.profit_factor >= c.min_profit_factor, f"profit factor {s.profit_factor:.2f} (cible ≥ {c.min_profit_factor:g})"),
        "jours_positifs": (s.positive_days_ratio >= c.min_positive_days, f"{s.positive_days_ratio:.0%} de jours gagnants (cible ≥ {c.min_positive_days:.0%})"),
        "drawdown": (s.max_drawdown_stakes <= c.max_drawdown_stakes, f"pire repli {s.max_drawdown_stakes:.1f} mises (max {c.max_drawdown_stakes:g})"),
        "recent": (s.recent_return >= c.min_recent_return, f"30 dernières positions {s.recent_return:+.1%}"),
        "sante": (health_ok, "apprentissage et flux de données sains" if health_ok else "apprentissage ou flux en anomalie"),
    }
    ready = all(ok for ok, _ in checks.values())
    regress = (s.n >= 30 and s.recent_return <= c.suspend_recent_return) or recent_drawdown >= c.suspend_drawdown_stakes \
        or not health_ok
    return Verdict(ready=ready, regress=regress, checks=checks)


def real_guard(real: list["Closed"], c: Criteria, min_n: int = 10, last: int = 20) -> tuple[bool, str]:
    """Garde-fou propre aux trades RÉELS : s'ils perdent trop (glissement de prix pire que simulé,
    exécution ratée…), on suspend même si les simulations vont bien — elles pourraient masquer le réel."""
    if len(real) < min_n:
        return False, f"{len(real)} trade(s) réel(s) : pas encore jugé"
    recent = sorted(real, key=lambda r: r.t)[-last:]
    mean = sum(r.pnl for r in recent) / len(recent)
    if mean <= c.suspend_recent_return:
        return True, f"les {len(recent)} derniers trades réels perdent {mean:+.0%} en moyenne"
    return False, f"{len(recent)} derniers trades réels : {mean:+.0%} en moyenne"


def next_state(state: str, all_time: Verdict, since_suspension: Verdict | None, live_enabled: bool) -> str:
    """Transition d'état. `since_suspension` = verdict calculé sur les seules positions postérieures
    à la dernière suspension (la reprise doit être prouvée sur des données récentes)."""
    if state in (READY, LIVE) and all_time.regress:
        return SUSPENDED
    if state == SUSPENDED:
        if since_suspension is not None and since_suspension.ready and not since_suspension.regress:
            return LIVE if live_enabled else READY
        return SUSPENDED
    if state == LEARNING and all_time.ready:
        return READY
    if state == READY and live_enabled:
        return LIVE
    if state == LIVE and not live_enabled:
        return READY
    return state
