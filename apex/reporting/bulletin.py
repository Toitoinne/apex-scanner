"""Bulletins Telegram en langage simple, pour le propriétaire (pas de jargon technique).

`gather()` lit l'état du système, `render()` (pure, testée) met en forme. Les rapports
techniques détaillés restent disponibles avec la commande /tech.
"""
from __future__ import annotations

import datetime as dt
import html
import json
import time
from typing import Any

SERVICE_TXT = {
    "ingestor": "la réception des données de la blockchain", "features": "l'analyse des tokens",
    "labeler": "la vérification des résultats", "learner": "l'apprentissage",
    "supervisor": "l'auto-surveillance", "notifier": "l'envoi des messages",
    "trader": "le système d'ordres", "dashboard": "le tableau de bord",
}

ERROR_TXT = {
    "GAGNANT_MANQUE": "il n'a pas signalé un token qui a ensuite fortement monté",
    "GAGNANT_DETECTE_TROP_TARD": "il a repéré un gagnant, mais trop tard",
    "BUNDLE_RATE": "il a signalé un token manipulé dès sa création (achats groupés)",
    "RUG_ALERTE": "il a signalé un token dont le créateur a tout revendu (arnaque)",
    "FAUX_SMART_MONEY": "il s'est fié à des portefeuilles « malins » qui ne l'étaient pas",
    "MORT_LENTE": "il a signalé un token qui s'est éteint doucement",
    "ENTREE_TARDIVE": "il a signalé un token qui avait déjà trop monté",
    "AUTRE": "erreur d'un autre type",
}

ACTION_TXT = {
    "add_diverse_competitor": "a mis un nouveau modèle en concurrence avec l'actuel",
    "early_lgbm_retrain": "a réentraîné son modèle principal plus tôt que prévu",
    "rollback_stable": "est revenu à sa dernière version stable",
    "revert_recent_correction": "a annulé un de ses récents changements",
    "upweight_error_type": "a insisté sur un type d'erreur qu'il commet souvent",
    "switch_calibration": "a changé sa façon d'estimer les probabilités",
    "shorten_window": "s'est concentré sur les données les plus récentes",
    "disable_drifted_feature": "a mis de côté un indicateur devenu peu fiable",
    "bandit_recenter_up": "est devenu plus exigeant avant d'envoyer une alerte",
    "bandit_recenter_down": "est devenu moins exigeant pour moins rater de gagnants",
    "request_claude_features": "a demandé à Claude de nouvelles idées d'indicateurs",
    "request_claude_targeted_feature": "a demandé à Claude un indicateur contre une erreur précise",
    "claude_feature": "a testé un indicateur proposé par Claude",
}

STATUS_TXT = {
    "applied": "en test", "evaluating": "en test", "promoted": "✅ gardé (ça marche mieux)",
    "closed": "terminé", "rolled_back": "↩️ annulé (n'aidait pas)", "degradation": "↩️ rejeté (moins bon)",
    "no_effect": "abandonné (sans effet)", "skipped": "pas lancé",
}

FEATURE_TXT = {
    "unique_buyers": "beaucoup d'acheteurs différents", "independent_buyers": "des acheteurs indépendants les uns des autres",
    "weighted_buyers": "des acheteurs nombreux et sérieux", "independence_ratio": "peu de portefeuilles liés entre eux",
    "smart_share": "des portefeuilles réputés gagnants achètent", "smart_count": "des portefeuilles réputés gagnants achètent",
    "velocity_mc_per_min": "sa valeur monte vite", "accel_mc": "sa montée accélère", "mult_since_launch": "il a déjà bien monté",
    "vol_sol_per_min": "beaucoup de volume échangé", "vol_sol_last_60s": "beaucoup de volume la dernière minute",
    "buy_sell_ratio_n": "bien plus d'achats que de ventes", "buy_sell_ratio_vol": "bien plus d'achats que de ventes (en SOL)",
    "curve_progress": "proche de quitter la phase de lancement", "n_trades": "beaucoup d'échanges",
    "ret_last_30s": "en hausse sur les 30 dernières secondes", "median_buy_sol": "des achats de belle taille",
    "dev_winner_rate": "son créateur a déjà lancé des tokens gagnants", "narrative_score": "un thème à la mode en ce moment",
    "meta_n_socials": "il a des réseaux sociaux", "meta_has_twitter": "il a un compte X/Twitter",
    "meta_has_telegram": "il a un groupe Telegram", "meta_has_website": "il a un site web",
    "mkt_temperature": "le marché est très actif", "fresh_wallet_share": "beaucoup de portefeuilles neufs",
    "sniper_share": "des robots ont acheté dès la première seconde", "bot_share": "des robots achètent",
    "top10_concentration": "quelques portefeuilles détiennent beaucoup", "dev_holding_pct": "le créateur détient beaucoup",
}

