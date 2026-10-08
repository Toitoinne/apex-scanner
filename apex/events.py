"""Événements normalisés circulant sur Redis Streams."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Literal

import orjson

TOTAL_SUPPLY = 1_000_000_000 * 10**6   # pump.fun : 1 Md tokens, 6 décimales
LAMPORTS = 10**9


@dataclass(slots=True)
class TokenCreated:
    mint: str
    name: str
    symbol: str
    uri: str
    creator: str
    bonding_curve: str
    slot: int
    ts: float
    signature: str
    chain: str = "solana"
    kind: Literal["create"] = "create"


@dataclass(slots=True)
class Trade:
    mint: str
    signature: str
    slot: int
    ts: float
    trader: str
    is_buy: bool
    sol: float                 # SOL échangés (hors frais)
    tokens: float              # tokens (unités entières, décimales appliquées)
    v_sol: float               # réserves virtuelles après le trade (SOL)
    v_tokens: float            # réserves virtuelles après le trade (tokens)
    source: str = "helius"
    chain: str = "solana"
    kind: Literal["trade"] = "trade"

    @property
    def price(self) -> float:
        """Prix spot en SOL/token après le trade."""
        return self.v_sol / self.v_tokens if self.v_tokens > 0 else 0.0

    @property
    def mc_sol(self) -> float:
        return self.price * (TOTAL_SUPPLY / 10**6)


@dataclass(slots=True)
class Migration:
    mint: str
    slot: int
    ts: float
    signature: str
    pool: str = ""
    chain: str = "solana"
    kind: Literal["migration"] = "migration"


@dataclass(slots=True)
class Decision:
    """Vecteur de features à un point de décision (émis par le feature engine)."""
    decision_id: str
    mint: str
    point: str                 # "30", "60", ..., "migration"
    ts: float
    features: dict[str, float]
    entry_price: float         # prix effectif accessible pour reference_trade_sol (slippage compris)
    spot_price: float
    mc_sol: float
    v_sol: float
    v_tokens: float
    safety_flags: list[str] = field(default_factory=list)
    blocked: bool = False
    feature_versions: dict[str, int] = field(default_factory=dict)
    meta: dict[str, Any] = field(default_factory=dict)
    kind: Literal["decision"] = "decision"


@dataclass(slots=True)
class Label:
    decision_id: str
    mint: str
    point: str
    horizon: str               # L2, L5, L15, L60
    y: int
    ts: float
    max_return: float
    max_drawdown: float
    time_to_peak_s: float
    rug: bool
    final_return: float
    sim_pnl: float             # PnL du trade simulé (TP/SL) pour 1 unité notionnelle
    kind: Literal["label"] = "label"


@dataclass(slots=True)
class PriceTick:
    """Prix observé hors bonding curve (PumpSwap, via DexScreener) pour un token migré."""
    mint: str
    ts: float
    price: float               # SOL par token
    source: str = "dexscreener"
    # renseignés quand le prix vient d'un trade PumpSwap décodé (signaux de danger après migration)
    trader: str | None = None
    is_buy: bool | None = None
    sol: float | None = None
    tokens: float | None = None
    pool_sol: float | None = None      # réserve SOL du pool (profondeur, pour le système d'ordres)
    kind: Literal["tick"] = "tick"


@dataclass(slots=True)
class Outcome:
    """Résultat d'une décision pour CHAQUE stratégie de sortie simulée (récompense du bandit)."""
    decision_id: str
    mint: str
    point: str
    ts: float
    pnl: dict[str, float]      # stratégie -> PnL (fraction de la mise)
    max_return: float          # rendement max observé (24 h)
    reached: dict[str, float]  # "x2"/"x5"/"x10"/"x20" -> secondes pour l'atteindre
    kind: Literal["outcome"] = "outcome"


EVENT_TYPES = {
    "create": TokenCreated,
    "trade": Trade,
    "migration": Migration,
    "decision": Decision,
    "label": Label,
    "tick": PriceTick,
    "outcome": Outcome,
}


def dumps(ev: Any) -> bytes:
    if hasattr(ev, "__dataclass_fields__"):
        return orjson.dumps(asdict(ev))
    return orjson.dumps(ev)


def loads(raw: bytes | str) -> Any:
    d = orjson.loads(raw)
    cls = EVENT_TYPES.get(d.get("kind")) if isinstance(d, dict) else None
    if cls is None:
        return d
    return cls(**d)
