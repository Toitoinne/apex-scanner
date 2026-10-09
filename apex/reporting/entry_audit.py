"""Audit de l'ENTRÉE : quels indices prédisent vraiment une hausse, et le bot se surestime-t-il ?

Sur les décisions alertables des dernières heures (labels connus) :
  - pour chaque feature : pouvoir prédictif (AUC : 0,5 = hasard, 1 = parfait, < 0,5 = sens inverse),
    stabilité (AUC sur la 1re et la 2nde moitié de la période : un indice utile garde le même sens),
    lift (taux de hausse dans les 10 % de tokens où l'indice est le plus favorable / taux moyen) ;
  - calibration du modèle champion : probabilité annoncée vs fréquence réelle, par dixième ;
  - qualité de tri du modèle (AUC) et taux de hausse selon le point de décision.
Lancé par `python -m apex.reporting.entry_audit` (JSON) ; résultat gardé dans Redis (apex:entry:audit).
"""
from __future__ import annotations

import asyncio
import json
import math
from typing import Any

import numpy as np
from scipy.stats import rankdata

SQL = """
SELECT d.ts, d.point, d.features, l.y, p.p_cal
FROM decisions d
JOIN labels l ON l.decision_id = d.decision_id AND l.horizon = $3
LEFT JOIN predictions p ON p.decision_id = d.decision_id AND p.horizon = $3 AND p.is_champion
WHERE d.ts > now() - make_interval(hours => $1) AND d.ts < now() - interval '70 minutes'
  AND NOT d.blocked AND (d.features->>'unique_buyers')::float >= $2
"""


def auc(x: np.ndarray, y: np.ndarray) -> float | None:
    """AUC de Mann-Whitney (robuste aux ex æquo). None si une seule classe."""
    pos = y == 1
    n1, n0 = int(pos.sum()), int((~pos).sum())
    if n1 == 0 or n0 == 0:
        return None
    r = rankdata(x)
    return float((r[pos].sum() - n1 * (n1 + 1) / 2) / (n1 * n0))


def feature_report(X: dict[str, np.ndarray], y: np.ndarray, half: np.ndarray) -> list[dict]:
    base = float(y.mean()) if len(y) else 0.0
    out = []
    for f, x in X.items():
        ok = np.isfinite(x)
        if ok.sum() < 200 or np.nanstd(x[ok]) == 0:
            continue
        xv, yv, hv = x[ok], y[ok], half[ok]
        a = auc(xv, yv)
        a1 = auc(xv[~hv], yv[~hv])
        a2 = auc(xv[hv], yv[hv])
        if a is None:
            continue
        sign = 1 if a >= 0.5 else -1
        q = np.quantile(sign * xv, 0.9)
        top = yv[(sign * xv) >= q]
        out.append({
            "feature": f, "auc": round(a, 3), "pouvoir": round(abs(a - 0.5) * 2, 3),
            "auc_moitie1": None if a1 is None else round(a1, 3), "auc_moitie2": None if a2 is None else round(a2, 3),
            "stable": a1 is not None and a2 is not None and (a1 - 0.5) * (a2 - 0.5) > 0 and min(abs(a1 - 0.5), abs(a2 - 0.5)) > 0.01,
            "lift_top10": round(float(top.mean()) / base, 2) if base > 0 and len(top) else None,
            "couverture": round(float(ok.mean()), 3),
        })
    out.sort(key=lambda r: -r["pouvoir"])
    return out


def calibration(p: np.ndarray, y: np.ndarray, bins: int = 10) -> tuple[list[dict], float]:
    ok = np.isfinite(p)
    p, y = p[ok], y[ok]
    if len(p) < bins * 20:
        return [], float("nan")
    order = np.argsort(p)
    rows, ece = [], 0.0
    for i, idx in enumerate(np.array_split(order, bins)):
        ann, real = float(p[idx].mean()), float(y[idx].mean())
        ece += len(idx) / len(p) * abs(ann - real)
        rows.append({"dixieme": i + 1, "annonce": round(ann, 3), "reel": round(real, 3), "n": int(len(idx))})
    return rows[::-1], round(ece, 4)


async def run(db: Any, cfg: Any, hours: int = 48) -> dict:
    primary = cfg["labels"]["primary"]
    rows = [dict(r) for r in await db.fetch(SQL, hours, cfg.get("bandit.min_buyers_to_alert", 10), primary)]
    # calcul lourd (dizaines de milliers de décisions) hors de la boucle principale : sinon le service
    # se fige plusieurs minutes (plus de signal de vie)
    return await asyncio.to_thread(analyze, rows, hours)