GLOSSAIRE = (
    "<b>Petit lexique APEX</b>\n"
    "• <b>Token / memecoin</b> : une cryptomonnaie lancée sur pump.fun (des milliers par heure).\n"
    "• <b>x2, x10</b> : le prix est multiplié par 2, par 10.\n"
    "• <b>Alerte</b> : le bot pense qu'un token peut monter. Pour l'instant, rien n'est acheté pour de vrai.\n"
    "• <b>Trade simulé (paper trading)</b> : le bot fait « comme s'il » achetait 0,1 SOL à chaque alerte et revendait "
    "selon son plan, frais compris. C'est son entraînement avant l'argent réel.\n"
    "• <b>Plan de sortie</b> : quand revendre (ex. vendre à x2, ou vendre la moitié puis laisser courir).\n"
    "• <b>Migration</b> : le token a assez de succès pour quitter pump.fun et s'échanger sur PumpSwap.\n"
    "• <b>Fiabilité</b> : de combien le bot se trompe moins qu'en devinant au hasard. Plus c'est haut, mieux c'est.\n"
    "• <b>Modèle</b> : le « cerveau » qui fait les prédictions. Plusieurs modèles sont en concurrence et le meilleur "
    "est utilisé.\n"
    "• <b>Rug / arnaque</b> : le créateur revend tout d'un coup et le prix s'effondre.\n"
    "• <b>Suivi Claude Code</b> : 4 fois par jour, une IA vérifie le bot, corrige et améliore son code.\n\n"
    "<b>Commandes utiles</b>\n"
    "/bilan — le point maintenant · /sorties — classement des façons de revendre\n"
    "/trading — où il en est avant l'argent réel · /paper — ses trades simulés\n"
    "/tech — rapport technique détaillé · /pause et /reprendre — couper les alertes"
)


def paris_now() -> dt.datetime:
    try:
        from zoneinfo import ZoneInfo
        return dt.datetime.now(ZoneInfo("Europe/Paris"))
    except Exception:  # noqa: BLE001 — base de fuseaux absente : approximation heure d'été
        return dt.datetime.now(dt.timezone(dt.timedelta(hours=2)))


def e(x: Any) -> str:
    return html.escape(str(x), quote=False)


def n_fr(x: float) -> str:
    """12345 → « 12 345 » (espace insécable fine)."""
    return f"{x:,.0f}".replace(",", " ")


def pct(x: float, digits: int = 0) -> str:
    return f"{x:+.{digits}%}".replace(".", ",")


def bar(done: int, total: int, width: int = 8) -> str:
    k = round(width * done / total) if total else 0
    return "▓" * k + "░" * (width - k)


def skill_of(learner: dict, primary: str) -> float | None:
    """Fiabilité = de combien le champion se trompe moins que le taux de base (prior)."""
    en = (learner.get("ensembles") or {}).get(primary) or {}
    ll = {c["id"]: c.get("logloss") for c in en.get("competitors", [])}
    champ, prior = ll.get(en.get("champion")), ll.get("prior")
    if not champ or not prior:
        return None
    return 1 - champ / prior


