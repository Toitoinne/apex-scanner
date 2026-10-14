"""GAIN ATTENDU PAR STRATÉGIE : quelle stratégie de sortie pour QUEL token, et le trade vaut-il le coup ?

Pour chaque décision alertable, le labeler simule TOUTES les stratégies de sortie : on connaît donc le
résultat réel de chacune (information complète). Un modèle LightGBM par stratégie apprend
« caractéristiques du token à l'entrée → gain réel (frais compris) ». À la décision :
  - stratégie choisie pour CE token = celle dont le gain attendu est le plus élevé ;
  - score « ev » = ce gain attendu → le bandit peut n'alerter que si le gain attendu est positif.
Le choix « AUTO » est un bras du bandit comme les autres : il n'est utilisé que s'il rapporte
réellement plus que les stratégies fixes (récompense = gain réel de la stratégie choisie).

NOTE « GAGNANT PROBABLE » (14/10) : prédire le gain moyen pousse le modèle vers des tokens qui ne bougent pas
(ils ne perdent « que » les frais) ; prédire si le trade sera GAGNANT après frais trie bien mieux (analyse du
14/10 sur 158 000 décisions : AUC 0,78 ; top 0,5 % à +9,9 % par trade sur 2 jours jamais vus, contre −7,2 % pour
tous). Le score « win » publié est le RANG de la décision (0–1) ; il ne sert aux alertes qu'après avoir été
gagnant sur des tokens jamais vus deux validations de suite (should_activate_win).

Validation honnête à chaque entraînement : modèles appris sur les 80 % plus anciens, testés sur les
20 % plus récents (jamais vus), puis réappris sur tout.
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from typing import Any

import numpy as np

PARAMS = {"objective": "regression", "learning_rate": 0.05, "num_leaves": 15, "min_data_in_leaf": 100,
          "feature_fraction": 0.8, "bagging_fraction": 0.8, "bagging_freq": 1, "lambda_l2": 1.0, "verbose": -1}
WIN_PARAMS = {"objective": "binary", "learning_rate": 0.03, "num_leaves": 31, "min_data_in_leaf": 200,
              "feature_fraction": 0.7, "bagging_fraction": 0.8, "bagging_freq": 1, "lambda_l2": 5.0, "verbose": -1}


@dataclass
class EVModels:
    feats: list[str] = field(default_factory=list)
    boosters: dict[str, Any] = field(default_factory=dict)
    stats: dict = field(default_factory=dict)
    win_policy: str | None = None            # stratégie de sortie de la note « gagnant probable »
    win_booster: Any = None
    win_ref: Any = None                      # quantiles des prédictions sur des tokens JAMAIS VUS (départ du rang)

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

    def win_raw(self, x: dict[str, float]) -> float | None:
        if self.win_booster is None:
            return None
        return float(self.win_booster.predict(self.vector(x))[0])

    def win_rank(self, x: dict[str, float]) -> float:
        """Rang (0–1) par rapport aux prédictions sur des tokens jamais vus : 0,99 = dans le top 1 %. Le learner
        utilise plutôt les décisions réelles récentes dès qu'il en a assez (WinRanker)."""
        p = self.win_raw(x)
        if p is None or self.win_ref is None:
            return 0.0
        return float(np.searchsorted(self.win_ref, p, side="right")) / len(self.win_ref)


class WinRanker:
    """Rang d'une note parmi les N dernières décisions alertables RÉELLES : « top 1 % » veut dire le 1 % le
    mieux noté en ce moment (les seuils tirés des données d'entraînement étaient trop hauts : le modèle y est
    trop sûr de lui)."""

    def __init__(self, size: int = 20000, min_live: int = 2000):
        from collections import deque
        self.size, self.min_live = size, min_live
        self.recent: Any = deque()
        self.sorted: list[float] = []

    def rank(self, p: float, ref: Any = None) -> float:
        import bisect
        if len(self.sorted) >= self.min_live or ref is None:
            r = bisect.bisect_right(self.sorted, p) / max(1, len(self.sorted))
        else:
            r = float(np.searchsorted(ref, p, side="right")) / len(ref)
        self.recent.append(p)
        bisect.insort(self.sorted, p)
        if len(self.recent) > self.size:
            old = self.recent.popleft()
            del self.sorted[bisect.bisect_left(self.sorted, old)]
        return r


