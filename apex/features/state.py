"""État par token et calcul des features (fonctions pures, pilotées par les
timestamps des événements : le même code sert en production et en rejeu backfill).

Chaque feature a un identifiant stable et une version (FEATURE_VERSIONS) : si la
définition change, la version est incrémentée, ce qui permet à la boucle 3 de
mesurer l'effet d'un changement de feature.
"""
from __future__ import annotations

import math
import re
import statistics
from collections import deque
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from ..events import TokenCreated, Trade
from .metadata import meta_features
from .curve import INITIAL_VIRTUAL_SOL, INITIAL_VIRTUAL_TOKENS, TOTAL_SUPPLY, curve_progress

if TYPE_CHECKING:
    from .wallets import WalletIntel
    from .market import MarketContext

FEATURE_VERSIONS: dict[str, int] = {
    # momentum
    "age_s": 1, "mc_sol": 1, "mult_since_launch": 1, "velocity_mc_per_min": 1, "accel_mc": 1,
    "curve_progress": 1, "vol_sol_per_min": 1, "vol_sol_last_60s": 1, "buy_sell_ratio_n": 1,
    "buy_sell_ratio_vol": 1, "median_buy_sol": 1, "n_trades": 1, "max_drawdown_so_far": 1,
    "ret_last_30s": 1,
    # qualité des acheteurs
    "unique_buyers": 1, "weighted_buyers": 1, "independent_buyers": 1, "independence_ratio": 1,
    "smart_share": 1, "smart_count": 1, "sniper_share": 1, "bot_share": 1, "fresh_wallet_share": 1,
    # manipulation
    "creation_slot_supply_pct": 1, "top10_concentration": 1, "dev_holding_pct": 1, "dev_sold_pct": 1,
    "dev_n_tokens": 1, "dev_rug_rate": 1, "dev_winner_rate": 1, "wash_score": 1,
    "mint_authority": 1, "freeze_authority": 1,
    # contexte
    "mkt_temperature": 1, "mkt_launches_per_h": 1, "mkt_migrations_per_h": 1, "sol_price_usd": 1,
    "hour_sin": 1, "hour_cos": 1, "dow": 1, "narrative_score": 1,
    # 1 si le token a reçu l'enrichissement RPC ciblé (graphe de financement, autorités)
    "enriched": 1,
    # métadonnées (réseaux sociaux, description) lues à la création
    "meta_fetched": 1, "meta_has_twitter": 1, "meta_has_telegram": 1, "meta_has_website": 1, "meta_n_socials": 1,
    "meta_desc_len": 1, "meta_twitter_is_post": 1, "meta_twitter_is_community": 1, "meta_website_is_social": 1,
}

_WORD = re.compile(r"[a-z0-9]{3,}")


def tokenize_name(name: str, symbol: str) -> set[str]:
    return set(_WORD.findall(f"{name} {symbol}".lower()))


