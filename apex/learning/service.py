"""Service LEARNER : apex:decisions + apex:labels + apex:control.

Reprise après crash sans perte : les messages ne sont acquittés qu'après une
sauvegarde automatique de l'état (toutes les 5 min). Après un crash, l'état est
restauré puis les messages non acquittés sont rejoués. Les alertes sont
dédupliquées par decision_id (contrainte UNIQUE en base).
"""
from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path

from .. import bus as B
from ..config import Config, secrets
from ..db import DB, ts
from ..events import Decision, Label, Outcome
from .core import Learner, train_lgbm_async

log = logging.getLogger("learner")
GROUP = "learner"
AUTOSAVE_S = 300


class LearnerService:
    def __init__(self, cfg: Config, bus: B.Bus, db: DB):
        self.cfg, self.bus, self.db = cfg, bus, db
        self.learner = Learner(cfg)
        self.snap_dir = Path(secrets().data_dir) / "snapshots"
        self.snap_dir.mkdir(parents=True, exist_ok=True)
        self.latest = self.snap_dir / "latest.pkl"
        self._unacked: dict[str, list[bytes]] = {}
        self._pred_rows: list[tuple] = []
        self._eval_rows: list[tuple] = []
        self._lock = asyncio.Lock()
        self._latency: list[float] = []

    # ------------------------------------------------------------------
    async def handle(self, stream: str, mid: bytes, ev) -> None:
        L = self.learner
        if isinstance(ev, Decision):
            t0 = time.perf_counter()
            rows, alert = L.on_decision(ev)
            if L.last_candidate and ev.point != "migration":
                p = next((r[6] for r in rows if r[3] == L.primary), 0.0)
                await self.bus.r.zadd("apex:candidates", {ev.mint: p}, gt=True)
            now = ts(ev.ts)
            cv = self.cfg.version()
            self._pred_rows += [(now, did, mint, point, h, cid, raw, cal, ch, cv) for did, mint, point, h, cid, raw, cal, ch in rows]
            if alert:
                await self.emit_alert(alert)
            self._latency.append(time.perf_counter() - t0)
        elif isinstance(ev, Outcome):
            L.on_outcome(ev)
        elif isinstance(ev, Label) and ev.horizon in L.long:
            row = await self.db.fetchrow(
                "SELECT features FROM decisions WHERE decision_id=$1 ORDER BY ts DESC LIMIT 1", ev.decision_id)
            res = L.on_label_long(ev, row["features"] if row else {})
            self._eval_rows += [(ts(ev.ts), *r) for r in res.evaluations]
        elif isinstance(ev, Label):
            t0 = time.perf_counter()
            res = L.on_label(ev)
            now = ts(ev.ts)
            self._eval_rows += [(now, *r) for r in res.evaluations]
            if res.error is not None:
                dc_feats = res.error_ctx["features"]
                market = {k: v for k, v in dc_feats.items() if k.startswith("mkt_") or k == "sol_price_usd"}
                await self.db.execute(
                    """INSERT INTO errors (ts, decision_id, mint, point, horizon, error_type, model_id, p, alerted, cost,
                       features, market, outcome) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13)""",
                    now, ev.decision_id, ev.mint, ev.point, ev.horizon, res.error.error_type, res.error_ctx["model"],
                    res.error_ctx["p"], res.error_ctx["alerted"], res.error.cost, dc_feats, market, res.error_ctx["outcome"])
            if res.alert_result:
                col = "result_15" if res.alert_result.get("horizon") == "L15" else "result_60"
                await self.db.execute(f"UPDATE alerts SET {col}=$2, sim_pnl=COALESCE($3, sim_pnl) WHERE decision_id=$1",
                                      ev.decision_id, res.alert_result,
                                      res.alert_result["pnl"] if col == "result_60" else None)
                await self.bus.publish(B.NOTIFY, {"type": "alert_followup", **res.alert_result})
            await self.bus.r.lpush("apex:label_latency", f"{time.perf_counter() - t0:.6f}")
            await self.bus.r.ltrim("apex:label_latency", 0, 999)
        elif stream == B.CONTROL:
            await self.handle_command(ev)
        self._unacked.setdefault(stream, []).append(mid)

    async def emit_alert(self, alert: dict) -> None:
        aid = await self.db.fetchval(
            """INSERT INTO alerts (ts, decision_id, mint, point, p, model_id, arm, payload)
               VALUES ($1,$2,$3,$4,$5,$6,$7,$8) ON CONFLICT (decision_id) DO NOTHING RETURNING id""",
            ts(alert["ts"]), alert["decision_id"], alert["mint"], alert["point"], alert["p"], alert["model"], alert["arm"], alert)
        if aid is not None:
            alert["latency_s"] = time.time() - alert["ts"]
            await self.bus.publish(B.ALERTS, alert)

    async def handle_command(self, cmd: dict) -> None:
        op = cmd.get("op")
        try:
            if op == "snapshot":
                res = await self.snapshot(stable=cmd.get("stable", False), label=cmd.get("label", "periodic"))
            elif op == "rollback":
                path = Path(cmd["path"])
                self.learner.restore(path, keep_runtime=True)
                res = {"ok": True, "restored": str(path)}
            elif op == "retrain_lgbm":
                if cmd.get("window"):
                    self.cfg.set_override("models.lgbm_window", int(cmd["window"]))
                cid = await train_lgbm_async(self.learner, cmd.get("horizon", self.learner.primary))
                res = {"ok": cid is not None, "competitor": cid}
            else:
                res = self.learner.apply_command(cmd)
        except Exception as e:  # noqa: BLE001
            log.exception("commande %s", op)
            res = {"ok": False, "error": str(e)}
        await self.bus.publish(B.CONTROL_ACK, {"cmd_id": cmd.get("cmd_id"), "op": op, "result": res, "ts": time.time()})

    async def snapshot(self, stable: bool, label: str) -> dict:
        path = self.snap_dir / f"snap_{int(time.time())}.pkl"
        await asyncio.to_thread(self.learner.snapshot, path)
        sid = await self.db.fetchval(
            "INSERT INTO snapshots (ts, path, stable, config_version, meta) VALUES (now(),$1,$2,$3,$4) RETURNING id",
            str(path), stable, self.cfg.version(), {"label": label, "champions": {h: e.champion_id for h, e in self.learner.ensembles.items()}})
        await self.prune_snapshots()
        return {"ok": True, "snapshot_id": sid, "path": str(path)}

    async def prune_snapshots(self) -> int:
        """Rétention : tout ce qui a moins de 3 h, puis le dernier snapshot STABLE de chaque
        tranche de 6 h sur 3 jours, plus le snapshot du backfill. Le reste est supprimé."""
        rows = await self.db.fetch("SELECT id, ts, path, stable, meta FROM snapshots ORDER BY ts DESC")
        now = time.time()
        kept_buckets: set[int] = set()
        removed = 0
        for r in rows:
            age = now - r["ts"].timestamp()
            keep = age < 3 * 3600 or (r["meta"] or {}).get("label") == "backfill"
            if not keep and r["stable"] and age < 3 * 86400:
                bucket = int(r["ts"].timestamp() // (6 * 3600))
                if bucket not in kept_buckets:
                    kept_buckets.add(bucket)
                    keep = True
            if not keep:
                Path(r["path"]).unlink(missing_ok=True)
                await self.db.execute("DELETE FROM snapshots WHERE id=$1", r["id"])
                removed += 1
        return removed

    # ------------------------------------------------------------------
    async def consume(self, first: bool = True) -> None:
        # après une relance interne, on ne rejoue pas les messages en attente : ils ont déjà été
        # appliqués au modèle en mémoire (ils seront acquittés à la prochaine sauvegarde)
        async for stream, mid, ev in self.bus.consume([B.DECISIONS, B.LABELS, B.OUTCOMES, B.CONTROL], GROUP, "learner-1",
                                                      count=200, replay_pending=first):
            async with self._lock:
                try:
                    await self.handle(stream, mid, ev)
                except Exception:  # noqa: BLE001
                    log.exception("message ignoré (%s)", stream)
                    self._unacked.setdefault(stream, []).append(mid)

    async def flush_loop(self) -> None:
        while True:
            await asyncio.sleep(2)
            p, self._pred_rows = self._pred_rows, []
            e, self._eval_rows = self._eval_rows, []
            try:
                await self.db.copy("predictions", ["ts", "decision_id", "mint", "point", "horizon", "model_id", "p_raw", "p_cal", "is_champion", "config_version"], p)
                await self.db.copy("evaluations", ["ts", "decision_id", "horizon", "model_id", "is_champion", "p", "y", "logloss", "error_type", "cost"], e)
            except Exception:  # noqa: BLE001
                log.exception("persistance prédictions/évaluations")

    async def autosave_loop(self) -> None:
        while True:
            await asyncio.sleep(AUTOSAVE_S)
            async with self._lock:
                await asyncio.to_thread(self.learner.snapshot, self.latest)
                unacked, self._unacked = self._unacked, {}
            for stream, ids in unacked.items():
                for i in range(0, len(ids), 1000):
                    await self.bus.ack(stream, GROUP, *ids[i:i + 1000])

    async def periodic_loop(self) -> None:
        c = self.cfg
        last_champ = last_bandit = 0.0
        last_lgbm = time.time()
        while True:
            try:
                await self._periodic_once(c, last_champ, last_bandit, last_lgbm)
            except Exception:  # noqa: BLE001
                log.exception("boucle périodique (relance)")
            now = time.time()
            if now - last_champ >= c.get("models.champion_eval_every_s"):
                last_champ = now
            if now - last_bandit >= c.get("bandit.resample_every_s"):
                last_bandit = now
            if now - last_lgbm >= c.get("models.lgbm_every_s"):
                last_lgbm = now
            await asyncio.sleep(2)

    async def _periodic_once(self, c, last_champ: float, last_bandit: float, last_lgbm: float) -> None:
        now = time.time()
        async with self._lock:
            if now - last_champ >= c.get("models.champion_eval_every_s"):
                for msg in self.learner.periodic(now):
                    await self.db.log_event("info", "champion", msg)
            if now - last_bandit >= c.get("bandit.resample_every_s"):
                self.learner.set_evolved(await self.bus.get_json("apex:exits:evolved", {}) or {})
                arm = self.learner.bandit.resample()
                await self.db.log_event("info", "bandit", f"bras actif {arm.key}", {"mean": arm.mean(), "rate": self.learner.bandit.apd(arm)})
        if now - last_lgbm >= c.get("models.lgbm_every_s"):
            for h in self.learner.horizons:
                async with self._lock:
                    cid = await train_lgbm_async(self.learner, h)
                if cid:
                    await self.db.log_event("info", "lgbm", f"[{h}] {cid} ajouté comme concurrent")
        gaps = []
        for m in await self.bus.r.zrange("apex:gaps", 0, -1):
            a, b = (m.decode() if isinstance(m, bytes) else m).split(":")
            gaps.append((float(a), float(b)))
        op = await self.bus.r.get("apex:gap_open")
        if op:
            gaps.append((float(op), float("inf")))
        self.learner.gaps = gaps
        st = self.learner.state_summary()
        lat = self._latency[-500:]
        st["decision_latency_ms_p95"] = sorted(lat)[int(len(lat) * 0.95)] * 1000 if lat else None
        self._latency = lat
        await self.bus.set_json("apex:learner:state", st)
        await self.bus.heartbeat("learner")

    async def run(self) -> None:
        if self.latest.exists():
            log.info("restauration de l'état %s", self.latest)
            self.learner.restore(self.latest)
        await asyncio.gather(
            B.resilient("consume", lambda first: self.consume(first)),
            B.resilient("flush", lambda _: self.flush_loop()),
            B.resilient("autosave", lambda _: self.autosave_loop()),
            B.resilient("periodic", lambda _: self.periodic_loop()),
        )


async def main() -> None:
    cfg = Config.load()
    db = await DB.connect(secrets().database_url)
    await LearnerService(cfg, B.Bus(secrets().redis_url), db).run()
