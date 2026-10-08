"""Boucle 2 — détection de l'état d'apprentissage (10.2) et diagnostic (10.3)."""
from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass, field
from typing import Any

from ..db import DB
from .metrics import HIGHER_IS_BETTER, Metrics
from .stats import (INSUFFICIENT, PLATEAU, PROGRESSION, REGRESSION, STABLE, classify_curve, ks_drift, trend)

log = logging.getLogger(__name__)

REGRESSION_TYPE = "REGRESSION_TYPE"
DECALIBRATION = "DECALIBRATION"
DRIFT = "DERIVE_MARCHE"
THRESHOLD_LOOSE = "SEUIL_PERMISSIF"
THRESHOLD_STRICT = "SEUIL_STRICT"
ABNORMAL = {PLATEAU, REGRESSION, REGRESSION_TYPE, DECALIBRATION, DRIFT, THRESHOLD_LOOSE, THRESHOLD_STRICT}


@dataclass
class Detected:
    state: str
    curve: str
    slope: float = 0.0
    p_value: float = 1.0
    details: dict[str, Any] = field(default_factory=dict)

    @property
    def problem_key(self) -> str:
        return f"{self.state}:{self.curve}"


class Detector:
    def __init__(self, db: DB, metrics: Metrics, scfg: dict, errors_cfg: dict, bandit_cfg: dict):
        self.db, self.m, self.c = db, metrics, scfg
        self.bandit_cfg = bandit_cfg

    async def _hours_since_progress(self, curve: str) -> float:
        last = await self.db.fetchval(
            "SELECT max(ts) FROM learning_states WHERE curve=$1 AND state=$2", curve, PROGRESSION)
        if last is None:
            last = await self.db.fetchval("SELECT min(ts) FROM evaluations")
        if last is None:
            return 0.0
        return (dt.datetime.now(dt.timezone.utc) - last).total_seconds() / 3600

    async def curve_state(self, curve: str) -> Detected:
        c = self.c
        t, v = await self.m.hourly_series(curve, c["trend_lookback_h"], c["min_events_per_bucket"])
        tr = trend(t, v, c["trend_alpha"])
        hsp = await self._hours_since_progress(curve)
        base = curve.split(":")[0]
        state = classify_curve(tr, base not in HIGHER_IS_BETTER, hsp, c["plateau_hours"])
        if state == PLATEAU and not await self.db.fetchval("SELECT count(*) FROM reference_curves"):
            state = STABLE      # avant les 24 h de référence, une absence de progrès n'est pas encore un plateau
        return Detected(state, curve, tr.slope, tr.p_value, {"n_points": tr.n, "hours_since_progress": round(hsp, 1),
                                                            "last": v[-1] if v else None})

    async def detect(self, primary: str) -> list[Detected]:
        c = self.c
        out: list[Detected] = []
        main_curves = ["error_rate", "error_cost", f"logloss:{primary}", "alert_precision"]
        types = [r["error_type"] for r in await self.db.fetch(
            "SELECT DISTINCT error_type FROM evaluations WHERE error_type IS NOT NULL AND ts > now() - interval '24 hours'")]
        states: dict[str, Detected] = {}
        for curve in main_curves + [f"error_rate:{t}" for t in types]:
            d = await self.curve_state(curve)
            states[curve] = d
        glob = states["error_rate"]
        for curve, d in states.items():
            if curve.startswith("error_rate:"):
                if d.state == REGRESSION and glob.state != REGRESSION:
                    out.append(Detected(REGRESSION_TYPE, curve, d.slope, d.p_value, d.details))
                elif d.state in (PROGRESSION, STABLE, INSUFFICIENT, PLATEAU):
                    out.append(d)       # journalisé, mais seuls les états anormaux déclenchent des corrections
            else:
                out.append(d)
        # décalibration
        cal = await self.m.calibration(24)
        if cal["n"] >= 500 and cal["ece"] > c["ece_threshold"]:
            out.append(Detected(DECALIBRATION, "ece", details={"ece": cal["ece"], "n": cal["n"]}))
        # dérive de marché
        oldest = await self.db.fetchval("SELECT min(ts) FROM decisions")
        enough_history = oldest is not None and (dt.datetime.now(dt.timezone.utc) - oldest).total_seconds() > 30 * 3600
        recent = await self.m.feature_samples("1 hour", "0 seconds", 2000) if enough_history else {}
        ref = await self.m.feature_samples("7 days", "6 hours", 5000) if enough_history else {}
        drifted = ks_drift(recent, ref, c["drift_ks_alpha"])
        n_feats = len([f for f in recent if f in ref])
        if n_feats and len(drifted) / n_feats > c["drift_feature_share"]:
            top = sorted(drifted.items(), key=lambda kv: -kv[1])[:8]
            out.append(Detected(DRIFT, "features", details={"drifted": dict(top), "share": len(drifted) / n_feats}))
        # seuil d'alerte
        a = await self.db.fetchrow(
            """SELECT count(*) n,
                      avg(CASE WHEN result_60->>'error_type'='RUG_ALERTE' THEN 1 ELSE 0 END) rug
               FROM alerts WHERE result_60 IS NOT NULL AND ts > now() - interval '24 hours'""")
        if a and a["n"] >= 8 and (a["rug"] or 0) > 0.3:
            out.append(Detected(THRESHOLD_LOOSE, "alert_rug_rate", details={"rug_rate": float(a["rug"]), "n": a["n"]}))
        missed = await self.db.fetchrow(
            """SELECT count(*) FILTER (WHERE error_type='GAGNANT_MANQUE') k, count(*) n FROM evaluations
               WHERE is_champion AND horizon=$1 AND ts > now() - interval '24 hours'""", primary)
        n_alerts = await self.db.fetchval("SELECT count(*) FROM alerts WHERE ts > now() - interval '24 hours'")
        ref_missed = await self.db.fetchval("SELECT value FROM reference_curves WHERE curve='error_rate:GAGNANT_MANQUE'")
        if missed and missed["n"] >= 500 and n_alerts < self.bandit_cfg["alerts_per_day"]["min"]:
            rate = missed["k"] / missed["n"]
            if ref_missed is None or rate > 1.5 * ref_missed:
                out.append(Detected(THRESHOLD_STRICT, "error_rate:GAGNANT_MANQUE", details={"rate": rate, "alerts_24h": n_alerts}))
        return out

    # ------------------------------------------------------------------
    async def diagnose(self, d: Detected, primary: str) -> tuple[dict, float]:
        """Compare la période dégradée (6 h) à la période saine (6–30 h avant)."""
        causes: dict[str, Any] = {}
        recent = await self.m.feature_samples("6 hours", "0 seconds", 3000)
        healthy = await self.m.feature_samples("30 hours", "6 hours", 3000)
        drifted = ks_drift(recent, healthy, self.c["drift_ks_alpha"])
        causes["features_changed"] = dict(sorted(drifted.items(), key=lambda kv: -kv[1])[:5])
        rows = await self.db.fetch(
            """SELECT error_type,
                      count(*) FILTER (WHERE ts > now() - interval '6 hours')::float / greatest(1, (SELECT count(*) FROM evaluations WHERE is_champion AND horizon=$1 AND ts > now() - interval '6 hours')) AS r_recent,
                      count(*) FILTER (WHERE ts <= now() - interval '6 hours')::float / greatest(1, (SELECT count(*) FROM evaluations WHERE is_champion AND horizon=$1 AND ts > now() - interval '30 hours' AND ts <= now() - interval '6 hours')) AS r_before
               FROM evaluations WHERE is_champion AND horizon=$1 AND error_type IS NOT NULL AND ts > now() - interval '30 hours'
               GROUP BY error_type""", primary)
        deltas = {r["error_type"]: r["r_recent"] - r["r_before"] for r in rows}
        causes["error_types_delta"] = dict(sorted(deltas.items(), key=lambda kv: -kv[1])[:5])
        models = await self.db.fetch(
            """SELECT model_id,
                      avg(logloss) FILTER (WHERE ts > now() - interval '6 hours') recent,
                      avg(logloss) FILTER (WHERE ts <= now() - interval '6 hours') before
               FROM evaluations WHERE horizon=$1 AND ts > now() - interval '30 hours' GROUP BY model_id""", primary)
        mdelta = {r["model_id"]: (r["recent"] or 0) - (r["before"] or 0) for r in models if r["recent"] and r["before"]}
        causes["model_responsible"] = max(mdelta, key=mdelta.get) if mdelta else None
        causes["model_logloss_delta"] = mdelta
        mk = await self.db.fetchrow(
            """SELECT avg(temperature) FILTER (WHERE ts > now() - interval '6 hours') t_recent,
                      avg(temperature) FILTER (WHERE ts <= now() - interval '6 hours') t_before,
                      avg(launches_per_h) FILTER (WHERE ts > now() - interval '6 hours') l_recent,
                      avg(launches_per_h) FILTER (WHERE ts <= now() - interval '6 hours') l_before
               FROM market_context WHERE ts > now() - interval '30 hours'""")
        if mk:
            causes["market"] = {k: (float(mk[k]) if mk[k] is not None else None) for k in mk.keys()}
        recent_corr = await self.db.fetch(
            """SELECT id, action, component, ts FROM corrections WHERE ts > now() - interval '12 hours'
               AND status NOT IN ('rolled_back') ORDER BY ts DESC""")
        causes["recent_corrections"] = [dict(r) for r in recent_corr]
        # confiance : significativité de la tendance × netteté de la cause principale
        strength = 0.0
        if causes["error_types_delta"]:
            strength = max(strength, min(1.0, max(causes["error_types_delta"].values()) * 20))
        if causes["features_changed"]:
            strength = max(strength, max(causes["features_changed"].values()))
        if causes["recent_corrections"] and d.state in (REGRESSION, REGRESSION_TYPE):
            strength = max(strength, 0.7)
        confidence = round((1 - min(1.0, d.p_value)) * 0.5 + strength * 0.5, 3)
        return causes, confidence