@dataclass
class TokenState:
    created: TokenCreated
    trades: list[Trade] = field(default_factory=list)
    balances: dict[str, float] = field(default_factory=dict)      # tokens par wallet
    sol_in: dict[str, float] = field(default_factory=dict)
    sol_out: dict[str, float] = field(default_factory=dict)
    first_buy_ts: dict[str, float] = field(default_factory=dict)
    first_buy_slot: dict[str, int] = field(default_factory=dict)
    creation_slot_tokens: float = 0.0
    dev_bought: float = 0.0
    dev_sold: float = 0.0
    peak_price: float = 0.0
    max_dd: float = 0.0
    migrated_ts: float | None = None
    due: dict[str, float] = field(default_factory=dict)          # échéance de chaque point de décision
    recent: deque = field(default_factory=deque)                   # (ts, trader, achat ?, prix) sur 15 min
    seen_traders: set = field(default_factory=set)
    last_wave_check: float = 0.0
    amm_first_price: float | None = None
    amm_peak: float = 0.0
    amm_buy_sol: float = 0.0
    amm_sell_sol: float = 0.0
    amm_n: int = 0
    amm_price: float | None = None        # dernier prix PumpSwap/DexScreener (après migration)
    amm_ts: float | None = None
    amm_pool_sol: float | None = None
    mint_authority: float | None = None   # 1 actif, 0 révoqué, None inconnu
    freeze_authority: float | None = None
    emitted_points: set[str] = field(default_factory=set)
    meta: dict | None = None

    @property
    def t0(self) -> float:
        return self.created.ts

    @property
    def mint(self) -> str:
        return self.created.mint

    def last(self) -> Trade | None:
        return self.trades[-1] if self.trades else None

    def apply(self, t: Trade, sniper_slot_window: int = 2) -> None:
        self.trades.append(t)
        w = t.trader
        if t.is_buy:
            self.balances[w] = self.balances.get(w, 0.0) + t.tokens
            self.sol_in[w] = self.sol_in.get(w, 0.0) + t.sol
            if w not in self.first_buy_ts:
                self.first_buy_ts[w] = t.ts
                self.first_buy_slot[w] = t.slot
            if self.created.slot and t.slot and t.slot <= self.created.slot:
                self.creation_slot_tokens += t.tokens
            elif not self.created.slot and t.ts - self.t0 < 0.5:
                self.creation_slot_tokens += t.tokens   # source sans slot : approximation 500 ms
            if w == self.created.creator:
                self.dev_bought += t.tokens
        else:
            self.balances[w] = max(0.0, self.balances.get(w, 0.0) - t.tokens)
            self.sol_out[w] = self.sol_out.get(w, 0.0) + t.sol
            if w == self.created.creator:
                self.dev_sold += t.tokens
        p = t.price
        if p > self.peak_price:
            self.peak_price = p
        elif self.peak_price > 0:
            self.max_dd = max(self.max_dd, 1 - p / self.peak_price)

    def price_at(self, ts: float) -> float:
        """Dernier prix connu à l'instant ts."""
        lo, hi = 0, len(self.trades)
        while lo < hi:
            mid = (lo + hi) // 2
            if self.trades[mid].ts <= ts:
                lo = mid + 1
            else:
                hi = mid
        if lo == 0:
            return INITIAL_VIRTUAL_SOL / INITIAL_VIRTUAL_TOKENS
        return self.trades[lo - 1].price

    def is_sniper(self, wallet: str, window: int) -> bool:
        s = self.first_buy_slot.get(wallet)
        if s and self.created.slot:
            return s - self.created.slot <= window
        ts = self.first_buy_ts.get(wallet)
        return ts is not None and ts - self.t0 <= 1.0 * window


def _safe_div(a: float, b: float) -> float:
    return a / b if b else 0.0