@dataclass
class Data:
    """Résultats sous forme compacte (matrices float32) : le format « liste de dictionnaires » pesait ~0,5 Go
    dans le learner à chaque entraînement."""
    X: np.ndarray
    feats: list[str]
    P: np.ndarray                            # gain réel (n, stratégies), NaN si inconnu
    pols: list[str]
    ts: np.ndarray
    mint: np.ndarray

    def __len__(self) -> int:
        return len(self.ts)

    @classmethod
    def from_rows(cls, rows: list[tuple], feats: list[str] | None = None, pols: list[str] | None = None) -> "Data":
        """rows : (features, {stratégie: gain}, ts[, mint])."""
        rows = sorted(rows, key=lambda r: r[2])
        feats = feats if feats is not None else sorted(
            {k for x, *_ in rows[:5000] for k, v in x.items() if isinstance(v, (int, float)) and not k.startswith("cx_")})
        pols = pols if pols is not None else sorted({k for _, p, *_ in rows for k in p})
        fi, pi = {f: j for j, f in enumerate(feats)}, {q: j for j, q in enumerate(pols)}
        X = np.full((len(rows), len(feats)), np.nan, dtype=np.float32)
        P = np.full((len(rows), len(pols)), np.nan, dtype=np.float32)
        for i, r in enumerate(rows):
            for k, v in r[0].items():
                j = fi.get(k)
                if j is not None and isinstance(v, (int, float)) and v is not None and math.isfinite(v):
                    X[i, j] = v
            for k, v in r[1].items():
                j = pi.get(k)
                if j is not None:
                    P[i, j] = float(v)
        mint = np.array([r[3] if len(r) > 3 else str(i) for i, r in enumerate(rows)], dtype=object)
        return cls(X, list(feats), P, list(pols), np.array([r[2] for r in rows], dtype=float), mint)

    def take(self, idx: np.ndarray) -> "Data":
        return Data(self.X[idx], self.feats, self.P[idx], self.pols, self.ts[idx], self.mint[idx])

    def col(self, pol: str) -> np.ndarray | None:
        return self.P[:, self.pols.index(pol)] if pol in self.pols else None


def _fit(d: Data, policies: list[str], clip: tuple[float, float], min_rows: int, rounds: int) -> dict[str, Any]:
    import lightgbm as lgb
    out = {}
    for pol in policies:
        col = d.col(pol)
        if col is None:
            continue
        mask = ~np.isnan(col)
        if mask.sum() < min_rows:
            continue
        y = np.clip(col[mask], *clip)
        out[pol] = lgb.train(PARAMS, lgb.Dataset(d.X[mask], label=y, feature_name=d.feats), num_boost_round=rounds)
    return out


def _fit_win(d: Data, pol: str, rounds: int) -> tuple[Any, np.ndarray] | None:
    import lightgbm as lgb
    col = d.col(pol)
    if col is None:
        return None
    mask = ~np.isnan(col)
    y = (col[mask] > 0).astype(int)
    if mask.sum() < 1000 or y.sum() < 50:
        return None
    b = lgb.train(WIN_PARAMS, lgb.Dataset(d.X[mask], label=y, feature_name=d.feats), num_boost_round=rounds)
    ref = np.quantile(b.predict(d.X[mask]), np.linspace(0, 1, 1001))
    return b, ref


def _auc(s: np.ndarray, y: np.ndarray) -> float | None:
    n1 = int(y.sum())
    n0 = len(y) - n1
    if n1 < 5 or n0 < 5:
        return None
    r = np.empty(len(s))
    r[np.argsort(s, kind="mergesort")] = np.arange(1, len(s) + 1)
    return float((r[y == 1].sum() - n1 * (n1 + 1) / 2) / (n1 * n0))


