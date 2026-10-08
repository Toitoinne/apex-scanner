"""Service SUPERVISOR — boucles 2 et 3 + garde-fous + déclenchement de Claude + rapports.

Cycle toutes les 15 min (< 30 s visé) :
 1. courbes (1 h / 6 h / 24 h / 7 j) → curve_points
 2. détection d'état par courbe → learning_states
 3. garde-fous (plancher de précision, régression forte, pannes)
 4. mesure de l'effet des corrections arrivées à échéance → table d'efficacité,
    promotion / annulation, gel après 3 échecs
 5. nouvelles corrections (une seule majeure à la fois par composant)
 6. snapshot (marqué stable si aucun état de régression)
"""
from __future__ import annotations

import asyncio
import datetime as dt
import logging
import time
import uuid
from typing import Any

from .. import bus as B
from ..config import Config, secrets
from ..db import DB
from ..reporting import bulletin as BU
from ..reporting.reports import Reporter
from .corrections import Plan, build_plan, candidates_for
from .detector import ABNORMAL, Detected, Detector
from .meta import EfficacyTable, verdict_is_failure
from .metrics import Metrics
from .stats import DEGRADATION, IMPROVEMENT, NO_EFFECT, REGRESSION, effect_before_after, effect_paired
from ..trading import readiness as R

log = logging.getLogger("supervisor")
SEVERITY = ["REGRESSION", "SEUIL_PERMISSIF", "REGRESSION_TYPE", "DECALIBRATION", "DERIVE_MARCHE", "SEUIL_STRICT", "PLATEAU"]
UNFREEZE_AFTER_H = 24


