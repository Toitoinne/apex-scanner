"""Dashboard web (lecture seule) : FastAPI + page statique Chart.js."""
from __future__ import annotations

import secrets as pysecrets
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse

from .. import bus as B
from ..config import Config, secrets
from ..db import DB
from ..errors.classifier import DISPLAY

STATIC = Path(__file__).parent / "static"
app = FastAPI(title="APEX SCANNER", docs_url=None, redoc_url=None)
state: dict = {}


def auth(request: Request) -> None:
    tok = secrets().dashboard_token
    if not tok:
        return
    given = request.query_params.get("token") or request.headers.get("x-apex-token") or request.cookies.get("apex_token") or ""
    if not pysecrets.compare_digest(given, tok):
        raise HTTPException(401, "token requis")


@app.on_event("startup")
async def startup() -> None:
    state["cfg"] = Config.load()
    state["db"] = await DB.connect(secrets().database_url, max_size=5)
    state["bus"] = B.Bus(secrets().redis_url)


def db() -> DB:
    return state["db"]


def rows(rs) -> list[dict]:
    return [dict(r) for r in rs]


@app.get("/")
async def index(_: None = Depends(auth)) -> FileResponse:
    return FileResponse(STATIC / "index.html")


@app.get("/api/live")
async def live(_: None = Depends(auth)) -> list[dict]:
    primary = state["cfg"]["labels"]["primary"]
    rs = await db().fetch(
        """SELECT DISTINCT ON (p.mint) p.mint, p.point, p.p_cal, p.ts, t.name, t.symbol, d.mc_sol, d.blocked, d.safety_flags
           FROM predictions p JOIN tokens t USING (mint)
           LEFT JOIN decisions d ON d.decision_id = p.decision_id AND d.ts = p.ts
           WHERE p.is_champion AND p.horizon=$1 AND p.ts > now() - interval '15 minutes'
             AND (d.features->>'unique_buyers')::float >= $2
           ORDER BY p.mint, p.ts DESC""", primary, state["cfg"].get("bandit.min_buyers_to_alert", 10))
    return sorted(rows(rs), key=lambda r: -(r["p_cal"] or 0))[:50]


@app.get("/api/curves")
async def curves(curve: str = Query("error_rate"), win: str = Query("1h"), model: str = Query("champion"), hours: int = 168,
                 _: None = Depends(auth)) -> dict:
    pts = await db().fetch(
        """SELECT ts, value, n FROM curve_points WHERE curve=$1 AND win=$2 AND model_id=$3
           AND ts > now() - make_interval(hours => $4) ORDER BY ts""", curve, win, model, hours)
    st = await db().fetch(
        "SELECT ts, state FROM learning_states WHERE curve=$1 AND ts > now() - make_interval(hours => $2) ORDER BY ts", curve, hours)
    corr = await db().fetch(
        "SELECT id, ts, action, status, problem_key FROM corrections WHERE ts > now() - make_interval(hours => $1) ORDER BY ts", hours)
    ref = await db().fetchval("SELECT value FROM reference_curves WHERE curve=$1", curve)
    return {"points": rows(pts), "states": rows(st), "corrections": rows(corr), "reference": ref}


@app.get("/api/curve_names")
async def curve_names(_: None = Depends(auth)) -> list[str]:
    rs = await db().fetch("SELECT DISTINCT curve FROM curve_points WHERE ts > now() - interval '2 days' ORDER BY curve")
    return [r["curve"] for r in rs]


@app.get("/api/states")
async def states(_: None = Depends(auth)) -> list[dict]:
    return rows(await db().fetch("SELECT DISTINCT ON (curve) curve, state, slope, p_value, ts FROM learning_states ORDER BY curve, ts DESC"))


@app.get("/api/models")
async def models(_: None = Depends(auth)) -> dict:
    return await state["bus"].get_json("apex:learner:state", {}) or {}


@app.get("/api/calibration")
async def calibration(hours: int = 24, _: None = Depends(auth)) -> dict:
    from ..supervision.metrics import Metrics
    return await Metrics(db(), state["cfg"]["labels"]["primary"]).calibration(hours)


@app.get("/api/efficacy")
async def efficacy(_: None = Depends(auth)) -> list[dict]:
    return rows(await db().fetch(
        """SELECT state, context, action, n, n_success, n_fail, CASE WHEN n>0 THEN gain_sum/n END mean_gain
           FROM efficacy ORDER BY state, n DESC"""))


@app.get("/api/corrections")
async def corrections(_: None = Depends(auth)) -> list[dict]:
    return rows(await db().fetch(
        "SELECT id, ts, component, problem_key, action, status, effect, params_after FROM corrections ORDER BY ts DESC LIMIT 100"))


