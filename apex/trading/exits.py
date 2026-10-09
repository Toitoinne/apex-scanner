"""Stratégies de sortie (cœur pur, testé).

Une même machine à états sert à trois choses :
- simulation sur TOUS les tokens (le système apprend quelle stratégie rapporte le plus) ;
- signaux de vente en direct pour les tokens alertés ;
- paper trading (PnL réaliste, frais et slippage compris).

Une stratégie = un stop initial, des paliers de prise de bénéfice (multiple, fraction de la
position initiale), un stop suiveur (activé après un palier ou un multiple), une sortie sur
danger (dev qui vend, gros porteur qui vide, panique vendeuse) et une limite de temps.
"""
from __future__ import annotations

from collections import deque
from dataclasses import asdict, dataclass
from typing import Any


@dataclass(frozen=True)
class Policy:
    name: str
    stop_loss: float                                  # 0,5 = sortie totale à −50 % (avant tout palier)
    take_profits: tuple[tuple[float, float], ...]     # ((multiple, fraction de la position initiale), …)
    trail: float | None = None                        # 0,4 = sortie du reste à −40 % depuis le plus haut
    trail_activate: float = 1.0e9                     # multiple qui active le stop suiveur (ou 1er palier)
    danger_exit: bool = True
    time_limit_s: float = 86400
    description: str = ""

    @classmethod
    def from_cfg(cls, name: str, c: dict) -> "Policy":
        return cls(name=name, stop_loss=c["stop_loss"], take_profits=tuple(tuple(x) for x in c.get("take_profits", [])),
                   trail=c.get("trail"), trail_activate=c.get("trail_activate", 1.0e9),
                   danger_exit=c.get("danger_exit", True), time_limit_s=c.get("time_limit_s", 86400),
                   description=c.get("description", ""))


@dataclass
class Action:
    kind: str            # PALIER | STOP | STOP_SUIVEUR | DANGER | TEMPS
    t: float
    price: float
    fraction: float      # fraction de la position initiale vendue
    multiple: float      # prix / prix d'entrée
    pnl_after: float     # PnL de la position (réalisé + latent) après l'action
    closed: bool
    reason: str = ""


@dataclass
class PolicyState:
    policy_name: str
    entry: float
    t0: float
    fee: float = 0.02                 # frais + slippage de sortie (par vente)
    remaining: float = 1.0
    realized: float = 0.0             # valeur récupérée (en multiple de la mise)
    peak: float = 0.0
    tp_idx: int = 0
    trailing: bool = False
    closed: bool = False
    last_price: float = 0.0
    last_t: float = 0.0

    def pnl(self) -> float:
        """PnL réalisé + latent (marqué au dernier prix), en fraction de la mise."""
        latent = self.remaining * (self.last_price / self.entry) * (1 - self.fee) if self.entry > 0 else 0.0
        return self.realized + latent - 1.0

    def _sell(self, fraction: float, price: float) -> None:
        fraction = min(fraction, self.remaining)
        self.realized += fraction * (price / self.entry) * (1 - self.fee)
        self.remaining -= fraction
        if self.remaining <= 1e-9:
            self.remaining = 0.0
            self.closed = True

    def on_price(self, p: Policy, t: float, price: float, danger: str | None = None) -> list[Action]:
        if self.closed or price <= 0 or self.entry <= 0:
            return []
        out: list[Action] = []
        self.last_price, self.last_t = price, t
        self.peak = max(self.peak, price)
        mult = price / self.entry

        def act(kind: str, frac: float, reason: str = "") -> None:
            self._sell(frac, price)
            out.append(Action(kind, t, price, frac, mult, self.pnl(), self.closed, reason))

        if danger and p.danger_exit:
            act("DANGER", self.remaining, danger)
            return out
        if t - self.t0 >= p.time_limit_s:
            act("TEMPS", self.remaining)
            return out
        while self.tp_idx < len(p.take_profits) and mult >= p.take_profits[self.tp_idx][0]:
            act("PALIER", p.take_profits[self.tp_idx][1], f"x{p.take_profits[self.tp_idx][0]:g}")
            self.tp_idx += 1
            self.trailing = self.trailing or p.trail is not None
            if self.closed:
                return out
        if p.trail is not None and self.peak / self.entry >= p.trail_activate:
            self.trailing = True
        if self.trailing and p.trail is not None:
            if price <= self.peak * (1 - p.trail):
                act("STOP_SUIVEUR", self.remaining, f"−{p.trail:.0%} depuis x{self.peak / self.entry:.2f}")
        elif mult <= 1 - p.stop_loss:
            act("STOP", self.remaining)
        return out

    def close_at_market(self, p: Policy, t: float) -> list[Action]:
        """Fin de suivi : on solde au dernier prix connu."""
        if self.closed:
            return []
        price = self.last_price or self.entry
        frac = self.remaining
        self._sell(frac, price)
        return [Action("TEMPS", t, price, frac, price / self.entry, self.pnl(), True, "fin de suivi")]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "PolicyState":
        return cls(**d)


