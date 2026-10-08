"""LABELER (cœur pur) : suit chaque token jusqu'à 24 h et produit
- les labels multi-horizons (courts : L2…L60 ; longs : x5 en 6 h, x10 en 24 h), chacun dès son horizon ;
- pour CHAQUE décision, la simulation de toutes les stratégies de sortie → un `Outcome`
  (PnL par stratégie) qui sert de récompense au bandit : le système apprend ce qui rapporte ;
- les positions des tokens alertés (même machine à états) → signaux de vente + paper trading ;
- un résumé de clôture (PnL par wallet, rug, gagnant) pour les bases smart wallets / devs.

Après la migration, le chemin de prix continue grâce aux `PriceTick` (PumpSwap via DexScreener).
"""
from __future__ import annotations

import bisect
import heapq
from dataclasses import dataclass, field
from typing import Any

from ..events import Decision, Label, Migration, Outcome, PriceTick, TokenCreated, Trade
from ..trading.exits import DangerDetector, Policy, PolicyState
from ..trading.pricefilter import PriceGuard
from .labels import outcome, simulated_trade

OUT = "__OUTCOME__"
REACH = (("x2", 1.0), ("x5", 4.0), ("x10", 9.0), ("x20", 19.0))


@dataclass(slots=True)
class SlimDecision:
    decision_id: str
    mint: str
    point: str
    ts: float
    entry_price: float
    spot_price: float


@dataclass
class Track:
    created: TokenCreated
    times: list[float] = field(default_factory=list)
    prices: list[float] = field(default_factory=list)
    flows: dict[str, list[float]] = field(default_factory=dict)   # wallet -> [sol_in, sol_out, tokens, first_ts, last_ts]
    early_slots: dict[int, list[str]] = field(default_factory=dict)  # slot -> acheteurs (10 premiers slots)
    pending: int = 0
    closed_emitted: bool = False
    peak_price: float = 0.0
    migrated: bool = False
    any_rug: bool = False
    any_winner: bool = False
    danger: DangerDetector | None = None
    sims: dict[str, dict[str, PolicyState]] = field(default_factory=dict)   # decision_id -> stratégie -> état

    def path_after(self, t: float, until: float) -> list[tuple[float, float]]:
        i = bisect.bisect_right(self.times, t)
        j = bisect.bisect_right(self.times, until)
        return list(zip(self.times[i:j], self.prices[i:j]))


@dataclass
class Position:
    """Position d'un token alerté (signaux de vente + paper trading)."""
    decision_id: str
    mint: str
    policy: str
    state: PolicyState
    symbol: str = ""


