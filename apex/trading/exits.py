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

import math
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
    learned_exit: bool = False                        # vend quand le modèle de sortie juge la hausse finie
    hold_threshold: float = 0.35                      # … c.-à-d. quand P(nouvelle hausse) < ce seuil
    min_hold_s: float = 30                            # jamais avant ce délai après l'achat
    time_stop_s: float = 0                            # sortie « au temps » : à ce délai, si pas assez monté…
    time_stop_mult: float = 0                         # … (moins de +X % et aucun palier encaissé) → on sort
    breakeven_at: float = 0                           # dès ce multiple atteint, le stop remonte au prix d'entrée

    @classmethod
    def from_cfg(cls, name: str, c: dict) -> "Policy":
        return cls(name=name, stop_loss=c["stop_loss"], take_profits=tuple(tuple(x) for x in c.get("take_profits", [])),
                   trail=c.get("trail"), trail_activate=c.get("trail_activate", 1.0e9),
                   danger_exit=c.get("danger_exit", True), time_limit_s=c.get("time_limit_s", 86400),
                   description=c.get("description", ""), learned_exit=c.get("learned_exit", False),
                   hold_threshold=c.get("hold_threshold", 0.35), min_hold_s=c.get("min_hold_s", 30),
                   time_stop_s=c.get("time_stop_s", 0), time_stop_mult=c.get("time_stop_mult", 0),
                   breakeven_at=c.get("breakeven_at", 0))


@dataclass
class Action:
    kind: str            # PALIER | STOP | STOP_SUIVEUR | DANGER | TEMPS | APPRIS
    t: float
    price: float
    fraction: float      # fraction de la position initiale vendue
    multiple: float      # prix / prix d'entrée
    pnl_after: float     # PnL de la position (réalisé + latent) après l'action
    closed: bool
    reason: str = ""


@dataclass(slots=True)
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
    sale_cost: float = 0.0            # coût fixe par vente (frais de réseau), en fraction de la mise initiale
    time_checked: bool = False

    def pnl(self) -> float:
        """PnL réalisé + latent (marqué au dernier prix), en fraction de la mise."""
        latent = self.remaining * (self.last_price / self.entry) * (1 - self.fee) if self.entry > 0 else 0.0
        return self.realized + latent - 1.0

    def _sell(self, fraction: float, price: float) -> None:
        fraction = min(fraction, self.remaining)
        self.realized += fraction * (price / self.entry) * (1 - self.fee) - self.sale_cost
        self.remaining -= fraction
        if self.remaining <= 1e-9:
            self.remaining = 0.0
            self.closed = True

    def on_price(self, p: Policy, t: float, price: float, danger: str | None = None,
                 hold_p: float | None = None) -> list[Action]:
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
        elif p.breakeven_at and self.peak / self.entry >= p.breakeven_at and mult <= 1.0:
            act("STOP", self.remaining, "retour au prix d'entrée")
        elif mult <= 1 - p.stop_loss:
            act("STOP", self.remaining)
        if (not self.closed and p.time_stop_s and not self.time_checked and t - self.t0 >= p.time_stop_s):
            self.time_checked = True
            if self.tp_idx == 0 and mult < 1 + p.time_stop_mult:
                act("TEMPS", self.remaining, f"moins de +{p.time_stop_mult:.0%} après {p.time_stop_s / 60:g} min")
        if (not self.closed and p.learned_exit and hold_p is not None and t - self.t0 >= p.min_hold_s
                and hold_p < p.hold_threshold):
            act("APPRIS", self.remaining, f"chances de nouvelle hausse {hold_p:.0%}")
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


