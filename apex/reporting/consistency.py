"""Vérification automatique de COHÉRENCE des résultats (toutes les 3 h, par le superviseur).

Refait automatiquement les contrôles manuels qui ont trouvé des bugs le 09/10 :
  1. chaque trade d'entraînement terminé : résultat recalculé à partir de ses ventes = résultat affiché ;
  2. pas de ventes en double (trade rejoué après un redémarrage) ;
  3. chaque vente correspond à un vrai prix du marché (± 2 s, ± 3 %) ;
  4. le simulateur d'ordres et l'entraînement donnent des résultats proches sur les mêmes trades ;
  5. aucune position bloquée ouverte depuis plus de 26 h ;
  6. le prix d'achat de chaque décision correspond au marché du moment (± 50 %) — un vrai x100 n'est pas une anomalie ;
  7. un achat raté ne compte que les frais (pas une grosse perte).
Sortie : {contrôle: {ok, n, détail}}, liste de problèmes en langage simple, résumé.
"""
from __future__ import annotations

import json
from typing import Any

MATCH_SQL = """
SELECT EXISTS (
  SELECT 1 FROM (
    SELECT v_sol / v_tokens p FROM trades WHERE mint = $1 AND ts BETWEEN to_timestamp($2 - 2) AND to_timestamp($2 + 2)
    UNION ALL
    SELECT price p FROM price_ticks WHERE mint = $1 AND ts BETWEEN to_timestamp($2 - 2) AND to_timestamp($2 + 2)
  ) z WHERE abs(p / $3 - 1) <= 0.03)
"""


def recompute(fills: list[dict], fee: float, sale_cost: float) -> tuple[float | None, bool]:
    """Résultat d'un trade à partir de ses ventes (jusqu'à la vente complète). Retourne (pnl, ventes en double)."""
    rem, real = 1.0, 0.0
    for i, f in enumerate(fills):
        if rem <= 1e-9:
            return real - 1, True
        fr = min(float(f.get("fraction", 0.0)), rem)
        real += fr * float(f.get("multiple", 0.0)) * (1 - fee) - sale_cost
        rem -= fr
    return (real - 1 if rem <= 1e-9 else None), False


def summarize(checks: dict[str, dict]) -> tuple[list[str], str]:
    problems = []
    c = checks
    if not c["calcul"]["ok"]:
        problems.append(f"{c['calcul']['n']} trade(s) d'entraînement affichent un résultat différent de leurs ventes réelles")
    if not c["doublons"]["ok"]:
        problems.append(f"{c['doublons']['n']} trade(s) vendus deux fois (rejoués après un redémarrage)")
    if not c["prix_ventes"]["ok"]:
        problems.append(f"{c['prix_ventes']['part']:.0%} des ventes ne correspondent à aucun vrai prix du marché")
    if not c["simulateur"]["ok"]:
        problems.append(f"le simulateur d'ordres s'écarte fortement de l'entraînement sur {c['simulateur']['part']:.0%} des trades")
    if not c["bloquees"]["ok"]:
        problems.append(f"{c['bloquees']['n']} position(s) bloquée(s) ouverte(s) depuis plus de 26 h")
    if not c["impossibles"]["ok"]:
        problems.append(f"{c['impossibles']['n']} décision(s) avec un prix d'achat très éloigné du marché du moment")
    if not c["achats_rates"]["ok"]:
        problems.append(f"{c['achats_rates']['n']} achat(s) raté(s) comptés comme une grosse perte")
    resume = ("✅ Résultats cohérents : chaque trade recalculé, ventes au vrai prix du marché, simulateur aligné"
              if not problems else "⚠️ Incohérences : " + " ; ".join(problems))
    return problems, resume


