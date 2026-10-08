"""Rejeu chronologique d'un historique à travers EXACTEMENT les mêmes composants que
la production (feature engine, filtres, labeler, learner), avec une horloge
simulée. Sert au pré-entraînement et à établir les courbes de référence."""
from __future__ import annotations

import logging
from collections import Counter
from typing import Any, Iterable

from ..config import Config
from ..features.engine import FeatureEngine
from ..features.market import MarketContext
from ..features.wallets import WalletIntel
from ..labeler.engine import LabelerEngine
from ..learning.core import Learner
from ..learning.models import train_lgbm
from ..safety.filters import SafetyFilters

log = logging.getLogger("backfill.replay")


class Replay:
    def __init__(self, cfg: Config, learner: Learner | None = None, lgbm: bool = True):
        c = cfg.data
        self.cfg = cfg
        self.intel = WalletIntel(c["features"])
        self.market = MarketContext(c["features"]["narrative_window_s"])
        self.fe = FeatureEngine(c, SafetyFilters(cfg.safety), self.intel, self.market)
        self.lab = LabelerEngine(c)
        self.learner = learner or Learner(cfg)
        self.lgbm = lgbm
        self.t_first: float | None = None
        self.t_last = 0.0
        self._next_champ = self._next_bandit = self._next_lgbm = 0.0
        self.log: list[tuple[float, str, int, str | None, float]] = []   # (ts, horizon, y, error_type, loss champion)
        self.n_decisions = 0
        self._features: dict[str, dict] = {}       # features des décisions (labels 6 h / 24 h)
        self.n_alerts = 0

    def _advance(self, t: float) -> None:
        L = self.learner
        for d in self.fe.tick(t):
            self._decision(d)
        labels, closed, _ = self.lab.tick(t)
        outs, _signals = self.lab.drain()
        for o in outs:
            L.on_outcome(o)
        for lb in labels:
            if lb.horizon in L.long:
                L.on_label_long(lb, self._features.get(lb.decision_id, {}))
                continue
            res = L.on_label(lb)
            champ_eval = next((e for e in res.evaluations if e[3]), None)
            if champ_eval:
                self.log.append((t, lb.horizon, lb.y, res.error.error_type if res.error else None, champ_eval[6]))
        for c in closed:
            self.intel.update_from_closed(c)
        if t >= self._next_champ:
            L.periodic(t)
            self._next_champ = t + self.cfg.get("models.champion_eval_every_s")
        if t >= self._next_bandit:
            L.bandit.resample()
            self._next_bandit = t + self.cfg.get("bandit.resample_every_s")
        if self.lgbm and t >= self._next_lgbm:
            if self._next_lgbm:
                for h in L.horizons:
                    rows = L.lgbm_rows(h)
                    if len(rows) >= self.cfg.get("models.lgbm_min_samples"):
                        booster, feats = train_lgbm(rows)
                        L.install_lgbm(h, booster, feats)
            self._next_lgbm = t + self.cfg.get("models.lgbm_every_s")

    def _decision(self, d: Any) -> None:
        self.n_decisions += 1
        self._features[d.decision_id] = d.features
        self.lab.on_decision(d)
        _, alert = self.learner.on_decision(d)
        self.n_alerts += alert is not None

    def run(self, events: Iterable[Any], progress_every: int = 200_000) -> dict:
        n = 0
        for ev in events:
            t = ev.ts
            if self.t_first is None:
                self.t_first = t
            self._advance(t)
            for d in self.fe.on_event(ev):
                self._decision(d)
            self.lab.on_event(ev)
            self.t_last = t
            n += 1
            if progress_every and n % progress_every == 0:
                log.info("rejeu : %d événements, %d décisions, %d labels", n, self.n_decisions, len(self.log))
        self._advance(self.t_last + 90000)          # laisse échoir les labels 24 h et les outcomes
        return self.reference()

    def reference(self, tail_share: float = 0.3) -> dict[str, dict]:
        """Niveaux d'erreur de départ (fin de rejeu = modèle déjà entraîné)."""
        primary = self.learner.primary
        if not self.log or self.t_first is None:
            return {}
        cut = self.t_last - (self.t_last - self.t_first) * tail_share
        tail = [r for r in self.log if r[0] >= cut and r[1] == primary]
        n = len(tail)
        if n == 0:
            return {}
        types = Counter(r[3] for r in tail if r[3])
        ref = {"error_rate": {"value": sum(types.values()) / n, "n": n},
               f"logloss:{primary}": {"value": sum(r[4] for r in tail) / n, "n": n}}
        for t, k in types.items():
            ref[f"error_rate:{t}"] = {"value": k / n, "n": n}
        return ref