def build_panel(ex: dict, evolved: dict[str, dict] | None = None) -> dict[str, dict]:
    """Toutes les stratégies de sortie testées : celles écrites dans la config + un PANEL généré
    (exits.panel) qui couvre les grandes familles. Chacune est simulée sur chaque décision alertable ;
    le bandit garde celles qui rapportent vraiment."""
    pols = dict(ex.get("policies", {}))
    pan = ex.get("panel") or {}
    if not pan.get("enabled"):
        return {**pols, **(evolved or {})}

    def add(name: str, c: dict) -> None:
        pols.setdefault(name, c)

    fast = pan.get("fast_time_limit_s", 7200)
    for tp in pan.get("tp", [1.3, 1.5, 2.0, 3.0]):                       # 1. tout vendre à un objectif
        for sl in pan.get("sl", [0.2, 0.35, 0.5]):
            add(f"OBJECTIF_X{tp:g}_STOP{int(round(sl * 100))}",
                {"stop_loss": sl, "take_profits": [[tp, 1.0]], "time_limit_s": fast,
                 "description": f"tout vendre à x{tp:g}, stop −{sl:.0%}"})
    for ts in pan.get("time_stop_s", [120, 300, 900]):                     # 2. sortie au temps
        for need in pan.get("time_stop_need", [0.1, 0.25]):
            add(f"TEMPS_{ts // 60}MIN_{int(round(need * 100))}",
                {"stop_loss": 0.35, "take_profits": [[2.0, 0.5]], "trail": 0.3, "trail_activate": 1.5,
                 "time_stop_s": ts, "time_stop_mult": need, "time_limit_s": 86400,
                 "description": f"sortir s'il n'a pas pris +{need:.0%} après {ts // 60} min ; "
                                f"sinon moitié à x2 et stop suiveur −30 %"})
    for tr in pan.get("trail", [0.15, 0.25, 0.35, 0.5]):                  # 3. stop suiveur pur
        for actv in pan.get("trail_activate", [1.0, 1.5]):
            add(f"SUIVEUR{int(round(tr * 100))}_DES_X{actv:g}",
                {"stop_loss": 0.35, "take_profits": [], "trail": tr, "trail_activate": actv, "time_limit_s": 86400,
                 "description": f"stop suiveur −{tr:.0%} dès x{actv:g} (stop −35 % avant)"})
    for be in pan.get("breakeven_at", [1.3, 1.5]):                         # 4. sécuriser la mise
        add(f"SECURISE_X{be:g}",
            {"stop_loss": 0.35, "take_profits": [[3.0, 1.0]], "trail": 0.4, "trail_activate": 2.0, "breakeven_at": be,
             "time_limit_s": 86400,
             "description": f"stop remonté au prix d'entrée dès x{be:g}, tout vendre à x3, stop suiveur −40 % après x2"})
    for name, (tps, tr) in {"PALIERS_RAPIDES": ([[1.5, 0.34], [3.0, 0.33]], 0.4),     # 5. paliers
                            "PALIERS_LARGES": ([[2.0, 0.5], [5.0, 0.25]], 0.5),
                            "MOITIE_X1.3": ([[1.3, 0.5]], 0.25)}.items():
        add(name, {"stop_loss": 0.35, "take_profits": tps, "trail": tr, "trail_activate": tps[0][0],
                   "time_limit_s": 86400,
                   "description": " puis ".join(f"{f:.0%} à x{m:g}" for m, f in tps) + f", reste en stop suiveur −{tr:.0%}"})
    for thr in pan.get("learned_thresholds", [0.25, 0.5]):                 # 6. sortie apprise, plus ou moins prudente
        k = int(round(thr * 100))
        add(f"SORTIE_APPRISE_{k}",
            {"stop_loss": 0.5, "take_profits": [[2.0, 0.5]], "trail": 0.5, "trail_activate": 2.0, "time_limit_s": 86400,
             "learned_exit": True, "hold_threshold": thr, "min_hold_s": 30,
             "description": f"récupérer la mise à x2, puis vendre quand ses chances de hausse passent sous {thr:.0%}"})
        add(f"SORTIE_APPRISE_LIBRE_{k}",
            {"stop_loss": 0.5, "take_profits": [], "trail": 0.5, "trail_activate": 3.0, "time_limit_s": 86400,
             "learned_exit": True, "hold_threshold": thr, "min_hold_s": 30,
             "description": f"tout garder, tout vendre quand ses chances de hausse passent sous {thr:.0%}"})
    for name, c in (evolved or {}).items():                                 # 7. variantes créées par l'évolution
        add(name, c)
    return pols


class DangerDetector:
    """Signaux de danger sur le flux de trades d'un token (cœur pur).
    - DEV_VEND : le créateur vend ≥ 50 % de ce qu'il détient ;
    - GROS_DUMP : une vente ≥ 3 % de la supply ;
    - PANIQUE : sur 30 s, ventes ≥ 3 × achats (en SOL) et prix −25 %."""

    def __init__(self, creator: str, supply: float = 1_000_000_000.0):
        self.creator = creator
        self.supply = supply
        self.dev_balance = 0.0
        self.dev_bought = 0.0
        self.dev_sold = 0.0
        self.window: deque[tuple[float, bool, float, float]] = deque()   # (t, is_buy, sol, price)
        self._buys = 0.0      # sommes glissantes sur la fenêtre (mises à jour à l'entrée et à la sortie)
        self._sells = 0.0
        # détail des ventes récentes (30 s) et volume sur 2 min : signaux qui précèdent un effondrement
        self.sells30: deque[tuple[float, float, str, float]] = deque()  # (t, tokens, vendeur, sol)
        self.vol120: deque[tuple[float, float]] = deque()               # (t, sol)

    def on_trade(self, t: float, trader: str, is_buy: bool, sol: float, tokens: float, price: float) -> str | None:
        signal = None
        if trader == self.creator:
            if is_buy:
                self.dev_balance += tokens
                self.dev_bought += tokens
            else:
                self.dev_sold += tokens
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
            self.sells30.append((t, tokens, trader, sol))
        self.vol120.append((t, sol))
        while self.sells30 and self.sells30[0][0] < t - 30:
            self.sells30.popleft()
        while self.vol120 and self.vol120[0][0] < t - 120:
            self.vol120.popleft()
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


def flow_features(d: "DangerDetector | None", t: float) -> dict[str, float]:
    """Pression vendeuse et essoufflement du volume (pour le modèle de sortie apprise)."""
    if d is None:
        return {}
    sells10 = sum(s for ts, _, _, s in d.sells30 if ts >= t - 10)
    vol10 = sum(s for ts, s in d.vol120 if ts >= t - 10)
    vol30 = sum(s for ts, s in d.vol120 if ts >= t - 30)
    vol_before = sum(s for ts, s in d.vol120 if ts < t - 30)            # 30 → 120 s avant
    return {
        "sell_share10": sells10 / vol10 if vol10 > 0 else 0.5,
        "big_sell30": max((tk for _, tk, _, _ in d.sells30), default=0.0) / d.supply,
        "sellers30": math.log1p(len({w for _, _, w, _ in d.sells30})),
        "dev_sold_frac": d.dev_sold / d.dev_bought if d.dev_bought > 0 else 0.0,
        "vol_accel": math.log((vol30 + 0.01) / (vol_before / 3 + 0.01)),   # > 0 : le volume accélère
    }


DANGER_LABELS = {"DEV_VEND": "le dev vend", "GROS_DUMP": "un gros porteur vide sa position",
                 "PANIQUE": "panique vendeuse (ventes ≫ achats, prix −25 % en 30 s)"}
