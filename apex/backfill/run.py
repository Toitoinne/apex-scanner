"""Commande de démarrage à chaud : `python -m apex backfill`.

1. Télécharge 7–14 jours d'historique (Dune, mis en cache).
2. Rejoue chronologiquement (mêmes composants qu'en production).
3. Écrit : état du learner (latest.pkl + snapshot STABLE), bases wallets/devs,
   courbes de référence de la boucle 2.
Si le backfill échoue, le learner démarre sur le concurrent « règles + momentum »
et les modèles prennent le relais selon leur précision live.
"""
from __future__ import annotations

import logging
import time
from pathlib import Path

from ..config import Config, secrets
from ..db import DB
from .dune import fetch_days, iter_events
from .replay import Replay

log = logging.getLogger("backfill")


async def main() -> None:
    cfg = Config.load()
    s = secrets()
    bf = cfg["backfill"]
    db = await DB.connect(s.database_url)
    await db.migrate()
    if bf["provider"] != "dune" or not s.dune_api_key or not bf["dune_query_id"]:
        log.warning("backfill désactivé ou non configuré : démarrage à froid (règles + momentum)")
        await db.log_event("warning", "backfill", "démarrage à froid : backfill non configuré")
        return
    data = Path(s.data_dir)
    try:
        paths = await fetch_days(s.dune_api_key, int(bf["dune_query_id"]), int(bf["days"]), int(bf["sample_mod"]), data / "backfill")
    except Exception as e:  # noqa: BLE001
        log.exception("échec du téléchargement")
        await db.log_event("error", "backfill", f"échec backfill, démarrage à froid : {e}")
        return
    t0 = time.time()
    rp = Replay(cfg)
    ref = rp.run(iter_events(paths))
    log.info("rejeu terminé en %.0f s : %d décisions, %d labels", time.time() - t0, rp.n_decisions, len(rp.log))
    snap_dir = data / "snapshots"
    snap_dir.mkdir(parents=True, exist_ok=True)
    rp.learner.cache.clear()
    rp.learner.mint_preds.clear()
    rp.learner.alerted_mints.clear()
    rp.learner.snapshot(snap_dir / "latest.pkl")
    stable = snap_dir / f"snap_backfill_{int(time.time())}.pkl"
    rp.learner.snapshot(stable)
    await db.execute("INSERT INTO snapshots (ts, path, stable, config_version, meta) VALUES (now(),$1,TRUE,$2,$3)",
                     str(stable), cfg.version(), {"label": "backfill", "decisions": rp.n_decisions})
    for curve, v in ref.items():
        await db.execute(
            """INSERT INTO reference_curves (curve, value, n, source) VALUES ($1,$2,$3,'backfill')
               ON CONFLICT (curve) DO UPDATE SET value=$2, n=$3, source='backfill', created_at=now()""",
            curve, v["value"], v["n"])
    rows = [(w, int(st["n_trades"]), int(st["n_wins"]), float(st["pnl"]), w in rp.intel.smart, w in rp.intel.bots)
            for w, st in rp.intel.stats.items()]
    await db.executemany(
        """INSERT INTO wallets (address, n_trades, n_wins, pnl_sol, is_smart, is_bot) VALUES ($1,$2,$3,$4,$5,$6)
           ON CONFLICT (address) DO UPDATE SET n_trades=$2, n_wins=$3, pnl_sol=$4, is_smart=$5, is_bot=$6""", rows)
    await db.executemany(
        """INSERT INTO devs (address, n_tokens, n_rugs, n_winners) VALUES ($1,$2,$3,$4)
           ON CONFLICT (address) DO UPDATE SET n_tokens=$2, n_rugs=$3, n_winners=$4""",
        [(d, v["n_tokens"], v["n_rugs"], v["n_winners"]) for d, v in rp.intel.devs.items()])
    await db.log_event("info", "backfill", f"backfill terminé : {rp.n_decisions} décisions, référence {ref.get('error_rate')}")
    log.info("courbes de référence : %s", {k: round(v["value"], 4) for k, v in ref.items()})
