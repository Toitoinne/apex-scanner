import math

from apex.learning.models import rules_score


def test_rules_score_valeurs_aberrantes_sans_debordement():
    # z ≈ -1e6 : math.exp(-z) débordait (OverflowError) et la décision était perdue
    p = rules_score({"dev_sold_pct": 1e6, "wash_score": 1e6})
    assert 0.0 <= p < 1e-6
    p = rules_score({"smart_share": 1e6})
    assert 1 - 1e-6 < p <= 1.0
    assert math.isclose(rules_score({}), 1 / (1 + math.exp(2.5)))
