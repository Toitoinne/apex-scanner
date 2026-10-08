"""Mathématiques de la bonding curve pump.fun (produit constant sur réserves virtuelles)
et estimation du slippage. Fonctions pures."""
from __future__ import annotations

INITIAL_VIRTUAL_SOL = 30.0
INITIAL_VIRTUAL_TOKENS = 1_073_000_000.0
INITIAL_REAL_TOKENS = 793_100_000.0
TOTAL_SUPPLY = 1_000_000_000.0


STANDARD_K = INITIAL_VIRTUAL_SOL * INITIAL_VIRTUAL_TOKENS   # invariant x·y de la courbe SOL standard


def is_standard_curve(v_sol: float, v_tokens: float, tol: float = 0.02) -> bool:
    """Vrai si le trade suit la bonding curve SOL standard de pump.fun (k = 30 × 1,073 Md).
    Les autres variantes présentes dans le programme (autre devise de cotation, autres
    paramètres de courbe) ne sont pas modélisées : leurs trades sont ignorés."""
    if v_sol <= 0 or v_tokens <= 0:
        return False
    return abs(v_sol * v_tokens / STANDARD_K - 1) <= tol


def tokens_out_for_sol(v_sol: float, v_tokens: float, sol_in: float, fee_bps: float) -> float:
    s = sol_in * (1 - fee_bps / 10_000)
    if v_sol <= 0 or v_tokens <= 0 or s <= 0:
        return 0.0
    k = v_sol * v_tokens
    return v_tokens - k / (v_sol + s)


def sol_out_for_tokens(v_sol: float, v_tokens: float, tokens_in: float, fee_bps: float) -> float:
    if v_sol <= 0 or v_tokens <= 0 or tokens_in <= 0:
        return 0.0
    k = v_sol * v_tokens
    gross = v_sol - k / (v_tokens + tokens_in)
    return gross * (1 - fee_bps / 10_000)


def effective_buy_price(v_sol: float, v_tokens: float, sol_in: float, fee_bps: float) -> float:
    """Prix moyen réellement payé (SOL/token) pour un achat de sol_in SOL, frais compris."""
    out = tokens_out_for_sol(v_sol, v_tokens, sol_in, fee_bps)
    return sol_in / out if out > 0 else float("inf")


def slippage(v_sol: float, v_tokens: float, sol_in: float, fee_bps: float) -> float:
    """Écart relatif entre prix effectif et prix spot (0,05 = 5 %)."""
    spot = v_sol / v_tokens if v_tokens > 0 else 0.0
    if spot <= 0:
        return 0.0
    return effective_buy_price(v_sol, v_tokens, sol_in, fee_bps) / spot - 1


def curve_progress(v_tokens: float) -> float:
    """0 = lancement, 1 = bonding curve complète (migration)."""
    real_tokens = v_tokens - (INITIAL_VIRTUAL_TOKENS - INITIAL_REAL_TOKENS)
    return max(0.0, min(1.0, 1 - real_tokens / INITIAL_REAL_TOKENS))
