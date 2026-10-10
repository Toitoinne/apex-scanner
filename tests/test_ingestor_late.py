"""Trades livrés en retard par une connexion lente : leur prix est périmé, ils ne doivent pas passer."""
from types import SimpleNamespace as N

from apex.events import Trade
from apex.ingestor.service import fix_late_price, is_late


def test_late_trades_are_dropped():
    ref: dict[str, int] = {}
    assert not is_late(ref, N(mint="A", slot=100), 4)
    assert not is_late(ref, N(mint="A", slot=103), 4)       # même seconde ou presque : normal
    assert not is_late(ref, N(mint="A", slot=99), 4)        # léger désordre entre connexions : toléré
    assert is_late(ref, N(mint="A", slot=50), 4)            # 22 s plus tôt (création reçue en retard)
    assert not is_late(ref, N(mint="B", slot=50), 4)        # chaque token a sa propre référence
    assert not is_late(ref, N(mint="A", slot=0), 4)         # source sans slot : jamais rejetée
    assert ref["A"] == 103


def _t(slot: int, v_sol: float) -> Trade:
    return Trade(mint="M", signature=str(slot), slot=slot, ts=0.0, trader="w", is_buy=True, sol=1.0, tokens=1.0,
                 v_sol=v_sol, v_tokens=1e9)


def test_late_trade_kept_with_current_price():
    slots, res = {}, {}
    assert not fix_late_price(slots, res, _t(100, 40.0), 4)
    assert not fix_late_price(slots, res, _t(101, 41.0), 4)
    old = _t(50, 31.0)                                       # achat de la création, livré 22 s plus tard
    assert fix_late_price(slots, res, old, 4)
    assert old.v_sol == 41.0                                 # gardé, mais au prix du moment
    assert res["M"] == (41.0, 1e9)