def simulate(p: Policy, path: list[tuple[float, float]], entry: float, t0: float, fee: float = 0.02,
             dangers: list[tuple[float, str]] | None = None) -> PolicyState:
    """Rejoue une stratégie sur un chemin de prix (tests, backfill)."""
    st = PolicyState(p.name, entry, t0, fee)
    dz = sorted(dangers or [])
    i = 0
    for t, price in path:
        d = None
        while i < len(dz) and dz[i][0] <= t:
            d = dz[i][1]
            i += 1
        st.on_price(p, t, price, d)
        if st.closed:
            break
    return st


class DangerDetector:
    """Signaux de danger sur le flux de trades d'un token (cœur pur).
    - DEV_VEND : le créateur vend ≥ 50 % de ce qu'il détient ;
    - GROS_DUMP : une vente ≥ 3 % de la supply ;
    - PANIQUE : sur 30 s, ventes ≥ 3 × achats (en SOL) et prix −25 %."""

    def __init__(self, creator: str, supply: float = 1_000_000_000.0):
        self.creator = creator
        self.supply = supply
        self.dev_balance = 0.0
        self.window: deque[tuple[float, bool, float, float]] = deque()   # (t, is_buy, sol, price)
        self._buys = 0.0      # sommes glissantes sur la fenêtre (mises à jour à l'entrée et à la sortie)
        self._sells = 0.0

    def on_trade(self, t: float, trader: str, is_buy: bool, sol: float, tokens: float, price: float) -> str | None:
        signal = None
        if trader == self.creator:
            if is_buy:
                self.dev_balance += tokens
            else:
                if self.dev_balance > 0 and tokens >= 0.5 * self.dev_balance:
                    signal = "DEV_VEND"
                self.dev_balance = max(0.0, self.dev_balance - tokens)
        if signal is None and not is_buy and tokens >= 0.03 * self.supply:
            signal = "GROS_DUMP"
        self.window.append((t, is_buy, sol, price))
        if is_buy:
            self._buys += sol
        else:
            self._sells += sol
        while self.window and self.window[0][0] < t - 30:
            _, b0, s0, _ = self.window.popleft()
            if b0:
                self._buys -= s0
            else:
                self._sells -= s0
        if signal is None and len(self.window) >= 5:
            buys, sells = max(self._buys, 0.0), max(self._sells, 0.0)
            p0 = self.window[0][3]
            if sells >= 3 * max(buys, 0.01) and p0 > 0 and price <= 0.75 * p0:
                signal = "PANIQUE"
        return signal


DANGER_LABELS = {"DEV_VEND": "le dev vend", "GROS_DUMP": "un gros porteur vide sa position",
                 "PANIQUE": "panique vendeuse (ventes ≫ achats, prix −25 % en 30 s)"}
