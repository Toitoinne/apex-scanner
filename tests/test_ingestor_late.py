"""Trades livrés en retard par une connexion lente : leur prix est périmé, ils ne doivent pas passer."""
from types import SimpleNamespace as N

from apex.ingestor.service import is_late


def test_late_trades_are_dropped():
    ref: dict[str, int] = {}
    assert not is_late(ref, N(mint="A", slot=100), 4)
    assert not is_late(ref, N(mint="A", slot=103), 4)       # même seconde ou presque : normal
    assert not is_late(ref, N(mint="A", slot=99), 4)        # léger désordre entre connexions : toléré
    assert is_late(ref, N(mint="A", slot=50), 4)            # 22 s plus tôt (création reçue en retard)
    assert not is_late(ref, N(mint="B", slot=50), 4)        # chaque token a sa propre référence
    assert not is_late(ref, N(mint="A", slot=0), 4)         # source sans slot : jamais rejetée
    assert ref["A"] == 103