def check_lines(trading: dict, cfg_criteria: dict) -> tuple[int, int, list[str]]:
    """Critères d'aptitude au trading réel, reformulés simplement ; renvoie (ok, total, manquants)."""
    checks = trading.get("checks") or {}
    s = trading.get("stats") or {}
    c = cfg_criteria
    plain = {
        "positions": f"{s.get('n', 0)}/{c.get('min_positions', 100)} trades simulés terminés",
        "duree": f"{s.get('days', 0):.1f}/{c.get('min_days', 7)} jours de recul".replace(".", ","),
        "rendement": f"gain moyen {pct(s.get('total_return', 0), 1)} par trade (objectif {pct(c.get('min_return', 0.1))})",
        "profit_factor": (f"gains {s.get('profit_factor', 0):.2f}× plus gros que les pertes "
                          f"(objectif {c.get('min_profit_factor', 1.3)}×)").replace(".", ","),
        "jours_positifs": "assez de jours gagnants",
        "drawdown": "pas de grosse série de pertes",
        "recent": "les derniers trades ne perdent pas",
        "sante": "bot en bonne santé",
    }
    ok = sum(1 for v in checks.values() if v[0])
    missing = [plain.get(k, v[1]) for k, v in checks.items() if not v[0]]
    return ok, len(checks), missing


def render(d: dict, title: str) -> str:
    """Bulletin simple. `d` est produit par gather() (ou à la main dans les tests)."""
    lines: list[str] = []
    problems = d.get("problems") or []
    if problems:
        lines.append(f"🟠 <b>APEX — {e(title)}</b> : un point à surveiller")
        lines += [f"• {e(p)}" for p in problems]
    else:
        lines.append(f"💚 <b>APEX tourne normalement</b> — {e(title)}")
    h = d["hours"]
    period = "Ces dernières 24 h" if h >= 24 else f"Ces {h} dernières heures"
    lines += ["", f"👀 <b>{period}</b>",
              f"• {n_fr(d.get('tokens', 0))} nouveaux tokens surveillés, {n_fr(d.get('decisions', 0))} analyses"]
    na = d.get("alerts", 0)
    if na:
        lines.append(f"• {na} alerte{'s' if na > 1 else ''} envoyée{'s' if na > 1 else ''} (tokens qui pouvaient monter selon lui)")
        for t in (d.get("top_alerts") or [])[:3]:
            lines.append(f"   ↳ ${e(t['symbol'] or '?')} : jusqu'à x{t['best']:.1f} après l'alerte".replace(".", ","))
    else:
        lines.append("• Aucune alerte : rien d'assez prometteur selon lui (il préfère attendre que se tromper)")
    p = d.get("paper") or {}
    if p.get("n"):
        lines.append(f"• Entraînement (trades simulés, 0,1 SOL chacun) : {p['n']} terminés, {p['wins']} gagnants, "
                     f"résultat {p['pnl_sol']:+.3f} SOL ({pct(p['ret'])})".replace(".", ",", 1))
    tot = d.get("paper_total") or {}
    if tot.get("n"):
        lines.append(f"• Depuis le début : {tot['n']} trades simulés, total {tot['pnl_sol']:+.3f} SOL".replace(".", ",", 1))

    lines += ["", "🧠 <b>Apprentissage</b>",
              f"• Il a vérifié {n_fr(d.get('labels', 0))} de ses prédictions et appris de chacune"]
    sk, prev = d.get("skill"), d.get("skill_prev")
    if sk is not None:
        txt = f"• Fiabilité : il se trompe {sk:.0%} moins qu'en devinant au hasard"
        if prev is not None:
            delta = sk - prev
            trend = "↗ en progrès" if delta > 0.01 else ("↘ en léger recul" if delta < -0.01 else "→ stable")
            txt += f" (hier {prev:.0%}, {trend})"
        lines.append(txt)
    best = d.get("best_exit")
    if best:
        verdict = {"solide": "gagne de façon prouvée", "pas prouvée": "pas encore prouvée",
                   "perd": "perd encore de l'argent"}.get(best["verdict"], "")
        lines.append(f"• Meilleure façon de revendre en ce moment ({best['n_testees']} testées) : {e(best['description'])} — "
                     f"{best['mean']:+.1%} par trade, {verdict} (/sorties)".replace(".", ",", 1))
    for pt, b in (d.get("terrains") or {}).items():
        name = {"mig60": "1 min après migration", "mig300": "5 min après migration", "mig900": "15 min après migration",
                "vague2": "2e vague"}.get(pt, pt)
        lines.append(f"• Nouveau terrain « {name} » (observation) : {b['mean']:+.1%} par trade au mieux, "
                     f"{b['n']} cas".replace(".", ",", 1))
    evs = d.get("ev") or {}
    if evs.get("auto_moyen") is not None:
        lines.append("• Choix de la stratégie token par token : " + ("✅ ACTIF, il fait mieux qu'une stratégie unique"
                     if evs.get("actif") else f"en observation (sur des tokens jamais vus : {evs['auto_moyen']:+.1%} par trade, "
                     f"contre {evs['meilleure_fixe_moyen']:+.1%} pour la meilleure stratégie unique)").replace(".", ","))
    ex = d.get("exit_model") or {}
    if ex.get("situations_apprises"):
        if ex.get("pret") and ex.get("fiabilite") is not None:
            lines.append(f"• Quand revendre : {n_fr(ex['situations_apprises'])} situations étudiées, il se trompe "
                         f"{ex['fiabilite']:.0%} moins qu'au hasard pour savoir si la hausse va continuer")
        else:
            lines.append(f"• Quand revendre : en apprentissage ({n_fr(ex['situations_apprises'])} situations étudiées, "
                         "il commence à décider seul à partir de 2 000)")
    errs = d.get("errors") or []
    if errs:
        top = errs[0]
        lines.append(f"• Son erreur la plus fréquente ({n_fr(top['n'])} fois) : {ERROR_TXT.get(top['type'], top['type'])}"
                     " — c'est ce qu'il travaille en priorité")
    acts = d.get("actions") or []
    nm = d.get("new_models", 0)
    if nm or acts:
        lines.append("• Ce qu'il a changé tout seul :")
        if nm:
            lines.append(f"   ↳ {nm} nouveau{'x' if nm > 1 else ''} modèle{'s' if nm > 1 else ''} entraîné{'s' if nm > 1 else ''} et mis en concurrence")
        for a in acts[:4]:
            lines.append(f"   ↳ il {ACTION_TXT.get(a['action'], a['action'])} → {STATUS_TXT.get(a['status'], a['status'])}")
    tr = d.get("trading") or {}
    if tr:
        ok, total, missing = check_lines(tr, d.get("criteria") or {})
        state = tr.get("state", "APPRENTISSAGE")
        head = {"APPRENTISSAGE": "encore à l'entraînement", "PRET": "🟢 PRÊT (selon ses critères)",
                "ACTIF": "🚀 trading réel actif", "SUSPENDU": "🟠 trading réel en pause (il a régressé)"}.get(state, state)
        lines += ["", f"🎯 <b>Vers le trading réel</b> : {head}", f"{bar(ok, total)} {ok}/{total} critères remplis"]
        if missing:
            lines.append("• Il manque surtout : " + " ; ".join(e(m) for m in missing[:3]))
    lines += ["", "Rien à faire de ton côté." if not problems else "Le suivi Claude Code va regarder ce point.",
              "/aide pour le vocabulaire · /bilan pour le point à tout moment"]
    return "\n".join(lines)