def compute_features(
    st: TokenState, now: float, intel: "WalletIntel | None", market: "MarketContext | None", cfg: dict
) -> dict[str, float]:
    f: dict[str, float] = {}
    trades = st.trades
    age = max(1e-6, now - st.t0)
    last = st.last()
    price = last.price if last else INITIAL_VIRTUAL_SOL / INITIAL_VIRTUAL_TOKENS
    p0 = INITIAL_VIRTUAL_SOL / INITIAL_VIRTUAL_TOKENS
    mc = price * TOTAL_SUPPLY
    f["age_s"] = age
    f["mc_sol"] = mc
    f["mult_since_launch"] = price / p0
    p60 = st.price_at(now - 60)
    p30 = st.price_at(now - 30)
    f["velocity_mc_per_min"] = (price - p60) * TOTAL_SUPPLY
    f["accel_mc"] = ((price - p30) - (p30 - p60)) * TOTAL_SUPPLY / 0.5
    f["ret_last_30s"] = _safe_div(price, p30) - 1
    f["curve_progress"] = curve_progress(last.v_tokens) if last else 0.0
    f["max_drawdown_so_far"] = st.max_dd

    buys = [t for t in trades if t.is_buy]
    sells = [t for t in trades if not t.is_buy]
    vol = sum(t.sol for t in trades)
    f["n_trades"] = float(len(trades))
    f["vol_sol_per_min"] = vol / (age / 60)
    f["vol_sol_last_60s"] = sum(t.sol for t in trades if t.ts >= now - 60)
    f["buy_sell_ratio_n"] = _safe_div(len(buys), len(sells) + 1)
    f["buy_sell_ratio_vol"] = _safe_div(sum(t.sol for t in buys), sum(t.sol for t in sells) + 0.01)
    f["median_buy_sol"] = statistics.median([t.sol for t in buys]) if buys else 0.0

    # ---- acheteurs ----
    buyers = list(st.first_buy_ts.keys())
    f["unique_buyers"] = float(len(buyers))
    win = cfg.get("sniper_slot_window", 2)
    snipers = [w for w in buyers if st.is_sniper(w, win)]
    buy_vol_by = {w: st.sol_in.get(w, 0.0) for w in buyers}
    total_buy_vol = sum(buy_vol_by.values()) or 1e-9
    f["sniper_share"] = sum(buy_vol_by[w] for w in snipers) / total_buy_vol
    if intel is not None:
        q = intel.buyer_quality(buyers, now)
        f["weighted_buyers"] = q["weighted"]
        f["smart_count"] = q["smart_count"]
        f["smart_share"] = _safe_div(sum(buy_vol_by[w] for w in buyers if intel.is_smart(w)), total_buy_vol)
        f["bot_share"] = _safe_div(sum(buy_vol_by[w] for w in buyers if intel.is_bot(w)), total_buy_vol)
        f["fresh_wallet_share"] = q["fresh_share"]
        f["independent_buyers"] = float(intel.independent_clusters(buyers))
        f["independence_ratio"] = _safe_div(f["independent_buyers"], len(buyers))
        dev = intel.dev_stats(st.created.creator)
        f["dev_n_tokens"] = float(dev["n_tokens"])
        f["dev_rug_rate"] = _safe_div(dev["n_rugs"], dev["n_tokens"])
        f["dev_winner_rate"] = _safe_div(dev["n_winners"], dev["n_tokens"])

    # ---- manipulation ----
    f["creation_slot_supply_pct"] = st.creation_slot_tokens / TOTAL_SUPPLY
    holders = sorted((b for w, b in st.balances.items() if b > 0), reverse=True)
    f["top10_concentration"] = sum(holders[:10]) / TOTAL_SUPPLY
    f["dev_holding_pct"] = st.balances.get(st.created.creator, 0.0) / TOTAL_SUPPLY
    f["dev_sold_pct"] = _safe_div(st.dev_sold, st.dev_bought)
    # wash : volume des wallets qui achètent ET revendent dans la même minute, en boucle
    round_trips = 0.0
    seen_buy: dict[str, float] = {}
    for t in trades:
        if t.is_buy:
            seen_buy[t.trader] = t.ts
        elif t.trader in seen_buy and t.ts - seen_buy[t.trader] < 60:
            round_trips += t.sol
    f["wash_score"] = _safe_div(round_trips, vol)
    if st.mint_authority is not None:
        f["mint_authority"] = st.mint_authority
    if st.freeze_authority is not None:
        f["freeze_authority"] = st.freeze_authority

    # ---- contexte ----
    if market is not None:
        m = market.snapshot(now)
        f["mkt_temperature"] = m["temperature"]
        f["mkt_launches_per_h"] = m["launches_per_h"]
        f["mkt_migrations_per_h"] = m["migrations_per_h"]
        if m.get("sol_price_usd"):
            f["sol_price_usd"] = m["sol_price_usd"]
        f["narrative_score"] = market.narrative_score(tokenize_name(st.created.name, st.created.symbol), now)
    # ---- après migration (PumpSwap) ----
    if st.migrated_ts is not None and st.amm_price and st.amm_first_price:
        f["amm_ret_since_mig"] = st.amm_price / st.amm_first_price - 1
        f["amm_dd_peak"] = st.amm_price / st.amm_peak - 1 if st.amm_peak else 0.0
        f["amm_buy_ratio"] = _safe_div(st.amm_buy_sol, st.amm_buy_sol + st.amm_sell_sol)
        f["amm_n_trades"] = math.log1p(st.amm_n)
        f["since_migration_s"] = now - st.migrated_ts
    f.update(meta_features(st.meta))
    hour = (now % 86400) / 3600
    f["hour_sin"] = math.sin(2 * math.pi * hour / 24)
    f["hour_cos"] = math.cos(2 * math.pi * hour / 24)
    f["dow"] = float(int(now // 86400 + 4) % 7)   # 0 = lundi
    return f


def claude_context(st: TokenState, now: float, base: dict[str, float]) -> dict[str, Any]:
    """Vue en lecture seule passée aux features générées par Claude."""
    return {
        "now_rel": now - st.t0,
        "features": dict(base),
        "creator": st.created.creator,
        "name": st.created.name,
        "symbol": st.created.symbol,
        "trades": [
            {"t": t.ts - st.t0, "is_buy": t.is_buy, "sol": t.sol, "tokens": t.tokens,
             "trader": t.trader, "price": t.price, "slot_rel": (t.slot - st.created.slot) if (t.slot and st.created.slot) else None}
            for t in st.trades if t.ts <= now
        ],
        "balances": {w: b for w, b in st.balances.items() if b > 0},
    }
