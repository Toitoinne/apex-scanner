"""Modèle d'exécution des ordres (cœur pur, testé).

Calcule ce qu'un ordre aurait RÉELLEMENT donné : prix au moment où la transaction atterrit
(après le délai d'exécution), impact sur la bonding curve (produit constant sur réserves
virtuelles) ou sur le pool PumpSwap, frais du protocole, frais PumpPortal (0,5 %) et frais
réseau (priorité). Un ordre dont le prix dépasse la tolérance de slippage échoue (comme
on-chain : la transaction est annulée).
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Market:
    """État du marché d'un token à un instant donné."""
    ts: float
    price: float                      # SOL par token (spot)
    v_sol: float | None = None        # bonding curve : réserves virtuelles
    v_tokens: float | None = None
    pool_sol: float | None = None     # PumpSwap : réserve SOL du pool
    migrated: bool = False


@dataclass(frozen=True)
class Fees:
    curve_bps: float = 125            # pump.fun (protocole + créateur)
    amm_bps: float = 30               # PumpSwap
    platform_pct: float = 0.5         # PumpPortal Local API
    network_sol: float = 0.0005       # frais de priorité + signature, par transaction


@dataclass(frozen=True)
class Fill:
    ok: bool
    side: str
    sol: float                        # SOL dépensés (achat) ou reçus nets (vente)
    tokens: float                     # tokens reçus (achat) ou vendus (vente)
    price: float                      # prix moyen effectif (SOL/token)
    slippage: float                   # écart vs prix attendu (positif = défavorable)
    fees_sol: float
    reason: str = ""


def _curve_buy_tokens(v_sol: float, v_tokens: float, sol_in: float) -> float:
    k = v_sol * v_tokens
    return v_tokens - k / (v_sol + sol_in)


def _curve_sell_sol(v_sol: float, v_tokens: float, tokens_in: float) -> float:
    k = v_sol * v_tokens
    return v_sol - k / (v_tokens + tokens_in)


def buy(m: Market, sol: float, expected_price: float, max_slippage: float, fees: Fees) -> Fill:
    """Achat de `sol` SOL (mise totale, frais compris)."""
    platform = sol * fees.platform_pct / 100
    proto_bps = fees.amm_bps if m.migrated else fees.curve_bps
    net = (sol - platform - fees.network_sol) * (1 - proto_bps / 10_000)
    if net <= 0 or m.price <= 0:
        return Fill(False, "buy", 0, 0, 0, 0, 0, "montant trop faible")
    if not m.migrated and m.v_sol and m.v_tokens:
        tokens = _curve_buy_tokens(m.v_sol, m.v_tokens, net)
    elif m.pool_sol:
        # pool à produit constant : réserve tokens = réserve SOL / prix
        pool_tokens = m.pool_sol / m.price
        tokens = pool_tokens - (m.pool_sol * pool_tokens) / (m.pool_sol + net)
    else:
        tokens = net / m.price * 0.97          # profondeur inconnue : impact prudent de 3 %
    if tokens <= 0:
        return Fill(False, "buy", 0, 0, 0, 0, 0, "liquidité insuffisante")
    price = sol / tokens
    slip = price / expected_price - 1 if expected_price > 0 else 0.0
    fees_sol = platform + fees.network_sol + (sol - platform - fees.network_sol) * proto_bps / 10_000
    if slip > max_slippage:
        # la transaction est annulée on-chain : seuls les frais réseau sont perdus
        return Fill(False, "buy", fees.network_sol, 0, price, slip, fees.network_sol,
                    f"slippage {slip:.1%} > tolérance {max_slippage:.0%}")
    return Fill(True, "buy", sol, tokens, price, slip, fees_sol)


def sell(m: Market, tokens: float, expected_price: float, max_slippage: float, fees: Fees) -> Fill:
    if tokens <= 0 or m.price <= 0:
        return Fill(False, "sell", 0, 0, 0, 0, 0, "rien à vendre")
    proto_bps = fees.amm_bps if m.migrated else fees.curve_bps
    if not m.migrated and m.v_sol and m.v_tokens:
        gross = _curve_sell_sol(m.v_sol, m.v_tokens, tokens)
    elif m.pool_sol:
        pool_tokens = m.pool_sol / m.price
        gross = m.pool_sol - (m.pool_sol * pool_tokens) / (pool_tokens + tokens)
    else:
        gross = tokens * m.price * 0.97
    after_proto = gross * (1 - proto_bps / 10_000)
    platform = after_proto * fees.platform_pct / 100
    net = after_proto - platform - fees.network_sol
    price = gross / tokens
    slip = 1 - price / expected_price if expected_price > 0 else 0.0
    fees_sol = gross - net
    if slip > max_slippage:
        return Fill(False, "sell", -fees.network_sol, 0, price, slip, fees.network_sol,
                    f"slippage {slip:.1%} > tolérance {max_slippage:.0%}")
    return Fill(True, "sell", max(net, 0.0), tokens, price, slip, fees_sol)
