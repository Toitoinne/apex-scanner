"""Concurrents : modèles incrémentaux (River) + LightGBM périodique, évalués en
mode prequential (prédire → noter → apprendre)."""
from __future__ import annotations

import copy
import inspect
import math
import random
from collections import deque
from dataclasses import dataclass, field
from typing import Any

import numpy as np
from river import compose, drift, forest, linear_model, optim, preprocessing, tree

from .calibration import Calibrator, sigmoid

EPS = 1e-6


def logloss(p: float, y: int) -> float:
    p = min(1 - EPS, max(EPS, p))
    return -(y * math.log(p) + (1 - y) * math.log(1 - p))


@dataclass
class CompetitorSpec:
    id: str
    kind: str                                  # logreg | arf | hat | lgbm
    params: dict = field(default_factory=dict)
    disabled_features: list[str] = field(default_factory=list)
    extra_features: list[str] = field(default_factory=list)    # features Claude autorisées (cx_*)
    error_weights: dict[str, float] | None = None              # surcharge des poids globaux
    positive_class_weight: float | None = None
    replay_window: int | None = None                           # fenêtre d'apprentissage raccourcie
    shadow_of_correction: int | None = None
    parent: str | None = None

    def to_dict(self) -> dict:
        return dict(self.__dict__)


def rules_score(x: dict[str, float]) -> float:
    """Démarrage à froid (backfill indisponible) : momentum pondéré + pénalités de risque.
    Concurrent fixe : les modèles en ligne le détrônent dès que leur précision live est meilleure."""
    def g(k: str, d: float = 0.0) -> float:
        return float(x.get(k, d))
    buyers = g("independent_buyers", g("unique_buyers"))
    z = (-2.5
         + 1.2 * math.tanh(g("velocity_mc_per_min") / 50)
         + 0.8 * min(g("buy_sell_ratio_vol"), 5) / 5
         + 0.6 * min(buyers, 60) / 60
         + 0.5 * math.tanh(g("ret_last_30s") * 3)
         + 0.8 * g("smart_share")
         - 1.5 * g("sniper_share")
         - 2.0 * max(0.0, g("top10_concentration") - 0.3)
         - 1.5 * g("dev_sold_pct")
         - 1.0 * g("wash_score")
         - 1.0 * g("max_drawdown_so_far"))
    # sigmoïde stable : une feature aberrante (z très négatif) faisait déborder math.exp
    # et toute la décision était ignorée par le learner
    return sigmoid(z)


def build_river_model(spec: CompetitorSpec, seed: int = 42) -> Any:
    if spec.kind == "rules":
        return None
    p = spec.params
    if spec.kind == "logreg":
        return compose.Pipeline(
            preprocessing.StandardScaler(),
            linear_model.LogisticRegression(optimizer=optim.SGD(p.get("lr", 0.01)), l2=p.get("l2", 1e-4)),
        )
    if spec.kind == "arf":
        return forest.ARFClassifier(n_models=p.get("n_models", 10), max_features=p.get("max_features", "sqrt"),
                                    lambda_value=p.get("lambda_value", 6), seed=seed)
    if spec.kind == "hat":
        return tree.HoeffdingAdaptiveTreeClassifier(grace_period=p.get("grace_period", 200),
                                                    delta=p.get("delta", 1e-5), seed=seed)
    raise ValueError(f"type de modèle inconnu : {spec.kind}")


def _accepts_w(model: Any) -> bool:
    target = model
    if isinstance(model, compose.Pipeline):
        target = list(model.steps.values())[-1]
    return "w" in inspect.signature(target.learn_one).parameters


