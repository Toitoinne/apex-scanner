"""Rapports automatiques (section 14) : toutes les 6 h + quotidien détaillé."""
from __future__ import annotations

import html
from typing import Any

from .. import bus as B
from ..config import Config
from ..db import DB
from ..errors.classifier import DISPLAY
from ..safety.filters import propose_change

STATE_ICON = {"PROGRESSION": "📈", "PLATEAU": "➖", "REGRESSION": "📉", "REGRESSION_TYPE": "📉",
              "DECALIBRATION": "🎯", "DERIVE_MARCHE": "🌊", "STABLE": "⏸", "DONNEES_INSUFFISANTES": "…",
              "SEUIL_PERMISSIF": "🚧", "SEUIL_STRICT": "🔒"}


def e(x: Any) -> str:
    return html.escape(str(x))


class Reporter:
    def __init__(self, db: DB, bus: B.Bus, cfg: Config, meta: Any):
        self.db, self.bus, self.cfg, self.meta = db, bus, cfg, meta

    async def verdict(self, states: list[dict], corrections: list[dict]) -> str:
        by = {s["curve"]: s["state"] for s in states}
        rolled = [c for c in corrections if c["status"] == "rolled_back"]
        if by.get("error_rate") == "REGRESSION":
            what = ", ".join(f"#{c['id']} {c['action']}" for c in rolled[:3]) or "retour à l'état stable"
            return f"J'ai régressé ; j'ai annulé : {what}."
        if rolled:
            what = ", ".join(f"#{c['id']} {c['action']}" for c in rolled[:3])
            return f"Une de mes corrections a dégradé les résultats : je l'ai annulée automatiquement ({what})."
        plateaus = [k for k, v in by.items() if v in ("PLATEAU", "REGRESSION_TYPE")]
        if plateaus:
            doing = ", ".join(sorted({c["action"] for c in corrections if c["status"] in ("evaluating", "applied")})) or "observation"
            return f"Je stagne sur {', '.join(plateaus[:3])} ; voici ce que je fais : {doing}."
        if by.get("error_rate") == "PROGRESSION" or by.get(f"logloss:{self.cfg['labels']['primary']}") == "PROGRESSION":
            return "Je progresse."
        return "Stable : pas de tendance significative, j'observe."

    async def build(self, hours: int, daily: bool = False) -> str:
        db = self.db
        states = [dict(r) for r in await db.fetch(
            """SELECT DISTINCT ON (curve) curve, state, slope, p_value FROM learning_states
               WHERE ts > now() - interval '1 hour' ORDER BY curve, ts DESC""")]
        corrections = [dict(r) for r in await db.fetch(
            "SELECT id, ts, action, state, problem_key, status, effect FROM corrections WHERE ts > now() - make_interval(hours => $1) ORDER BY ts", hours)]
        evaluated = [dict(r) for r in await db.fetch(
            "SELECT id, action, status, effect FROM corrections WHERE evaluated_at > now() - make_interval(hours => $1)", hours)]
        feats = [dict(r) for r in await db.fetch(
            "SELECT feature_id, name, status, reason FROM claude_features WHERE created_at > now() - make_interval(hours => $1) OR decided_at > now() - make_interval(hours => $1)", hours)]
        costly = [dict(r) for r in await db.fetch(
            "SELECT mint, error_type, point, p, cost FROM errors WHERE ts > now() - make_interval(hours => $1) ORDER BY cost DESC NULLS LAST LIMIT 5", hours)]
        alerts = await db.fetchrow(
            """SELECT count(*) n, count(result_60) resolved, avg((result_60->>'y')::int) prec,
                      percentile_cont(0.5) WITHIN GROUP (ORDER BY sim_pnl) med
               FROM alerts WHERE ts > now() - make_interval(hours => $1)""", hours)
        learner = await self.bus.get_json("apex:learner:state", {}) or {}
        primary = self.cfg["labels"]["primary"]
        champ = learner.get("ensembles", {}).get(primary, {}).get("champion", "?")

        lines = [f"<b>APEX — rapport {'quotidien' if daily else f'{hours} h'}</b>",
                 f"<b>Verdict :</b> {e(await self.verdict(states, corrections))}", ""]
        lines.append(f"Champion {primary} : <code>{e(champ)}</code> · bras d'alerte : <code>{e(learner.get('bandit', {}).get('active'))}</code>")
        if alerts:
            prec = f"{alerts['prec']:.0%}" if alerts["prec"] is not None else "n/d"
            med = f"{alerts['med']:+.0%}" if alerts["med"] is not None else "n/d"
            lines.append(f"Alertes : {alerts['n']} (résolues {alerts['resolved']}) · précision {prec} · PnL médian {med}")
        lines += ["", "<b>État d'apprentissage</b>"]
        for s in states:
            if daily or s["state"] not in ("DONNEES_INSUFFISANTES",):
                lines.append(f"{STATE_ICON.get(s['state'], '•')} {e(s['curve'])} : {e(s['state'])}")
        lines += ["", "<b>Corrections appliquées</b>"]
        lines += [f"#{c['id']} {e(c['action'])} ({e(c['problem_key'])}) → {e(c['status'])}" for c in corrections] or ["aucune"]
        lines += ["", "<b>Effets mesurés</b>"]
        for c in evaluated:
            ef = c["effect"] or {}
            lines.append(f"#{c['id']} {e(c['action'])} : {e(ef.get('verdict'))} (gain {ef.get('gain', 0):+.1%}, p={ef.get('p_value', 1):.3f})")
        if not evaluated:
            lines.append("aucun")
        lines += ["", "<b>Features (Claude)</b>"]
        lines += [f"{e(f['feature_id'])} : {e(f['status'])}{' — ' + e(f['reason']) if f['reason'] else ''}" for f in feats] or ["aucun changement"]
        lines += ["", "<b>Erreurs les plus coûteuses</b>"]
        lines += [f"{e(DISPLAY.get(c['error_type'], c['error_type']))} {e(c['mint'][:8])}… @{e(c['point'])} p={c['p']:.2f} coût {c['cost']:.2f} SOL" for c in costly] or ["aucune"]
        rows = sorted(self.meta.rows(), key=lambda r: -r["n"])[: (15 if daily else 6)]
        lines += ["", "<b>Ce que j'ai appris sur mes corrections</b>"]
        lines += [f"{e(r['state'])}/{e(r['context'])} → {e(r['action'])} : {r['success']}/{r['n']} succès, gain moyen {r['mean_gain'] or 0:+.1%}" for r in rows] or ["pas encore assez de recul"]
        pp = await db.fetchrow(
            """SELECT count(*) n, count(*) FILTER (WHERE pnl > 0) wins, coalesce(sum(pnl_sol), 0) pnl_sol,
                      coalesce(sum(notional_sol), 0) inv FROM paper_positions
               WHERE status='closed' AND closed_at > now() - make_interval(hours => $1)""", hours)
        lines += ["", "<b>Paper trading</b>"]
        if pp and pp["n"]:
            lines.append(f"{pp['n']} positions clôturées · {pp['wins']} gagnantes · PnL {pp['pnl_sol']:+.3f} SOL "
                         f"({(pp['pnl_sol'] / pp['inv']) if pp['inv'] else 0:+.1%} sur les mises)")
        else:
            lines.append("aucune position clôturée sur la période")
        bp = (learner.get("bandit") or {}).get("best_by_policy") or {}
        if bp:
            lines += ["", "<b>Stratégies de sortie (meilleur PnL moyen simulé par alerte)</b>"]
            lines += [f"{e(k)} : {'n/d' if v is None else f'{v:+.0%}'}" for k, v in bp.items()]
        props = await self.safety_proposals(hours)
        if props:
            lines += ["", "<b>Propositions sur les filtres de sécurité (non appliquées)</b>"]
            lines += [f"{e(p['key'])} : {p['current']} → {p['suggested']} ({e(p['evidence'])})" for p in props]
        return "\n".join(lines)

    async def safety_proposals(self, hours: int) -> list[dict]:
        """Le système ne modifie jamais les filtres : il propose, avec preuves."""
        out = []
        cur = self.cfg.safety["bundle_creation_slot_supply_pct"]
        n = await self.db.fetchval(
            """SELECT count(*) FROM errors WHERE error_type IN ('RUG_ALERTE','BUNDLE_RATE') AND ts > now() - make_interval(hours => $1)
               AND (features->>'creation_slot_supply_pct')::float BETWEEN $2 AND $3""", hours, cur * 0.6, cur)
        if n and n >= 5:
            out.append(propose_change("bundle_creation_slot_supply_pct", cur, round(cur * 0.6, 3),
                                      f"{n} rugs/bundles alertés avec une part du slot de création entre {cur * 0.6:.0%} et {cur:.0%}"))
        return out

    async def periodic(self, hours: int, daily: bool = False) -> None:
        text = await self.build(hours, daily)
        await self.bus.publish(B.NOTIFY, {"type": "report", "text": text})
        await self.db.log_event("info", "report", f"rapport {hours} h envoyé")
