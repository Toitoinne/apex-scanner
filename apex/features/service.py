"""Service FEATURE ENGINE : apex:raw → apex:decisions (+ persistance).

Consomme aussi apex:closed pour mettre à jour les bases smart wallets / devs
ruggers à chaque token clôturé, et recharge les features Claude actives.
"""
from __future__ import annotations

import asyncio
import logging
import time

import httpx

from .. import bus as B
from ..claude_improver.sandbox import UnsafeCode, compile_feature
from ..config import Config, secrets
from ..db import DB, ts
from ..events import Decision, Migration, TokenCreated
from ..safety.filters import SafetyFilters
from .engine import ClaudeFeature, FeatureEngine
from .market import MarketContext
from .metadata import MetadataFetcher
from .wallets import UPSERT_WALLET, WalletIntel

log = logging.getLogger("features")
GROUP = "features"
SOL_MINT = "So11111111111111111111111111111111111111112"


class FeatureService:
    def __init__(self, cfg: Config, bus: B.Bus, db: DB):
        self.cfg, self.bus, self.db = cfg, bus, db
        c = cfg.data
        use_rpc = bool(secrets().helius_api_key) and c["features"].get("enrichment_daily_credit_budget", 0) > 0
        self.intel = WalletIntel(c["features"], rpc_url=secrets().rpc_url() if use_rpc else None, db=db)
        self.market = MarketContext(c["features"]["narrative_window_s"])
        self.engine = FeatureEngine(c, SafetyFilters(cfg.safety), self.intel, self.market)
        self._dec_buffer: list[tuple] = []
        self._auth_sem = asyncio.Semaphore(4)
        self.meta = MetadataFetcher()
        self._requested: dict[str, set[str]] = {}   # mint -> wallets déjà envoyés à l'enrichissement
        self._prio: dict[str, float] = {}           # mint -> priorité (proba du champion, ou heuristique)

    async def publish_decisions(self, decisions: list[Decision]) -> None:
        for d in decisions:
            await self.bus.publish(B.DECISIONS, d)
            self._dec_buffer.append((
                ts(d.ts), d.decision_id, d.mint, d.point, d.features, d.feature_versions, d.entry_price,
                d.mc_sol, d.blocked, d.safety_flags, self.cfg.version(),
            ))

    async def flush_loop(self) -> None:
        while True:
            await asyncio.sleep(2)
            buf, self._dec_buffer = self._dec_buffer, []
            if buf:
                try:
                    await self.db.executemany(
                        """INSERT INTO decisions (ts, decision_id, mint, point, features, feature_versions, entry_price,
                           mc_sol, blocked, safety_flags, config_version) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11)
                           ON CONFLICT DO NOTHING""", buf)
                except Exception:  # noqa: BLE001
                    log.exception("persistance des décisions")

    async def fetch_meta(self, ev: TokenCreated) -> None:
        meta = await self.meta.fetch(ev.uri)
        st = self.engine.states.get(ev.mint)
        if st is not None:
            st.meta = meta
        if meta:
            try:
                await self.db.execute("UPDATE tokens SET meta=$2 WHERE mint=$1", ev.mint, meta)
            except Exception:  # noqa: BLE001
                log.debug("métadonnées non persistées")

    async def on_created_authorities(self, ev: TokenCreated) -> None:
        """Valeur par défaut identique pour TOUS les tokens (pas de biais entre tokens
        enrichis et non enrichis) : pump.fun révoque mint/freeze authority à la création.
        La vérification RPC (mode all / candidates) peut ensuite corriger à 1 → filtre dur."""
        st = self.engine.states.get(ev.mint)
        if st:
            st.mint_authority = st.freeze_authority = 0.0
        if self.cfg.get("features.check_authorities") == "all":
            await self.check_authorities(ev.mint)

    async def check_authorities(self, mint: str) -> None:
        if not secrets().helius_api_key or self.cfg.get("features.check_authorities") == "none":
            return
        if not self.intel.spend():
            return
        async with self._auth_sem:
            try:
                async with httpx.AsyncClient(timeout=5) as c:
                    r = await c.post(secrets().rpc_url(), json={
                        "jsonrpc": "2.0", "id": 1, "method": "getAccountInfo",
                        "params": [mint, {"encoding": "jsonParsed", "commitment": "confirmed"}]})
                    info = r.json()["result"]["value"]["data"]["parsed"]["info"]
                st = self.engine.states.get(mint)
                if st:
                    st.mint_authority = 1.0 if info.get("mintAuthority") else 0.0
                    st.freeze_authority = 1.0 if info.get("freezeAuthority") else 0.0
            except Exception as e:  # noqa: BLE001
                log.debug("autorités %s : %s", mint, e)

    def _request_buyers(self, mint: str) -> None:
        st = self.engine.states.get(mint)
        if st is None:
            return
        cap = self.cfg.get("features.enrichment_max_wallets_per_token", 15)
        done = self._requested.setdefault(mint, set())
        if len(done) >= cap:
            return
        # les plus GROS acheteurs comptent le plus pour juger l'indépendance des acheteurs
        top = sorted(st.sol_in, key=st.sol_in.get, reverse=True)[:cap]
        prio = self._prio.get(mint, 0.0)
        for w in top:
            if w not in done and len(done) < cap:
                self.intel.request_funding(w, priority=prio, ttl_s=max(30.0, 900 - (time.time() - st.t0)))
                done.add(w)

    async def _load_credit_counters(self) -> None:
        """Les compteurs jour/heure survivent aux redémarrages (sinon chaque redémarrage
        rouvrirait un budget journalier complet)."""
        now = time.time()
        day = await self.bus.r.get("apex:enrich:day:" + time.strftime("%Y%m%d", time.gmtime(now)))
        hour = await self.bus.r.get("apex:enrich:hour:" + time.strftime("%Y%m%d%H", time.gmtime(now)))
        self.intel._credit_day, self.intel._credit_hour = int(now // 86400), int(now // 3600)
        self.intel.credits_used = int(float(day or 0))
        self.intel.credits_hour = int(float(hour or 0))

    async def _save_credit_counters(self) -> None:
        now = time.time()
        await self.bus.r.set("apex:enrich:day:" + time.strftime("%Y%m%d", time.gmtime(now)), self.intel.credits_used, ex=2 * 86400)
        await self.bus.r.set("apex:enrich:hour:" + time.strftime("%Y%m%d%H", time.gmtime(now)), self.intel.credits_hour, ex=7200)

    async def enrichment_loop(self) -> None:
        """Enrichissement CIBLÉ : graphe de financement et autorités uniquement pour les tokens
        candidats (heuristique précoce ou proba du champion proche du seuil d'alerte)."""
        tick = 0
        await self._load_credit_counters()
        billed = self.intel.credits_used
        while True:
            await asyncio.sleep(1)
            tick += 1
            try:
                for m, score in await self.bus.r.zpopmax("apex:candidates", 200) or []:
                    m = m.decode() if isinstance(m, bytes) else m
                    # candidat désigné par le modèle : priorité = 1 + proba (au-dessus des heuristiques)
                    self._prio[m] = max(self._prio.get(m, 0.0), 1.0 + float(score))
                    if m in self.engine.candidates:
                        if self.cfg.get("features.check_authorities") == "candidates":
                            B.spawn(self.check_authorities(m))
                        self._request_buyers(m)
                    self.engine.mark_candidate(m)
            except Exception:  # noqa: BLE001
                log.exception("lecture des candidats")
            new, self.engine.new_candidates = self.engine.new_candidates, []
            for m in new:
                st = self.engine.states.get(m)
                if m not in self._prio and st:
                    self._prio[m] = min(0.99, len(st.first_buy_ts) / 100)   # heuristique : nb d'acheteurs
                if self.cfg.get("features.check_authorities") == "candidates" and self._prio.get(m, 0) >= 1.0:
                    B.spawn(self.check_authorities(m))           # autorités : candidats du modèle seulement
                self._request_buyers(m)
            if tick % 10 == 0:     # nouveaux acheteurs des candidats encore jeunes
                now = time.time()
                for m in list(self.engine.candidates):
                    st = self.engine.states.get(m)
                    if st and now - st.t0 < 900:
                        self._request_buyers(m)
                for m in [m for m in self._requested if m not in self.engine.states]:
                    del self._requested[m]
                    self._prio.pop(m, None)
                delta = self.intel.credits_used - billed if self.intel.credits_used >= billed else self.intel.credits_used
                billed = self.intel.credits_used
                key = "apex:helius:credits:" + time.strftime("%Y%m", time.gmtime())
                if delta:
                    await self.bus.r.incrbyfloat(key, delta)
                    await self.bus.r.expire(key, 40 * 86400)
                await self._save_credit_counters()
                pool = float(await self.bus.r.get(key) or 0)
                wd = self.cfg.get("ingestion.watchdog") or {}
                limit = wd.get("monthly_credit_cap", 980_000) - wd.get("backup_reserve_credits", 150_000)
                if pool >= limit and not self.intel.pool_blocked:
                    log.warning("plafond mensuel Helius atteint pour l'enrichissement (%.0f) : pause jusqu'au mois prochain", pool)
                self.intel.pool_blocked = pool >= limit
                await self.bus.set_json("apex:metadata", self.meta.stats)
                # voie rapide : candidats les plus prometteurs, encore jeunes (≤ 15 min)
                # seulement les candidats désignés par le MODÈLE (priorité ≥ 1), pas les heuristiques
                young = [(self._prio.get(m, 0.0), m) for m in self.engine.candidates
                         if self._prio.get(m, 0.0) >= 1.0
                         and (st := self.engine.states.get(m)) is not None and now - st.t0 < 900]
                await self.bus.set_json("apex:fastlane:candidates", [m for _, m in sorted(young, reverse=True)][:40])
                await self.bus.set_json("apex:enrichment", {
                    "candidates": len(self.engine.candidates), "credits_used_today": self.intel.credits_used,
                    "credits_budget": self.intel.daily_credits, "funding_known": len(self.intel.funder),
                    "credits_this_hour": self.intel.credits_hour, "hourly_budget": self.intel.hourly_credits,
                    "queue": self.intel._queue.qsize(),
                    "operator_wallets": len(self.intel.operators.p)})

    async def consume_raw(self) -> None:
        async for stream, mid, ev in self.bus.consume([B.RAW], GROUP, "features-1"):
            try:
                decs = self.engine.on_event(ev)
            except Exception:  # noqa: BLE001
                log.exception("événement ignoré")
                decs = []
            if isinstance(ev, TokenCreated):
                B.spawn(self.on_created_authorities(ev))
                B.spawn(self.fetch_meta(ev))
                await self.db.execute(
                    """INSERT INTO tokens (mint, chain, name, symbol, uri, creator, bonding_curve, created_at, created_slot)
                       VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9) ON CONFLICT (mint) DO NOTHING""",
                    ev.mint, ev.chain, ev.name, ev.symbol, ev.uri, ev.creator, ev.bonding_curve, ts(ev.ts), ev.slot)
            elif isinstance(ev, Migration):
                await self.db.execute("UPDATE tokens SET migrated_at=$2 WHERE mint=$1", ev.mint, ts(ev.ts))
            if decs:
                await self.publish_decisions(decs)
            await self.bus.ack(stream, GROUP, mid)

    async def consume_closed(self) -> None:
        async for stream, mid, closed in self.bus.consume([B.CLOSED], GROUP, "features-closed"):
            rows = self.intel.update_from_closed(closed)
            await self.db.executemany(UPSERT_WALLET, rows)
            dev = closed.get("creator")
            if dev:
                d = self.intel.dev_stats(dev)
                await self.db.execute(
                    """INSERT INTO devs (address, n_tokens, n_rugs, n_winners, last_token) VALUES ($1,$2,$3,$4,$5)
                       ON CONFLICT (address) DO UPDATE SET n_tokens=$2, n_rugs=$3, n_winners=$4, last_token=$5, updated_at=now()""",
                    dev, d["n_tokens"], d["n_rugs"], d["n_winners"], closed["mint"])
            await self.bus.ack(stream, GROUP, mid)

    async def tick_loop(self) -> None:
        while True:
            try:
                decs = self.engine.tick(time.time())
            except Exception:  # noqa: BLE001
                log.exception("tick du feature engine")
                decs = []
            if decs:
                await self.publish_decisions(decs)
            await asyncio.sleep(0.2)

    async def claude_features_loop(self) -> None:
        """Recharge les features Claude actives (statut shadow/champion)."""
        budget = self.cfg.get("features.claude_feature_timeout_ms", 5)
        while True:
            try:
                rows = await self.db.fetch("SELECT feature_id, version, code FROM claude_features WHERE status IN ('shadow','champion','validated')")
                active = {r["feature_id"] for r in rows}
                for r in rows:
                    cur = self.engine.claude_features.get(r["feature_id"])
                    if cur and cur.version == r["version"]:
                        continue
                    try:
                        self.engine.claude_features[r["feature_id"]] = ClaudeFeature(r["feature_id"], r["version"], compile_feature(r["code"]), budget)
                        log.info("feature Claude chargée : %s v%s", r["feature_id"], r["version"])
                    except UnsafeCode as e:
                        await self.db.execute("UPDATE claude_features SET status='rejected', reason=$2 WHERE feature_id=$1", r["feature_id"], str(e))
                for fid in list(self.engine.claude_features):
                    cf = self.engine.claude_features[fid]
                    if fid not in active:
                        del self.engine.claude_features[fid]
                    elif cf.disabled_reason:
                        await self.db.execute("UPDATE claude_features SET status='disabled', reason=$2, decided_at=now() WHERE feature_id=$1", fid, cf.disabled_reason)
                        del self.engine.claude_features[fid]
            except Exception:  # noqa: BLE001
                log.exception("rechargement features Claude")
            await asyncio.sleep(60)

    async def market_loop(self) -> None:
        async with httpx.AsyncClient(timeout=5) as c:
            while True:
                try:
                    r = await c.get(f"https://lite-api.jup.ag/price/v3?ids={SOL_MINT}")
                    self.market.sol_price_usd = float(r.json()[SOL_MINT]["usdPrice"])
                except Exception:  # noqa: BLE001
                    pass
                snap = self.market.snapshot(time.time())
                await self.db.execute(
                    "INSERT INTO market_context (ts, temperature, launches_per_h, migrations_per_h, sol_price_usd, data) VALUES (now(),$1,$2,$3,$4,$5)",
                    snap["temperature"], snap["launches_per_h"], snap["migrations_per_h"], snap["sol_price_usd"],
                    {"active_tokens": len(self.engine.states), "regime": self.market.regime(time.time())})
                await self.bus.set_json("apex:market", {**snap, "regime": self.market.regime(time.time())})
                await self.bus.heartbeat("features")
                await asyncio.sleep(60)

    async def rebuild(self) -> None:
        """Reconstruit l'état des tokens actifs (2 h) après un redémarrage, sans réémettre
        les décisions déjà passées."""
        events, last_ts = await self.bus.history([B.RAW], GROUP, 7200)
        for ev in events:
            self.engine.on_event(ev)
        for st in self.engine.states.values():
            for s in self.engine.points:
                if st.t0 + s <= last_ts:
                    st.emitted_points.add(str(s))
            if st.migrated_ts:
                st.emitted_points.add("migration")
            for pt, due in st.due.items():          # points d'étude (après migration, 2e vague) déjà échus
                if due <= last_ts:
                    st.emitted_points.add(pt)
        log.info("état reconstruit : %d événements, %d tokens actifs", len(events), len(self.engine.states))

    async def run(self) -> None:
        await self.intel.load(self.db)
        if not await self.bus.r.get("apex:wallets:v2"):
            # une seule fois : recalcul des smart wallets avec la méthode v2 (gains encaissés, robots écartés)
            hb = asyncio.create_task(B.heartbeat_loop(self.bus, "features"))
            try:
                await self.intel.rebuild_from_trades(self.db)
                await self.bus.r.set("apex:wallets:v2", "1")
            except Exception:  # noqa: BLE001
                log.exception("recalcul des wallets v2 impossible : on garde l'ancienne base")
            finally:
                hb.cancel()
        await self.rebuild()
        await asyncio.gather(
            self.consume_raw(), self.consume_closed(), self.tick_loop(), self.flush_loop(),
            self.claude_features_loop(), self.market_loop(), self.intel.funding_worker(), self.enrichment_loop(),
        )


async def main() -> None:
    cfg = Config.load()
    db = await DB.connect(secrets().database_url)
    await FeatureService(cfg, B.Bus(secrets().redis_url), db).run()