def analyze(rows: list[dict], hours: int) -> dict:
    if len(rows) < 500:
        return {"n": len(rows), "resume": "pas encore assez de décisions avec résultat connu"}
    y = np.array([r["y"] for r in rows], dtype=float)
    ts = np.array([r["ts"].timestamp() for r in rows])
    half = ts >= np.median(ts)
    feats: dict[str, np.ndarray] = {}
    for i, r in enumerate(rows):
        fd = r["features"] if isinstance(r["features"], dict) else json.loads(r["features"] or "{}")
        for k, v in fd.items():
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                col = feats.get(k)
                if col is None:
                    col = feats[k] = np.full(len(rows), np.nan)
                col[i] = float(v)
    X = feats
    rep = feature_report(X, y, half)
    p = np.array([r["p_cal"] if r["p_cal"] is not None else math.nan for r in rows], dtype=float)
    cal, ece = calibration(p, y)
    okp = np.isfinite(p)
    by_point: dict[str, list] = {}
    for r in rows:
        by_point.setdefault(r["point"], []).append(r["y"])
    useful = [r for r in rep if r["pouvoir"] >= 0.1 and r["stable"]]
    useless = [r["feature"] for r in rep if r["pouvoir"] < 0.03]
    unstable = [r["feature"] for r in rep if r["pouvoir"] >= 0.05 and not r["stable"]]
    out = {
        "heures": hours, "n": len(rows), "taux_base": round(float(y.mean()), 4),
        "modele_auc": round(auc(p[okp], y[okp]) or 0, 3) if okp.sum() > 100 else None,
        "calibration": cal, "ecart_calibration": ece,
        "par_point": {k: {"n": len(v), "taux": round(sum(v) / len(v), 3)} for k, v in sorted(by_point.items())},
        "features": rep, "utiles": [r["feature"] for r in useful], "inutiles": useless, "instables": unstable,
    }
    top = cal[0] if cal else None
    out["resume"] = (
        f"{len(rows)} décisions, {y.mean():.1%} font x2 en 1 h ; tri du modèle AUC {out['modele_auc']} ; "
        + (f"les 10 % mieux notés : annoncé {top['annonce']:.0%}, réel {top['reel']:.0%} ; " if top else "")
        + f"{len(useful)} indices utiles et stables, {len(useless)} sans effet, {len(unstable)} instables")
    return out


async def main() -> None:
    from ..bus import Bus
    from ..config import Config, secrets
    from ..db import DB
    cfg = Config.load()
    db = await DB.connect(secrets().database_url, max_size=2)
    res = await run(db, cfg)
    await Bus(secrets().redis_url).set_json("apex:entry:audit", res)
    print(json.dumps(res, ensure_ascii=False))


if __name__ == "__main__":
    asyncio.run(main())


NAMES = {"mc_sol": "taille du token (capitalisation)", "curve_progress": "avancement vers la migration",
         "mult_since_launch": "hausse depuis le lancement", "velocity_mc_per_min": "vitesse de hausse",
         "vol_sol_last_60s": "volume de la dernière minute", "vol_sol_per_min": "volume par minute",
         "buy_sell_ratio_vol": "achats vs ventes (en SOL)", "buy_sell_ratio_n": "achats vs ventes (en nombre)",
         "top10_concentration": "part des 10 plus gros porteurs", "wash_score": "faux volume (wash trading)",
         "ret_last_30s": "hausse des 30 dernières secondes", "independent_buyers": "acheteurs indépendants",
         "unique_buyers": "nombre d'acheteurs", "weighted_buyers": "acheteurs pondérés par leur qualité",
         "age_s": "âge du token", "smart_share": "part de smart wallets", "smart_count": "nombre de smart wallets",
         "sniper_share": "part de snipers", "bot_share": "part de robots", "dev_holding_pct": "part détenue par le créateur",
         "fresh_wallet_share": "part de portefeuilles neufs", "meta_n_socials": "réseaux sociaux"}


def render(a: dict) -> str:
    """Message Telegram simple (commande /entree)."""
    if not a.get("features"):
        return "Audit de l'entrée pas encore disponible."
    cal = a.get("calibration") or []
    lines = [f"<b>Audit de l'entrée</b> — {a['n']} décisions sur {a['heures']} h, {a['taux_base']:.1%} font x2 en 1 h"]
    if a.get("modele_auc"):
        lines.append(f"Qualité de tri du bot : {a['modele_auc']:.2f} (0,5 = hasard, 1 = parfait)")
    if cal:
        t, b = cal[0], cal[-1]
        lines.append(f"Ses 10 % mieux notés : il annonce {t['annonce']:.0%}, réalité {t['reel']:.0%} · "
                     f"ses 10 % moins bien notés : réalité {b['reel']:.1%}")
    lines += ["", "<b>Les indices qui prédisent le mieux</b> (stables dans le temps) :"]
    for r in [r for r in a["features"] if r["stable"]][:8]:
        sens = "plus c'est haut, mieux c'est" if r["auc"] >= 0.5 else "plus c'est bas, mieux c'est"
        lines.append(f"• {NAMES.get(r['feature'], r['feature'])} : {r['auc']:.2f} ({sens})")
    if a.get("inutiles"):
        lines += ["", "Sans effet mesurable : " + ", ".join(NAMES.get(f, f) for f in a["inutiles"][:8])]
    return "\n".join(lines).replace(".", ",").replace("0,5 = hasard", "0,5 = hasard")
