"""Service NOTIFIER — alertes tokens, suivis +15/+60 min, rapports, alertes système
et commandes Telegram (/top /stats /erreurs /etat /corrections /model /features
/seuil /pause /reprendre)."""
from __future__ import annotations

import asyncio
import html
import logging
import time
import uuid

import httpx

from .. import bus as B
from ..config import Config, secrets
from ..db import DB
from ..errors.classifier import DISPLAY

log = logging.getLogger("notifier")
GROUP = "notifier"


def e(x) -> str:
    return html.escape(str(x))


def fmt_alert(a: dict) -> str:
    sol_usd = a.get("sol_price_usd") or 0
    mc = a["mc_sol"]
    mc_txt = f"{mc:,.0f} SOL" + (f" (~${mc * sol_usd:,.0f})" if sol_usd else "")
    reasons = "\n".join(f"  • {e(r['feature'])} = {r['value']} (+{r['contribution']})" for r in a.get("reasons", [])) or "  • n/d"
    flags = ", ".join(a.get("flags", [])) or "aucun"
    slip = " · ".join(f"{k} SOL : {v:.1%}" for k, v in a.get("slippage", {}).items())
    hz = " · ".join(f"{k} {v:.1%}" for k, v in a.get("horizons", {}).items())
    sc = a.get("scores", {})
    m = a["mint"]
    why = ("proba de x10 en 24 h" if a.get("score") == "x10" else "proba de x2 en 1 h")
    arm_txt = (f"{a.get('arm_mean_pnl', 0):+.0%} en moyenne sur {a.get('arm_n', 0):.0f} cas simulés"
               if a.get("arm_n") else "en cours d'apprentissage")
    point = "migration" if a["point"] == "migration" else f"T+{int(a['point'])} s"
    return (
        f"🚀 <b>{e(a.get('name'))}</b> (${e(a.get('symbol'))})\n"
        f"<code>{e(m)}</code>\n"
        f"Point : {point} · MC : {mc_txt}\n"
        f"<b>x2 en 1 h : {sc.get('x2', a['p']):.0%}</b> · x5 en 6 h : {a.get('horizons', {}).get('X5_6H', 0):.1%}"
        f" · <b>x10 en 24 h : {sc.get('x10', 0):.1%}</b>\n"
        f"Déclenchée sur : {why}\n"
        f"🎯 <b>Stratégie de sortie</b> : {e(a.get('policy_description', a.get('policy', '')))}\n"
        f"   (cette combinaison rapporte {arm_txt} — je t'enverrai les signaux de vente ici)\n"
        f"Horizons : {hz}\n"
        f"Modèle champion : <code>{e(a['model'])}</code> · bras {e(a.get('arm'))}\n"
        f"Raisons :\n{reasons}\n"
        f"Drapeaux : {e(flags)}\n"
        f"Slippage estimé : {slip}\n"
        f"<a href=\"https://dexscreener.com/solana/{m}\">DexScreener</a> · "
        f"<a href=\"https://solscan.io/token/{m}\">Solscan</a> · "
        f"<a href=\"https://pump.fun/coin/{m}\">pump.fun</a>"
    )


class Telegram:
    def __init__(self, token: str, chat_id: str):
        self.base = f"https://api.telegram.org/bot{token}"
        self.chat_id = chat_id
        self.client = httpx.AsyncClient(timeout=40)
        self._last_send = 0.0

    async def send(self, text: str, reply_to: int | None = None) -> int | None:
        msg_id = None
        for chunk in [text[i:i + 3900] for i in range(0, len(text), 3900)] or [""]:
            wait = 1.05 - (time.time() - self._last_send)
            if wait > 0:
                await asyncio.sleep(wait)
            payload = {"chat_id": self.chat_id, "text": chunk, "parse_mode": "HTML", "disable_web_page_preview": True}
            if reply_to:
                payload["reply_to_message_id"] = reply_to
            try:
                r = await self.client.post(f"{self.base}/sendMessage", json=payload)
                self._last_send = time.time()
                if r.status_code == 429:
                    await asyncio.sleep(r.json().get("parameters", {}).get("retry_after", 5))
                    r = await self.client.post(f"{self.base}/sendMessage", json=payload)
                data = r.json()
                if data.get("ok") and msg_id is None:
                    msg_id = data["result"]["message_id"]
            except httpx.HTTPError as ex:
                log.warning("telegram : %s", type(ex).__name__)
        return msg_id

    async def updates(self, offset: int) -> list[dict]:
        r = await self.client.get(f"{self.base}/getUpdates", params={"offset": offset, "timeout": 30})
        return r.json().get("result", [])