async def run(db: Any, hours: int = 24) -> dict:
    checks: dict[str, dict] = {}
    rows = await db.fetch(
        """SELECT decision_id, mint, entry_price, pnl, fills, state FROM paper_positions
           WHERE status = 'closed' AND closed_at > now() - make_interval(hours => $1)""", hours)
    bad, doubles, fills_n, unmatched, examples = [], [], 0, 0, []
    for r in rows:
        fills = r["fills"] if isinstance(r["fills"], list) else json.loads(r["fills"] or "[]")
        st = r["state"] if isinstance(r["state"], dict) else json.loads(r["state"] or "{}")
        pnl, dbl = recompute(fills, float(st.get("fee", 0.02)), float(st.get("sale_cost", 0.0)))
        if dbl:
            doubles.append(r["decision_id"])
        if pnl is not None and r["pnl"] is not None and abs(pnl - float(r["pnl"])) > 0.02:
            bad.append(r["decision_id"])
        for f in fills:
            if f.get("reason") == "fin de suivi" or not f.get("multiple"):
                continue
            fills_n += 1
            price = float(f["multiple"]) * float(r["entry_price"])
            if not await db.fetchval(MATCH_SQL, r["mint"], float(f["t"]), price):
                unmatched += 1
                if len(examples) < 3:
                    examples.append(f"{r['decision_id'][:10]} {f.get('kind')} x{f['multiple']}")
    checks["calcul"] = {"ok": not bad, "n": len(bad), "exemples": bad[:5], "verifies": len(rows)}
    checks["doublons"] = {"ok": not doubles, "n": len(doubles), "exemples": doubles[:5]}
    part = unmatched / fills_n if fills_n else 0.0
    checks["prix_ventes"] = {"ok": fills_n < 10 or part <= 0.2, "n": unmatched, "ventes": fills_n, "part": part,
                             "exemples": examples}
    pairs = await db.fetch(
        """SELECT e.pnl ep, p.pnl pp FROM exec_positions e JOIN paper_positions p USING (decision_id)
           WHERE e.status = 'closed' AND p.status = 'closed' AND e.closed_at > now() - make_interval(hours => $1)""", hours)
    far = sum(1 for x in pairs if x["ep"] is not None and x["pp"] is not None and abs(x["ep"] - x["pp"]) > 0.25)
    sp = far / len(pairs) if pairs else 0.0
    checks["simulateur"] = {"ok": len(pairs) < 5 or sp <= 0.3, "n": far, "paires": len(pairs), "part": sp}
    stuck = await db.fetchval(
        """SELECT (SELECT count(*) FROM exec_positions WHERE status IN ('open','pending') AND opened_at < now() - interval '26 hours')
                + (SELECT count(*) FROM paper_positions WHERE status = 'open' AND opened_at < now() - interval '26 hours')""")
    checks["bloquees"] = {"ok": stuck == 0, "n": stuck}
    # prix d'entrée vs marché au moment de la décision (échantillon de décisions alertables) : un prix d'entrée
    # 2× trop bas ou trop haut est la signature des bugs passés (prix périmé, mauvaise heure) — un vrai x100
    # n'est PAS une anomalie (c'est ce que le bot cherche)
    sample = await db.fetch(
        """SELECT d.mint, extract(epoch from d.ts)::float8 t, d.entry_price FROM decisions d
           WHERE d.ts > now() - make_interval(hours => $1) AND NOT d.blocked
             AND (d.features->>'unique_buyers')::float >= 10 ORDER BY random() LIMIT 300""", hours)
    off, checked = [], 0
    for r in sample:
        m = await db.fetchval(
            """SELECT p FROM (SELECT ts, v_sol / v_tokens p FROM trades WHERE mint = $1 AND ts BETWEEN to_timestamp($2 - 30) AND to_timestamp($2)
                              UNION ALL SELECT ts, price FROM price_ticks WHERE mint = $1 AND ts BETWEEN to_timestamp($2 - 30) AND to_timestamp($2)) z
               ORDER BY ts DESC LIMIT 1""", r["mint"], r["t"])
        if m:
            checked += 1
            ratio = r["entry_price"] / m
            if ratio > 2 or ratio < 0.5:
                off.append(f"{r['mint'][:8]} x{ratio:.2f}")
    checks["impossibles"] = {"ok": not checked or len(off) / checked <= 0.02, "n": len(off), "verifies": checked,
                             "exemples": off[:5]}
    rated = await db.fetchval(
        """SELECT count(*) FROM exec_positions WHERE status = 'failed' AND coalesce(tokens_initial, 0) = 0
             AND pnl < -0.05 AND opened_at > now() - make_interval(hours => $1)""", hours)
    checks["achats_rates"] = {"ok": rated == 0, "n": rated}
    problems, resume = summarize(checks)
    return {"heures": hours, "controles": checks, "problemes": problems, "resume": resume, "ok": not problems}
