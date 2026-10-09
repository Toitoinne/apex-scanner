"""Classement des stratégies de sortie, avec garde-fous contre le HASARD.

Avec des dizaines de stratégies, la meilleure en moyenne peut l'être par chance. Pour chacune :
  - résultat moyen par trade (frais compris) avec sa marge d'incertitude (intervalle à 95 %) ;
  - médiane et part de trades gagnants (un seul gros gain ne doit pas tout porter) ;
  - STABILITÉ : résultat sur la 1re moitié de la période vs la 2nde (une vraie bonne stratégie
    reste bonne sur les deux) ;
  - deux populations : toutes les décisions alertables, et les 10 % que le bot juge les meilleures
    (celles qui ressemblent à ses vraies alertes).
Verdict « solide » seulement si la stratégie reste dans le haut du classement sur les deux moitiés et
que sa marge basse dépasse la moyenne de la stratégie de référence.
"""
from __future__ import annotations

import math
from typing import Any

from ..trading.exits import build_panel

SQL = """
WITH o AS (
  SELECT o.decision_id, o.ts, j.k pol, j.v::float pnl
  FROM outcomes o JOIN decisions d USING (decision_id), jsonb_each_text(o.pnl) AS j(k, v)
  WHERE o.ts > now() - make_interval(hours => $1) AND NOT d.blocked
    AND (d.features->>'unique_buyers')::float >= $2 AND d.ts > now() - make_interval(hours => $1 + 24)
    AND (d.point ~ '^[0-9]+$' OR d.point = 'migration')),          -- terrains en observation exclus
mid AS (SELECT min(ts) + (max(ts) - min(ts)) / 2 m FROM o),
top AS (
  SELECT p.decision_id FROM predictions p
  WHERE p.is_champion AND p.horizon = $3 AND p.ts > now() - make_interval(hours => $1 + 24)
    AND p.p_cal >= (SELECT percentile_cont(0.9) WITHIN GROUP (ORDER BY p_cal) FROM predictions
                    WHERE is_champion AND horizon = $3 AND ts > now() - make_interval(hours => $1 + 24)))
SELECT pol, count(*) n, avg(least(pnl, 20)) mean, stddev_samp(least(pnl, 20)) sd,
       percentile_cont(0.5) WITHIN GROUP (ORDER BY pnl) med, avg((pnl > 0)::int)::float8 win,
       avg(least(pnl, 20)) FILTER (WHERE o.ts < (SELECT m FROM mid)) h1,
       avg(least(pnl, 20)) FILTER (WHERE o.ts >= (SELECT m FROM mid)) h2
FROM o WHERE NOT $4 OR o.decision_id IN (SELECT decision_id FROM top) GROUP BY 1
"""


SQL_POINT = """
WITH o AS (
  SELECT d.point, o.ts, j.k pol, j.v::float pnl
  FROM outcomes o JOIN decisions d USING (decision_id), jsonb_each_text(o.pnl) AS j(k, v)
  WHERE o.ts > now() - make_interval(hours => $1) AND d.point = ANY($3) AND NOT d.blocked
    AND (d.features->>'unique_buyers')::float >= $2),
mid AS (SELECT point, min(ts) + (max(ts) - min(ts)) / 2 m FROM o GROUP BY 1)
SELECT o.point, pol, count(*) n, avg(least(pnl, 20)) mean, stddev_samp(least(pnl, 20)) sd,
       percentile_cont(0.5) WITHIN GROUP (ORDER BY pnl) med, avg((pnl > 0)::int)::float8 win,
       avg(least(pnl, 20)) FILTER (WHERE o.ts < mid.m) h1, avg(least(pnl, 20)) FILTER (WHERE o.ts >= mid.m) h2
FROM o JOIN mid USING (point) GROUP BY 1, 2
"""

TERRAIN = {"mig60": "1 min après la migration", "mig300": "5 min après la migration", "mig900": "15 min après la migration",
           "vague2": "deuxième vague (token de plus d'1 h qui repart)"}