class Supervisor:
    def __init__(self, cfg: Config, bus: B.Bus, db: DB, improver: Any = None):
        self.cfg, self.bus, self.db = cfg, bus, db
        self.s = cfg["supervision"]
        self.primary = cfg["labels"]["primary"]
        self.metrics = Metrics(db, self.primary)
        self.detector = Detector(db, self.metrics, self.s, cfg["errors"], cfg["bandit"])
        self.meta = EfficacyTable(explore=self.s["meta_explore"])
        self.improver = improver
        self.reporter = Reporter(db, bus, cfg, self.meta)
        self._last_strong_alert = 0.0
        self._last_outage: dict[str, float] = {}
        self._down: set[str] = set()

    # ------------------------------------------------------------------
    async def send(self, cmd: dict, timeout: float = 120) -> dict:
        cmd = {**cmd, "cmd_id": uuid.uuid4().hex}
        last = await self.bus.r.xrevrange(B.CONTROL_ACK, count=1)
        last_id = last[0][0] if last else b"0-0"
        await self.bus.publish(B.CONTROL, cmd)
        deadline = time.time() + timeout
        while time.time() < deadline:
            resp = await self.bus.r.xread({B.CONTROL_ACK: last_id}, count=50, block=2000)
            for _, entries in resp or []:
                for mid, fields in entries:
                    last_id = mid
                    ack = B.loads(fields[b"d"])
                    if ack.get("cmd_id") == cmd["cmd_id"]:
                        return ack["result"]
        return {"ok": False, "error": "timeout learner"}

    async def learner_state(self) -> dict:
        return await self.bus.get_json("apex:learner:state", {}) or {}

    async def load_meta(self) -> None:
        for r in await self.db.fetch("SELECT * FROM efficacy"):
            c = self.meta.cell(r["state"], r["context"], r["action"])
            c.n, c.success, c.fail, c.gain_sum = r["n"], r["n_success"], r["n_fail"], r["gain_sum"]

    # ------------------------------------------------------------------
    async def record_curves(self) -> dict[str, float]:
        snap: dict[str, float] = {}
        rows = []
        for win, secs in self.s["windows_s"].items():
            for curve, model, value, n in await self.metrics.window_values(secs, win):
                rows.append((curve, model, win, value, n))
                if win == "24h":
                    snap[f"{curve}|{model}"] = value
        lat = await self.bus.r.lrange("apex:label_latency", 0, 999)
        if lat:
            vals = sorted(float(x) for x in lat)
            rows.append(("label_to_update_latency_s", "learner", "1h", vals[int(len(vals) * 0.95)], len(vals)))
        st = await self.learner_state()
        if st.get("decision_latency_ms_p95") is not None:
            rows.append(("decision_latency_ms", "learner", "1h", st["decision_latency_ms_p95"], 0))
        a = await self.db.fetchrow("SELECT percentile_cont(0.95) WITHIN GROUP (ORDER BY (payload->>'latency_s')::float) v, count(*) n FROM alerts WHERE ts > now() - interval '24 hours'")
        if a and a["n"] and a["v"] is not None:
            rows.append(("alert_latency_s", "learner", "24h", float(a["v"]), a["n"]))
        await self.db.executemany(
            "INSERT INTO curve_points (ts, curve, model_id, win, value, n) VALUES (now(),$1,$2,$3,$4,$5)", rows)
        return snap

    async def self_reference(self) -> None:
        """Sans backfill : après N heures de live, les courbes de référence sont fixées sur
        la fenêtre [now-N h, now-N/2 h] (modèle déjà entraîné sur la première moitié)."""
        if await self.db.fetchval("SELECT count(*) FROM reference_curves"):
            return
        n = int(self.s.get("self_reference_after_h", 48))
        first = await self.db.fetchval("SELECT min(ts) FROM evaluations")
        if first is None or (dt.datetime.now(dt.timezone.utc) - first).total_seconds() < n * 3600:
            return
        rows = await self.db.fetch(
            """SELECT coalesce(error_type, '_ok') t, count(*) k, avg(logloss) ll FROM evaluations
               WHERE is_champion AND horizon=$1 AND ts > now() - make_interval(hours => $2)
               AND ts <= now() - make_interval(hours => $3) GROUP BY 1""", self.primary, n, n // 2)
        total = sum(r["k"] for r in rows)
        if total < 500:
            return
        ref = {"error_rate": sum(r["k"] for r in rows if r["t"] != "_ok") / total,
               f"logloss:{self.primary}": sum(r["ll"] * r["k"] for r in rows) / total}
        ref.update({f"error_rate:{r['t']}": r["k"] / total for r in rows if r["t"] != "_ok"})
        for curve, v in ref.items():
            await self.db.execute(
                "INSERT INTO reference_curves (curve, value, n, source) VALUES ($1,$2,$3,'live') ON CONFLICT (curve) DO NOTHING",
                curve, float(v), total)
        await self.db.log_event("info", "reference", f"courbes de référence établies sur le live ({total} évaluations)")

    async def guardrails(self, states: list[Detected]) -> list[str]:
        events = []
        # Plancher de RENTABILITÉ des alertes (paper trading), mesuré sur les positions ouvertes APRÈS la
        # dernière intervention. Un problème d'alertes se corrige sur la POLITIQUE D'ALERTE (plus sélective),
        # jamais en faisant revenir les modèles en arrière : leur qualité est jugée sur leurs propres mesures.
        last_act = float(await self.bus.r.get("apex:last_alert_floor_action") or 0)
        pp = await self.db.fetchrow(
            """SELECT count(*) n, coalesce(sum(pnl_sol), 0) pnl, coalesce(sum(notional_sol), 0) inv FROM paper_positions
               WHERE status='closed' AND opened_at > greatest(now() - interval '48 hours', to_timestamp($1))""", last_act)
        floor = self.s.get("paper_pnl_floor", -0.30)
        if pp and pp["n"] >= 20 and pp["inv"] and pp["pnl"] / pp["inv"] < floor:
            st = await self.learner_state()
            lo, hi = (st.get("bandit") or {}).get("range", [0.0, 1.0])
            new_lo = min(0.7, round(lo + 0.1, 2))
            res = await self.send({"op": "bandit_range", "lo": new_lo, "hi": max(hi, new_lo)})
            await self.bus.r.set("apex:last_alert_floor_action", str(time.time()))
            msg = (f"📉 Paper trading sous le plancher ({pp['pnl'] / pp['inv']:+.0%} sur {pp['n']} positions) : "
                   f"alertes rendues plus sélectives (seuil minimum {new_lo:.0%}). Les modèles ne sont pas touchés.")
            events.append(msg + f" ({'ok' if res.get('ok') else 'échec'})")
            await self.notify_now(msg)
        ref = await self.db.fetchval("SELECT value FROM reference_curves WHERE curve='error_rate'")
        cur = await self.db.fetchval(
            "SELECT value FROM curve_points WHERE curve='error_rate' AND win='1h' ORDER BY ts DESC LIMIT 1")
        if ref and cur and cur > ref * self.s["strong_regression_ratio"] and time.time() - self._last_strong_alert > 6 * 3600:
            self._last_strong_alert = time.time()
            await self.notify_now(
                f"📉 Le bot se trompe plus que d'habitude depuis 1 h ({cur:.1%} d'erreurs contre {ref:.1%} en temps normal). "
                "C'est souvent lié au marché (activité inhabituelle). Il s'ajuste tout seul : rien à faire de ton côté.")
        hb = await self.bus.heartbeats()
        for svc, t in hb.items():
            name = BU.SERVICE_TXT.get(svc, svc)
            if time.time() - t > 120 and time.time() - self._last_outage.get(svc, 0) > 3600:
                self._last_outage[svc] = time.time()
                self._down.add(svc)
                await self.notify_now(
                    f"🔴 Panne : {name} ne répond plus depuis {int((time.time() - t) / 60) or 1} min. "
                    "Le serveur le relance normalement tout seul ; je te préviens dès que ça repart.")
            elif time.time() - t <= 60 and svc in self._down:
                self._down.discard(svc)
                await self.notify_now(f"✅ Réparé : {name} fonctionne de nouveau.")
        # dégel automatique
        for r in await self.db.fetch("SELECT component FROM frozen_components WHERE since < now() - make_interval(hours => $1)", UNFREEZE_AFTER_H):
            await self.db.execute("DELETE FROM frozen_components WHERE component=$1", r["component"])
            events.append(f"dégel automatique du composant {r['component']}")
        return events

    async def learning_health(self) -> list[str]:
        """Preuve continue que le système apprend : chaque cycle vérifie que des labels sont appris,
        que le bandit reçoit des récompenses et que des décisions sont prises. Sinon : alerte immédiate."""
        st = await self.learner_state()
        now = time.time()
        prev = getattr(self, "_health_prev", None)
        problems = []
        if st:
            if now - (st.get("last_decision_ts") or now) > 300:
                problems.append("aucune nouvelle décision depuis plus de 5 min (flux ou calcul des features à l'arrêt)")
            if now - (st.get("last_label_ts") or now) > 600:
                problems.append("aucun nouveau label appris depuis plus de 10 min (boucle 1 à l'arrêt)")
            if prev and now - prev["ts"] >= 3000 and st.get("n_outcomes", 0) <= prev["n_outcomes"]:
                problems.append("le bandit n'a reçu aucune récompense depuis ~1 h (simulations de sortie à l'arrêt)")
            if not prev or now - prev["ts"] >= 3000:
                self._health_prev = {"ts": now, "n_outcomes": st.get("n_outcomes", 0)}
        else:
            problems.append("état du learner indisponible")
        n_hour = await self.db.fetchval("SELECT count(*) FROM labels WHERE ts > now() - interval '1 hour'")
        health = {"ts": now, "ok": not problems, "problems": problems, "labels_last_hour": n_hour,
                  "n_labels": st.get("n_labels") if st else None, "n_outcomes": st.get("n_outcomes") if st else None}
        await self.bus.set_json("apex:learning:health", health)
        if problems and now - getattr(self, "_last_health_alert", 0) > 3600:
            self._last_health_alert = now
            await self.notify_now("🧠 <b>L'apprentissage a un souci</b> : " + " ; ".join(problems)
                                  + "\nLe bot tente de se corriger seul, et le suivi Claude Code vérifiera à son prochain passage.")
        return [f"santé de l'apprentissage : {p}" for p in problems]

    async def trading_gate(self) -> list[str]:
        """Aptitude au trading réel, jugée sur le paper trading (frais et slippage compris).
        APPRENTISSAGE → PRÊT (alerte) → ACTIF (si activé) ; SUSPENDU si régression, reprise prouvée."""
        tcfg = self.cfg.get("trading") or {}
        crit = R.Criteria.from_cfg(tcfg.get("criteria"))
        # source : exécution réaliste (système d'ordres) dès qu'elle a des résultats, sinon paper trading
        rows = await self.db.fetch(
            """SELECT extract(epoch from closed_at) t, pnl FROM exec_positions
               WHERE status IN ('closed','failed') AND pnl IS NOT NULL AND closed_at IS NOT NULL""")
        source = "exécution simulée réaliste"
        if not rows:
            rows = await self.db.fetch(
                "SELECT extract(epoch from closed_at) t, pnl FROM paper_positions WHERE status='closed' AND pnl IS NOT NULL")
            source = "paper trading"
        closed = [R.Closed(float(r["t"]), float(r["pnl"])) for r in rows]
        stats = R.compute_stats(closed)
        recent = R.compute_stats(sorted(closed, key=lambda c: c.t)[-50:])
        health = await self.bus.get_json("apex:learning:health", {}) or {}
        ing = await self.bus.get_json("apex:ingestor:stats", {}) or {}
        health_ok = health.get("ok", True) and (ing.get("watchdog") or {}).get("healthy", True)
        verdict = R.evaluate(stats, crit, health_ok, recent.max_drawdown_stakes)
        st = await self.bus.get_json("apex:trading:state") or {"state": R.LEARNING, "since": time.time()}
        since_v = None
        if st["state"] == R.SUSPENDED:
            after = [c for c in closed if c.t > st.get("suspended_at", st["since"])]
            rc = R.Criteria(**{**crit.__dict__, "min_positions": crit.resume_positions, "min_days": 1.0})
            since_v = R.evaluate(R.compute_stats(after), rc, health_ok)
        live_enabled = bool(tcfg.get("live_enabled", False))
        new = R.next_state(st["state"], verdict, since_v, live_enabled)
        events = []
        if new != st["state"]:
            old = st["state"]
            st = {"state": new, "since": time.time(), "previous": old,
                  "suspended_at": time.time() if new == R.SUSPENDED else st.get("suspended_at")}
            await self.db.log_event("warning", "trading", f"{old} → {new}", {"stats": stats.to_dict()})
            events.append(f"trading : {old} → {new}")
            await self.notify_now(self._trading_message(old, new, stats, verdict, since_v))
        status = {"state": st["state"], "since": st["since"], "source": source, "stats": stats.to_dict(), "recent50": recent.to_dict(),
                  "checks": {k: [ok, txt] for k, (ok, txt) in verdict.checks.items()}, "regress": verdict.regress,
                  "live_enabled": live_enabled, "reinvest": self._reinvest_plan(stats, closed), "ts": time.time()}
        await self.bus.set_json("apex:trading:state", st)
        await self.bus.set_json("apex:trading:status", status)
        return events

    def _reinvest_plan(self, stats: R.Stats, closed: list) -> dict:
        """Les premiers gains servent à améliorer le bot. Le système ne paie rien lui-même :
        il recommande, dans l'ordre, les améliorations que les gains RÉELS couvrent."""
        rc = self.cfg.get("reinvest") or {}
        real_month_sol = 0.0       # alimenté par le trading réel quand il sera actif
        sol_eur = float(rc.get("sol_eur", 150))
        notional = float(self.cfg.get("paper.notional_sol", 0.1))
        paper_30d = sum(c.pnl for c in closed if c.t > time.time() - 30 * 86400) * notional
        budget = real_month_sol * sol_eur * float(rc.get("share", 0.5))
        options, spent, plan = rc.get("options", []), 0.0, []
        for o in options:
            if spent + o["eur_month"] <= budget:
                plan.append(o["name"])
                spent += o["eur_month"]
        return {"real_profit_30d_sol": real_month_sol, "paper_profit_30d_sol": round(paper_30d, 4),
                "budget_eur_month": round(budget, 2), "recommended": plan,
                "next": next((o for o in options if o["name"] not in plan), None)}

    @staticmethod
    def _trading_message(old: str, new: str, s: R.Stats, v: R.Verdict, since_v) -> str:
        crit = "\n".join(f"{'✅' if ok else '❌'} {txt}" for ok, txt in v.checks.values())
        head = {
            R.READY: "🟢 <b>APEX est PRÊT à trader en réel</b> (selon ses critères).\n"
                     "Il a prouvé sa rentabilité en paper trading, frais et slippage compris. "
                     "Le trading réel n'est PAS encore actif : réponds-moi pour l'activer avec ton portefeuille et tes limites.",
            R.LIVE: "🚀 <b>Trading réel ACTIF.</b>",
            R.SUSPENDED: "🟠 <b>Trading SUSPENDU</b> : les performances ont régressé. Le bot continue d'apprendre en "
                         "paper trading et reprendra automatiquement quand il aura de nouveau fait ses preuves.",
            R.LEARNING: "🔵 Retour en apprentissage.",
        }.get(new, new)
        extra = ""
        if since_v is not None and new != R.SUSPENDED:
            extra = "\n(reprise prouvée sur les positions postérieures à la suspension)"
        return f"{head}\n\n<b>Critères</b> ({s.n} positions, {s.days:.1f} j)\n{crit}{extra}"

    async def bulletin_if_due(self) -> None:
        """Bulletin en langage simple à heures fixes (heure de Paris) ; un seul par créneau, même après redémarrage."""
        local = BU.paris_now()
        hours = sorted(self.cfg.get("reports.bulletin_hours_paris") or [9, 15, 21])
        if local.hour not in hours:
            return
        slot = local.strftime("%Y-%m-%d-%H")
        old = await self.bus.r.set("apex:bulletin:last", slot, get=True)
        if old == slot.encode():
            return
        daily = local.hour == hours[0]
        prev = [h for h in hours if h < local.hour]
        span = 24 if daily else local.hour - prev[-1]
        title = "bilan des dernières 24 h" if daily else f"point de {local.hour} h"
        text = BU.render(await BU.gather(self.db, self.bus, self.cfg, span), title)
        await self.bus.publish(B.NOTIFY, {"type": "report", "text": text})
        await self.db.log_event("info", "report", f"bulletin {slot} envoyé")

    async def notify_now(self, text: str) -> None:
        await self.bus.publish(B.NOTIFY, {"type": "urgent", "text": text})
        await self.db.log_event("warning", "urgent", text)

    async def rollback_stable(self, reason: str, before_ts: Any = None) -> bool:
        if before_ts is not None:
            snap = await self.db.fetchrow(
                "SELECT id, path, ts FROM snapshots WHERE stable AND ts <= $1 ORDER BY ts DESC LIMIT 1", before_ts)
        else:
            snap = await self.db.fetchrow("SELECT id, path, ts FROM snapshots WHERE stable ORDER BY ts DESC LIMIT 1")
        if not snap:
            return False
        res = await self.send({"op": "rollback", "path": snap["path"]})
        if res.get("ok"):
            await self.db.execute(
                "UPDATE corrections SET status='rolled_back', evaluated_at=now() WHERE ts > $1 AND status IN ('evaluating','applied','promoted')", snap["ts"])
            await self.db.log_event("warning", "rollback", reason, {"snapshot": snap["id"]})
        return bool(res.get("ok"))

    # ------------------------------------------------------------------
    async def evaluate_corrections(self) -> list[str]:
        events = []
        rows = await self.db.fetch(
            "SELECT * FROM corrections WHERE status='evaluating' AND ts < now() - make_interval(secs => $1)", float(self.s["effect_delay_s"]))
        st = await self.learner_state()
        for c in rows:
            verdict, gain, p = await self.measure(c, st)
            effect = {"verdict": verdict, "gain": gain, "p_value": p}
            status = {IMPROVEMENT: "improvement", NO_EFFECT: "no_effect", DEGRADATION: "degradation"}[verdict]
            if c["shadow_id"]:
                # le jumeau témoin n'a servi qu'à la mesure
                await self.send({"op": "remove_competitor", "id": f"{c['shadow_id']}_ctl", "horizon": self.primary})
                if verdict == IMPROVEMENT:
                    ens = st.get("ensembles", {}).get(self.primary, {})
                    losses = {x["id"]: x["logloss"] for x in ens.get("competitors", [])}
                    sh, ch = losses.get(c["shadow_id"]), losses.get(ens.get("champion"))
                    if sh is not None and ch is not None and sh < ch:
                        res = await self.send({"op": "promote", "id": c["shadow_id"], "horizon": self.primary,
                                               "reason": f"correction #{c['id']} {c['action']} : gain {gain:+.1%}"})
                        effect["promoted"], effect["previous_champion"] = res.get("ok"), res.get("previous")
                        status = "promoted"
                    else:
                        # améliore son modèle d'origine mais ne bat pas (encore) le champion : reste en lice
                        await self.send({"op": "adopt", "id": c["shadow_id"], "horizon": self.primary})
                        effect["adopted"] = True
                        status = "improvement"
                else:
                    await self.send({"op": "remove_competitor", "id": c["shadow_id"], "horizon": self.primary})
            elif verdict == DEGRADATION:
                for u in (c["params_before"] or {}).get("_undo", []):
                    await self.send(u)
                status = "rolled_back"
                effect["auto_reverted"] = True
            await self.db.execute("UPDATE corrections SET status=$2, effect=$3, evaluated_at=now() WHERE id=$1", c["id"], status, effect)
            cell = self.meta.update(c["state"], c["context"], c["action"], verdict, gain)
            await self.db.execute(
                """INSERT INTO efficacy (state, context, action, n, n_success, n_fail, gain_sum) VALUES ($1,$2,$3,$4,$5,$6,$7)
                   ON CONFLICT (state, context, action) DO UPDATE SET n=$4, n_success=$5, n_fail=$6, gain_sum=$7""",
                c["state"], c["context"], c["action"], cell.n, cell.success, cell.fail, cell.gain_sum)
            if c["action"] == "claude_feature":
                fid = (c["params_after"] or {}).get("feature_id")
                new_status = {"promoted": "champion", "improvement": "validated"}.get(status, "rejected")
                await self.db.execute("UPDATE claude_features SET status=$2, reason=$3, decided_at=now() WHERE feature_id=$1",
                                      fid, new_status, f"{verdict} gain {gain:+.2%} p={p:.3f} (vs modèle d'origine)")
            events.append(f"correction #{c['id']} {c['action']} → {verdict} ({gain:+.1%}, p={p:.3f})")
            await self.check_freeze(c["component"], c["problem_key"])
        return events

    async def measure(self, c: Any, st: dict) -> tuple[str, float, float]:
        alpha = self.s["effect_alpha"]
        if c["shadow_id"]:
            # référence = le modèle d'origine de l'ombre (même modèle sans la correction) : on isole
            # ainsi l'effet de la correction ; à défaut, le champion
            ens = st.get("ensembles", {}).get(self.primary, {})
            comps = {x["id"]: x for x in ens.get("competitors", [])}
            parent = (comps.get(c["shadow_id"], {}).get("spec") or {}).get("parent")
            ctl = f"{c['shadow_id']}_ctl"
            ref = ctl if ctl in comps else (parent if parent in comps else ens.get("champion"))
            cl, sl = await self.metrics.paired_losses(ref, c["shadow_id"], self.primary, c["ts"])
            return effect_paired(cl, sl, alpha)
        start = c["ts"]
        delay = dt.timedelta(seconds=self.s["effect_delay_s"])
        if c["target_curve"] == "ece":
            before = await self.metrics.champion_losses(start - delay, start)
            after = await self.metrics.champion_losses(start, start + delay)
            return effect_before_after(before, after, lower_is_better=True, alpha=alpha)
        if c["target_curve"] == "alert_precision":
            rows_b = await self.db.fetch("SELECT sim_pnl FROM alerts WHERE ts > $1 AND ts <= $2 AND sim_pnl IS NOT NULL", start - 4 * delay, start)
            rows_a = await self.db.fetch("SELECT sim_pnl FROM alerts WHERE ts > $1 AND sim_pnl IS NOT NULL", start)
            return effect_before_after([r[0] for r in rows_b], [r[0] for r in rows_a], lower_is_better=False, alpha=alpha)
        before = await self.metrics.champion_error_indicators(start - delay, start)
        after = await self.metrics.champion_error_indicators(start, start + delay)
        return effect_before_after(before, after, lower_is_better=True, alpha=alpha)

    async def check_freeze(self, component: str, problem_key: str) -> None:
        n = self.s["max_failures_before_freeze"]
        rows = await self.db.fetch(
            """SELECT effect->>'verdict' v FROM corrections WHERE problem_key=$1 AND component=$2 AND effect IS NOT NULL
               ORDER BY ts DESC LIMIT $3""", problem_key, component, n)
        if len(rows) == n and all(verdict_is_failure(r["v"]) for r in rows):
            await self.db.execute(
                "INSERT INTO frozen_components (component, problem_key, since, reason) VALUES ($1,$2,now(),$3) ON CONFLICT DO NOTHING",
                component, problem_key, f"{n} corrections successives en échec")
            ok = await self.rollback_stable(f"gel de {component}")
            await self.notify_now(
                f"🧊 Le bot a essayé {n} corrections d'affilée sur un même point sans succès : il fait une pause sur ce "
                f"point{' et revient à sa dernière version stable' if ok else ''}, et réessaiera dans {UNFREEZE_AFTER_H} h. "
                f"Rien à faire de ton côté. (détail technique : {component}, {problem_key})")

    # ------------------------------------------------------------------
    async def correct(self, states: list[Detected], snap: dict) -> list[str]:
        events = []
        if not await self.db.fetchval("SELECT count(*) FROM reference_curves"):
            # sans référence (backfill ou 24 h de live), les tendances ne sont pas interprétables
            return ["en attente des courbes de référence : corrections automatiques suspendues"]
        n_last_hour = await self.db.fetchval(
            "SELECT count(*) FROM corrections WHERE ts > now() - interval '1 hour' AND action <> 'claude_feature'")
        budget = self.s["max_corrections_per_hour"] - n_last_hour
        frozen = {r["component"] for r in await self.db.fetch("SELECT component FROM frozen_components")}
        busy = {r["component"] for r in await self.db.fetch("SELECT DISTINCT component FROM corrections WHERE status='evaluating'")}
        market = await self.bus.get_json("apex:market", {}) or {}
        ctx = market.get("regime", "normal")
        st = await self.learner_state()
        if not st:
            return ["état du learner indisponible : aucune correction"]
        abnormal = sorted([d for d in states if d.state in ABNORMAL], key=lambda d: SEVERITY.index(d.state) if d.state in SEVERITY else 99)
        for d in abnormal:
            if budget <= 0:
                events.append("limite de corrections/heure atteinte (anti-emballement)")
                break
            causes, conf = await self.detector.diagnose(d, self.primary)
            causes.update(d.details)
            diag_id = await self.db.fetchval(
                "INSERT INTO diagnoses (ts, state, curve, causes, confidence) VALUES (now(),$1,$2,$3,$4) RETURNING id",
                d.state, d.curve, causes, conf)
            cands = candidates_for(d.state, causes)
            # exclut les actions dont le composant est gelé ou occupé
            usable = []
            for a in cands:
                comp = self._component_of(a)
                if comp in frozen or comp in busy:
                    continue
                usable.append(a)
            action = self.meta.choose(d.state, ctx, usable)
            if action is None:
                continue
            corr_id = await self.db.fetchval(
                """INSERT INTO corrections (ts, component, problem_key, state, context, action, diagnosis_id, curves_snapshot, status)
                   VALUES (now(),$1,$2,$3,$4,$5,$6,$7,'applying') RETURNING id""",
                self._component_of(action), d.problem_key, d.state, ctx, action, diag_id, snap)
            plan = build_plan(action, d.state, d.curve, causes, st, self.cfg, corr_id, self.primary)
            if plan is None:
                await self.db.execute("UPDATE corrections SET status='skipped' WHERE id=$1", corr_id)
                continue
            ok = await self.execute_plan(plan, corr_id, d, causes)
            busy.add(plan.component)
            budget -= 1
            events.append(f"{d.state} sur {d.curve} → {action} (#{corr_id}, confiance diag. {conf:.2f}) {'OK' if ok else 'ÉCHEC'}")
        return events

    def _component_of(self, action: str) -> str:
        return {
            "switch_calibration": "calibration", "bandit_recenter_up": "bandit", "bandit_recenter_down": "bandit",
            "request_claude_features": "claude", "request_claude_targeted_feature": "claude",
            "rollback_stable": "global", "revert_recent_correction": "global",
            "early_lgbm_retrain": f"lgbm:{self.primary}",
        }.get(action, f"model:{self.primary}")

    async def execute_plan(self, plan: Plan, corr_id: int, d: Detected, causes: dict) -> bool:
        ok = True
        if plan.rollback_stable:
            ok = await self.rollback_stable(f"régression {d.curve}")
        elif plan.action == "revert_recent_correction":
            target = plan.params_after["revert"]
            row = await self.db.fetchrow("SELECT * FROM corrections WHERE id=$1", target)
            if row is not None:
                prev = (row["effect"] or {}).get("previous_champion")
                if row["status"] == "promoted" and prev:
                    await self.send({"op": "promote", "id": prev, "horizon": self.primary,
                                     "reason": f"annulation de la correction #{target}"})
                if row["shadow_id"]:
                    await self.send({"op": "remove_competitor", "id": row["shadow_id"], "horizon": self.primary})
                for u in (row["params_before"] or {}).get("_undo", []):
                    await self.send(u)
            await self.db.execute("UPDATE corrections SET status='rolled_back', evaluated_at=now() WHERE id=$1", target)
        elif plan.needs_claude:
            if self.improver is not None:
                B.spawn(self.improver.run(trigger=d.state, problem_type=d.curve, diagnosis=causes, supervisor=self))
        for cmd in plan.commands:
            res = await self.send(cmd)
            ok = ok and bool(res.get("ok"))
        status = "evaluating" if ok and (plan.commands or plan.rollback_stable or plan.action == "revert_recent_correction") else ("applied" if ok else "failed")
        if plan.needs_claude:
            status = "applied"      # l'effet est mesuré sur les corrections « claude_feature » qui en découlent
        before = {**plan.params_before, "_undo": plan.undo}
        await self.db.execute(
            "UPDATE corrections SET status=$2, params_before=$3, params_after=$4, shadow_id=$5, target_curve=$6, component=$7 WHERE id=$1",
            corr_id, status, before, plan.params_after, plan.shadow_id, plan.target_curve, plan.component)
        return ok

    async def add_claude_feature_shadow(self, feature_id: str, problem_key: str) -> int | None:
        """Appelé par l'améliorateur quand une feature Claude passe la sandbox."""
        market = await self.bus.get_json("apex:market", {}) or {}
        corr_id = await self.db.fetchval(
            """INSERT INTO corrections (ts, component, problem_key, state, context, action, status, params_after, target_curve)
               VALUES (now(), $1, $2, 'CLAUDE', $3, 'claude_feature', 'applying', $4, $5) RETURNING id""",
            f"claude_feature:{feature_id}", problem_key, market.get("regime", "normal"), {"feature_id": feature_id}, f"logloss:{self.primary}")
        sid = f"shadow_c{corr_id}"
        res = await self.send({"op": "enable_claude_feature", "feature_id": feature_id, "new_id": sid,
                               "correction_id": corr_id, "horizon": self.primary})
        await self.db.execute("UPDATE corrections SET status=$2, shadow_id=$3 WHERE id=$1", corr_id,
                              "evaluating" if res.get("ok") else "failed", sid)
        return corr_id

    # ------------------------------------------------------------------
    async def cycle(self) -> dict:
        t0 = time.time()
        snap = await self.record_curves()
        await self.self_reference()
        states = await self.detector.detect(self.primary)
        await self.db.executemany(
            "INSERT INTO learning_states (ts, curve, state, slope, p_value, details) VALUES (now(),$1,$2,$3,$4,$5)",
            [(d.curve, d.state, d.slope, d.p_value, d.details) for d in states])
        events = await self.guardrails(states)
        events += await self.learning_health()
        events += await self.trading_gate()
        events += await self.evaluate_corrections()
        events += await self.correct(states, snap)
        stable = not any(d.state in (REGRESSION, "REGRESSION_TYPE") for d in states)
        res = await self.send({"op": "snapshot", "stable": stable, "label": "cycle"})
        dur = time.time() - t0
        summary = {"ts": time.time(), "duration_s": round(dur, 2), "states": [d.__dict__ for d in states],
                   "events": events, "snapshot": res, "stable": stable}
        await self.bus.set_json("apex:supervisor:last_cycle", summary)
        for e in events:
            await self.db.log_event("info", "supervision", e)
        if dur > 30:
            log.warning("cycle d'auto-supervision trop long : %.1f s", dur)
        return summary

    async def run(self) -> None:
        await self.load_meta()
        if self.improver is not None:
            B.spawn(self.improver.resume(self))
        # signal de vie indépendant du cycle de 15 min (sinon le supervisor se croit lui-même en panne)
        B.spawn(B.heartbeat_loop(self.bus, "supervisor"))
        # calendrier de Claude basé sur son dernier appel réel (un redémarrage ne le repousse plus)
        last_db = await self.db.fetchval("SELECT extract(epoch from max(ts)) FROM claude_proposals")
        last_claude = float(last_db) if last_db else time.time() - self.cfg.get("claude.cycle_s")
        last_claude_check = 0.0
        last_report = 0.0
        while True:
            try:
                await self.cycle()
            except Exception:  # noqa: BLE001
                log.exception("cycle de supervision")
            now = time.time()
            if self.improver is not None and now - last_claude_check >= 6 * 3600:
                B.spawn(self.improver.health_check())     # crédit / clé API Claude
                last_claude_check = now
            if self.improver is not None and now - last_claude >= self.cfg.get("claude.cycle_s"):
                B.spawn(self.improver.run(trigger="CYCLE_6H", problem_type="general", diagnosis={}, supervisor=self))
                last_claude = now
            # hausse des erreurs AUTRE → Claude
            if self.improver is not None:
                autre = [s for s in (await self.bus.get_json("apex:supervisor:last_cycle", {}) or {}).get("states", [])
                         if s["curve"] == "error_rate:AUTRE" and s["state"] in ("REGRESSION", "REGRESSION_TYPE")]
                if autre:
                    B.spawn(self.improver.run(trigger="AUTRE_EN_HAUSSE", problem_type="error_rate:AUTRE", diagnosis={}, supervisor=self))
            # rapport technique : gardé pour /tech (plus envoyé d'office)
            if now - last_report >= self.cfg.get("reports.every_s"):
                await self.bus.r.set("apex:report:tech", await self.reporter.build(6))
                last_report = now
            await self.bulletin_if_due()
            await asyncio.sleep(self.s["cycle_s"])


async def main() -> None:
    from ..claude_improver.service import Improver

    cfg = Config.load()
    db = await DB.connect(secrets().database_url)
    bus = B.Bus(secrets().redis_url)
    improver = Improver(cfg, db, bus) if secrets().anthropic_api_key else None
    await Supervisor(cfg, bus, db, improver).run()
