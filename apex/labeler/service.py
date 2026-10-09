"""Service LABELER : apex:raw + apex:decisions + apex:alerts →
apex:labels (labels multi-horizons), apex:outcomes (PnL par stratégie de sortie),
apex:closed (résumés wallets/devs), apex:notify (signaux de vente).

Gère aussi :
- le flux de prix PumpSwap des tokens migrés (DexScreener, gratuit) → PriceTick dans apex:raw ;
- le paper trading des tokens alertés (positions persistées, reprises après redémarrage) ;
- la persistance des trades bruts (purgés par TimescaleDB après agrégation).
"""
from __future__ import annotations

import asyncio
import heapq
import logging
import time

import httpx

from .. import bus as B
from ..config import Config, secrets
from ..db import DB, ts
from ..events import Decision, PriceTick, TokenCreated, Trade
from ..trading.exits import DANGER_LABELS, PolicyState
from ..trading.pricefilter import PriceGuard
from .engine import OUT, LabelerEngine, Position

log = logging.getLogger("labeler")
GROUP = "labeler"
WSOL = "So11111111111111111111111111111111111111112"


class LabelerService:
    def __init__(self, cfg: Config, bus: B.Bus, db: DB):
        self.cfg, self.bus, self.db = cfg, bus, db
        self.engine = LabelerEngine(cfg.data)
        self._trades: list[tuple] = []
        self._ticks: list[tuple] = []
        self.pools: dict[str, str] = {}            # mint -> pool PumpSwap (découvert via DexScreener)
        self.notional = cfg.get("paper.notional_sol", 0.1)
        self._feed_stats = {"polls": 0, "ticks": 0, "errors": 0, "rejected": 0}
        self.guard = PriceGuard(cfg.get("pricefeed.max_jump", 5.0))

    # ------------------------------------------------------------------
    async def consume(self, first: bool = True) -> None:
        async for stream, mid, ev in self.bus.consume([B.RAW, B.DECISIONS, B.ALERTS], GROUP, "labeler-1",
                                                      replay_pending=first):
            try:
                if stream == B.ALERTS:
                    await self.on_alert(ev)
                else:
                    self.engine.on_event(ev)
            except Exception:  # noqa: BLE001
                log.exception("événement ignoré")
            if isinstance(ev, Trade):
                self._trades.append((ts(ev.ts), ev.mint, ev.signature, ev.slot, ev.trader, ev.is_buy, ev.sol, ev.tokens, ev.v_sol, ev.v_tokens))
            elif isinstance(ev, PriceTick):
                self._ticks.append((ts(ev.ts), ev.mint, ev.price, ev.source))
            await self.bus.ack(stream, GROUP, mid)

    async def on_alert(self, alert: dict) -> None:
        self.engine.open_position(alert)
        pos = self.engine.positions.get(alert["decision_id"])
        if pos is None or not self.cfg.get("paper.enabled", True):
            return
        await self.db.execute(
            """INSERT INTO paper_positions (decision_id, mint, symbol, policy, opened_at, notional_sol, entry_price, state)
               VALUES ($1,$2,$3,$4,$5,$6,$7,$8) ON CONFLICT (decision_id) DO NOTHING""",
            pos.decision_id, pos.mint, pos.symbol, pos.policy, ts(pos.state.t0), self.notional, pos.state.entry,
            pos.state.to_dict())

    # ------------------------------------------------------------------
    async def tick_loop(self) -> None:
        while True:
            try:
                labels, closed, finals = self.engine.tick(time.time())
                outcomes, signals = self.engine.drain()
            except Exception:  # noqa: BLE001
                log.exception("tick du labeler")
                await asyncio.sleep(1)
                continue
            try:
                await self.publish(labels, closed, finals, outcomes, signals)
            except Exception:  # noqa: BLE001
                log.exception("publication des résultats")
            await asyncio.sleep(0.25)

    async def publish(self, labels, closed, finals, outcomes, signals) -> None:
        rows = []
        for lb in labels:
            await self.bus.publish(B.LABELS, lb)
            rows.append((ts(lb.ts), lb.decision_id, lb.mint, lb.point, lb.horizon, lb.y, lb.max_return,
                         lb.max_drawdown, lb.time_to_peak_s, lb.rug, lb.final_return, lb.sim_pnl))
        if rows:
            await self.db.executemany(
                """INSERT INTO labels (ts, decision_id, mint, point, horizon, y, max_return, max_drawdown,
                   time_to_peak_s, rug, final_return, sim_pnl) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12)""", rows)
        orow = []
        for o in outcomes:
            await self.bus.publish(B.OUTCOMES, o)
            orow.append((o.decision_id, ts(o.ts), o.mint, o.point, o.pnl, o.max_return, o.reached))
        if orow:
            await self.db.executemany(
                """INSERT INTO outcomes (decision_id, ts, mint, point, pnl, max_return, reached)
                   VALUES ($1,$2,$3,$4,$5,$6,$7) ON CONFLICT (decision_id) DO NOTHING""", orow)
        for s in signals:
            await self.on_signal(s)
        for c in closed:
            await self.bus.publish(B.CLOSED, c)
        for f in finals:
            await self.db.execute(
                "UPDATE tokens SET closed_at=now(), peak_mc_sol=$2, rugged=$3, outcome=$4 WHERE mint=$1",
                f["mint"], f["peak_mc_sol"], f["rugged"], f["outcome"])

    async def on_signal(self, s: dict) -> None:
        """Action sur une position alertée → signal de vente Telegram + paper trading."""
        pos = self.engine.positions.get(s["decision_id"])
        st = pos.state if pos else None
        fill = {"t": s["t"], "kind": s["kind"], "fraction": round(s["fraction"], 4), "multiple": round(s["multiple"], 4),
                "reason": s.get("reason", "")}
        await self.db.execute(
            """UPDATE paper_positions SET state=$2, fills = fills || $3::jsonb, pnl=$4, pnl_sol=$4 * notional_sol,
               max_multiple=$5, status=$6, closed_at = CASE WHEN $6='closed' THEN now() ELSE closed_at END
               WHERE decision_id=$1""",
            s["decision_id"], st.to_dict() if st else {}, [fill], s["pnl_after"],
            (st.peak / st.entry) if st and st.entry else None, "closed" if s["closed"] else "open")
        reason = DANGER_LABELS.get(s.get("reason", ""), s.get("reason", ""))
        await self.bus.publish(B.SIGNALS, s)                 # → système d'ordres
        await self.bus.publish(B.NOTIFY, {"type": "sell_signal", **s, "reason_text": reason,
                                          "notional_sol": self.notional})

    # ------------------------------------------------------------------
    async def pricefeed_loop(self) -> None:
        """Prix PumpSwap des tokens migrés (suivi des labels longs, stratégies et positions)."""
        poll = self.cfg.get("pricefeed.poll_s", 20)
        batch = self.cfg.get("pricefeed.batch", 30)
        async with httpx.AsyncClient(timeout=10) as client:
            while True:
                await asyncio.sleep(poll)
                mints = sorted(self.engine.migrated_mints(time.time()))
                # pools à écouter en direct par l'ingestor (trades PumpSwap)
                await self.bus.set_json("apex:pools:active",
                                        [{"pool": self.pools[m], "mint": m} for m in mints if m in self.pools][:200])
                for i in range(0, min(len(mints), batch * 10), batch):
                    chunk = mints[i:i + batch]
                    try:
                        r = await client.get(f"https://api.dexscreener.com/tokens/v1/solana/{','.join(chunk)}")
                        if r.status_code == 429:
                            self._feed_stats["errors"] += 1
                            await asyncio.sleep(30)
                            break
                        r.raise_for_status()
                        now = time.time()
                        best: dict[str, tuple[float, float]] = {}
                        for p in r.json() or []:
                            base = (p.get("baseToken") or {}).get("address")
                            if base not in chunk or (p.get("quoteToken") or {}).get("address") != WSOL:
                                continue
                            liq = float((p.get("liquidity") or {}).get("usd") or 0)
                            price = float(p.get("priceNative") or 0)
                            if liq < self.cfg.get("pricefeed.min_liquidity_usd", 5000):
                                continue          # pool trop petit : prix manipulable
                            if p.get("dexId") == "pumpswap" and p.get("pairAddress"):
                                self.pools[base] = p["pairAddress"]
                            if price > 0 and liq > best.get(base, (0.0, -1.0))[1]:
                                best[base] = (price, liq)
                        for mint, (price, _) in best.items():
                            tr = self.engine.tracks.get(mint)
                            if tr is not None and tr.prices:
                                self.guard.seed(mint, tr.prices[-1])
                            if not self.guard.accept(mint, price):
                                self._feed_stats["rejected"] += 1
                                continue
                            await self.bus.publish(B.RAW, PriceTick(mint=mint, ts=now, price=price))
                            self._feed_stats["ticks"] += 1
                        self._feed_stats["polls"] += 1
                    except Exception as e:  # noqa: BLE001
                        self._feed_stats["errors"] += 1
                        log.debug("dexscreener : %s", e)

    async def flush_trades(self) -> None:
        cols = ["ts", "mint", "signature", "slot", "trader", "is_buy", "sol", "tokens", "v_sol", "v_tokens"]
        while True:
            await asyncio.sleep(2)
            buf, self._trades = self._trades, []
            tk, self._ticks = self._ticks, []
            try:
                await self.db.copy("trades", cols, buf)
                await self.db.copy("price_ticks", ["ts", "mint", "price", "source"], tk)
            except Exception:  # noqa: BLE001
                log.exception("persistance trades")
            await self.bus.heartbeat("labeler")
            await self.bus.set_json("apex:fastlane:positions",
                                    [p.mint for p in self.engine.positions.values() if not p.state.closed])
            await self.bus.set_json("apex:labeler:stats", {
                "tracked": len(self.engine.tracks), "pending": len(self.engine.heap),
                "open_positions": sum(1 for p in self.engine.positions.values() if not p.state.closed),
                "simulated_decisions": sum(len(t.sims) for t in self.engine.tracks.values()),
                "pricefeed": self._feed_stats, "migrated_followed": len(self.engine.migrated_mints(time.time()))})

    # ------------------------------------------------------------------
    async def rebuild(self) -> None:
        """Après redémarrage : rejoue l'historique déjà traité (trades, prix, décisions, alertes)
        pour retrouver chemins de prix, simulations et positions ; les labels déjà échus ne sont
        pas réémis, les signaux déjà envoyés non plus. Les positions plus anciennes que
        l'historique sont rechargées depuis la base."""
        window = 2 * 3600 + 600
        try:
            n_long = await self.rebuild_long(time.time() - window)
        except Exception:  # noqa: BLE001
            log.exception("reprise du suivi long (24 h) impossible — on continue sans")
            n_long = 0
        events, last_ts = await self.bus.history([B.RAW, B.DECISIONS, B.ALERTS], GROUP, window)
        for ev in events:
            if isinstance(ev, dict) and "decision_id" in ev and "policy" in ev:
                self.engine.open_position(ev)
            else:
                self.engine.on_event(ev)
        kept = [h for h in self.engine.heap if h[0] > last_ts or h[2] == OUT]
        dropped = len(self.engine.heap) - len(kept)
        heapq.heapify(kept)
        self.engine.heap = kept
        for tr in self.engine.tracks.values():
            tr.pending = 0
        for _, did, _ in kept:
            d = self.engine.decisions.get(did)
            if d and d.mint in self.engine.tracks:
                self.engine.tracks[d.mint].pending += 1
        outcomes, _signals = self.engine.drain()          # signaux déjà envoyés avant l'arrêt
        self.engine.outcomes = outcomes                    # outcomes republiés (dédupliqués en aval)
        restored = 0
        for r in await self.db.fetch("SELECT decision_id, mint, symbol, policy, state FROM paper_positions WHERE status='open'"):
            if r["decision_id"] not in self.engine.positions and r["policy"] in self.engine.policies:
                self.engine.positions[r["decision_id"]] = Position(
                    r["decision_id"], r["mint"], r["policy"], PolicyState.from_dict(r["state"]), r["symbol"] or "")
                restored += 1
        log.info("état reconstruit : %d événements, %d décisions longues reprises depuis la base, "
                 "%d labels en attente (%d déjà émis), %d positions rechargées",
                 len(events), n_long, len(kept), dropped, restored)

    async def rebuild_long(self, cutoff: float) -> int:
        """Reprise du suivi 24 h (labels x5/x10, simulations de stratégies) pour les décisions
        alertables plus anciennes que l'historique Redis : chemin de prix reconstitué depuis la base
        (bougies 1 min de la bonding curve, ordre prudent bas → haut → clôture, + prix PumpSwap)."""
        min_buyers = self.cfg.get("bandit.min_buyers_to_alert", 10)
        decs = await self.db.fetch(
            """SELECT decision_id, mint, point, ts, entry_price, mc_sol FROM decisions
               WHERE ts > now() - interval '24 hours 10 minutes' AND ts <= to_timestamp($1)
                 AND entry_price > 0 AND NOT blocked AND (features->>'unique_buyers')::float >= $2""", cutoff, min_buyers)
        if not decs:
            return 0
        mints = sorted({r["mint"] for r in decs})
        events: dict[str, list[tuple[float, int, object]]] = {m: [] for m in mints}
        for i in range(0, len(mints), 500):
            chunk = mints[i:i + 500]
            for r in await self.db.fetch(
                    "SELECT mint, name, symbol, creator, created_at, created_slot FROM tokens WHERE mint = ANY($1)", chunk):
                t0 = r["created_at"].timestamp()
                events[r["mint"]].append((t0, 0, TokenCreated(mint=r["mint"], name=r["name"] or "", symbol=r["symbol"] or "",
                                                            uri="", creator=r["creator"] or "", bonding_curve="",
                                                            slot=r["created_slot"] or 0, ts=t0, signature="")))
            for r in await self.db.fetch(
                    """SELECT bucket, mint, low, high, close FROM candles_1m WHERE mint = ANY($1)
                       AND bucket > now() - interval '25 hours' AND bucket < to_timestamp($2)""", chunk, cutoff):
                b = r["bucket"].timestamp()
                for dt_, price in ((5, r["low"]), (30, r["high"]), (55, r["close"])):
                    if price:
                        events[r["mint"]].append((b + dt_, 1, PriceTick(mint=r["mint"], ts=b + dt_, price=price, source="candle")))
            # prix après migration regroupés par tranches de 10 s (bas → haut → dernier, ordre prudent) :
            # des millions de relevés PumpSwap à la seconde rendaient la reprise trop lente (> 4 min)
            for r in await self.db.fetch(
                    """SELECT time_bucket('10 seconds', ts) b, mint, min(price) low, max(price) high, last(price, ts) close
                       FROM price_ticks WHERE mint = ANY($1)
                       AND ts > now() - interval '25 hours' AND ts < to_timestamp($2) GROUP BY 1, 2""", chunk, cutoff):
                b = r["b"].timestamp()
                for dt_, price in ((2, r["low"]), (5, r["high"]), (8, r["close"])):
                    events[r["mint"]].append((b + dt_, 1, PriceTick(mint=r["mint"], ts=b + dt_, price=price, source="replay")))
        for r in decs:
            t = r["ts"].timestamp()
            events[r["mint"]].append((t, 2, Decision(
                decision_id=r["decision_id"], mint=r["mint"], point=r["point"], ts=t, features={},
                entry_price=r["entry_price"], spot_price=(r["mc_sol"] or 0) / 1e9, mc_sol=r["mc_sol"] or 0,
                v_sol=0.0, v_tokens=0.0)))
        for m in mints:
            for _, _, ev in sorted(events[m], key=lambda x: (x[0], x[1])):
                self.engine.on_event(ev)
        self.engine.drain()          # résultats déjà publiés avant l'arrêt : non renvoyés
        return len(decs)

    async def run(self) -> None:
        # signal de vie pendant la reprise (sinon le contrôle de santé croit le labeler en panne)
        hb = asyncio.create_task(B.heartbeat_loop(self.bus, "labeler"))
        try:
            await self.rebuild()
        finally:
            hb.cancel()
        await asyncio.gather(
            B.resilient("consume", lambda first: self.consume(first)),
            B.resilient("tick", lambda _: self.tick_loop()),
            B.resilient("flush", lambda _: self.flush_trades()),
            B.resilient("pricefeed", lambda _: self.pricefeed_loop()),
        )


async def main() -> None:
    cfg = Config.load()
    db = await DB.connect(secrets().database_url)
    await LabelerService(cfg, B.Bus(secrets().redis_url), db).run()
