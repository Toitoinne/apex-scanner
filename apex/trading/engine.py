"""Moteur du trader (cœur pur, testé) : ordres en attente, positions, exécution SIMULÉE
réaliste (délai d'exécution, profondeur, frais, slippage, échecs comme on-chain).
Le mode réel passe par le service (LiveVenue) ; la comptabilité des positions est la même."""
from __future__ import annotations

import itertools
from collections import deque
from dataclasses import dataclass, field

from .fills import Fees, Fill, Market, buy, sell
from .risk import Limits, can_open

SIM, REAL = "simulation", "reel"


@dataclass
class Order:
    id: int
    decision_id: str
    mint: str
    side: str
    mode: str
    reason: str
    expected_price: float
    created: float
    execute_at: float
    sol: float = 0.0            # achat : mise
    tokens: float = 0.0         # vente : quantité
    attempt: int = 0


@dataclass
class Position:
    decision_id: str
    mint: str
    mode: str
    symbol: str = ""
    policy: str = ""
    opened_at: float = 0.0
    sol_in: float = 0.0
    sol_out: float = 0.0
    tokens_initial: float = 0.0
    tokens: float = 0.0
    status: str = "pending"     # pending | open | closed | failed

    @property
    def pnl_sol(self) -> float:
        return self.sol_out - self.sol_in

    def pnl(self, price: float = 0.0) -> float:
        """PnL réalisé + latent (au prix donné), en fraction de la mise."""
        if self.sol_in <= 0:
            return 0.0
        return (self.sol_out + self.tokens * price - self.sol_in) / self.sol_in


@dataclass
class TraderEngine:
    limits: Limits = field(default_factory=Limits)
    fees: Fees = field(default_factory=Fees)
    latency_s: float = 2.0
    feed_lag_s: float = 0.0          # retard mesuré du flux de données (ajouté au délai en simulation)
    max_buy_slippage: float = 0.15
    sell_slippages: tuple[float, ...] = (0.15, 0.30, 0.60, 1.0)
    max_wait_s: float = 15.0
    positions: dict[str, Position] = field(default_factory=dict)
    pending: list[Order] = field(default_factory=list)
    market: dict[str, deque] = field(default_factory=dict)        # mint -> derniers états
    _ids: itertools.count = field(default_factory=lambda: itertools.count(1))

    # ---------------- données de marché ----------------
    def watching(self, mint: str) -> bool:
        return mint in self.market

    def on_market(self, mint: str, m: Market) -> None:
        dq = self.market.get(mint)
        if dq is not None:
            dq.append(m)

    def _watch(self, mint: str, seed: Market | None) -> None:
        if mint not in self.market:
            self.market[mint] = deque(maxlen=400)
            if seed is not None:
                self.market[mint].append(seed)

    def _snapshot_at(self, mint: str, t: float, now: float) -> Market | None:
        dq = self.market.get(mint) or ()
        for m in dq:
            if m.ts >= t:
                return m
        if now >= t + self.max_wait_s and dq:
            return dq[-1]               # aucun trade depuis : on exécute sur le dernier état connu
        return None

    # ---------------- ordres ----------------
    def open(self, alert: dict, mode: str, now: float, *, killed: bool, realized_today_sol: float,
             trades_today: int, wallet_sol: float | None = None) -> tuple[Order | None, str]:
        did = alert["decision_id"]
        if did in self.positions:
            return None, "déjà en position"
        n_open = sum(1 for p in self.positions.values() if p.status in ("pending", "open") and p.mode == mode)
        dec = can_open(self.limits, killed=killed, open_positions=n_open, realized_today_sol=realized_today_sol,
                       trades_today=trades_today, wallet_sol=wallet_sol)
        if not dec.ok:
            return None, dec.reason
        seed = None
        if alert.get("v_sol") and alert.get("v_tokens"):
            seed = Market(ts=alert["ts"], price=alert["v_sol"] / alert["v_tokens"], v_sol=alert["v_sol"],
                          v_tokens=alert["v_tokens"], migrated=bool(alert.get("migrated")))
        self._watch(alert["mint"], seed)
        self.positions[did] = Position(did, alert["mint"], mode, alert.get("symbol") or "", alert.get("policy") or "", now)
        o = Order(next(self._ids), did, alert["mint"], "buy", mode, "alerte", alert.get("entry_price") or 0.0,
                  now, now + (self.latency_s + self.feed_lag_s if mode == SIM else 0.0), sol=self.limits.sol_per_trade)
        self.pending.append(o)
        return o, ""

    def on_signal(self, sig: dict, now: float) -> Order | None:
        p = self.positions.get(sig["decision_id"])
        if p is None or p.status != "open" or p.tokens <= 0:
            return None
        qty = p.tokens if sig.get("closed") else min(p.tokens, sig.get("fraction", 0.0) * p.tokens_initial)
        if qty <= 0:
            return None
        o = Order(next(self._ids), p.decision_id, p.mint, "sell", p.mode, sig.get("kind", "VENTE"),
                  sig.get("price") or 0.0, now, now + (self.latency_s + self.feed_lag_s if p.mode == SIM else 0.0), tokens=qty)
        self.pending.append(o)
        return o

    # ---------------- exécution simulée ----------------
    def step(self, now: float) -> list[tuple[Order, Fill]]:
        """Exécute les ordres SIMULÉS arrivés à échéance (les ordres réels sont exécutés par le service)."""
        done, keep = [], []
        for o in self.pending:
            if o.mode != SIM:
                keep.append(o)
                continue
            m = self._snapshot_at(o.mint, o.execute_at, now)
            if m is None:
                keep.append(o)
                continue
            if o.side == "buy":
                f = buy(m, o.sol, o.expected_price or m.price, self.max_buy_slippage, self.fees)
            else:
                tol = self.sell_slippages[min(o.attempt, len(self.sell_slippages) - 1)]
                f = sell(m, o.tokens, o.expected_price or m.price, tol, self.fees)
                if not f.ok and o.attempt + 1 < len(self.sell_slippages):
                    o.attempt += 1                     # on retente avec une tolérance plus large
                    o.execute_at = now + 3
                    keep.append(o)
            self.apply(o, f, now)
            done.append((o, f))
        self.pending = keep
        return done

    def apply(self, o: Order, f: Fill, now: float) -> None:
        p = self.positions.get(o.decision_id)
        if p is None:
            return
        if o.side == "buy":
            if f.ok:
                p.sol_in, p.tokens_initial, p.tokens, p.status = f.sol, f.tokens, f.tokens, "open"
            else:
                p.sol_in, p.sol_out, p.status = f.sol, 0.0, "failed"   # seuls les frais réseau sont perdus
        elif f.ok:
            p.tokens = max(0.0, p.tokens - f.tokens)
            p.sol_out += f.sol
            if p.tokens <= p.tokens_initial * 1e-6:
                p.tokens, p.status = 0.0, "closed"
        else:
            p.sol_out += f.sol                            # frais réseau de la tentative (négatif)

    def last_price(self, mint: str) -> float:
        dq = self.market.get(mint)
        return dq[-1].price if dq else 0.0

    def forget_closed(self) -> None:
        for did in [d for d, p in self.positions.items() if p.status in ("closed", "failed")]:
            mint = self.positions.pop(did).mint
            if not any(p.mint == mint for p in self.positions.values()) and not any(o.mint == mint for o in self.pending):
                self.market.pop(mint, None)