class LabelerEngine:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.lcfg = {k: v for k, v in cfg["labels"].items() if isinstance(v, dict) and "horizon_s" in v}
        self.rug = cfg["labels"]["rug"]
        self.sim = cfg["bandit"]["sim_trade"]
        self.sell_fee = cfg["fees"]["pump_fee_bps"]
        self.max_point = max(int(s) for s in cfg["decision_points_s"])
        self.track_horizon = cfg["track_horizon_s"]
        self.idle_close = cfg["token_idle_close_s"]
        ex = cfg.get("exits", {})
        self.exit_fee = ex.get("fee", 0.02)
        self.policies = {n: Policy.from_cfg(n, c) for n, c in ex.get("policies", {}).items()}
        self.outcome_horizon = max([p.time_limit_s for p in self.policies.values()] + [3600])
        self.tracks: dict[str, Track] = {}
        self.decisions: dict[str, SlimDecision] = {}
        self.heap: list[tuple[float, str, str]] = []   # (échéance, decision_id, horizon | OUT)
        self.positions: dict[str, Position] = {}       # decision_id -> position alertée
        self.signals: list[dict] = []                  # actions sur positions (à envoyer)
        self.outcomes: list[Outcome] = []              # outcomes prêts (stratégies toutes clôturées)
        self._last_closure = 0.0
        self.guard = PriceGuard(cfg.get("pricefeed", {}).get("max_jump", 5.0))

    # ------------------------------------------------------------------
    def on_event(self, ev: Any) -> None:
        if isinstance(ev, TokenCreated):
            if ev.mint not in self.tracks:
                self.tracks[ev.mint] = Track(created=ev, danger=DangerDetector(ev.creator))
        elif isinstance(ev, Trade):
            tr = self.tracks.get(ev.mint)
            danger = None
            if tr is not None:
                t = self._append(tr, ev.ts, ev.price)
                if ev.is_buy and ev.slot and tr.created.slot and ev.slot - tr.created.slot <= 10:
                    tr.early_slots.setdefault(ev.slot, []).append(ev.trader)
                if ev.ts - tr.created.ts <= 600 or ev.trader in tr.flows:
                    fl = tr.flows.setdefault(ev.trader, [0.0, 0.0, 0.0, ev.ts, ev.ts])
                    if ev.is_buy:
                        fl[0] += ev.sol
                        fl[2] += ev.tokens
                    else:
                        fl[1] += ev.sol
                        fl[2] -= ev.tokens
                    fl[4] = ev.ts
                danger = tr.danger.on_trade(t, ev.trader, ev.is_buy, ev.sol, ev.tokens, ev.price) if tr.danger else None
                self._update_sims(tr, t, ev.price, danger)
            self._update_positions(ev.mint, ev.ts, ev.price, danger)
        elif isinstance(ev, PriceTick):
            tr = self.tracks.get(ev.mint)
            if tr is not None and tr.prices:
                self.guard.seed(ev.mint, tr.prices[-1])
            if not self.guard.accept(ev.mint, ev.price):
                return                      # relevé isolé aberrant (aussi lors du rejeu après redémarrage)
            danger = None
            if tr is not None:
                t = self._append(tr, ev.ts, ev.price)
                if ev.trader and tr.danger is not None:     # trade PumpSwap : signaux de danger après migration
                    danger = tr.danger.on_trade(t, ev.trader, bool(ev.is_buy), ev.sol or 0.0, ev.tokens or 0.0, ev.price)
                self._update_sims(tr, t, ev.price, danger)
            self._update_positions(ev.mint, ev.ts, ev.price, danger)
        elif isinstance(ev, Migration):
            tr = self.tracks.get(ev.mint)
            if tr:
                tr.migrated = True
        elif isinstance(ev, Decision):
            self.on_decision(ev)

    @staticmethod
    def _append(tr: Track, ts: float, price: float) -> float:
        t = tr.times[-1] if tr.times and ts < tr.times[-1] else ts     # préserve l'ordre
        tr.times.append(t)
        tr.prices.append(price)
        tr.peak_price = max(tr.peak_price, price)
        return t

    def _update_sims(self, tr: Track, t: float, price: float, danger: str | None) -> None:
        done = []
        for did, states in tr.sims.items():
            for name, st in states.items():
                if not st.closed:
                    st.on_price(self.policies[name], t, price, danger)
            if all(st.closed for st in states.values()):
                done.append(did)
        for did in done:
            self._finalize(did, t)

    def _update_positions(self, mint: str, t: float, price: float, danger: str | None) -> None:
        if not self.positions:
            return
        for pos in [p for p in self.positions.values() if p.mint == mint and not p.state.closed]:
            for a in pos.state.on_price(self.policies[pos.policy], t, price, danger):
                self.signals.append({"decision_id": pos.decision_id, "mint": mint, "policy": pos.policy,
                                     "symbol": pos.symbol, **a.__dict__})

    # ------------------------------------------------------------------
    def on_decision(self, d: Decision) -> None:
        tr = self.tracks.get(d.mint)
        if tr is None or not d.entry_price or d.entry_price <= 0:
            return
        self.decisions[d.decision_id] = SlimDecision(d.decision_id, d.mint, d.point, d.ts, d.entry_price, d.spot_price)
        for h, c in self.lcfg.items():
            heapq.heappush(self.heap, (d.ts + c["horizon_s"], d.decision_id, h))
            tr.pending += 1
        if self.policies:
            tr.sims[d.decision_id] = {n: PolicyState(n, d.entry_price, d.ts, self.exit_fee) for n in self.policies}
            heapq.heappush(self.heap, (d.ts + self.outcome_horizon, d.decision_id, OUT))
            tr.pending += 1

    def open_position(self, alert: dict) -> None:
        """Une alerte a été envoyée : on suit la position avec la stratégie choisie par le bandit."""
        did, pol = alert["decision_id"], alert.get("policy")
        if did in self.positions or pol not in self.policies:
            return
        d = self.decisions.get(did)
        entry = d.entry_price if d else alert.get("entry_price")
        if not entry:
            return
        self.positions[did] = Position(did, alert["mint"], pol, PolicyState(pol, entry, alert["ts"], self.exit_fee),
                                       alert.get("symbol") or "")

    def _finalize(self, did: str, now: float) -> None:
        d = self.decisions.get(did)
        tr = self.tracks.get(d.mint) if d else None
        if d is None or tr is None or did not in tr.sims:
            return
        states = tr.sims.pop(did)
        for name, st in states.items():
            st.close_at_market(self.policies[name], now)
        path = tr.path_after(d.ts, d.ts + self.outcome_horizon)
        maxret = max((p for _, p in path), default=d.entry_price) / d.entry_price - 1
        reached = {}
        for key, r in REACH:
            hit = next((t for t, p in path if p / d.entry_price - 1 >= r), None)
            if hit is not None:
                reached[key] = hit - d.ts
        self.outcomes.append(Outcome(decision_id=did, mint=d.mint, point=d.point, ts=now,
                                     pnl={n: round(st.pnl(), 5) for n, st in states.items()},
                                     max_return=maxret, reached=reached))

    # ------------------------------------------------------------------
    def tick(self, now: float) -> tuple[list[Label], list[dict], list[dict]]:
        """Retourne (labels émis, résumés de clôture, tokens finalisés)."""
        labels: list[Label] = []
        while self.heap and self.heap[0][0] <= now:
            _, did, h = heapq.heappop(self.heap)
            d = self.decisions.get(did)
            if d is None:
                continue
            tr = self.tracks.get(d.mint)
            if tr is None:
                continue
            tr.pending -= 1
            if h == OUT:
                self._finalize(did, now)
                continue
            c = self.lcfg[h]
            path = tr.path_after(d.ts, d.ts + c["horizon_s"])
            o = outcome(path, d.ts, d.entry_price, d.spot_price, c.get("up", 1.0), c.get("down", 1.0), c["horizon_s"],
                        self.rug["drop_from_peak"], self.rug["window_s"])
            y = int(o.max_return >= c["reach"] - 1) if "reach" in c else o.y
            pnl = simulated_trade(path, d.ts, d.entry_price, self.sim["take_profit"], self.sim["stop_loss"],
                                  min(self.sim["horizon_s"], c["horizon_s"]), self.sell_fee)
            labels.append(Label(
                decision_id=did, mint=d.mint, point=d.point, horizon=h, y=y, ts=now,
                max_return=o.max_return, max_drawdown=o.max_drawdown, time_to_peak_s=o.time_to_peak_s,
                rug=o.rug, final_return=o.final_return, sim_pnl=pnl,
            ))
            tr.any_rug |= o.rug
            tr.any_winner |= (h == "L60" and y == 1)
        # positions suivies au-delà de leur limite de temps sans nouveau prix : clôture au dernier prix
        for pos in self.positions.values():
            p = self.policies.get(pos.policy)
            if p and not pos.state.closed and now - pos.state.t0 >= p.time_limit_s:
                for a in pos.state.close_at_market(p, now):
                    self.signals.append({"decision_id": pos.decision_id, "mint": pos.mint, "policy": pos.policy,
                                         "symbol": pos.symbol, **a.__dict__})
        closed, finals = ([], [])
        if now - self._last_closure >= 5:
            closed, finals = self._closures(now)
            self._last_closure = now
        if len(self.decisions) > 50_000:
            live = {did for _, did, _ in self.heap} | set(self.positions)
            self.decisions = {k: v for k, v in self.decisions.items() if k in live}
        return labels, closed, finals

    def drain(self) -> tuple[list[Outcome], list[dict]]:
        out, sig = self.outcomes, self.signals
        self.outcomes, self.signals = [], []
        return out, sig

    def _closures(self, now: float) -> tuple[list[dict], list[dict]]:
        closed, finals = [], []
        for mint in list(self.tracks):
            tr = self.tracks[mint]
            age = now - tr.created.ts
            # résumé wallets/devs dès que les labels courts sont faits (sans attendre les labels 24 h)
            if not tr.closed_emitted and age > self.max_point + 3700:
                tr.closed_emitted = True
                closed.append(self._summary(tr))
            idle = now - (tr.times[-1] if tr.times else tr.created.ts)
            if tr.closed_emitted and tr.pending <= 0 and (age > self.track_horizon or idle > self.idle_close):
                finals.append({
                    "mint": mint, "peak_mc_sol": tr.peak_price * 1e9, "rugged": tr.any_rug,
                    "outcome": {"winner": tr.any_winner, "migrated": tr.migrated, "n_trades": len(tr.times)},
                })
                del self.tracks[mint]
        return closed, finals

    def migrated_mints(self) -> set[str]:
        """Tokens dont le prix doit venir de PumpSwap (migrés et encore utiles)."""
        out = {m for m, tr in self.tracks.items() if tr.migrated and (tr.pending > 0)}
        out |= {p.mint for p in self.positions.values() if not p.state.closed
                and (self.tracks.get(p.mint) is None or self.tracks[p.mint].migrated)}
        return out

    def _summary(self, tr: Track) -> dict:
        last_price = tr.prices[-1] if tr.prices else 0.0
        wallets, flippers = {}, []
        for w, (sin, sout, tok, first, last) in tr.flows.items():
            if sin <= 0:
                continue
            wallets[w] = sout + max(0.0, tok) * last_price - sin
            if sout > 0 and last - first < 20:
                flippers.append(w)
        if len(wallets) > 300:   # garde les plus gros flux
            wallets = dict(sorted(wallets.items(), key=lambda kv: -abs(kv[1]))[:300])
        return {
            "mint": tr.created.mint, "creator": tr.created.creator, "rugged": tr.any_rug,
            "winner": tr.any_winner, "wallets": wallets, "fast_flippers": flippers,
            "slot_groups": [g for g in tr.early_slots.values() if len(set(g)) >= 2],
        }
