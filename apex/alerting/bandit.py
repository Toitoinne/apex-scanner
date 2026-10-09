"""Politique d'alerte auto-ajustée : bandit Thompson sampling sur
(score, seuil, point de décision, stratégie de sortie).

- score : « x2 » (proba de x2 en 1 h) ou « x10 » (proba de x10 en 24 h) ;
- récompense : PnL RÉEL simulé de la stratégie de sortie (frais et slippage compris) ;
- information complète : comme on prédit et simule TOUS les tokens, chaque résultat met à
  jour tous les bras qui auraient alerté → le système apprend en continu ce qui rapporte ;
- contrainte : nombre d'alertes/jour dans [min, max] (estimé par bras).
"""
from __future__ import annotations

import math
import random
from dataclasses import dataclass, field


@dataclass
class Arm:
    score: str
    thr: float
    point: str
    policy: str
    n: float = 0.0          # effectif actualisé
    s: float = 0.0          # somme des récompenses
    ss: float = 0.0         # somme des carrés
    rate_n: float = 0.0     # décisions qualifiantes (actualisé)
    rate_t: float = 0.0     # durée observée (s, actualisée)

    @property
    def key(self) -> str:
        return f"{self.score}:{self.thr:g}@{self.point}#{self.policy}"

    def mean(self) -> float:
        return self.s / self.n if self.n > 0 else 0.0

    def var(self) -> float:
        if self.n < 2:
            return 1.0
        m = self.mean()
        return max(1e-4, self.ss / self.n - m * m)

    def alerts_per_day(self) -> float:
        return self.rate_n / self.rate_t * 86400 if self.rate_t > 0 else 0.0

    @property
    def slot(self) -> str:
        return f"{self.score}:{self.thr:g}@{self.point}"


@dataclass
class Slot:
    """(score, seuil, point) : le nombre d'alertes/jour ne dépend pas de la stratégie de sortie."""
    rate_n: float = 0.0
    rate_t: float = 0.0


