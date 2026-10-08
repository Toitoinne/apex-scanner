"""Intelligence wallets : smart wallets, bots, devs ruggers, graphe de financement à 2 sauts.

- Les bases smart/bot/devs sont mises à jour à chaque token clôturé (flux apex:closed).
- Le graphe de financement est résolu de façon asynchrone et budgétée (appels RPC
  Helius) : au moment d'une décision, on utilise ce qui est déjà résolu ; un wallet
  non résolu compte comme un acteur indépendant (hypothèse prudente côté qualité :
  la feature `independent_buyers` ne peut que baisser quand le graphe se complète).
"""
from __future__ import annotations

import asyncio
import itertools
import logging
import time
from collections import OrderedDict
from typing import Any

import httpx

log = logging.getLogger(__name__)


class UnionFind:
    def __init__(self) -> None:
        self.p: dict[str, str] = {}

    def find(self, x: str) -> str:
        self.p.setdefault(x, x)
        while self.p[x] != x:
            self.p[x] = self.p[self.p[x]]
            x = self.p[x]
        return x

    def union(self, a: str, b: str) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.p[ra] = rb


class WalletIntel:
    def __init__(self, cfg: dict, rpc_url: str | None = None, db: Any = None):
        self.cfg = cfg
        self.rpc_url = rpc_url
        self.db = db
        self.stats: dict[str, dict[str, float]] = {}      # wallet -> n_trades, n_wins, pnl, first_seen
        self.smart: set[str] = set()
        self.bots: set[str] = set()
        self.devs: dict[str, dict[str, int]] = {}
        self.funder: OrderedDict[str, tuple[str | None, str | None, float | None]] = OrderedDict()
        self.ignore_funders: set[str] = set()              # exchanges / services de financement massif
        # file PRIORITAIRE : (-priorité, ordre, wallet, échéance) — les tokens les plus prometteurs d'abord
        self._queue: asyncio.PriorityQueue = asyncio.PriorityQueue(maxsize=20000)
        self._queued: set[str] = set()
        self._seq = itertools.count()
        self._budget = cfg.get("enrichment_rate_per_min", cfg.get("funding_lookup_budget_per_min", 60))
        self._funder_counts: dict[str, int] = {}
        self.daily_credits = cfg.get("enrichment_daily_credit_budget", 25000)
        # budget LISSÉ : jamais plus de (quota/24 × burst) par heure → enrichissement toute la journée
        self.hourly_credits = int(self.daily_credits / 24 * cfg.get("enrichment_hourly_burst", 1.5))
        self.credits_hour = 0
        self._credit_hour = 0
        self.pool_blocked = False     # plafond mensuel commun atteint (géré par le service)
        self.credits_used = 0
        self._credit_day = 0
        self._next_call = 0.0
        # groupes d'opérateurs détectés GRATUITEMENT : wallets qui achètent dans le même slot
        # que d'autres wallets sur plusieurs tokens différents (bundles / bots coordonnés)
        self.co_pairs: dict[tuple[str, str], int] = {}
        self.operators = UnionFind()

    # ---------- lecture rapide (chemin critique) ----------
    def is_smart(self, w: str) -> bool:
        return w in self.smart

    def is_bot(self, w: str) -> bool:
        return w in self.bots

    def dev_stats(self, dev: str) -> dict[str, int]:
        return self.devs.get(dev, {"n_tokens": 0, "n_rugs": 0, "n_winners": 0})

    def buyer_quality(self, buyers: list[str], now: float) -> dict[str, float]:
        weighted, fresh, smart = 0.0, 0, 0
        for w in buyers:
            s = self.stats.get(w)
            fi = self.funder.get(w)
            age = (now - fi[2]) if fi and fi[2] else None
            weight = 1.0
            if age is not None and age < 86400:
                weight *= 0.3
                fresh += 1
            if s:
                n = s.get("n_trades", 0)
                wr = s.get("n_wins", 0) / n if n else 0
                weight *= 0.5 + min(1.5, wr * 3)
            if w in self.bots:
                weight *= 0.2
            if w in self.smart:
                weight *= 2.0
                smart += 1
            weighted += weight
        return {"weighted": weighted, "fresh_share": fresh / len(buyers) if buyers else 0.0, "smart_count": float(smart)}

    def independent_clusters(self, buyers: list[str]) -> int:
        uf = UnionFind()
        for w in buyers:
            uf.find(w)
            if w in self.operators.p:
                uf.union(w, f"O:{self.operators.find(w)}")
            fi = self.funder.get(w)
            if not fi:
                continue
            for f in fi[:2]:
                if f and f not in self.ignore_funders:
                    uf.union(w, f"F:{f}")
        return len({uf.find(w) for w in buyers})

    # ---------- mise à jour à la clôture d'un token ----------
    def update_from_closed(self, closed: dict) -> list[tuple]:
        """closed = {mint, creator, rugged, winner, wallets: {w: pnl_sol}} → lignes DB."""
        rows = []
        mn = self.cfg.get("smart_wallet_min_trades", 8)
        wr_min = self.cfg.get("smart_wallet_min_winrate", 0.35)
        for w, pnl in closed.get("wallets", {}).items():
            s = self.stats.setdefault(w, {"n_trades": 0, "n_wins": 0, "pnl": 0.0, "fast_flips": 0})
            s["n_trades"] += 1
            s["n_wins"] += 1 if pnl > 0 else 0
            s["pnl"] += pnl
            if w in closed.get("fast_flippers", []):
                s["fast_flips"] += 1
            if s["n_trades"] >= mn and s["n_wins"] / s["n_trades"] >= wr_min and s["pnl"] > 0:
                self.smart.add(w)
            else:
                self.smart.discard(w)
            if s["n_trades"] >= 30 and s["fast_flips"] / s["n_trades"] > 0.8:
                self.bots.add(w)
            rows.append((w, int(s["n_trades"]), int(s["n_wins"]), float(s["pnl"]), w in self.smart, w in self.bots))
        for group in closed.get("slot_groups", []):
            g = sorted(set(group))[:20]
            for i, a in enumerate(g):
                for b in g[i + 1:]:
                    k = (a, b)
                    self.co_pairs[k] = self.co_pairs.get(k, 0) + 1
                    if self.co_pairs[k] >= 2:
                        self.operators.union(a, b)
        if len(self.co_pairs) > 2_000_000:
            self.co_pairs = {k: v for k, v in self.co_pairs.items() if v >= 2}
        dev = closed.get("creator")
        if dev:
            d = self.devs.setdefault(dev, {"n_tokens": 0, "n_rugs": 0, "n_winners": 0})
            d["n_tokens"] += 1
            d["n_rugs"] += int(bool(closed.get("rugged")))
            d["n_winners"] += int(bool(closed.get("winner")))
        return rows

    async def load(self, db: Any) -> None:
        for r in await db.fetch("SELECT address, n_trades, n_wins, pnl_sol, is_smart, is_bot, funder, funder2, first_seen FROM wallets"):
            self.stats[r["address"]] = {"n_trades": r["n_trades"], "n_wins": r["n_wins"], "pnl": r["pnl_sol"], "fast_flips": 0}
            if r["is_smart"]:
                self.smart.add(r["address"])
            if r["is_bot"]:
                self.bots.add(r["address"])
            if r["funder"] or r["first_seen"]:
                self.funder[r["address"]] = (r["funder"], r["funder2"], r["first_seen"].timestamp() if r["first_seen"] else None)
        for r in await db.fetch("SELECT address, n_tokens, n_rugs, n_winners FROM devs"):
            self.devs[r["address"]] = {"n_tokens": r["n_tokens"], "n_rugs": r["n_rugs"], "n_winners": r["n_winners"]}
        log.info("wallet intel : %d wallets, %d smart, %d bots, %d devs", len(self.stats), len(self.smart), len(self.bots), len(self.devs))

    # ---------- graphe de financement (asynchrone, budgété) ----------
    def request_funding(self, w: str, priority: float = 0.0, ttl_s: float = 600.0) -> None:
        if self.rpc_url is None or w in self.funder or w in self._queued:
            return
        try:
            self._queue.put_nowait((-priority, next(self._seq), w, time.time() + ttl_s))
            self._queued.add(w)
        except asyncio.QueueFull:
            pass

    def spend(self, n: int = 1) -> bool:
        """Budget quotidien de crédits RPC (remis à zéro chaque jour UTC)."""
        now = time.time()
        day, hour = int(now // 86400), int(now // 3600)
        if day != self._credit_day:
            self._credit_day, self.credits_used = day, 0
        if hour != self._credit_hour:
            self._credit_hour, self.credits_hour = hour, 0
        if self.pool_blocked or self.credits_used + n > self.daily_credits or self.credits_hour + n > self.hourly_credits:
            return False
        self.credits_used += n
        self.credits_hour += n
        return True

    def budget_left_this_hour(self) -> bool:
        self.spend(0)
        return self.credits_hour < self.hourly_credits and self.credits_used < self.daily_credits

    async def _throttle(self) -> None:
        """Espace CHAQUE appel RPC (enrichment_rate_per_min appels/min au total) pour rester
        sous la limite de requêtes/s du plan, quel que soit le nombre d'appels par recherche."""
        gap = 60.0 / max(1, self._budget)
        wait = self._next_call - time.monotonic()
        self._next_call = max(self._next_call, time.monotonic()) + gap
        if wait > 0:
            await asyncio.sleep(wait)

    async def _rpc(self, client: httpx.AsyncClient, method: str, params: list) -> Any:
        if not self.spend():
            raise RuntimeError("budget de crédits RPC du jour épuisé")
        await self._throttle()
        r = await client.post(self.rpc_url, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
        r.raise_for_status()
        return r.json().get("result")

    async def _first_funding(self, client: httpx.AsyncClient, w: str) -> tuple[str | None, float | None]:
        """Retourne (financeur, timestamp première transaction). Un seul appel paginé :
        si le wallet a > 1000 signatures, il est considéré ancien et le financeur inconnu."""
        sigs = await self._rpc(client, "getSignaturesForAddress", [w, {"limit": 1000}])
        if not sigs:
            return None, None
        oldest = sigs[-1]
        if len(sigs) >= 1000:
            return None, float(oldest.get("blockTime") or 0) or None
        tx = await self._rpc(client, "getTransaction", [oldest["signature"], {"maxSupportedTransactionVersion": 0, "encoding": "json"}])
        if not tx:
            return None, oldest.get("blockTime")
        keys = tx["transaction"]["message"]["accountKeys"]
        keys = [k if isinstance(k, str) else k.get("pubkey") for k in keys]
        pre, post = tx["meta"]["preBalances"], tx["meta"]["postBalances"]
        best, best_delta = None, 0
        for i, k in enumerate(keys):
            if k == w:
                continue
            delta = post[i] - pre[i]
            if delta < best_delta:
                best, best_delta = k, delta
        return best, tx.get("blockTime")

    async def funding_worker(self) -> None:
        if self.rpc_url is None:
            return
        interval = 0.0
        hops = self.cfg.get("funding_hops", 2)
        async with httpx.AsyncClient(timeout=10) as client:
            while True:
                _, _, w, deadline = await self._queue.get()
                self._queued.discard(w)
                if time.time() > deadline or w in self.funder:
                    continue              # demande périmée (token trop vieux pour être alerté)
                if not self.budget_left_this_hour():
                    await asyncio.sleep(5)  # budget de l'heure épuisé : on attend l'heure suivante
                    continue
                try:
                    f1, t1 = await self._first_funding(client, w)
                    await asyncio.sleep(interval)
                    f2 = None
                    if hops >= 2 and f1 and f1 not in self.ignore_funders:
                        known = self.funder.get(f1)
                        if known:
                            f2 = known[0]
                        else:
                            f2, t2 = await self._first_funding(client, f1)
                            self.funder[f1] = (f2, None, t2)
                            await asyncio.sleep(interval)
                    self.funder[w] = (f1, f2, t1)
                    if f1:
                        c = self._funder_counts[f1] = self._funder_counts.get(f1, 0) + 1
                        if c > 500:     # finance des centaines de wallets : exchange / service
                            self.ignore_funders.add(f1)
                    while len(self.funder) > 500_000:
                        self.funder.popitem(last=False)
                    if self.db is not None:
                        await self.db.execute(
                            """INSERT INTO wallets (address, funder, funder2, first_seen) VALUES ($1,$2,$3,to_timestamp($4))
                               ON CONFLICT (address) DO UPDATE SET funder=$2, funder2=$3, first_seen=to_timestamp($4), updated_at=now()""",
                            w, f1, f2, float(t1 or time.time()),
                        )
                except Exception as e:  # noqa: BLE001
                    log.debug("funding %s: %s", w, e)
                    await asyncio.sleep(interval)
