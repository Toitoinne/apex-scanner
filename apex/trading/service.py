"""Service TRADER : système d'ordres.

Mode SIMULATION (par défaut, dès maintenant) : chaque alerte devient un ordre d'achat, chaque
signal de vente un ordre de vente, exécutés de façon réaliste (délai, profondeur, frais, échecs).
La vraie transaction PumpPortal est construite et vérifiée, mais JAMAIS envoyée.

Mode RÉEL : uniquement si TOUTES ces conditions sont vraies :
  config trading.live_enabled = true · état de trading ACTIF (prouvé par le paper/simulation) ·
  activation par l'utilisateur (/activer) · portefeuille dédié configuré · pas d'arrêt d'urgence (/stop).
Les limites de risque s'appliquent aux deux modes.
"""
from __future__ import annotations

import asyncio
import logging
import time

import httpx

from .. import bus as B
from ..config import Config, secrets
from ..db import DB
from ..events import PriceTick, Trade
from .engine import REAL, SIM, Order, TraderEngine
from .fills import Fees, Fill, Market
from .risk import Limits
from .venues import LiveVenue, PumpPortalBuilder

log = logging.getLogger("trader")
GROUP = "trader"


class TraderService:
    def __init__(self, cfg: Config, bus: B.Bus, db: DB):
        self.cfg, self.bus, self.db = cfg, bus, db
        ex = cfg.get("execution") or {}
        self.ex = ex
        f = cfg.data["fees"]
        self.engine = TraderEngine(
            limits=Limits.from_cfg(cfg.get("risk")),
            fees=Fees(curve_bps=f["pump_fee_bps"], amm_bps=f["pumpswap_fee_bps"],
                      platform_pct=ex.get("platform_fee_pct", 0.5), network_sol=ex.get("priority_fee_sol", 0.0005)),
            latency_s=ex.get("latency_s", 2.0), max_buy_slippage=ex.get("max_buy_slippage", 0.15),
            sell_slippages=tuple(ex.get("sell_slippages", [0.15, 0.30, 0.60, 1.0])),
        )
        self.client = httpx.AsyncClient()
        self.builder = PumpPortalBuilder(self.client)
        self.live: LiveVenue | None = None
        key = secrets().wallet_private_key
        if key:
            try:
                self.live = LiveVenue(secrets().rpc_url(), key, self.builder, self.client)
                log.info("portefeuille dédié configuré : %s…", self.live.pubkey[:6])
            except Exception:  # noqa: BLE001
                log.error("clé de portefeuille invalide (WALLET_PRIVATE_KEY) : trading réel impossible")
        self.stats = {"orders": 0, "filled": 0, "failed": 0, "skipped": 0, "live_orders": 0}

    # ---------------- mode ----------------
    async def mode_now(self) -> tuple[str, str]:
        """Retourne (mode, raison si simulation)."""
        if not self.cfg.get("trading.live_enabled", False):
            return SIM, "trading réel non activé dans la configuration"
        st = await self.bus.get_json("apex:trading:state", {}) or {}
        if st.get("state") != "ACTIF":
            return SIM, f"état {st.get('state', 'APPRENTISSAGE')} (il faut ACTIF)"
        if await self.bus.r.get("apex:trading:killed"):
            return SIM, "arrêt d'urgence (/stop)"
        if not await self.bus.r.get("apex:trading:armed"):
            return SIM, "pas activé par l'utilisateur (/activer)"
        if self.live is None:
            return SIM, "aucun portefeuille dédié configuré"
        return REAL, ""

    async def today(self, mode: str) -> tuple[float, int]:
        r = await self.db.fetchrow(
            """SELECT coalesce(sum(pnl_sol) FILTER (WHERE status IN ('closed','failed')), 0) pnl,
                      count(*) n FROM exec_positions WHERE mode=$1 AND opened_at > date_trunc('day', now())""", mode)
        return float(r["pnl"]), int(r["n"])

    # ---------------- flux ----------------
    async def consume(self, first: bool = True) -> None:
        async for stream, mid, ev in self.bus.consume([B.ALERTS, B.SIGNALS, B.RAW], GROUP, "trader-1",
                                                      replay_pending=first, count=500, start_id="$"):
            # start_id "$" : un nouveau groupe ne traite que les NOUVELLES alertes (jamais l'historique)
            try:
                if stream == B.RAW:
                    self.on_market(ev)
                elif stream == B.ALERTS:
                    await self.on_alert(ev)
                elif stream == B.SIGNALS:
                    await self.refresh_feed_lag()
                    o = self.engine.on_signal(ev, time.time())
                    if o is not None and o.mode == REAL:
                        await self.execute_live(o)
            except Exception:  # noqa: BLE001
                log.exception("événement ignoré")
            await self.bus.ack(stream, GROUP, mid)

    def on_market(self, ev) -> None:
        if isinstance(ev, Trade) and self.engine.watching(ev.mint):
            self.engine.on_market(ev.mint, Market(ev.ts, ev.price, ev.v_sol, ev.v_tokens))
        elif isinstance(ev, PriceTick) and self.engine.watching(ev.mint):
            self.engine.on_market(ev.mint, Market(ev.ts, ev.price, pool_sol=getattr(ev, "pool_sol", None), migrated=True))

    async def refresh_feed_lag(self) -> None:
        """Le bot voit le marché avec le retard du flux : un vrai ordre arriverait d'autant plus tard.
        La simulation exécute donc au prix du moment où l'ordre serait VRAIMENT arrivé."""
        lag = await self.bus.get_json("apex:feed:lag", {}) or {}
        fresh = time.time() - lag.get("ts", 0) < 120
        self.engine.feed_lag_s = min(60.0, float(lag.get("moyen_5min", 0.0))) if fresh else 0.0

    async def on_alert(self, alert: dict) -> None:
        await self.refresh_feed_lag()
        mode, why = await self.mode_now()
        realized, n_today = await self.today(mode)
        wallet = None
        if mode == REAL and self.live is not None:
            wallet = await self.live.balance_sol()
        o, reason = self.engine.open(alert, mode, time.time(), killed=bool(await self.bus.r.get("apex:trading:killed")),
                                     realized_today_sol=realized, trades_today=n_today, wallet_sol=wallet)
        if o is None:
            self.stats["skipped"] += 1
            await self.record_skip(alert, mode, reason)
            return
        p = self.engine.positions[o.decision_id]
        await self.db.execute(
            """INSERT INTO exec_positions (decision_id, mint, symbol, mode, policy) VALUES ($1,$2,$3,$4,$5)
               ON CONFLICT (decision_id) DO NOTHING""", p.decision_id, p.mint, p.symbol, p.mode, p.policy)
        if mode == SIM and self.ex.get("check_pumpportal", True):
            # preuve que la chaîne réelle fonctionne : la transaction est construite et vérifiée, puis jetée
            raw = await self.builder.build(self.ex.get("dry_run_pubkey"), "buy", o.mint, o.sol, True,
                                           self.engine.max_buy_slippage * 100, self.engine.fees.network_sol)
            o.builder_ok = raw is not None  # type: ignore[attr-defined]
        if mode == REAL:
            await self.execute_live(o)

    # ---------------- exécution ----------------
    async def execute_live(self, o: Order) -> None:
        assert self.live is not None
        self.stats["live_orders"] += 1
        if o.side == "buy":
            r = await self.live.execute("buy", o.mint, o.sol, True, self.engine.max_buy_slippage * 100,
                                        self.engine.fees.network_sol)
            f = Fill(r.ok, "buy", -r.sol_delta if r.ok else max(0.0, -r.sol_delta), r.token_delta if r.ok else 0.0,
                     (-r.sol_delta / r.token_delta) if r.ok and r.token_delta else 0.0, 0.0, 0.0, r.error)
        else:
            tol = self.engine.sell_slippages[min(o.attempt, len(self.engine.sell_slippages) - 1)]
            r = await self.live.execute("sell", o.mint, o.tokens, False, tol * 100, self.engine.fees.network_sol)
            f = Fill(r.ok, "sell", r.sol_delta, -r.token_delta if r.ok else 0.0,
                     (r.sol_delta / -r.token_delta) if r.ok and r.token_delta else 0.0, 0.0, 0.0, r.error)
            if not r.ok and o.attempt + 1 < len(self.engine.sell_slippages):
                o.attempt += 1
                await asyncio.sleep(2)
                return await self.execute_live(o)
        self.engine.pending = [x for x in self.engine.pending if x is not o]
        self.engine.apply(o, f, time.time())
        await self.record(o, f, r.signature, r.latency_s)

    async def step_loop(self) -> None:
        last_sync = 0.0
        while True:
            for o, f in self.engine.step(time.time()):
                await self.record(o, f)
            self.engine.forget_closed()
            if time.time() - last_sync > 60:          # positions orphelines (signal manqué pendant un redémarrage)
                last_sync = time.time()
                try:
                    opened = [d for d, p in self.engine.positions.items() if p.status == "open"]
                    if opened:
                        rows = await self.db.fetch("SELECT decision_id, (state->>'last_price')::float8 p FROM paper_positions "
                                                   "WHERE status='closed' AND decision_id = ANY($1)", opened)
                        self.engine.orphans({r["decision_id"]: r["p"] or 0.0 for r in rows})
                except Exception:  # noqa: BLE001
                    log.exception("synchronisation des positions")
            await asyncio.sleep(0.25)

    # ---------------- persistance / notifications ----------------
    async def record_skip(self, alert: dict, mode: str, reason: str) -> None:
        await self.db.execute(
            """INSERT INTO exec_orders (decision_id, mint, side, mode, reason, status, error)
               VALUES ($1,$2,'buy',$3,'alerte','skipped',$4)""", alert["decision_id"], alert["mint"], mode, reason)

    async def record(self, o: Order, f: Fill, sig: str = "", latency: float | None = None) -> None:
        self.stats["orders"] += 1
        self.stats["filled" if f.ok else "failed"] += 1
        await self.db.execute(
            """INSERT INTO exec_orders (decision_id, mint, side, mode, reason, sol, tokens, expected_price, fill_price,
               slippage, fees_sol, status, error, tx_signature, latency_s, builder_ok)
               VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16)""",
            o.decision_id, o.mint, o.side, o.mode, o.reason, f.sol, f.tokens, o.expected_price, f.price, f.slippage,
            f.fees_sol, "filled" if f.ok else "failed", f.reason or None, sig or None,
            latency if latency is not None else (time.time() - o.created), getattr(o, "builder_ok", None))
        p = self.engine.positions.get(o.decision_id)
        if p is None:
            return
        price = self.engine.last_price(p.mint)
        await self.db.execute(
            """UPDATE exec_positions SET sol_in=$2, sol_out=$3, tokens_initial=$4, tokens=$5, status=$6,
               pnl_sol=$7, pnl=$8, closed_at = CASE WHEN $6 IN ('closed','failed') THEN now() ELSE closed_at END
               WHERE decision_id=$1""",
            p.decision_id, p.sol_in, p.sol_out, p.tokens_initial, p.tokens, p.status, p.pnl_sol, p.pnl(price))
        if p.mode == REAL:
            verb = "achat" if o.side == "buy" else "vente"
            await self.bus.publish(B.NOTIFY, {"type": "urgent", "text": (
                f"{'✅' if f.ok else '❌'} Ordre RÉEL ({verb}) ${p.symbol} : "
                + (f"{f.sol:.4f} SOL, prix {f.price:.3e}" if f.ok else f"échec — {f.reason}")
                + (f"\nhttps://solscan.io/tx/{sig}" if sig else ""))})

    async def reload(self) -> None:
        """Reprise après redémarrage : positions ouvertes rechargées depuis la base."""
        from .engine import Position
        for r in await self.db.fetch("SELECT * FROM exec_positions WHERE status IN ('open','pending')"):
            self.engine.positions[r["decision_id"]] = Position(
                r["decision_id"], r["mint"], r["mode"], r["symbol"] or "", r["policy"] or "", r["opened_at"].timestamp(),
                r["sol_in"], r["sol_out"], r["tokens_initial"], r["tokens"], "open" if r["tokens"] > 0 else r["status"],
                stake=self.engine.limits.sol_per_trade)
            self.engine._watch(r["mint"], None)

    async def stats_loop(self) -> None:
        while True:
            mode, why = await self.mode_now()
            await self.bus.set_json("apex:trader:stats", {
                **self.stats, "mode": mode, "why_not_live": why, "wallet_configured": self.live is not None,
                "pumpportal_builder": self.builder.stats, "open_positions": sum(
                    1 for p in self.engine.positions.values() if p.status in ("open", "pending")),
                "pending_orders": len(self.engine.pending), "ts": time.time()})
            await self.bus.heartbeat("trader")
            await asyncio.sleep(10)

    async def run(self) -> None:
        await self.reload()
        await asyncio.gather(
            B.resilient("consume", lambda first: self.consume(first)),
            B.resilient("step", lambda _: self.step_loop()),
            B.resilient("stats", lambda _: self.stats_loop()),
        )


async def main() -> None:
    cfg = Config.load()
    db = await DB.connect(secrets().database_url)
    await TraderService(cfg, B.Bus(secrets().redis_url), db).run()
