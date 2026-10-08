"""Slippage estimé pour plusieurs tailles d'achat (affiché dans les alertes)."""
from __future__ import annotations

from ..features.curve import slippage


def slippage_table(v_sol: float, v_tokens: float, sizes: list[float], fee_bps: float) -> dict[str, float]:
    return {f"{s:g}": round(slippage(v_sol, v_tokens, s, fee_bps), 4) for s in sizes}
