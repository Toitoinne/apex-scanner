"""INGESTOR : flux temps réel pump.fun → Redis Stream apex:raw.

Sources :
- helius_logs : logsSubscribe(mentions programme pump.fun) sur le websocket Helius,
  décodage des events Anchor. Flux complet (créations, trades, fin de bonding curve).
- pumpportal  : wss://pumpportal.fun/api/data — créations et migrations gratuites ;
  trades facturés (0,01 SOL / 10 000 messages) donc désactivés par défaut.

Reconnexion automatique avec backoff, déduplication inter-sources par clé
(signature, mint, sens, montant) dans Redis avec TTL. Ce service n'est jamais
modifié par les boucles automatiques.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

import orjson
import websockets

from .. import bus as B
from ..config import Config, secrets
from ..events import Migration, PriceTick, TokenCreated, Trade
from ..features.curve import is_standard_curve
from .pump_decoder import events_from_logs
from .pumpswap_decoder import events_from_amm_logs
from .watchdog import BACKUP, FlowWatchdog

log = logging.getLogger("ingestor")


class Deduper:
    def __init__(self, bus: B.Bus, ttl_s: int):
        self.bus, self.ttl = bus, ttl_s

    @staticmethod
    def key(ev: Any) -> str:
        if isinstance(ev, Trade):
            base_sig = ev.signature.split(":")[0]
            return f"t:{base_sig}:{ev.mint}:{int(ev.is_buy)}:{round(ev.sol, 4)}"
        if isinstance(ev, TokenCreated):
            return f"c:{ev.mint}"
        if isinstance(ev, Migration):
            return f"m:{ev.mint}"
        return f"x:{id(ev)}"

    async def first_time(self, ev: Any) -> bool:
        return bool(await self.bus.r.set(f"apex:dedupe:{self.key(ev)}", b"1", nx=True, ex=self.ttl))


def plan_pool_subs(wanted: set[str], subscribed: set[str], pending: set[str],
                   budget: int) -> tuple[list[str], list[str]]:
    """Abonnements PumpSwap à faire maintenant : au plus `budget` requêtes (désabonnements d'abord,
    puis nouveaux pools). Le RPC public coupe la connexion (« Too many subscriptions attempted »)
    si on lui envoie ~200 abonnements d'un coup : on étale donc sur plusieurs tours."""
    unsub = sorted(subscribed - wanted)[:budget]
    sub = sorted(wanted - subscribed - pending)[:max(0, budget - len(unsub))]
    return unsub, sub


def is_late(last_slot: dict[str, int], ev: Any, tolerance: int) -> bool:
    """Vrai si l'événement est plus ancien de plus de `tolerance` slots (~0,4 s chacun) que le plus récent
    déjà reçu pour ce token. Met à jour le slot de référence sinon. Sans slot (PumpPortal) : jamais en retard."""
    slot = getattr(ev, "slot", 0) or 0
    mint = getattr(ev, "mint", None)
    if not slot or not mint:
        return False
    ref = last_slot.get(mint, 0)
    if slot < ref - tolerance:
        return True
    if slot > ref:
        last_slot[mint] = slot
        if len(last_slot) > 200_000:            # mémoire bornée : on oublie les tokens les plus anciens
            for m in list(last_slot)[:100_000]:
                del last_slot[m]
    return False


def expand_ws_urls(urls: list[str], copies: int) -> list[str]:
    """Le RPC public sature : il prend du retard puis coupe la connexion toutes les 1–2 min, et la
    file en attente côté serveur est perdue (~10 % des trades par connexion, mesuré le 09/10).
    Plusieurs connexions décalées vers la même URL ne coupent pas au même moment : leur union
    (dédupliquée) voit tout. Ordre conservé : ws0 reste la 1re connexion de la 1re URL."""
    copies = max(1, int(copies))
    return [u for k in range(copies) for u in urls] if copies > 1 else list(urls)


