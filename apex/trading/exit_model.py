"""SORTIE APPRISE (cœur pur, testé) : un modèle en ligne apprend, à partir de l'état du token à un
instant donné, s'il vaut mieux GARDER ou VENDRE.

Question posée toutes les `every_s` secondes sur chaque token suivi (« point de contrôle ») :
    à partir du prix actuel c, le prix touchera-t-il c × up AVANT c × down (dans `horizon_s`) ?
(méthode de la « triple barrière » : la réponse arrive dès qu'une barrière est touchée, sinon à
l'échéance, où l'on regarde si le prix est au-dessus de c.)

La probabilité prédite (`hold_p`) est donnée aux stratégies « apprises », qui vendent quand elle
devient trop faible. Évaluation honnête : chaque prédiction est faite AVANT de connaître la réponse,
puis le modèle apprend de la réponse (prédire → observer → apprendre), comme pour les entrées.
"""
from __future__ import annotations

import bisect
import math
from dataclasses import dataclass, field
from typing import Any

from river import compose, linear_model, optim, preprocessing

from .exits import flow_features


@dataclass
class ExitTrackState:
    pending: list[tuple[float, float, dict, float | None]] = field(default_factory=list)   # (t, prix, x, p prédite)
    last_cp: float = 0.0
    hold_p: float | None = None


def _ret(times: list[float], prices: list[float], t: float, price: float, ago: float) -> float:
    i = bisect.bisect_right(times, t - ago) - 1
    if i < 0 or prices[i] <= 0:
        return 0.0
    return math.log(price / prices[i])


class ExitLearner:
    def __init__(self, cfg: dict | None = None):
        c = cfg or {}
        self.up = c.get("up", 1.3)
        self.down = c.get("down", 0.8)
        self.horizon_s = c.get("horizon_s", 600)
        self.every_s = c.get("every_s", 10)
        self.min_samples = c.get("min_samples", 2000)
        self.max_pending = int(self.horizon_s / self.every_s) + 2
        self.model = compose.Pipeline(
            preprocessing.StandardScaler(),
            linear_model.LogisticRegression(optimizer=optim.SGD(0.01), l2=1e-4),
        )
        self.learn = True                 # coupé pendant la reprise après redémarrage (pas de double apprentissage)
        self.n_learned = 0
        self.n_pos = 0
        self.ll_model = 0.0               # perte logarithmique cumulée (fenêtre glissante exponentielle)
        self.ll_base = 0.0
        self.n_eval = 0.0

    # ------------------------------------------------------------------
    @staticmethod
    def features(created_ts: float, times: list[float], prices: list[float], peak: float,
                 t: float, price: float, migrated: bool, danger: Any) -> dict[str, float]:
        x = {
            "log_mult_launch": math.log(price / prices[0]) if prices and prices[0] > 0 else 0.0,
            "dd_peak": price / peak - 1 if peak > 0 else 0.0,
            "ret15": _ret(times, prices, t, price, 15),
            "ret60": _ret(times, prices, t, price, 60),
            "ret300": _ret(times, prices, t, price, 300),
            "age": math.log1p(max(0.0, t - created_ts)),
            "migrated": 1.0 if migrated else 0.0,
        }
        if danger is not None:
            buys, sells = max(getattr(danger, "_buys", 0.0), 0.0), max(getattr(danger, "_sells", 0.0), 0.0)
            x["buy_ratio30"] = buys / (buys + sells) if buys + sells > 0 else 0.5
            x["flow30"] = math.log1p(buys + sells)
            x["n30"] = float(len(getattr(danger, "window", ())))
            if hasattr(danger, "sells30"):
                x.update(flow_features(danger, t))
        # sommet récent : depuis quand, et à quelle distance (un repli après un pic annonce souvent la fin)
        i = bisect.bisect_left(times, t - 300)
        if i < len(prices):
            j = max(range(i, len(prices)), key=prices.__getitem__)
            x["since_peak300"] = math.log1p(max(0.0, t - times[j]))
            x["from_peak300"] = price / prices[j] - 1 if prices[j] > 0 else 0.0
        return x

    @property
    def ready(self) -> bool:
        return self.n_learned >= self.min_samples

    def predict(self, x: dict[str, float]) -> float | None:
        if not self.ready:
            return None
        return float(self.model.predict_proba_one(x).get(True, 0.5))

    def _learn(self, x: dict, y: int, p: float | None) -> None:
        if not self.learn:
            return
        if p is not None:          # évaluation prequential : la prédiction précède la réponse
            base = (self.n_pos + 1) / (self.n_learned + 2)
            eps = 1e-6
            decay = 0.999
            self.ll_model = self.ll_model * decay - math.log(max(eps, p if y else 1 - p))
            self.ll_base = self.ll_base * decay - math.log(max(eps, base if y else 1 - base))
            self.n_eval = self.n_eval * decay + 1
        self.model.learn_one(x, bool(y))
        self.n_learned += 1
        self.n_pos += y

    # ------------------------------------------------------------------
    def on_price(self, st: ExitTrackState, x_fn, t: float, price: float) -> float | None:
        """Appelé à chaque prix d'un token suivi. Résout les points de contrôle en attente, en crée
        un nouveau toutes les `every_s` secondes, et renvoie la probabilité de « garder » à jour."""
        if price <= 0:
            return st.hold_p
        if st.pending:
            keep = []
            for cp in st.pending:
                t0, c, x, p = cp
                if price >= c * self.up:
                    self._learn(x, 1, p)
                elif price <= c * self.down:
                    self._learn(x, 0, p)
                elif t - t0 >= self.horizon_s:
                    self._learn(x, int(price > c), p)
                else:
                    keep.append(cp)
            st.pending = keep
        if t - st.last_cp >= self.every_s:
            st.last_cp = t
            x = x_fn()
            st.hold_p = self.predict(x)
            st.pending.append((t, price, x, st.hold_p))
            if len(st.pending) > self.max_pending:
                st.pending.pop(0)
        return st.hold_p

    def close(self, st: ExitTrackState, last_price: float) -> None:
        """Token abandonné (plus aucun échange) : les points en attente sont résolus au dernier prix."""
        for t0, c, x, p in st.pending:
            self._learn(x, int(last_price > c), p)
        st.pending = []

    def summary(self) -> dict:
        skill = (1 - self.ll_model / self.ll_base) if self.ll_base > 0 else None
        return {"situations_apprises": self.n_learned, "pret": self.ready,
                "taux_hausse": round(self.n_pos / self.n_learned, 3) if self.n_learned else None,
                "fiabilite": round(skill, 3) if skill is not None else None}
