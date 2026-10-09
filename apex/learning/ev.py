"""GAIN ATTENDU PAR STRATÉGIE : quelle stratégie de sortie pour QUEL token, et le trade vaut-il le coup ?

Pour chaque décision alertable, le labeler simule TOUTES les stratégies de sortie : on connaît donc le
résultat réel de chacune (information complète). Un modèle LightGBM par stratégie apprend
« caractéristiques du token à l'entrée → gain réel (frais compris) ». À la décision :
  - stratégie choisie pour CE token = celle dont le gain attendu est le plus élevé ;
  - score « ev » = ce gain attendu → le bandit peut n'alerter que si le gain attendu est positif.
Le choix « AUTO » est un bras du bandit comme les autres : il n'est utilisé que s'il rapporte
réellement plus que les stratégies fixes (récompense = gain réel de la stratégie choisie).

Validation honnête à chaque entraînement : modèles appris sur les 80 % plus anciens, testés sur les
20 % plus récents (jamais vus), puis réappris sur tout.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

import numpy as np

PARAMS = {"objective": "regression", "learning_rate": 0.05, "num_leaves": 15, "min_data_in_leaf": 100,
          "feature_fraction": 0.8, "bagging_fraction": 0.8, "bagging_freq": 1, "lambda_l2": 1.0, "verbose": -1}


@dataclass
class EVModels:
    feats: list[str] = field(default_factory=list)
    boosters: dict[str, Any] = field(default_factory=dict)
    stats: dict = field(default_factory=dict)

    @property
    def ready(self) -> bool:
        return bool(self.boosters)

    def vector(self, x: dict[str, float]) -> np.ndarray:
        return np.array([[float(x[f]) if isinstance(x.get(f), (int, float)) and x.get(f) is not None
                          and math.isfinite(x[f]) else np.nan for f in self.feats]])

    def predict(self, x: dict[str, float]) -> dict[str, float]:
        if not self.boosters:
            return {}
        v = self.vector(x)
        return {pol: float(b.predict(v)[0]) for pol, b in self.boosters.items()}

    def best(self, x: dict[str, float]) -> tuple[str | None, float]:
        evs = self.predict(x)
        if not evs:
            return None, -1.0
        pol = max(evs, key=evs.get)
        return pol, evs[pol]


def _matrix(rows: list[tuple[dict, dict, float]], feats: list[str]) -> np.ndarray:
    X = np.full((len(rows), len(feats)), np.nan)
    idx = {f: j for j, f in enumerate(feats)}
    for i, (x, _, _) in enumerate(rows):
        for k, v in x.items():
            j = idx.get(k)
            if j is not None and isinstance(v, (int, float)) and v is not None and math.isfinite(v):
                X[i, j] = v
    return X


def _fit(rows: list[tuple[dict, dict, float]], policies: list[str], feats: list[str], clip: tuple[float, float],
         min_rows: int, rounds: int) -> dict[str, Any]:
    import lightgbm as lgb
    X = _matrix(rows, feats)
    out = {}
    for pol in policies:
        mask = np.array([pol in p for _, p, _ in rows])
        if mask.sum() < min_rows:
            continue
        y = np.clip(np.array([p.get(pol, 0.0) for _, p, _ in rows])[mask], *clip)
        out[pol] = lgb.train(PARAMS, lgb.Dataset(X[mask], label=y, feature_name=feats), num_boost_round=rounds)
    return out


def train(rows: list[tuple[dict, dict, float]], policies: list[str], clip: tuple[float, float] = (-1.0, 3.0),
          min_rows: int = 3000, rounds: int = 150, holdout: float = 0.2) -> EVModels:
    """rows : (features à l'entrée, {stratégie: gain réel}, ts). Retourne les modèles (appris sur tout)
    et la validation sur les plus récents (appris sur les plus anciens)."""
    rows = sorted(rows, key=lambda r: r[2])
    feats = sorted({k for x, _, _ in rows[:5000] for k, v in x.items() if isinstance(v, (int, float))})
    cut = int(len(rows) * (1 - holdout))
    old, new = rows[:cut], rows[cut:]
    stats: dict = {"n": len(rows)}
    val = _fit(old, policies, feats, clip, int(min_rows * (1 - holdout)), rounds)
    # comparaison ÉQUITABLE : seulement les stratégies connues sur (presque) tous les tokens de test, et
    # seulement les tokens où toutes sont connues (une stratégie récente n'a pas de résultat sur les anciens)
    pols = [p for p in val if sum(p in r[1] for r in new) >= 0.95 * len(new)] if new else []
    new = [r for r in new if all(p in r[1] for p in pols)]
    stats["strategies_comparees"] = len(pols)
    if len(pols) >= 2 and new:
        Xn = _matrix(new, feats)
        P = np.vstack([val[p].predict(Xn) for p in pols]).T              # (n_test, n_strat)
        R = np.array([[r[1][p] for p in pols] for r in new])
        choice = P.argmax(axis=1)
        auto = R[np.arange(len(new)), choice]
        best_fixed = int(np.argmax(R.mean(axis=0)))                     # meilleure stratégie fixe, choisie APRÈS coup
        sel = P.max(axis=1) > 0                                        # n'entrer que si le gain attendu est positif
        stats.update({
            "n_test": len(new), "auto_moyen": float(auto.mean()),
            "meilleure_fixe": pols[best_fixed], "meilleure_fixe_moyen": float(R[:, best_fixed].mean()),
            "part_ev_positive": float(sel.mean()),
            "auto_si_ev_positive": float(auto[sel].mean()) if sel.any() else None,
            "fixe_si_ev_positive": float(R[sel, best_fixed].mean()) if sel.any() else None,
        })
    models = EVModels(feats=feats, boosters=_fit(rows, policies, feats, clip, min_rows, rounds), stats=stats)
    return models


def summary(stats: dict) -> str:
    """Résumé simple du test sur des tokens jamais vus."""
    if not stats.get("n_test") or stats.get("auto_moyen") is None:
        return "pas encore assez de données pour juger le choix de stratégie par token"
    s = (f"sur {stats['n_test']} tokens jamais vus : choisir la stratégie token par token rapporte "
         f"{stats['auto_moyen']:+.1%} par trade, contre {stats['meilleure_fixe_moyen']:+.1%} pour la meilleure "
         f"stratégie unique")
    if stats.get("auto_si_ev_positive") is not None:
        s += (f" ; en n'entrant que si le gain attendu est positif ({stats['part_ev_positive']:.0%} des tokens) : "
              f"{stats['auto_si_ev_positive']:+.1%} par trade")
    return s.replace(".", ",")


def should_activate(history: list[dict], margin: float = 0.01, min_test: int = 3000, runs: int = 2) -> bool:
    """Le choix par token ne prend la main que s'il bat la meilleure stratégie fixe (sur des tokens jamais
    vus) d'au moins `margin` par trade, `runs` validations de suite."""
    last = history[-runs:]
    return len(last) == runs and all(
        h.get("auto_moyen") is not None and h.get("n_test", 0) >= min_test
        and h["auto_moyen"] > h["meilleure_fixe_moyen"] + margin for h in last)