async def gather(db: Any, bus: Any, cfg: Any, hours: int, record: bool = True) -> dict:
    iv = hours
    d: dict[str, Any] = {"hours": hours}
    d["tokens"] = await db.fetchval("SELECT count(*) FROM tokens WHERE created_at > now() - make_interval(hours => $1)", iv)
    d["decisions"] = await db.fetchval("SELECT count(*) FROM decisions WHERE ts > now() - make_interval(hours => $1)", iv)
    d["labels"] = await db.fetchval("SELECT count(*) FROM labels WHERE ts > now() - make_interval(hours => $1)", iv)
    d["alerts"] = await db.fetchval("SELECT count(*) FROM alerts WHERE ts > now() - make_interval(hours => $1)", iv)
    d["top_alerts"] = [dict(r) for r in await db.fetch(
        """SELECT symbol, max_multiple best FROM paper_positions
           WHERE opened_at > now() - make_interval(hours => $1) AND max_multiple IS NOT NULL
           ORDER BY max_multiple DESC LIMIT 3""", iv)]
    r = await db.fetchrow(
        """SELECT count(*) n, count(*) FILTER (WHERE pnl > 0) wins, coalesce(sum(pnl_sol), 0) pnl_sol,
                  coalesce(sum(notional_sol), 0) inv FROM paper_positions
           WHERE status='closed' AND closed_at > now() - make_interval(hours => $1)""", iv)
    d["paper"] = {"n": r["n"], "wins": r["wins"], "pnl_sol": r["pnl_sol"], "ret": (r["pnl_sol"] / r["inv"]) if r["inv"] else 0}
    r = await db.fetchrow("SELECT count(*) n, coalesce(sum(pnl_sol), 0) pnl_sol FROM paper_positions WHERE status='closed'")
    d["paper_total"] = {"n": r["n"], "pnl_sol": r["pnl_sol"]}
    d["errors"] = [{"type": r["error_type"], "n": r["n"]} for r in await db.fetch(
        """SELECT error_type, count(*) n FROM errors WHERE ts > now() - make_interval(hours => $1)
           GROUP BY 1 ORDER BY 2 DESC""", iv)]
    d["actions"] = [dict(r) for r in await db.fetch(
        """SELECT action, status FROM corrections WHERE ts > now() - make_interval(hours => $1)
           ORDER BY ts DESC LIMIT 6""", iv)]
    d["new_models"] = await db.fetchval(
        "SELECT count(*) FROM system_events WHERE kind='lgbm' AND ts > now() - make_interval(hours => $1)", iv)
    learner = await bus.get_json("apex:learner:state", {}) or {}
    primary = cfg["labels"]["primary"]
    d["skill"] = skill_of(learner, primary)
    d["skill_prev"] = await skill_history(bus, d["skill"] if record else None)
    d["trading"] = await bus.get_json("apex:trading:status", {}) or {}
    d["criteria"] = cfg.get("trading.criteria") or {}
    board = await bus.get_json("apex:exits:board", {}) or {}
    sel = board.get("selection") or []
    d["best_exit"] = {**sel[0], "n_testees": len(board.get("toutes") or sel)} if sel else None
    d["ev"] = await bus.get_json("apex:ev:stats", {}) or {}
    d["terrains"] = {pt: rk[0] for pt, rk in (board.get("terrains") or {}).items() if rk}
    d["exit_model"] = ((await bus.get_json("apex:labeler:stats", {}) or {}).get("sortie_apprise") or {})
    d["problems"] = await health_problems(bus)
    return d