def win_validation(old: Data, new: Data, pol: str, rounds: int, tops: tuple[float, ...]) -> dict:
    """Appris sur les plus anciens, testé sur les plus récents (jamais vus) : résultat réel des tokens les mieux
    notés, UNE fois par token (comme le vrai bot, qui n'alerte qu'une fois par token)."""
    fit = _fit_win(old, pol, rounds)
    col = new.col(pol)
    if fit is None or col is None:
        return {}
    ok = ~np.isnan(col)
    if ok.sum() < 200:
        return {}
    b, _ref = fit
    p = b.predict(new.X[ok])
    # rang parmi les décisions de test elles-mêmes (comme en direct : parmi les décisions récentes réelles)
    rank = np.empty(len(p))
    rank[np.argsort(p, kind="mergesort")] = np.arange(1, len(p) + 1) / len(p)
    pnl, ts_, mint = col[ok].astype(float), new.ts[ok], new.mint[ok]
    out = {"win_policy": pol, "win_auc": _auc(p, (pnl > 0).astype(int)), "win_n_test": int(ok.sum()),
           "win_moyen_tous": float(pnl.mean()), "win_top": {},
           "_win_ref_oos": np.quantile(p, np.linspace(0, 1, 1001))}
    for q in tops:
        idx = np.where(rank >= 1 - q)[0]
        seen, sel = set(), []
        for i in idx[np.argsort(ts_[idx], kind="mergesort")]:
            if mint[i] not in seen:
                seen.add(mint[i])
                sel.append(i)
        v = pnl[sel]
        out["win_top"][str(q)] = {"n": int(len(v)), "moyen": float(v.mean()) if len(v) else None,
                                  "gagnants": float((v > 0).mean()) if len(v) else None}
    return out


def train(rows: list[tuple] | Data, policies: list[str] | None = None, clip: tuple[float, float] = (-1.0, 3.0),
          min_rows: int = 3000, rounds: int = 150, holdout: float = 0.2, win_policy: str | None = None,
          win_rounds: int = 400, win_tops: tuple[float, ...] = (0.005, 0.01, 0.02)) -> EVModels:
    """rows (ou Data) : features à l'entrée, gain réel par stratégie, ts. Retourne les modèles (appris sur tout)
    et la validation sur les plus récents (appris sur les plus anciens)."""
    d = rows if isinstance(rows, Data) else Data.from_rows(rows)
    policies = policies or d.pols
    d = d.take(np.argsort(d.ts, kind="mergesort"))
    cut = int(len(d) * (1 - holdout))
    old, new = d.take(np.arange(cut)), d.take(np.arange(cut, len(d)))
    stats: dict = {"n": len(d)}
    val = _fit(old, policies, clip, int(min_rows * (1 - holdout)), rounds)
    # comparaison ÉQUITABLE : seulement les stratégies connues sur (presque) tous les tokens de test, et
    # seulement les tokens où toutes sont connues (une stratégie récente n'a pas de résultat sur les anciens)
    known = {p: ~np.isnan(new.col(p)) for p in val}
    pols = [p for p in val if len(new) and known[p].sum() >= 0.95 * len(new)]
    n_idx = np.where(np.all(np.vstack([known[p] for p in pols]), axis=0))[0] if pols else np.array([], int)
    stats["strategies_comparees"] = len(pols)
    if len(pols) >= 2 and len(n_idx):
        Xn = new.X[n_idx]
        Pp = np.vstack([val[p].predict(Xn) for p in pols]).T              # (n_test, n_strat)
        R = np.vstack([new.col(p)[n_idx] for p in pols]).T.astype(float)
        choice = Pp.argmax(axis=1)
        auto = R[np.arange(len(n_idx)), choice]
        best_fixed = int(np.argmax(R.mean(axis=0)))                     # meilleure stratégie fixe, choisie APRÈS coup
        sel = Pp.max(axis=1) > 0                                       # n'entrer que si le gain attendu est positif
        stats.update({
            "n_test": int(len(n_idx)), "auto_moyen": float(auto.mean()),
            "meilleure_fixe": pols[best_fixed], "meilleure_fixe_moyen": float(R[:, best_fixed].mean()),
            "part_ev_positive": float(sel.mean()),
            "auto_si_ev_positive": float(auto[sel].mean()) if sel.any() else None,
            "fixe_si_ev_positive": float(R[sel, best_fixed].mean()) if sel.any() else None,
        })
    models = EVModels(feats=d.feats, boosters=_fit(d, policies, clip, min_rows, rounds), stats=stats)
    if win_policy:
        v = win_validation(old, new, win_policy, win_rounds, win_tops)
        ref_oos = v.pop("_win_ref_oos", None)
        stats.update(v)
        fit = _fit_win(d, win_policy, win_rounds)
        if fit is not None:
            models.win_policy = win_policy
            models.win_booster = fit[0]
            models.win_ref = ref_oos if ref_oos is not None else fit[1]
    return models