@dataclass
class AlertBandit:
    scores_cfg: dict[str, list[float]]      # score -> seuils
    points: list[str]
    policies: list[str]
    alerts_min: float
    alerts_max: float
    discount: float = 0.999
    prior_mean: float = -0.05
    prior_n: float = 3.0
    thr_lo: float = 0.0                      # plage d'exploration (score x2), ajustable par la boucle 2
    thr_hi: float = 1.0
    seed: int = 7
    arms: dict[str, Arm] = field(default_factory=dict)
    active: str | None = None
    _last_ts: float | None = None

    def __post_init__(self) -> None:
        self.rng = random.Random(self.seed)
        self.sync_arms(self.scores_cfg, self.points, self.policies)

    def sync_arms(self, scores_cfg: dict[str, list[float]], points: list[str], policies: list[str]) -> None:
        """Aligne les bras sur la config (ajoute les nouveaux, retire ceux qui n'y sont plus)."""
        self.scores_cfg, self.points, self.policies = dict(scores_cfg), list(points), list(policies)
        wanted = set()
        for sc, thrs in scores_cfg.items():
            for p in points:
                for t in thrs:
                    for pol in policies:
                        a = Arm(sc, t, p, pol)
                        wanted.add(a.key)
                        self.arms.setdefault(a.key, a)
        for k in [k for k in self.arms if k not in wanted]:
            del self.arms[k]
        if self.active not in self.arms:
            self.active = None
        # taux d'alertes partagés par créneau (reprend les valeurs d'un bras existant)
        old = getattr(self, "slots", None) or {}
        self.slots = {}
        self._by_point = {}
        for a in self.arms.values():
            if a.slot not in self.slots:
                self.slots[a.slot] = old.get(a.slot) or Slot(a.rate_n, a.rate_t)
            self._by_point.setdefault(a.point, []).append(a)
        self._slot_list = {}
        for key, sl in self.slots.items():
            sc, rest = key.split(":", 1)
            thr, point = rest.split("@", 1)
            self._slot_list.setdefault(point, []).append((sc, float(thr), sl))

    # ---- observations ----
    def observe_decision(self, point: str, scores: dict[str, float], ts: float) -> None:
        """Met à jour les taux d'alertes estimés de chaque bras."""
        if self._last_ts is not None and ts > self._last_ts:
            dt = ts - self._last_ts
            f = self.discount ** (dt / 60)
            for sl in self.slots.values():
                sl.rate_t = sl.rate_t * f + dt
                sl.rate_n *= f
        self._last_ts = ts if self._last_ts is None else max(self._last_ts, ts)
        for sc, thr, sl in self._slot_list.get(point, ()):
            if scores.get(sc, 0.0) >= thr:
                sl.rate_n += 1

    def observe_reward(self, point: str, scores: dict[str, float], pnl_by_policy: dict[str, float]) -> None:
        for a in self._by_point.get(point, ()):
            if scores.get(a.score, 0.0) >= a.thr and a.policy in pnl_by_policy:
                r = max(-1.0, min(pnl_by_policy[a.policy], 20.0))     # borne les valeurs extrêmes
                a.n = a.n * self.discount + 1
                a.s = a.s * self.discount + r
                a.ss = a.ss * self.discount + r * r

    # ---- politique ----
    def _sample(self, a: Arm) -> float:
        n = a.n + self.prior_n
        mean = (a.s + self.prior_mean * self.prior_n) / n
        sd = math.sqrt(a.var() / n)
        return self.rng.gauss(mean, sd)

    def apd(self, a: Arm) -> float:
        sl = self.slots.get(a.slot)
        return sl.rate_n / sl.rate_t * 86400 if sl and sl.rate_t > 0 else 0.0

    def _allowed(self, a: Arm) -> bool:
        return a.score != "x2" or self.thr_lo <= a.thr <= self.thr_hi

    def resample(self) -> Arm:
        lo, hi = self.alerts_min, self.alerts_max
        allowed = [a for a in self.arms.values() if self._allowed(a)]
        feasible = [a for a in allowed if lo <= self.apd(a) <= hi]
        if feasible:
            best = max(feasible, key=self._sample)
        else:
            def dist(a: Arm) -> tuple[float, float]:
                r = self.apd(a)
                # à distance égale, le seuil le plus HAUT (le plus prudent)
                return (lo - r if r < lo else r - hi, -a.thr)
            best = min(allowed or self.arms.values(), key=dist)
        self.active = best.key
        return best

    def ready(self, min_observed_s: float = 3600) -> bool:
        """Le bandit n'alerte qu'après avoir observé assez de décisions pour estimer
        le nombre d'alertes/jour de chaque combinaison."""
        sl = next(iter(self.slots.values()), None)
        return sl is not None and sl.rate_t >= min_observed_s

    def active_arm(self) -> Arm:
        if self.active is None or self.active not in self.arms:
            return self.resample()
        return self.arms[self.active]

    def should_alert(self, point: str, scores: dict[str, float]) -> bool:
        a = self.active_arm()
        return a.point == point and scores.get(a.score, 0.0) >= a.thr

    def recenter(self, lo: float, hi: float) -> None:
        self.thr_lo, self.thr_hi = lo, hi
        if self.active and not self._allowed(self.arms[self.active]):
            self.resample()

    def summary(self) -> dict:
        arms = sorted((a for a in self.arms.values() if a.n >= 5), key=lambda a: -a.mean())
        act = self.arms.get(self.active) if self.active else None
        return {
            "active": self.active, "range": [self.thr_lo, self.thr_hi],
            "active_detail": None if act is None else {
                "score": act.score, "threshold": act.thr, "point": act.point, "policy": act.policy,
                "mean_reward": round(act.mean(), 4), "n": round(act.n, 1), "alerts_per_day": round(self.apd(act), 1)},
            "arms": [{"key": a.key, "mean_reward": round(a.mean(), 4), "n": round(a.n, 1),
                      "alerts_per_day": round(self.apd(a), 1)} for a in arms[:30]],
            "best_by_policy": {pol: max((round(a.mean(), 4) for a in self.arms.values() if a.policy == pol and a.n >= 20),
                                        default=None) for pol in self.policies},
        }