async def skill_history(bus: Any, skill: float | None) -> float | None:
    """Mémorise la fiabilité ; renvoie celle d'il y a ~24 h (pour la tendance)."""
    key, now = "apex:bulletin:skill", time.time()
    hist = [json.loads(x) for x in await bus.r.lrange(key, 0, -1)]
    prev = None
    old = [h for h in hist if 20 * 3600 <= now - h[0] <= 30 * 3600]
    if old:
        prev = min(old, key=lambda h: abs(now - h[0] - 86400))[1]
    if skill is not None:
        await bus.r.rpush(key, json.dumps([now, skill]))
        await bus.r.ltrim(key, -40, -1)
    return prev


async def health_problems(bus: Any) -> list[str]:
    out = []
    for svc, t in (await bus.heartbeats()).items():
        if time.time() - t > 180:
            out.append(f"{SERVICE_TXT.get(svc, svc).capitalize()} ne répond plus depuis {int((time.time() - t) / 60)} min")
    h = await bus.get_json("apex:learning:health", {}) or {}
    if h and not h.get("ok", True):
        out.append("L'apprentissage est ralenti ou arrêté")
    wd = ((await bus.get_json("apex:ingestor:stats", {}) or {}).get("watchdog") or {})
    if wd and not wd.get("healthy", True):
        out.append("La réception des données de la blockchain est perturbée (le secours prend le relais)")
    co = await bus.get_json("apex:consistency", {}) or {}
    if co and not co.get("ok", True) and time.time() - co.get("ts", 0) < 4 * 3600:
        out.append("La vérification automatique trouve des résultats d'entraînement incohérents (voir /tech)")
    lag = await bus.get_json("apex:feed:lag", {}) or {}
    if lag.get("moyen_5min", 0) > 10 and time.time() - lag.get("ts", 0) < 300:
        out.append(f"Les données gratuites arrivent avec ~{lag['moyen_5min']:.0f} s de retard (serveurs publics saturés) : "
                   "le bot en tient compte dans ses simulations")
    c = await bus.get_json("apex:claude:status", {}) or {}
    if c and not c.get("ok", True):
        out.append("Plus de crédit sur l'API Claude : recharge sur console.anthropic.com"
                   if c.get("problem") == "credit" else "L'API Claude ne répond pas correctement")
    return out
