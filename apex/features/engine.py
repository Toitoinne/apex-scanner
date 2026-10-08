"""FEATURE ENGINE (cœur pur) : maintient l'état des tokens, planifie les points de
décision et produit des `Decision`. Utilisé tel quel par le service live et par
le rejeu backfill (horloge = timestamps des événements)."""
from __future__ import annotations

import heapq
import logging
import math
import time
from typing import Any, Callable

from ..events import Decision, Migration, TokenCreated, Trade
from ..safety.filters import SafetyFilters
from .curve import curve_progress, effective_buy_price
from .market import MarketContext
from .state import FEATURE_VERSIONS, TokenState, claude_context, compute_features, tokenize_name
from .wallets import WalletIntel

log = logging.getLogger(__name__)
STATE_TTL_S = 7200


class ClaudeFeature:
    def __init__(self, feature_id: str, version: int, fn: Callable[[dict], Any], budget_ms: float):
        self.id, self.version, self.fn, self.budget_ms = feature_id, version, fn, budget_ms
        self.calls = 0
        self.errors = 0
        self.total_ms = 0.0
        self.disabled_reason: str | None = None

    def __call__(self, ctx: dict) -> float | None:
        if self.disabled_reason:
            return None
        t = time.perf_counter()
        try:
            v = self.fn(ctx)
            out = None if v is None else float(v)
            if out is not None and out != out:
                out = None
        except Exception:  # noqa: BLE001
            self.errors += 1
            out = None
        dt_ms = (time.perf_counter() - t) * 1000
        self.calls += 1
        self.total_ms += dt_ms
        if self.calls >= 50:
            if self.errors / self.calls > 0.2:
                self.disabled_reason = "trop d'exceptions"
            elif self.total_ms / self.calls > self.budget_ms:
                self.disabled_reason = f"trop lente ({self.total_ms / self.calls:.1f} ms)"
        return out


class FeatureEngine:
    def __init__(self, cfg: dict, safety: SafetyFilters, intel: WalletIntel | None, market: MarketContext):
        self.cfg = cfg
        self.safety = safety
        self.intel = intel
        self.market = market
        self.points = [int(s) for s in cfg["decision_points_s"]]
        self.ref_sol = cfg["reference_trade_sol"]
        self.fee_bps = cfg["fees"]["pump_fee_bps"]
        self.amm_fee_bps = cfg["fees"]["pumpswap_fee_bps"]
        self.fcfg = cfg["features"]
        self.states: dict[str, TokenState] = {}
        self.heap: list[tuple[float, str, str]] = []
        self.claude_features: dict[str, ClaudeFeature] = {}
        self._pumped: set[str] = set()
        self.candidates: set[str] = set()
        self.new_candidates: list[str] = []

    # ---------------- événements ----------------
    def on_event(self, ev: Any) -> list[Decision]:
        if isinstance(ev, TokenCreated):
            if ev.mint not in self.states:
                self.states[ev.mint] = TokenState(created=ev)
                for s in self.points:
                    heapq.heappush(self.heap, (ev.ts + s, ev.mint, str(s)))
                self.market.on_launch(ev.ts)
            return []
        if isinstance(ev, Trade):
            st = self.states.get(ev.mint)
            if st is None:
                return []
            st.apply(ev, self.fcfg.get("sniper_slot_window", 2))
            if (ev.mint not in self.candidates and ev.ts - st.t0 <= 30
                    and len(st.first_buy_ts) >= self.fcfg.get("candidate_trigger_buyers", 12)):
                self.mark_candidate(ev.mint)
            mult = ev.price / (30.0 / 1_073_000_000.0)
            if mult > 5 and ev.mint not in self._pumped:
                self._pumped.add(ev.mint)
                self.market.on_pump(ev.ts, tokenize_name(st.created.name, st.created.symbol), mult)
            return []
        if isinstance(ev, Migration):
            self.market.on_migration(ev.ts)
            st = self.states.get(ev.mint)
            if st is None or st.migrated_ts is not None:
                return []
            st.migrated_ts = ev.ts
            if not self.cfg.get("decision_on_migration", False):
                return []
            # léger délai : laisse arriver les trades du même slot qui complètent la courbe
            heapq.heappush(self.heap, (ev.ts + 3, ev.mint, "migration"))
            return []
        return []

    def mark_candidate(self, mint: str) -> None:
        if mint in self.states and mint not in self.candidates:
            self.candidates.add(mint)
            self.new_candidates.append(mint)

    def tick(self, now: float) -> list[Decision]:
        out: list[Decision] = []
        while self.heap and self.heap[0][0] <= now:
            due, mint, point = heapq.heappop(self.heap)
            st = self.states.get(mint)
            if st is None:
                continue
            d = self.make_decision(st, point, due)
            if d:
                out.append(d)
        self._evict(now)
        return out

    def _evict(self, now: float) -> None:
        if len(self.states) < 1000:
            return
        for m in [m for m, s in self.states.items() if now - s.t0 > STATE_TTL_S]:
            del self.states[m]
            self._pumped.discard(m)
            self.candidates.discard(m)

    # ---------------- décision ----------------
    def make_decision(self, st: TokenState, point: str, now: float) -> Decision | None:
        if point in st.emitted_points:
            return None
        st.emitted_points.add(point)
        last = st.last()
        if last is None:
            return None           # aucun trade : rien à prédire (token mort-né)
        if point == "migration" and curve_progress(last.v_tokens) < 0.9:
            return None           # état incohérent avec une courbe complète : on s'abstient
        feats = compute_features(st, now, self.intel, self.market, self.fcfg)
        versions = dict(FEATURE_VERSIONS)
        if self.claude_features:
            ctx = claude_context(st, now, feats)
            for fid, cf in self.claude_features.items():
                v = cf(ctx)
                if v is not None:
                    feats[fid] = v
                versions[fid] = cf.version
        feats["enriched"] = 1.0 if st.mint in self.candidates else 0.0
        res = self.safety.check(feats)
        fee = self.amm_fee_bps if st.migrated_ts else self.fee_bps
        entry = effective_buy_price(last.v_sol, last.v_tokens, self.ref_sol, fee)
        if not math.isfinite(entry) or entry <= 0 or not math.isfinite(last.price) or last.price <= 0:
            return None           # pas de prix d'entrée valide : rien à prédire
        return Decision(
            decision_id=f"{st.mint}:{point}", mint=st.mint, point=point, ts=now, features=feats,
            entry_price=entry, spot_price=last.price, mc_sol=last.mc_sol, v_sol=last.v_sol,
            v_tokens=last.v_tokens, safety_flags=list(res.flags), blocked=res.blocked,
            feature_versions=versions,
            meta={"name": st.created.name, "symbol": st.created.symbol, "creator": st.created.creator,
                  "t0": st.t0, "migrated": st.migrated_ts is not None},
        )