@app.get("/api/claude")
async def claude(_: None = Depends(auth)) -> dict:
    props = await db().fetch("SELECT id, ts, trigger, problem_type, hypothesis, n_features, status, usage FROM claude_proposals ORDER BY ts DESC LIMIT 50")
    feats = await db().fetch("SELECT feature_id, name, description, status, reason, created_at, decided_at, problem_type FROM claude_features ORDER BY created_at DESC LIMIT 100")
    return {"proposals": rows(props), "features": rows(feats)}


@app.get("/api/alerts")
async def alerts(_: None = Depends(auth)) -> list[dict]:
    return rows(await db().fetch(
        """SELECT ts, mint, point, p, model_id, payload->>'name' AS name, payload->>'symbol' AS symbol, sim_pnl,
                  result_15, result_60 FROM alerts ORDER BY ts DESC LIMIT 200"""))


@app.get("/api/paper")
async def paper(_: None = Depends(auth)) -> dict:
    stats = await db().fetchrow(
        """SELECT count(*) n, count(*) FILTER (WHERE status='closed') closed,
                  count(*) FILTER (WHERE status='closed' AND pnl > 0) wins,
                  coalesce(sum(pnl_sol) FILTER (WHERE status='closed'), 0) realized,
                  coalesce(sum(pnl_sol) FILTER (WHERE status='open'), 0) latent,
                  coalesce(sum(notional_sol), 0) invested FROM paper_positions""")
    pos = await db().fetch(
        """SELECT decision_id, mint, symbol, policy, opened_at, status, pnl, pnl_sol, max_multiple, fills, closed_at
           FROM paper_positions ORDER BY opened_at DESC LIMIT 200""")
    return {"stats": dict(stats), "positions": rows(pos)}


@app.get("/api/errors")
async def errors(_: None = Depends(auth)) -> list[dict]:
    rs = await db().fetch(
        """SELECT error_type, count(*) n, sum(cost) cost FROM errors WHERE ts > now() - interval '24 hours'
           GROUP BY error_type ORDER BY cost DESC NULLS LAST""")
    return [{**dict(r), "label": DISPLAY.get(r["error_type"], r["error_type"])} for r in rs]


@app.get("/api/wallets")
async def wallets(_: None = Depends(auth)) -> list[dict]:
    return rows(await db().fetch(
        """SELECT address, n_trades, n_wins, pnl_sol FROM wallets WHERE is_smart ORDER BY pnl_sol DESC LIMIT 100"""))


@app.get("/api/devs")
async def devs(_: None = Depends(auth)) -> list[dict]:
    return rows(await db().fetch(
        """SELECT address, n_tokens, n_rugs, n_winners FROM devs WHERE n_rugs > 0 ORDER BY n_rugs DESC LIMIT 100"""))


@app.get("/bureau")
async def bureau_page(_: None = Depends(auth)) -> FileResponse:
    return FileResponse(STATIC / "bureau.html")


