"""Tests statistiques de la boucle 2 (tendances) et de la boucle 3 (effet des
corrections). Fonctions pures, testées."""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Sequence

import numpy as np
from scipy import stats

PROGRESSION = "PROGRESSION"
PLATEAU = "PLATEAU"
REGRESSION = "REGRESSION"
STABLE = "STABLE"              # pas de tendance significative, pas encore un plateau
INSUFFICIENT = "DONNEES_INSUFFISANTES"

IMPROVEMENT = "AMELIORATION"
NO_EFFECT = "SANS_EFFET"
DEGRADATION = "DEGRADATION"


@dataclass(frozen=True)
class Trend:
    slope: float           # pente de Theil-Sen (unités / heure)
    p_value: float         # test de Mann-Kendall (tau de Kendall)
    n: int
    direction: str         # "down" | "up" | "flat"


def trend(times_h: Sequence[float], values: Sequence[float], alpha: float = 0.05) -> Trend:
    t = np.asarray(times_h, dtype=float)
    v = np.asarray(values, dtype=float)
    mask = np.isfinite(v)
    t, v = t[mask], v[mask]
    if len(v) < 4 or np.ptp(v) == 0:
        return Trend(0.0, 1.0, len(v), "flat")
    tau, p = stats.kendalltau(t, v)
    slope = stats.theilslopes(v, t).slope
    if p is None or math.isnan(p):
        p = 1.0
    direction = "flat"
    if p < alpha:
        direction = "down" if slope < 0 else "up"
    return Trend(float(slope), float(p), len(v), direction)


def classify_curve(tr: Trend, lower_is_better: bool, hours_since_progress: float, plateau_hours: float, min_points: int = 4) -> str:
    if tr.n < min_points:
        return INSUFFICIENT
    good = "down" if lower_is_better else "up"
    bad = "up" if lower_is_better else "down"
    if tr.direction == good:
        return PROGRESSION
    if tr.direction == bad:
        return REGRESSION
    return PLATEAU if hours_since_progress >= plateau_hours else STABLE


def two_proportion_test(k1: int, n1: int, k2: int, n2: int) -> tuple[float, float]:
    """Compare deux taux (avant=1, après=2). Retourne (différence p2-p1, p-value bilatérale)."""
    if n1 == 0 or n2 == 0:
        return 0.0, 1.0
    p1, p2 = k1 / n1, k2 / n2
    p = (k1 + k2) / (n1 + n2)
    se = math.sqrt(p * (1 - p) * (1 / n1 + 1 / n2))
    if se == 0:
        return p2 - p1, 1.0
    z = (p2 - p1) / se
    return p2 - p1, float(2 * (1 - stats.norm.cdf(abs(z))))


def effect_before_after(before: Sequence[float], after: Sequence[float], lower_is_better: bool, alpha: float = 0.05) -> tuple[str, float, float]:
    """Effet d'une correction non-ombre : Mann-Whitney sur la métrique avant/après.
    Retourne (verdict, gain relatif, p-value). gain > 0 = mieux."""
    b, a = np.asarray(before, dtype=float), np.asarray(after, dtype=float)
    if len(b) < 10 or len(a) < 10:
        return NO_EFFECT, 0.0, 1.0
    _, p = stats.mannwhitneyu(b, a, alternative="two-sided")
    mb, ma = float(np.mean(b)), float(np.mean(a))
    gain = (mb - ma) / abs(mb) if mb else 0.0
    if not lower_is_better:
        gain = -gain
    if p < alpha:
        return (IMPROVEMENT if gain > 0 else DEGRADATION), gain, float(p)
    return NO_EFFECT, gain, float(p)


def effect_paired(champion_losses: Sequence[float], shadow_losses: Sequence[float], alpha: float = 0.05) -> tuple[str, float, float]:
    """Effet d'une correction appliquée à une ombre : test de Wilcoxon apparié sur les
    pertes (mêmes exemples). gain > 0 = l'ombre fait mieux que le champion."""
    c, s = np.asarray(champion_losses, dtype=float), np.asarray(shadow_losses, dtype=float)
    n = min(len(c), len(s))
    if n < 30:
        return NO_EFFECT, 0.0, 1.0
    c, s = c[:n], s[:n]
    d = c - s
    if np.all(d == 0):
        return NO_EFFECT, 0.0, 1.0
    _, p = stats.wilcoxon(d)
    gain = float(np.mean(d) / np.mean(c)) if np.mean(c) else 0.0
    if p < alpha:
        return (IMPROVEMENT if gain > 0 else DEGRADATION), gain, float(p)
    return NO_EFFECT, gain, float(p)


# Features qui changent « par construction » entre une heure et une semaine (heure du jour, jour,
# indicateurs de contexte, drapeau d'enrichissement) : leur décalage n'est pas une dérive du marché.
DRIFT_EXCLUDED = {"hour_sin", "hour_cos", "dow", "age_s", "sol_price_usd", "enriched",
                  "mkt_temperature", "mkt_launches_per_h", "mkt_migrations_per_h", "narrative_score"}


def ks_drift(recent: dict[str, list[float]], reference: dict[str, list[float]], alpha: float = 0.01) -> dict[str, float]:
    """KS par feature, correction de Bonferroni. Retourne {feature: statistique} des features ayant dérivé."""
    feats = [f for f in recent if f in reference and f not in DRIFT_EXCLUDED
             and len(recent[f]) >= 30 and len(reference[f]) >= 30]
    if not feats:
        return {}
    thr = alpha / len(feats)
    out = {}
    for f in feats:
        st, p = stats.ks_2samp(recent[f], reference[f])
        if p < thr:
            out[f] = float(st)
    return out
