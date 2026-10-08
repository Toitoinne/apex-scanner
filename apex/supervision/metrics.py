"""Courbes suivies en continu (section 10.1), calculées depuis la base."""
from __future__ import annotations

import random
from typing import Any

from ..db import DB
from ..learning.calibration import expected_calibration_error, reliability_table

# courbes « plus bas = mieux » sauf mention contraire
HIGHER_IS_BETTER = {"alert_precision", "alert_median_pnl", "learn_volume"}


class Metrics:
    def __init__(self, db: DB, primary: str):
        self.db, self.primary = db, primary

    async def window_values(self, seconds: int, win: str) -> list[tuple[str, str, float, int]]:
        """(curve, model_id, value, n) pour une fenêtre glissante."""
        out: list[tuple[str, str, float, int]] = []
        iv = f"{seconds} seconds"
        # logloss par modèle et horizon
        for r in await self.db.fetch(
            f"""SELECT model_id, horizon, avg(logloss) v, count(*) n FROM evaluations
                WHERE ts > now() - interval '{iv}' GROUP BY model_id, horizon"""):
            out.append((f"logloss:{r['horizon']}", r["model_id"], float(r["v"]), r["n"]))
        # taux d'erreur global, par type, coût (champion, horizon principal)
        r = await self.db.fetchrow(
            f"""SELECT count(*) n, count(error_type) e, coalesce(sum(cost),0) c FROM evaluations
                WHERE is_champion AND horizon=$1 AND ts > now() - interval '{iv}'""", self.primary)
        n = r["n"] or 0
        if n:
            out.append(("error_rate", "champion", r["e"] / n, n))
            out.append(("error_cost", "champion", float(r["c"]), n))
            for t in await self.db.fetch(
                f"""SELECT error_type, count(*) k FROM evaluations WHERE is_champion AND horizon=$1
                    AND error_type IS NOT NULL AND ts > now() - interval '{iv}' GROUP BY error_type""", self.primary):
                out.append((f"error_rate:{t['error_type']}", "champion", t["k"] / n, n))
        # alertes
        a = await self.db.fetchrow(
            f"""SELECT count(*) n, avg((result_60->>'y')::int) prec,
                       percentile_cont(0.5) WITHIN GROUP (ORDER BY sim_pnl) med,
                       avg(CASE WHEN result_60->>'error_type'='RUG_ALERTE' THEN 1 ELSE 0 END) rug
                FROM alerts WHERE result_60 IS NOT NULL AND ts > now() - interval '{iv}'""")
        if a and a["n"]:
            out += [("alert_precision", "champion", float(a["prec"] or 0), a["n"]),
                    ("alert_median_pnl", "champion", float(a["med"] or 0), a["n"]),
                    ("alert_rug_rate", "champion", float(a["rug"] or 0), a["n"])]
        # calibration
        rows = await self.db.fetch(
            f"""SELECT p, y FROM evaluations WHERE is_champion AND horizon=$1 AND ts > now() - interval '{iv}'
                ORDER BY ts DESC LIMIT 20000""", self.primary)
        if len(rows) >= 100:
            out.append(("ece", "champion", expected_calibration_error([x["p"] for x in rows], [x["y"] for x in rows]), len(rows)))
        # volume d'apprentissage
        vol = await self.db.fetchval(f"SELECT count(*) FROM labels WHERE ts > now() - interval '{iv}'")
        out.append(("learn_volume", "all", float(vol) / max(1, seconds / 3600), vol))
        return out

    async def hourly_series(self, curve: str, lookback_h: int, min_n: int) -> tuple[list[float], list[float]]:
        """Série horaire d'une courbe (pour le test de tendance)."""
        if curve == "error_rate" or curve.startswith("error_rate:"):
            cond = "error_type IS NOT NULL" if curve == "error_rate" else "error_type = $3"
            args: list[Any] = [self.primary, lookback_h] + ([curve.split(":", 1)[1]] if ":" in curve else [])
            rows = await self.db.fetch(
                f"""SELECT extract(epoch from time_bucket('1 hour', ts))/3600 AS h, count(*) n,
                           count(*) FILTER (WHERE {cond}) k
                    FROM evaluations WHERE is_champion AND horizon=$1 AND ts > now() - make_interval(hours => $2)
                    GROUP BY 1 ORDER BY 1""", *args)
            pts = [(r["h"], r["k"] / r["n"]) for r in rows if r["n"] >= min_n]
        elif curve == "error_cost":
            rows = await self.db.fetch(
                """SELECT extract(epoch from time_bucket('1 hour', ts))/3600 AS h, count(*) n, coalesce(sum(cost),0)/count(*) v
                   FROM evaluations WHERE is_champion AND horizon=$1 AND ts > now() - make_interval(hours => $2)
                   GROUP BY 1 ORDER BY 1""", self.primary, lookback_h)
            pts = [(r["h"], float(r["v"])) for r in rows if r["n"] >= min_n]
        elif curve.startswith("logloss"):
            rows = await self.db.fetch(
                """SELECT extract(epoch from time_bucket('1 hour', ts))/3600 AS h, count(*) n, avg(logloss) v
                   FROM evaluations WHERE is_champion AND horizon=$1 AND ts > now() - make_interval(hours => $2)
                   GROUP BY 1 ORDER BY 1""", curve.split(":")[1] if ":" in curve else self.primary, lookback_h)
            pts = [(r["h"], float(r["v"])) for r in rows if r["n"] >= min_n]
        elif curve == "alert_precision":
            rows = await self.db.fetch(
                """SELECT extract(epoch from time_bucket('6 hours', ts))/3600 AS h, count(*) n, avg((result_60->>'y')::int) v
                   FROM alerts WHERE result_60 IS NOT NULL AND ts > now() - make_interval(hours => $1) GROUP BY 1 ORDER BY 1""",
                lookback_h * 4)
            pts = [(r["h"], float(r["v"])) for r in rows if r["n"] >= 3]
        else:
            rows = await self.db.fetch(
                """SELECT extract(epoch from ts)/3600 AS h, value FROM curve_points
                   WHERE curve=$1 AND win='1h' AND model_id='champion' AND ts > now() - make_interval(hours => $2) ORDER BY ts""",
                curve, lookback_h)
            pts = [(r["h"], float(r["value"])) for r in rows]
        return [p[0] for p in pts], [p[1] for p in pts]

    async def paired_losses(self, champion_id: str, shadow_id: str, horizon: str, since_ts) -> tuple[list[float], list[float]]:
        rows = await self.db.fetch(
            """SELECT c.logloss cl, s.logloss sl FROM evaluations c JOIN evaluations s
               ON s.decision_id = c.decision_id AND s.horizon = c.horizon AND s.model_id = $2
               WHERE c.model_id = $1 AND c.horizon = $3 AND c.ts > $4 AND s.ts > $4 LIMIT 50000""",
            champion_id, shadow_id, horizon, since_ts)
        return [r["cl"] for r in rows], [r["sl"] for r in rows]

    async def champion_error_indicators(self, start, end) -> list[float]:
        rows = await self.db.fetch(
            """SELECT (error_type IS NOT NULL)::int e FROM evaluations
               WHERE is_champion AND horizon=$1 AND ts > $2 AND ts <= $3""", self.primary, start, end)
        return [float(r["e"]) for r in rows]

    async def champion_losses(self, start, end) -> list[float]:
        rows = await self.db.fetch(
            "SELECT logloss FROM evaluations WHERE is_champion AND horizon=$1 AND ts > $2 AND ts <= $3",
            self.primary, start, end)
        return [float(r["logloss"]) for r in rows]

    async def calibration(self, hours: int = 24) -> dict:
        rows = await self.db.fetch(
            """SELECT p, y FROM evaluations WHERE is_champion AND horizon=$1 AND ts > now() - make_interval(hours => $2)
               ORDER BY ts DESC LIMIT 20000""", self.primary, hours)
        ps, ys = [r["p"] for r in rows], [r["y"] for r in rows]
        return {"ece": expected_calibration_error(ps, ys), "table": reliability_table(ps, ys), "n": len(rows)}

    async def feature_samples(self, start_sql: str, end_sql: str, limit: int) -> dict[str, list[float]]:
        rows = await self.db.fetch(
            f"""SELECT features FROM decisions TABLESAMPLE SYSTEM (2)
                WHERE ts > now() - interval '{start_sql}' AND ts <= now() - interval '{end_sql}'
                LIMIT {int(limit)}""")
        out: dict[str, list[float]] = {}
        for r in rows:
            for k, v in r["features"].items():
                if isinstance(v, (int, float)):
                    out.setdefault(k, []).append(float(v))
        return out

    async def costly_errors(self, hours: int, limit: int) -> list[dict]:
        rows = await self.db.fetch(
            """SELECT ts, mint, point, error_type, model_id, p, alerted, cost, features, market, outcome FROM errors
               WHERE ts > now() - make_interval(hours => $1) ORDER BY cost DESC NULLS LAST LIMIT $2""", hours, limit)
        return [dict(r) for r in rows]

    async def other_errors(self, hours: int, limit: int) -> list[dict]:
        rows = await self.db.fetch(
            """SELECT ts, mint, point, model_id, p, alerted, cost, features, market, outcome FROM errors
               WHERE error_type='AUTRE' AND ts > now() - make_interval(hours => $1) ORDER BY ts DESC LIMIT $2""", hours, limit)
        return [dict(r) for r in rows]


def subsample(xs: list, k: int) -> list:
    return xs if len(xs) <= k else random.sample(xs, k)