@app.get("/api/bureau")
async def bureau(_: None = Depends(auth)) -> dict:
    """Le bureau : ce que fait chaque « employé » (service) en ce moment, en chiffres simples.
    Uniquement des lectures légères (Redis + quelques petites requêtes bornées)."""
    import time

    bus, now = state["bus"], time.time()
    g = lambda k: bus.get_json(k, {})      # noqa: E731
    hb = await bus.heartbeats()
    ing, lag, mkt = await g("apex:ingestor:stats") or {}, await g("apex:feed:lag") or {}, await g("apex:market") or {}
    lab, trd, ts_ = await g("apex:labeler:stats") or {}, await g("apex:trader:stats") or {}, await g("apex:trading:status") or {}
    ln, health = await g("apex:learner:state") or {}, await g("apex:learning:health") or {}
    sup, co, cl = await g("apex:supervisor:last_cycle") or {}, await g("apex:consistency") or {}, await g("apex:claude:status") or {}
    enr, board = await g("apex:enrichment") or {}, await g("apex:exits:board") or {}
    # débit du guetteur : écart du compteur d'événements depuis l'appel précédent
    prev = state.get("bureau_prev")
    ev = int(ing.get("events", 0) or 0)
    rate = None
    if prev and now - prev[0] > 1 and ev >= prev[1]:
        rate = round((ev - prev[1]) / (now - prev[0]) * 60)
    state["bureau_prev"] = (now, ev)
    primary = state["cfg"]["labels"]["primary"]
    ens = (ln.get("ensembles") or {}).get(primary) or {}
    champ = ens.get("champion")
    champ_ll = next((c.get("logloss") for c in ens.get("competitors", []) if c["id"] == champ), None)
    prior_ll = next((c.get("logloss") for c in ens.get("competitors", []) if c["id"] == "prior"), None)
    alerts = rows(await db().fetch(
        """SELECT a.ts, t.symbol, t.name FROM alerts a LEFT JOIN tokens t USING (mint)
           WHERE a.ts > now() - interval '24 hours' ORDER BY a.ts DESC LIMIT 4"""))
    events = rows(await db().fetch(
        """SELECT ts, kind, left(message, 140) message FROM system_events
           WHERE ts > now() - interval '12 hours' AND kind IN ('champion','lgbm','evolution','coherence','menage',
                 'urgent','rollback','trading','claude','report','ev') ORDER BY ts DESC LIMIT 12"""))
    paper = await db().fetchrow(
        """SELECT count(*) FILTER (WHERE status='closed') n, count(*) FILTER (WHERE status='open') ouverts,
                  coalesce(sum(pnl_sol) FILTER (WHERE status='closed'), 0) pnl_sol,
                  count(*) FILTER (WHERE status='closed' AND closed_at > now() - interval '24 hours') n24,
                  coalesce(sum(pnl_sol) FILTER (WHERE status='closed' AND closed_at > now() - interval '24 hours'), 0) pnl24
           FROM paper_positions""")
    cfeat = await db().fetchrow(
        "SELECT count(*) FILTER (WHERE status='validated') ok, count(*) n, max(created_at) dernier FROM claude_features")
    best = next((r for r in (board.get("selection") or []) if int(r.get("n", 0)) >= 300), None)
    checks = ts_.get("checks") or {}
    return {
        "now": now,
        "vie": {s: round(now - t) for s, t in hb.items()},
        "guetteur": {"par_min": rate, "retard_s": lag.get("moyen_5min"), "en_retard": ing.get("late"),
                     "reconnexions": ing.get("reconnects"), "lancements_h": mkt.get("launches_per_h"),
                     "migrations_h": mkt.get("migrations_per_h"), "ambiance": mkt.get("regime"),
                     "sol_usd": mkt.get("sol_price_usd"), "pools": ing.get("pumpswap_pools")},
        "analyste": {"depuis_derniere_s": round(now - (ln.get("last_decision_ts") or now)),
                     "candidats": enr.get("candidates"), "credits": enr.get("credits_used_today"),
                     "credits_max": enr.get("credits_budget")},
        "chrono": {"suivis": lab.get("tracked"), "en_attente": lab.get("pending"),
                   "migres": lab.get("migrated_followed"), "simules": lab.get("simulated_decisions"),
                   "situations": (lab.get("sortie_apprise") or {}).get("situations_apprises")},
        "chercheur": {"appris": ln.get("n_labels"), "par_heure": health.get("labels_last_hour"),
                      "champion": champ, "gain_vs_hasard": (round(1 - champ_ll / prior_ll, 3)
                                                             if champ_ll and prior_ll else None),
                      "concurrents": len(ens.get("competitors", [])), "lgbm_version": ln.get("lgbm_version")},
        "trader": {"etat": ts_.get("state"), "criteres_ok": sum(1 for v in checks.values() if v and v[0]),
                   "criteres": len(checks), "n": (ts_.get("stats") or {}).get("n"),
                   "rendement": (ts_.get("stats") or {}).get("total_return"), "ouverts": trd.get("open_positions"),
                   "entrainement": {k: (float(v) if v is not None else None) for k, v in dict(paper or {}).items()},
                   "meilleure_sortie": best and {"description": best.get("description"), "mean": best.get("mean")}},
        "cheffe": {"age_s": round(now - (sup.get("ts") or now)), "duree_s": sup.get("duration_s"),
                   "evenements": (sup.get("events") or [])[:3], "coherence_ok": co.get("ok"), "coherence": co.get("resume")},
        "facteur": {"alertes_jour": ln.get("alerts_today"), "dernieres": alerts},
        "claude": {"ok": cl.get("ok"), "vu_s": round(now - cl["ts"]) if cl.get("ts") else None,
                   "idees_validees": cfeat and cfeat["ok"], "idees": cfeat and cfeat["n"],
                   "derniere_idee": cfeat and cfeat["dernier"]},
        "fil": events,
    }


def main() -> None:
    import uvicorn

    cfg = Config.load()
    uvicorn.run(app, host=cfg.get("dashboard.host"), port=cfg.get("dashboard.port"), log_level="warning")