class Ingestor:
    def __init__(self, cfg: Config, bus: B.Bus):
        self.cfg, self.bus = cfg, bus
        ing = cfg["ingestion"]
        self.dedupe = Deduper(bus, ing["dedupe_ttl_s"])
        self.backoff = ing["reconnect_backoff_s"]
        self.maxlen = ing["stream_maxlen"]
        self.stats: dict[str, Any] = {"events": 0, "dupes": 0, "late": 0, "reconnects": 0, "bytes": {}}
        self.late_slots = ing.get("late_slots", 4)
        self._mint_slot: dict[str, int] = {}
        self._t_start = time.time()
        self._pp_ws: Any = None
        self._ws: dict[str, Any] = {}
        urls = [u.strip() for u in secrets().solana_ws_urls.split(",") if u.strip()] \
            if "public_logs" in ing["sources"] else []
        self.free_urls = expand_ws_urls(urls, ing.get("connections_per_url", 1))
        self.stagger_s = ing.get("connection_stagger_s", 20)
        # abonnements PumpSwap étalés (le RPC public refuse les rafales : ~40 requêtes / 10 s)
        self.pumpswap_burst = ing.get("pumpswap_subs_per_round", 5)
        self.pumpswap_gap_s = ing.get("pumpswap_sub_gap_s", 0.3)
        wd = ing.get("watchdog", {})
        self.wd = FlowWatchdog(log_sources=[f"ws{i}" for i in range(len(self.free_urls))],
                               stall_s=wd.get("stall_s", 30), backup_after_s=wd.get("backup_after_s", 15),
                               release_after_s=wd.get("release_after_s", 300),
                               outage_alert_s=wd.get("outage_alert_s", 60))
        self.wd_cfg = wd
        self._backup_task: asyncio.Task | None = None
        self._backup_bytes_unbilled = 0

    async def emit(self, ev: Any) -> None:
        if isinstance(ev, Trade) and not is_standard_curve(ev.v_sol, ev.v_tokens, self.cfg.get("ingestion.curve_tolerance", 0.02)):
            self.stats["ignored_nonstandard"] = self.stats.get("ignored_nonstandard", 0) + 1
            return
        if not await self.dedupe.first_time(ev):
            self.stats["dupes"] += 1
            return
        if is_late(self._mint_slot, ev, self.late_slots):
            # livré en retard par une connexion lente : le bot le daterait « maintenant » avec un prix
            # vieux de plusieurs secondes (ex. 09/10 : achats de la création reçus 22 s après → faux krach −76 %)
            self.stats["late"] += 1
            return
        self.stats["events"] += 1
        await self.bus.publish(B.RAW, ev, maxlen=self.maxlen)

    async def _forever(self, name: str, fn, delay: float = 0.0) -> None:
        attempt = 0
        if delay:
            await asyncio.sleep(delay)     # connexions de renfort décalées : elles ne coupent pas en même temps
        while True:
            started = time.time()
            try:
                await fn()
                attempt = 0
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                self.stats["reconnects"] += 1
                if time.time() - started > 60:
                    attempt = 0       # la connexion avait tenu : coupure normale, on se reconnecte tout de suite
                wait = self.backoff[min(attempt, len(self.backoff) - 1)]
                log.warning("%s déconnecté (%s) — reconnexion dans %ss", name, e, wait)
                await self.bus.r.hincrby("apex:ingestor:reconnects", name, 1)
                await asyncio.sleep(wait)
                attempt += 1

    # ---------------- logsSubscribe (websocket gratuit ou Helius) ----------------
    async def logs_ws(self, url: str, name: str) -> None:
        program = self.cfg["ingestion"]["pump_program_id"]
        nbytes = self.stats["bytes"]
        try:
            async with websockets.connect(url, ping_interval=20, max_size=2**23, open_timeout=15) as ws:
                self._ws[name] = ws
                self.wd.on_connect(name, time.time())
                await ws.send(orjson.dumps({
                    "jsonrpc": "2.0", "id": 1, "method": "logsSubscribe",
                    "params": [{"mentions": [program]}, {"commitment": "processed"}],
                }).decode())
                log.info("logsSubscribe actif sur %s", name)
                async for msg in ws:
                    now = time.time()
                    nbytes[name] = nbytes.get(name, 0) + len(msg)
                    if name == BACKUP:
                        self._backup_bytes_unbilled += len(msg)
                    self.wd.on_message(name, now)
                    d = orjson.loads(msg)
                    if d.get("method") != "logsNotification":
                        continue
                    res = d["params"]["result"]
                    val = res["value"]
                    if val.get("err") is not None:
                        continue
                    self.last_slot = max(getattr(self, "last_slot", 0), res["context"]["slot"])
                    for ev in events_from_logs(val.get("logs") or [], val["signature"], res["context"]["slot"], now):
                        self.wd.on_event(name, now, isinstance(ev, TokenCreated))
                        await self.emit(ev)
        finally:
            self._ws.pop(name, None)
            self.wd.on_disconnect(name)

    # ---------------- PumpPortal ----------------
    # ---------------- Voie rapide Helius : tokens candidats et positions ouvertes ----------------
    async def fastlane(self) -> None:
        """Le flux public a ~3 s de retard et rate ~15 % des transactions. Pour les tokens qui
        comptent (candidats à une alerte, positions ouvertes), on s'abonne à eux seuls sur Helius :
        temps réel et complet, pour quelques milliers de crédits par jour (plafond strict).
        Les trades sont dédupliqués avec le flux public : le premier arrivé l'emporte."""
        fl = self.cfg.get("ingestion.fastlane") or {}
        if not fl.get("enabled", True) or not (secrets().helius_api_key or secrets().helius_ws_url):
            await asyncio.Event().wait()
        cap = fl.get("daily_credits", 8000)
        max_mints = fl.get("max_mints", 60)
        nbytes = self.stats["bytes"]
        async with websockets.connect(secrets().ws_url(), ping_interval=20, max_size=2**23, open_timeout=15) as ws:
            pending: dict[int, str] = {}
            subs: dict[int, str] = {}
            by_mint: dict[str, int] = {}
            req = 1000
            unbilled = 0

            def day_key() -> str:
                return "apex:fastlane:credits:" + time.strftime("%Y%m%d", time.gmtime())

            async def sync() -> None:
                nonlocal req, unbilled
                if unbilled:
                    credits = unbilled / 1e6 * 20                       # tarif Helius : 20 crédits / Mo
                    unbilled = 0
                    await self.bus.r.incrbyfloat(day_key(), credits)
                    await self.bus.r.expire(day_key(), 3 * 86400)
                    await self.bus.r.incrbyfloat(self._pool_key(), credits)
                spent = float(await self.bus.r.get(day_key()) or 0)
                hour_frac = (time.time() % 86400) / 86400
                # budget lissé : à chaque heure, on n'a droit qu'à la part du budget écoulée (+ 2 h d'avance)
                paced = spent < cap * min(1.0, hour_frac + 2 / 24)
                pool_ok = await self.backup_allowed()
                pos = await self.bus.get_json("apex:fastlane:positions", []) or []
                cand = await self.bus.get_json("apex:fastlane:candidates", []) or []
                wanted: list[str] = []
                if pool_ok and spent < cap:
                    wanted = list(dict.fromkeys(pos))                                 # positions : toujours
                    if paced:
                        wanted = list(dict.fromkeys(wanted + cand[:fl.get("max_candidates", 6)]))
                    wanted = wanted[:max_mints]
                for m in set(wanted) - by_mint.keys() - set(pending.values()):
                    req += 1
                    pending[req] = m
                    await ws.send(orjson.dumps({"jsonrpc": "2.0", "id": req, "method": "logsSubscribe",
                                                "params": [{"mentions": [m]}, {"commitment": "processed"}]}).decode())
                for m in list(by_mint.keys() - set(wanted)):
                    sid = by_mint.pop(m)
                    subs.pop(sid, None)
                    req += 1
                    await ws.send(orjson.dumps({"jsonrpc": "2.0", "id": req, "method": "logsUnsubscribe",
                                                "params": [sid]}).decode())
                self.stats["fastlane"] = {"mints": len(by_mint), "positions": len(pos), "credits_today": round(spent),
                                          "cap": cap, "paced": paced}

            await sync()
            last_sync = time.time()
            log.info("voie rapide Helius active")
            while True:
                try:
                    msg = await asyncio.wait_for(ws.recv(), timeout=5)
                except asyncio.TimeoutError:
                    msg = None
                if time.time() - last_sync > 5:
                    await sync()
                    last_sync = time.time()
                if msg is None:
                    continue
                nbytes["fastlane"] = nbytes.get("fastlane", 0) + len(msg)
                unbilled += len(msg)
                d = orjson.loads(msg)
                if "id" in d and d["id"] in pending:
                    m = pending.pop(d["id"])
                    if isinstance(d.get("result"), int):
                        subs[d["result"]] = m
                        by_mint[m] = d["result"]
                    continue
                if d.get("method") != "logsNotification":
                    continue
                res = d["params"]["result"]
                val = res["value"]
                if val.get("err") is not None:
                    continue
                for ev in events_from_logs(val.get("logs") or [], val["signature"], res["context"]["slot"], time.time()):
                    if isinstance(ev, Trade):
                        ev.source = "helius_fast"
                    await self.emit(ev)

    # ---------------- PumpSwap : abonnement aux SEULS pools suivis ----------------
    async def pumpswap(self) -> None:
        """Trades PumpSwap en direct pour les tokens migrés suivis (positions, labels longs,
        stratégies). Le labeler publie la liste des pools dans apex:pools:active ; on s'abonne /
        se désabonne dynamiquement (quelques dizaines de pools au lieu des ~400 tx/s du programme)."""
        url = self.free_urls[0] if self.free_urls else "wss://api.mainnet-beta.solana.com"
        nbytes = self.stats["bytes"]
        async with websockets.connect(url, ping_interval=20, max_size=2**23, open_timeout=15) as ws:
            pending: dict[int, str] = {}           # id de requête -> pool
            subs: dict[int, str] = {}              # id d'abonnement -> pool
            by_pool: dict[str, int] = {}
            pool_mint: dict[str, str] = {}
            req = 100

            async def sync() -> bool:
                """Un tour d'abonnements, à débit limité ; vrai s'il en reste à faire."""
                nonlocal req
                wanted = {d["pool"]: d["mint"] for d in (await self.bus.get_json("apex:pools:active", []) or [])}
                pool_mint.update(wanted)
                unsub, sub = plan_pool_subs(set(wanted), set(by_pool), set(pending.values()), self.pumpswap_burst)
                for pool in unsub:
                    sid = by_pool.pop(pool)
                    subs.pop(sid, None)
                    req += 1
                    await ws.send(orjson.dumps({"jsonrpc": "2.0", "id": req, "method": "logsUnsubscribe",
                                                "params": [sid]}).decode())
                    await asyncio.sleep(self.pumpswap_gap_s)
                for pool in sub:
                    req += 1
                    pending[req] = pool
                    await ws.send(orjson.dumps({"jsonrpc": "2.0", "id": req, "method": "logsSubscribe",
                                                "params": [{"mentions": [pool]}, {"commitment": "processed"}]}).decode())
                    await asyncio.sleep(self.pumpswap_gap_s)
                self.stats["pumpswap_pools"] = len(by_pool)
                return len(wanted.keys() - by_pool.keys() - set(pending.values())) > 0 or bool(by_pool.keys() - wanted.keys())

            backlog = await sync()
            last_sync = time.time()
            log.info("pumpswap actif")
            while True:
                every = 3.0 if backlog else 15.0
                try:
                    msg = await asyncio.wait_for(ws.recv(), timeout=max(0.5, last_sync + every - time.time()))
                except asyncio.TimeoutError:
                    msg = None
                if time.time() - last_sync >= every:
                    backlog = await sync()
                    last_sync = time.time()
                if msg is None:
                    continue
                nbytes["pumpswap"] = nbytes.get("pumpswap", 0) + len(msg)
                d = orjson.loads(msg)
                if "id" in d and d["id"] in pending:
                    pool = pending.pop(d["id"])
                    if isinstance(d.get("result"), int):
                        subs[d["result"]] = pool
                        by_pool[pool] = d["result"]
                    continue
                if d.get("method") != "logsNotification":
                    continue
                pool = subs.get(d["params"]["subscription"])
                val = d["params"]["result"]["value"]
                if pool is None or val.get("err") is not None:
                    continue
                now = time.time()
                for ev in events_from_amm_logs(val.get("logs") or []):
                    mint = pool_mint.get(ev["pool"])
                    if mint is None:
                        continue
                    self.stats["pumpswap_trades"] = self.stats.get("pumpswap_trades", 0) + 1
                    await self.bus.publish(B.RAW, PriceTick(mint=mint, ts=now, price=ev["price"], source="pumpswap",
                                                            trader=ev["user"], is_buy=ev["is_buy"], sol=ev["sol"],
                                                            tokens=ev["tokens"], pool_sol=ev.get("pool_sol")),
                                        maxlen=self.maxlen)

    async def pumpportal(self) -> None:
        key = secrets().pumpportal_api_key
        url = "wss://pumpportal.fun/api/data" + (f"?api-key={key}" if key else "")
        async with websockets.connect(url, ping_interval=20) as ws:
            self._pp_ws = ws
            await ws.send(orjson.dumps({"method": "subscribeNewToken"}).decode())
            await ws.send(orjson.dumps({"method": "subscribeMigration"}).decode())
            log.info("pumpportal actif")
            async for msg in ws:
                d = orjson.loads(msg)
                for ev in self._from_pumpportal(d):
                    self.wd.on_event("pumpportal", time.time(), isinstance(ev, TokenCreated))
                    await self.emit(ev)
                    if isinstance(ev, TokenCreated) and self.cfg.get("ingestion.pumpportal_trades"):
                        await ws.send(orjson.dumps({"method": "subscribeTokenTrade", "keys": [ev.mint]}).decode())

    @staticmethod
    def _from_pumpportal(d: dict) -> list[Any]:
        tx = d.get("txType")
        now = time.time()
        if tx is None or "mint" not in d:
            return []
        if tx == "migrate":
            return [Migration(mint=d["mint"], slot=0, ts=now, signature=d.get("signature", ""), pool=d.get("pool", ""))]
        out: list[Any] = []
        vs, vt = float(d.get("vSolInBondingCurve", 0)), float(d.get("vTokensInBondingCurve", 0))
        if tx == "create":
            out.append(TokenCreated(
                mint=d["mint"], name=d.get("name", ""), symbol=d.get("symbol", ""), uri=d.get("uri", ""),
                creator=d.get("traderPublicKey", ""), bonding_curve=d.get("bondingCurveKey", ""),
                slot=0, ts=now, signature=d.get("signature", ""),
            ))
            if float(d.get("initialBuy", 0)) > 0:
                out.append(Trade(
                    mint=d["mint"], signature=d.get("signature", "") + ":0", slot=0, ts=now,
                    trader=d.get("traderPublicKey", ""), is_buy=True, sol=float(d.get("solAmount", 0)),
                    tokens=float(d["initialBuy"]), v_sol=vs, v_tokens=vt, source="pumpportal",
                ))
        elif tx in ("buy", "sell"):
            out.append(Trade(
                mint=d["mint"], signature=d.get("signature", "") + ":0", slot=0, ts=now,
                trader=d.get("traderPublicKey", ""), is_buy=tx == "buy", sol=float(d.get("solAmount", 0)),
                tokens=float(d.get("tokenAmount", 0)), v_sol=vs, v_tokens=vt, source="pumpportal",
            ))
        return out

    async def stats_loop(self) -> None:
        while True:
            el = max(1.0, time.time() - self._t_start)
            # projection : volume/jour par source et coût si ce flux passait par Helius (20 crédits/Mo)
            proj = {k: {"mb_per_day": round(v / el * 86400 / 1e6, 1),
                        "helius_credits_per_month_if_streamed": int(v / el * 86400 * 30 / 1e6 * 20)}
                    for k, v in self.stats["bytes"].items()}
            await self.bus.set_json("apex:ingestor:stats", {**self.stats, "volume": proj, "ts": time.time()})
            await self.bus.heartbeat("ingestor")
            await asyncio.sleep(10)

    # ---------------- chien de garde + secours Helius ----------------
    @staticmethod
    def _pool_key() -> str:
        return "apex:helius:credits:" + time.strftime("%Y%m", time.gmtime())

    async def backup_allowed(self) -> bool:
        if not self.wd_cfg.get("helius_backup", True) or not (secrets().helius_api_key or secrets().helius_ws_url):
            return False
        used = float(await self.bus.r.get(self._pool_key()) or 0)
        return used < self.wd_cfg.get("monthly_credit_cap", 980_000)

    async def watchdog_loop(self) -> None:
        while True:
            await asyncio.sleep(2)
            try:
                if self._backup_bytes_unbilled:      # 20 crédits / Mo (tarif Helius websockets)
                    credits = self._backup_bytes_unbilled / 1e6 * 20
                    self._backup_bytes_unbilled = 0
                    await self.bus.r.incrbyfloat(self._pool_key(), credits)
                    await self.bus.r.expire(self._pool_key(), 40 * 86400)
                acts = self.wd.evaluate(time.time(), await self.backup_allowed())
                for s in acts.reconnect:
                    ws = self._ws.get(s)
                    if ws is not None:
                        log.warning("source %s muette : reconnexion forcée", s)
                        await ws.close()
                if acts.start_backup and self._backup_task is None:
                    log.warning("flux gratuit en panne : activation du secours Helius")
                    self._backup_task = asyncio.create_task(
                        self._forever(BACKUP, lambda: self.logs_ws(secrets().ws_url(), BACKUP)))
                if acts.stop_backup and self._backup_task is not None:
                    log.info("sources gratuites rétablies : arrêt du secours Helius")
                    self._backup_task.cancel()
                    self._backup_task = None
                if acts.gap_opened is not None:
                    await self.bus.r.set("apex:gap_open", str(acts.gap_opened))
                if acts.gap_closed is not None:
                    s0, s1 = acts.gap_closed
                    await self.bus.r.zadd("apex:gaps", {f"{s0:.3f}:{s1:.3f}": s1})
                    await self.bus.r.zremrangebyscore("apex:gaps", 0, time.time() - 3 * 86400)
                    await self.bus.r.delete("apex:gap_open")
                    log.warning("trou de données %.0f s enregistré", s1 - s0)
                for text in (acts.alert, acts.recovered):
                    if text:
                        await self.bus.publish(B.NOTIFY, {"type": "urgent", "text": text})
                self.stats["watchdog"] = {
                    "healthy": self.wd.unhealthy_since is None, "backup_running": self.wd.backup_running,
                    "last_event_age_s": {s: round(time.time() - t, 1) for s, t in self.wd.last_event.items()},
                    "helius_credits_month": float(await self.bus.r.get(self._pool_key()) or 0),
                }
            except Exception:  # noqa: BLE001
                log.exception("chien de garde")

    async def lag_loop(self) -> None:
        """Retard réel du flux : dernier bloc reçu vs dernier bloc de la blockchain (getSlot, gratuit,
        toutes les 15 s). Les serveurs publics gratuits saturent aux heures de pointe : le système
        d'ordres simulé ajoute ce retard pour que ses résultats restent honnêtes."""
        import collections
        import httpx
        rpc = (self.free_urls[0] if self.free_urls else "wss://api.mainnet-beta.solana.com").replace("wss://", "https://")
        samples: collections.deque = collections.deque(maxlen=20)        # 5 min
        async with httpx.AsyncClient(timeout=10) as c:
            while True:
                await asyncio.sleep(15)
                try:
                    if not getattr(self, "last_slot", 0):
                        continue
                    r = await c.post(rpc, json={"jsonrpc": "2.0", "id": 1, "method": "getSlot",
                                                "params": [{"commitment": "processed"}]})
                    lag = max(0.0, (r.json()["result"] - self.last_slot) * 0.4)
                    samples.append(lag)
                    info = {"actuel": round(lag, 1), "moyen_5min": round(sum(samples) / len(samples), 1),
                            "max_5min": round(max(samples), 1), "ts": time.time()}
                    self.stats["retard_flux_s"] = info
                    await self.bus.set_json("apex:feed:lag", info)
                except Exception:  # noqa: BLE001
                    log.debug("mesure du retard du flux impossible", exc_info=True)

    async def run(self) -> None:
        tasks = [asyncio.create_task(self.stats_loop()), asyncio.create_task(self.watchdog_loop()),
                 asyncio.create_task(self.lag_loop())]
        srcs = self.cfg["ingestion"]["sources"]
        n_urls = max(1, len(set(self.free_urls)))
        for i, url in enumerate(self.free_urls):
            name = f"ws{i}"
            tasks.append(asyncio.create_task(self._forever(name, lambda u=url, n=name: self.logs_ws(u, n),
                                                           delay=(i // n_urls) * self.stagger_s)))
        if "helius_logs" in srcs and (secrets().helius_api_key or secrets().helius_ws_url):
            # ⚠ facturé 20 crédits/Mo : épuise vite l'offre gratuite avec le flux pump.fun complet
            tasks.append(asyncio.create_task(self._forever("helius", lambda: self.logs_ws(secrets().ws_url(), "helius"))))
        if "pumpportal" in srcs:
            tasks.append(asyncio.create_task(self._forever("pumpportal", self.pumpportal)))
        if "pumpswap" in srcs:
            tasks.append(asyncio.create_task(self._forever("pumpswap", self.pumpswap)))
        tasks.append(asyncio.create_task(self._forever("fastlane", self.fastlane)))
        await asyncio.gather(*tasks)


async def main() -> None:
    cfg = Config.load()
    bus = B.Bus(secrets().redis_url)
    await Ingestor(cfg, bus).run()