class Notifier:
    def __init__(self, cfg: Config, bus: B.Bus, db: DB):
        self.cfg, self.bus, self.db = cfg, bus, db
        s = secrets()
        self.tg = Telegram(s.telegram_bot_token, s.telegram_chat_id) if s.telegram_bot_token else None

    async def out(self, text: str, reply_to: int | None = None) -> int | None:
        if self.tg is None:
            log.info("[telegram désactivé] %s", text[:300])
            return None
        return await self.tg.send(text, reply_to)

    async def consume(self) -> None:
        async for stream, mid, ev in self.bus.consume([B.ALERTS, B.NOTIFY], GROUP, "notifier-1", count=50):
            try:
                if stream == B.ALERTS:
                    msg_id = await self.out(fmt_alert(ev))
                    if msg_id:
                        await self.db.execute("UPDATE alerts SET tg_message_id=$2 WHERE decision_id=$1", ev["decision_id"], msg_id)
                elif ev.get("type") == "alert_followup":
                    await self.followup(ev)
                elif ev.get("type") == "sell_signal":
                    await self.sell_signal(ev)
                else:
                    await self.out(ev.get("text", ""))
            except Exception:  # noqa: BLE001
                log.exception("notification")
            await self.bus.ack(stream, GROUP, mid)

    async def followup(self, ev: dict) -> None:
        row = await self.db.fetchrow("SELECT tg_message_id FROM alerts WHERE decision_id=$1", ev["decision_id"])
        label = "+15 min" if ev.get("horizon") == "L15" else "+60 min"
        ok = "✅" if ev.get("y") else "❌"
        et = ev.get("error_type")
        txt = (f"{ok} Suivi {label} : PnL simulé {ev['pnl']:+.0%} · rendement max {ev.get('max_return', 0):+.0%}"
               + (f" · erreur : {DISPLAY.get(et, et)}" if et else "")
               + (" · ⚠ données incomplètes (coupure du flux)" if ev.get("incomplete") else ""))
        await self.out(txt, reply_to=row["tg_message_id"] if row else None)

    async def sell_signal(self, ev: dict) -> None:
        """Signal de vente en réponse à l'alerte d'origine."""
        row = await self.db.fetchrow("SELECT tg_message_id FROM alerts WHERE decision_id=$1", ev["decision_id"])
        k = ev["kind"]
        frac = ev["fraction"]
        mult = ev["multiple"]
        pnl = ev["pnl_after"]
        sym = e(ev.get("symbol") or ev["mint"][:6])
        part = "tout le reste" if ev["closed"] else f"{frac:.0%} de la position initiale"
        head = {
            "PALIER": f"🟡 <b>VENDS {part}</b> — palier {e(ev.get('reason', ''))} atteint",
            "STOP_SUIVEUR": f"🔴 <b>SORS ({part})</b> — stop suiveur {e(ev.get('reason', ''))}",
            "STOP": f"🔴 <b>SORS ({part})</b> — stop de protection touché",
            "DANGER": f"🚨 <b>SORS VITE ({part})</b> — {e(ev.get('reason_text', ''))}",
            "TEMPS": f"⏱ <b>SORS ({part})</b> — fin de la durée de suivi",
        }.get(k, k)
        txt = (f"{head}\n${sym} : prix actuel = x{mult:.2f} ton entrée\n"
               f"Position : {pnl:+.0%}" + (" (clôturée)" if ev["closed"] else " (le reste continue de courir)")
               + f"\nPaper trading ({ev.get('notional_sol', 0.1):g} SOL) : {pnl * ev.get('notional_sol', 0.1):+.3f} SOL")
        await self.out(txt, reply_to=row["tg_message_id"] if row else None)

    # ---------------- commandes ----------------
    async def commands(self) -> None:
        if self.tg is None:
            return
        offset = 0
        while True:
            try:
                for u in await self.tg.updates(offset):
                    offset = u["update_id"] + 1
                    msg = u.get("message") or {}
                    if str(msg.get("chat", {}).get("id")) != str(self.tg.chat_id):
                        continue
                    text = (msg.get("text") or "").strip().split()
                    if text:
                        await self.out(await self.handle(text[0].split("@")[0].lower()))
            except Exception:  # noqa: BLE001
                log.exception("commandes")
                await asyncio.sleep(5)

    async def handle(self, cmd: str) -> str:
        db, primary = self.db, self.cfg["labels"]["primary"]
        st = await self.bus.get_json("apex:learner:state", {}) or {}
        if cmd == "/top":
            rows = await db.fetch(
                """SELECT DISTINCT ON (p.mint) p.mint, p.point, p.p_cal, t.name, t.symbol FROM predictions p JOIN tokens t USING (mint)
                   JOIN decisions d ON d.decision_id = p.decision_id AND d.ts = p.ts
                   WHERE p.is_champion AND p.horizon=$1 AND p.ts > now() - interval '15 minutes'
                     AND NOT d.blocked AND (d.features->>'unique_buyers')::float >= $2
                   ORDER BY p.mint, p.ts DESC""", primary, self.cfg.get("bandit.min_buyers_to_alert", 10))
            rows = sorted(rows, key=lambda r: -r["p_cal"])[:10]
            return "<b>Top live</b>\n" + "\n".join(f"{r['p_cal']:.0%} {e(r['name'])} (${e(r['symbol'])}) @{e(r['point'])} <code>{e(r['mint'])}</code>" for r in rows)
        if cmd == "/stats":
            r = await db.fetchrow("""SELECT count(*) n, avg((result_60->>'y')::int) prec, percentile_cont(0.5) WITHIN GROUP (ORDER BY sim_pnl) med
                                     FROM alerts WHERE ts > now() - interval '24 hours'""")
            ing = await self.bus.get_json("apex:ingestor:stats", {}) or {}
            health = await self.bus.get_json("apex:learning:health", {}) or {}
            claude = await self.bus.get_json("apex:claude:status", {}) or {}
            return (f"<b>24 h</b> : {r['n']} alertes · précision {r['prec'] or 0:.0%} · PnL médian {r['med'] or 0:+.0%}\n"
                    f"Labels appris : {st.get('n_labels')} · alertes aujourd'hui : {st.get('alerts_today')}\n"
                    f"Ingestion : {ing.get('events')} évts, {ing.get('dupes')} doublons, {ing.get('reconnects')} reconnexions\n"
                    f"Latence décision p95 : {st.get('decision_latency_ms_p95') or 0:.1f} ms\n"
                    + self._flow_status(ing, st)
                    + f"\nSanté de l'apprentissage : {'✅ OK' if health.get('ok', True) else '🔴 ' + '; '.join(health.get('problems', []))}"
                    f" ({health.get('labels_last_hour', 'n/d')} labels la dernière heure)"
                    + f"\nAPI Claude : {'✅ OK' if claude.get('ok', True) else '🔴 ' + str(claude.get('problem', claude.get('error')))}")
        if cmd == "/erreurs":
            rows = await db.fetch("SELECT error_type, count(*) n, sum(cost) c FROM errors WHERE ts > now() - interval '24 hours' GROUP BY 1 ORDER BY c DESC NULLS LAST")
            return "<b>Erreurs 24 h</b>\n" + "\n".join(f"{e(DISPLAY.get(r['error_type'], r['error_type']))} : {r['n']} (coût {r['c'] or 0:.1f} SOL)" for r in rows)
        if cmd == "/etat":
            rows = await db.fetch("""SELECT DISTINCT ON (curve) curve, state, p_value FROM learning_states
                                     WHERE ts > now() - interval '1 hour' ORDER BY curve, ts DESC""")
            return "<b>État d'apprentissage</b>\n" + "\n".join(f"{e(r['curve'])} : {e(r['state'])} (p={r['p_value']:.3f})" for r in rows)
        if cmd == "/corrections":
            rows = await db.fetch("SELECT id, action, problem_key, status, effect FROM corrections ORDER BY ts DESC LIMIT 10")
            return "<b>Dernières corrections</b>\n" + "\n".join(
                f"#{r['id']} {e(r['action'])} ({e(r['problem_key'])}) → {e(r['status'])} {e((r['effect'] or {}).get('verdict', ''))}" for r in rows)
        if cmd == "/model":
            out = []
            for h, en in st.get("ensembles", {}).items():
                comps = sorted(en["competitors"], key=lambda c: c["logloss"] or 9)
                out.append(f"<b>{h}</b> champion <code>{e(en['champion'])}</code> : " + ", ".join(f"{e(c['id'])} {c['logloss']}" for c in comps[:5]))
            return "\n".join(out) or "learner indisponible"
        if cmd == "/features":
            rows = await db.fetch("SELECT feature_id, status, reason FROM claude_features ORDER BY created_at DESC LIMIT 15")
            return "<b>Features Claude</b>\n" + ("\n".join(f"{e(r['feature_id'])} : {e(r['status'])}" for r in rows) or "aucune")
        if cmd == "/seuil":
            b = st.get("bandit", {})
            act = b.get("active_detail") or {}
            arms = "\n".join(f"{e(a['key'])} : {a['mean_reward']:+.0%}/alerte, {a['alerts_per_day']}/j (n={a['n']:.0f})"
                              for a in b.get("arms", [])[:8])
            pols = "\n".join(f"  {e(k)} : {'n/d' if v is None else f'{v:+.0%}'}" for k, v in (b.get("best_by_policy") or {}).items())
            return (f"<b>Politique d'alerte active</b> : score {e(act.get('score'))}, seuil {act.get('threshold')}, "
                    f"point {e(act.get('point'))}, sortie {e(act.get('policy'))}\n"
                    f"PnL moyen simulé : {act.get('mean_reward', 0):+.0%} par alerte · {act.get('alerts_per_day')}/jour\n\n"
                    f"<b>Meilleur PnL par stratégie de sortie</b>\n{pols}\n\n<b>Meilleures combinaisons</b>\n{arms}")
        if cmd == "/trading":
            t = await self.bus.get_json("apex:trading:status", {}) or {}
            if not t:
                return "Statut de trading pas encore calculé (prochain cycle de supervision)."
            icon = {"APPRENTISSAGE": "🔵", "PRET": "🟢", "ACTIF": "🚀", "SUSPENDU": "🟠"}.get(t["state"], "")
            checks = "\n".join(f"{'✅' if ok else '❌'} {e(txt)}" for ok, txt in t["checks"].values())
            rv = t.get("reinvest") or {}
            nxt = (rv.get("next") or {}).get("name")
            return (f"{icon} <b>État : {e(t['state'])}</b> · trading réel {'activé' if t.get('live_enabled') else 'non activé'}\n\n"
                    f"<b>Critères pour trader en réel</b>\n{checks}\n\n"
                    f"<b>Réinvestissement</b> : gains réels 30 j {rv.get('real_profit_30d_sol', 0):+.3f} SOL "
                    f"(paper {rv.get('paper_profit_30d_sol', 0):+.3f} SOL) · budget {rv.get('budget_eur_month', 0):.0f} €/mois"
                    + (f"\nPremière amélioration visée : {e(nxt)}" if nxt else ""))
        if cmd == "/ordres":
            t = await self.bus.get_json("apex:trader:stats", {}) or {}
            r = await db.fetchrow(
                """SELECT count(*) FILTER (WHERE status IN ('closed','failed')) closed, count(*) FILTER (WHERE status='open') open,
                          coalesce(sum(pnl_sol) FILTER (WHERE status IN ('closed','failed')), 0) pnl_sol,
                          coalesce(sum(sol_in) FILTER (WHERE status IN ('closed','failed')), 0) inv,
                          count(*) FILTER (WHERE status='closed' AND pnl > 0) wins FROM exec_positions WHERE mode='simulation'""")
            o = await db.fetchrow(
                """SELECT count(*) n, count(*) FILTER (WHERE status='failed') fails, count(*) FILTER (WHERE status='skipped') skips,
                          avg(slippage) FILTER (WHERE status='filled' AND side='buy') slip_buy,
                          avg(slippage) FILTER (WHERE status='filled' AND side='sell') slip_sell FROM exec_orders
                   WHERE ts > now() - interval '24 hours'""")
            pp = t.get("pumpportal_builder") or {}
            return (f"<b>Système d'ordres</b> — mode <b>{e(t.get('mode', 'simulation')).upper()}</b>"
                    + (f" ({e(t.get('why_not_live'))})" if t.get("why_not_live") else "") + "\n"
                    f"Positions simulées : {r['closed']} clôturées ({r['wins']} gagnantes), {r['open']} ouvertes · "
                    f"PnL {r['pnl_sol']:+.4f} SOL ({(r['pnl_sol'] / r['inv']) if r['inv'] else 0:+.1%} sur les mises)\n"
                    f"Ordres 24 h : {o['n']} (échecs {o['fails']}, refus par les limites {o['skips']}) · slippage moyen "
                    f"achat {(o['slip_buy'] or 0):+.1%} / vente {(o['slip_sell'] or 0):+.1%}\n"
                    f"Transactions PumpPortal construites et vérifiées : {pp.get('ok', 0)} ok / {pp.get('fail', 0)} échecs\n"
                    f"Portefeuille dédié : {'configuré' if t.get('wallet_configured') else 'non configuré'}")
        if cmd == "/stop":
            await self.bus.r.set("apex:trading:killed", "1")
            await self.bus.r.delete("apex:trading:armed")
            return ("🛑 Arrêt d'urgence : aucun nouvel achat réel. Les positions ouvertes continuent d'être gérées "
                    "par leurs signaux de vente. Le bot continue d'apprendre. /activer pour relancer.")
        if cmd == "/activer":
            t = await self.bus.get_json("apex:trading:status", {}) or {}
            tr = await self.bus.get_json("apex:trader:stats", {}) or {}
            missing = []
            if not self.cfg.get("trading.live_enabled", False):
                missing.append("le trading réel n'est pas autorisé dans la configuration (trading.live_enabled)")
            if t.get("state") not in ("PRET", "ACTIF"):
                missing.append(f"le bot n'est pas prêt (état {t.get('state', 'APPRENTISSAGE')}, voir /trading)")
            if not tr.get("wallet_configured"):
                missing.append("aucun portefeuille dédié configuré")
            if missing:
                return "Activation impossible :\n• " + "\n• ".join(e(m) for m in missing)
            await self.bus.r.delete("apex:trading:killed")
            await self.bus.r.set("apex:trading:armed", "1")
            return ("✅ Trading réel ACTIVÉ, dans les limites configurées (mise, positions, perte max/jour). "
                    "Il sera suspendu automatiquement en cas de régression. /stop pour tout arrêter.")
        if cmd == "/paper":
            r = await db.fetchrow(
                """SELECT count(*) n, count(*) FILTER (WHERE status='closed') closed,
                          count(*) FILTER (WHERE status='closed' AND pnl > 0) wins,
                          coalesce(sum(pnl_sol) FILTER (WHERE status='closed'), 0) realized,
                          coalesce(sum(pnl_sol) FILTER (WHERE status='open'), 0) latent,
                          coalesce(sum(notional_sol), 0) invested, max(max_multiple) best
                   FROM paper_positions""")
            d7 = await db.fetchval("SELECT coalesce(sum(pnl_sol),0) FROM paper_positions WHERE status='closed' AND closed_at > now() - interval '7 days'")
            opens = await db.fetch("SELECT symbol, policy, pnl FROM paper_positions WHERE status='open' ORDER BY opened_at DESC LIMIT 8")
            wr = f"{r['wins'] / r['closed']:.0%}" if r["closed"] else "n/d"
            lines = [f"<b>Paper trading</b> (mise {self.cfg.get('paper.notional_sol', 0.1):g} SOL par alerte)",
                     f"Positions : {r['n']} (clôturées {r['closed']}, gagnantes {wr})",
                     f"PnL réalisé : <b>{r['realized']:+.3f} SOL</b> · latent : {r['latent']:+.3f} SOL · 7 j : {d7:+.3f} SOL",
                     f"Rendement sur mises : {(r['realized'] / r['invested']) if r['invested'] else 0:+.1%} · meilleur multiple : x{(r['best'] or 0):.1f}"]
            if opens:
                lines.append("\n<b>En cours</b>")
                lines += [f"${e(o['symbol'])} [{e(o['policy'])}] : {(o['pnl'] or 0):+.0%}" for o in opens]
            return "\n".join(lines)
        if cmd in ("/pause", "/reprendre"):
            await self.bus.publish(B.CONTROL, {"op": "pause", "value": cmd == "/pause", "cmd_id": uuid.uuid4().hex})
            return "⏸ Alertes en pause (l'apprentissage continue)." if cmd == "/pause" else "▶️ Alertes reprises."
        return ("Commandes : /trading /ordres /paper /stats /top /seuil /erreurs /etat /corrections /model /features "
                "/pause /reprendre · trading réel : /activer /stop")

    @staticmethod
    def _flow_status(ing: dict, st: dict) -> str:
        wd = ing.get("watchdog") or {}
        now = time.time()
        ages = ", ".join(f"{k} {v:.0f} s" for k, v in (wd.get("last_event_age_s") or {}).items())
        return (f"Flux : {'✅ sain' if wd.get('healthy', True) else '🔴 en panne'}"
                f"{' · secours Helius ACTIF' if wd.get('backup_running') else ''}\n"
                f"Dernier événement par source : {ages or 'n/d'}\n"
                f"Crédits Helius ce mois : {wd.get('helius_credits_month', 0):,.0f} / 1 000 000\n"
                f"Décisions/labels écartés (trou de données) : {st.get('skipped_data_gap', 0)}\n"
                f"Apprentissage : {st.get('n_labels', 0):,} labels appris · {st.get('n_outcomes', 0):,} récompenses de stratégies\n"
                f"Dernière décision il y a {now - (st.get('last_decision_ts') or now):.0f} s · dernier label appris il y a "
                f"{now - (st.get('last_label_ts') or now):.0f} s · trades PumpSwap reçus : {ing.get('pumpswap_trades', 0)}"
                f" ({ing.get('pumpswap_pools', 0)} pools suivis)")

    async def hb(self) -> None:
        while True:
            await self.bus.heartbeat("notifier")
            await asyncio.sleep(10)

    async def run(self) -> None:
        await asyncio.gather(self.consume(), self.commands(), self.hb())


async def main() -> None:
    cfg = Config.load()
    db = await DB.connect(secrets().database_url)
    await Notifier(cfg, B.Bus(secrets().redis_url), db).run()