def rank(rows: list[dict], reference: str = "TP2_SL50", top_k: int = 5) -> list[dict]:
    """rows : {pol, n, mean, sd, med, win, h1, h2} → classement avec marge et verdict de solidité."""
    out = []
    for r in rows:
        n = r["n"] or 0
        se = (r["sd"] or 0) / math.sqrt(n) if n > 1 else float("inf")
        out.append({**r, "lo": r["mean"] - 1.96 * se, "hi": r["mean"] + 1.96 * se})
    out.sort(key=lambda r: -r["mean"])
    by_h1 = sorted((r for r in out if r.get("h1") is not None), key=lambda r: -r["h1"])
    by_h2 = sorted((r for r in out if r.get("h2") is not None), key=lambda r: -r["h2"])
    top1 = {r["pol"] for r in by_h1[:top_k]}
    top2 = {r["pol"] for r in by_h2[:top_k]}
    ref = next((r for r in out if r["pol"] == reference), None)
    for i, r in enumerate(out):
        r["rang"] = i + 1
        stable = r["pol"] in top1 and r["pol"] in top2
        beats_ref = ref is None or r["pol"] == reference or r["lo"] > ref["mean"]
        if r["hi"] < 0:
            r["verdict"] = "perd"            # même dans le meilleur cas, elle perd de l'argent
        elif stable and beats_ref and r["n"] >= 200 and r["lo"] > 0:
            r["verdict"] = "solide"          # gagne, de façon stable, au-delà du hasard
        else:
            r["verdict"] = "pas prouvée"     # peut-être bonne, peut-être de la chance : il faut plus de recul
    return out


async def build(db: Any, cfg: Any, hours: int = 48, evolved: dict | None = None) -> dict:
    desc = {k: v.get("description", k) for k, v in build_panel(cfg.data.get("exits", {}), evolved).items()}
    res: dict = {"heures": hours}
    for key, only_top in (("toutes", False), ("selection", True)):
        rows = [dict(r) for r in await db.fetch(SQL, hours, cfg.get("bandit.min_buyers_to_alert", 10),
                                                 cfg["labels"]["primary"], only_top)]
        ranked = rank([r for r in rows if r["n"]])
        for r in ranked:
            r["description"] = desc.get(r["pol"], r["pol"])
        res[key] = ranked
    # terrains en observation (après migration, 2e vague) : meilleures stratégies, toutes décisions
    terr: dict[str, list] = {}
    obs_points = [f"mig{s}" for s in cfg.get("observe.after_migration_s") or [60, 300, 900]] + ["vague2"]
    rows = [dict(r) for r in await db.fetch(SQL_POINT, hours, cfg.get("bandit.min_buyers_to_alert", 10), obs_points)]
    for pt in obs_points:
        rk = rank([r for r in rows if r["point"] == pt and r["n"]])
        for r in rk:
            r["description"] = desc.get(r["pol"], r["pol"])
        if rk:
            terr[pt] = rk[:5]
    res["terrains"] = terr
    return res


def fr(x: float) -> str:
    return f"{float(x):+.1%}".replace(".", ",")


def render(board: dict, top: int = 8) -> str:
    """Message Telegram simple (commande /sorties)."""
    badges = {"solide": "✅", "pas prouvée": "⚪", "perd": "🔻"}

    def line(r: dict) -> str:
        return (f"{badges[r['verdict']]} {r['rang']}. {r.get('description', r['pol'])}\n"
                f"     {fr(r['mean'])} par trade en moyenne (entre {fr(r['lo'])} et {fr(r['hi'])}), "
                f"{float(r['win']):.0%} gagnants, {r['n']} cas")
    sel = board.get("selection") or []
    allr = board.get("toutes") or []
    lines = [f"<b>Stratégies de sortie</b> — {len(allr)} testées sur {board.get('heures', 48)} h (simulation, frais compris)", ""]
    if sel:
        lines += ["<b>Sur les tokens que le bot juge les meilleurs</b> (ceux qui ressemblent à ses alertes) :"]
        lines += [line(r) for r in sel[:top]]
        n_ok = sum(r["verdict"] == "solide" for r in sel)
        lines += ["", f"{n_ok} stratégie(s) gagnent de façon prouvée pour l'instant." if n_ok else
                  "Aucune ne gagne encore de façon prouvée : le bot doit d'abord mieux choisir ses tokens, "
                  "et il faut plus de recul."]
    terr = board.get("terrains") or {}
    if terr:
        lines += ["", "<b>Nouveaux terrains (en observation, aucune alerte)</b> :"]
        for pt, rk in terr.items():
            b = rk[0]
            lines.append(f"• {TERRAIN.get(pt, pt)} : meilleure façon de revendre {fr(b['mean'])} par trade "
                         f"({badges[b['verdict']]}, {b['n']} cas) — {b.get('description', b['pol'])}")
    lines += ["", "✅ gagne de façon prouvée · ⚪ pas encore prouvée (peut être de la chance) · 🔻 perd de l'argent",
              "La marge « entre … et … » montre l'incertitude : plus il y a de cas, plus elle se resserre.",
              "Le bot choisit seul parmi elles, et ce classement se met à jour toutes les heures."]
    return "\n".join(lines)