class Competitor:
    def __init__(self, spec: CompetitorSpec, window: int = 3000, calib_method: str = "platt", seed: int = 42):
        self.spec = spec
        self.model: Any = None if spec.kind in ("lgbm", "rules", "prior") else build_river_model(spec, seed)
        self._w_ok = self.model is not None and _accepts_w(self.model)
        self.losses: deque[float] = deque(maxlen=window)
        self.hits: deque[int] = deque(maxlen=window)
        self.adwin = drift.ADWIN()
        self.drift_events = 0
        self.last_drift_n = -1
        self.n_learned = 0
        self.n_evaluated = 0
        self.calibrator = Calibrator(calib_method)
        self.rng = random.Random(seed)
        # LightGBM
        self.booster: Any = None
        self.lgbm_features: list[str] = []

    @property
    def id(self) -> str:
        return self.spec.id

    def filter_x(self, x: dict[str, float]) -> dict[str, float]:
        dis = set(self.spec.disabled_features)
        extra = set(self.spec.extra_features)
        return {k: v for k, v in x.items()
                if k not in dis and (not k.startswith("cx_") or k in extra) and v is not None and math.isfinite(v)}

    def predict_raw(self, x: dict[str, float]) -> float:
        xf = self.filter_x(x)
        if self.spec.kind == "rules":
            return rules_score(xf)
        if self.spec.kind == "prior":
            return self.prior_rate()
        if self.spec.kind == "lgbm":
            if self.booster is None:
                return 0.5
            vec = np.array([[xf.get(f, np.nan) for f in self.lgbm_features]])
            return float(self.booster.predict(vec)[0])
        if self.n_learned == 0:
            return 0.5
        proba = self.model.predict_proba_one(xf)
        return float(proba.get(1, proba.get(True, 0.0)))

    def predict(self, x: dict[str, float]) -> tuple[float, float]:
        raw = self.predict_raw(x)
        return raw, self.calibrator.transform(raw)

    def evaluate(self, p_raw: float, y: int, p_cal: float | None = None) -> float:
        """Note la probabilité réellement utilisée (calibrée). Les modèles apprennent avec des
        poids (gagnants, erreurs coûteuses) qui gonflent leur proba brute : la noter les
        pénaliserait injustement face à un modèle non pondéré."""
        p_used = p_raw if p_cal is None else p_cal
        ll = logloss(p_used, y)
        self.losses.append(ll)
        self.hits.append(int((p_used >= 0.5) == bool(y)))
        self.n_evaluated += 1
        self.adwin.update(ll)
        if self.adwin.drift_detected:
            self.drift_events += 1
            self.last_drift_n = self.n_evaluated
        self.calibrator.update(p_raw, y)
        return ll

    def prior_rate(self) -> float:
        n, k = getattr(self, "_prior_n", 0.0), getattr(self, "_prior_k", 0.0)
        return (k + 1) / (n + 50)          # fréquence observée (lissée), oubli lent

    def learn(self, x: dict[str, float], y: int, w: float) -> None:
        if self.spec.kind == "prior":
            self._prior_n = getattr(self, "_prior_n", 0.0) * 0.9999 + 1
            self._prior_k = getattr(self, "_prior_k", 0.0) * 0.9999 + y
            self.n_learned += 1
            return
        if self.spec.kind in ("lgbm", "rules"):
            return
        xf = self.filter_x(x)
        if self._w_ok:
            self.model.learn_one(xf, y, w=w)
        else:
            reps = int(w) + (1 if self.rng.random() < (w - int(w)) else 0)
            for _ in range(max(1, reps)):
                self.model.learn_one(xf, y)
        self.n_learned += 1

    @property
    def ready(self) -> bool:
        """Un modèle n'est noté que sur les prédictions faites après un minimum d'apprentissage
        (sinon il prédit 0,5 par défaut et ces prédictions « à vide » faussent son score)."""
        if self.spec.kind in ("rules", "prior"):
            return True
        if self.spec.kind == "lgbm":
            return self.booster is not None
        return self.n_learned >= 50

    def reset_eval(self) -> None:
        self.losses.clear()
        self.hits.clear()

    def mean_loss(self, last: int | None = None) -> float:
        if not self.losses:
            return float("inf")
        if last is None or last >= len(self.losses):
            return float(np.mean(self.losses))
        return float(np.mean(list(self.losses)[-last:]))

    def summary(self) -> dict:
        return {
            "id": self.id, "kind": self.spec.kind, "n_learned": self.n_learned, "n_evaluated": self.n_evaluated,
            "logloss": None if not self.losses else round(self.mean_loss(), 5),
            "accuracy": None if not self.hits else round(float(np.mean(self.hits)), 4),
            "drift_events": self.drift_events, "spec": self.spec.to_dict(),
        }

    def clone_spec(self, new_id: str, **changes: Any) -> CompetitorSpec:
        spec = copy.deepcopy(self.spec)
        spec.id = new_id
        spec.parent = self.id
        for k, v in changes.items():
            setattr(spec, k, v)
        return spec


def train_lgbm(rows: list[tuple[dict, int, float]], params: dict | None = None) -> tuple[Any, list[str]]:
    """Entraîne un LightGBM sur la fenêtre récente (appelé dans un thread)."""
    import lightgbm as lgb

    feats = sorted({k for x, _, _ in rows for k in x})
    X = np.array([[x.get(f, np.nan) for f in feats] for x, _, _ in rows], dtype=float)
    y = np.array([r[1] for r in rows])
    w = np.array([r[2] for r in rows])
    p = {"objective": "binary", "learning_rate": 0.05, "num_leaves": 31, "min_data_in_leaf": 50,
         "feature_fraction": 0.8, "bagging_fraction": 0.8, "bagging_freq": 1, "verbose": -1}
    p.update(params or {})
    rounds = p.pop("rounds", 300)
    booster = lgb.train(p, lgb.Dataset(X, label=y, weight=w, feature_name=feats), num_boost_round=rounds)
    return booster, feats