ROWS_SQL = """SELECT extract(epoch from o.ts)::float8 ts, d.features, o.pnl, d.mint FROM outcomes o JOIN decisions d USING (decision_id)
   WHERE o.ts > now() - make_interval(hours => $1) AND d.ts > now() - make_interval(hours => $1 + 24)
     AND NOT d.blocked AND (d.features->>'unique_buyers')::float >= $2
     AND (d.point ~ '^[0-9]+$' OR d.point = 'migration')"""


async def load_rows(db, hours: float, min_buyers: float) -> list[tuple[dict, dict, float]]:
    """Décisions alertables et gains réels de chaque stratégie sur `hours` heures. Requête lourde exécutée
    SANS travailleurs parallèles (fetch_serial) : en parallèle, elle saturait la mémoire partagée du
    conteneur PostgreSQL (« No space left on device ») et la validation échouait à chaque fois."""
    rows = []
    for r in await db.fetch_serial(ROWS_SQL, hours, min_buyers):
        f = r["features"] if isinstance(r["features"], dict) else json.loads(r["features"])
        p = r["pnl"] if isinstance(r["pnl"], dict) else json.loads(r["pnl"])
        rows.append((f, {k: float(v) for k, v in p.items()}, r["ts"]))
    return rows


async def load_data(db, hours: float, min_buyers: float) -> Data:
    """Même lecture que load_rows, par paquets (curseur), convertie au fil de l'eau en matrices compactes."""
    feats: list[str] | None = None
    pols: list[str] | None = None
    parts: list[Data] = []
    async for chunk in db.iter_serial(ROWS_SQL, hours, min_buyers):
        rows = []
        for r in chunk:
            f = r["features"] if isinstance(r["features"], dict) else json.loads(r["features"])
            p = r["pnl"] if isinstance(r["pnl"], dict) else json.loads(r["pnl"])
            rows.append((f, {k: float(v) for k, v in p.items()}, r["ts"], r["mint"]))
        if feats is None:
            feats = sorted({k for x, *_ in rows for k, v in x.items()
                            if isinstance(v, (int, float)) and not k.startswith("cx_")})
            pols = sorted({k for _, p, *_ in rows for k in p})
        parts.append(Data.from_rows(rows, feats, pols))
    if not parts:
        return Data(np.zeros((0, 0), np.float32), [], np.zeros((0, 0), np.float32), [], np.zeros(0), np.zeros(0, object))
    return Data(np.vstack([d.X for d in parts]), feats or [], np.vstack([d.P for d in parts]), pols or [],
                np.concatenate([d.ts for d in parts]), np.concatenate([d.mint for d in parts]))


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
    w = (stats.get("win_top") or {}).get("0.01") or {}
    if w.get("moyen") is not None:
        s += (f" ; tokens « gagnants probables » (top 1 %, {w['n']} tokens jamais vus) : {w['moyen']:+.1%} par trade, "
              f"{w['gagnants']:.0%} gagnants")
    return s.replace(".", ",")


def should_activate(history: list[dict], margin: float = 0.01, min_test: int = 3000, runs: int = 2) -> bool:
    """Le choix par token ne prend la main que s'il bat la meilleure stratégie fixe (sur des tokens jamais
    vus) d'au moins `margin` par trade, `runs` validations de suite."""
    last = history[-runs:]
    return len(last) == runs and all(
        h.get("auto_moyen") is not None and h.get("n_test", 0) >= min_test
        and h["auto_moyen"] > h["meilleure_fixe_moyen"] + margin for h in last)


def should_activate_win(history: list[dict], top: str = "0.01", min_n: int = 50, runs: int = 2) -> bool:
    """La note « gagnant probable » ne prend la main que si son top (1 % par défaut) est GAGNANT sur des tokens
    jamais vus, `runs` validations de suite."""
    last = history[-runs:]
    if len(last) < runs:
        return False
    for h in last:
        w = (h.get("win_top") or {}).get(top) or {}
        if w.get("moyen") is None or w.get("n", 0) < min_n or w["moyen"] <= 0:
            return False
    return True
