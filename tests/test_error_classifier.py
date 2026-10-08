import pytest

from apex.errors.classifier import DynamicRule, classify

CFG = {"cost_notional_sol": 1.0, "late_entry_runup": 3.0, "late_entry_max_return": 0.10,
       "bundle_slot_supply_pct": 0.15, "fake_smart_share": 0.2, "slow_death_min_dd": 0.3}


def out(**kw):
    base = {"max_return": 0.0, "max_drawdown": 0.0, "rug": False, "sim_pnl": -0.2}
    base.update(kw)
    return base


def test_correct_prediction_is_not_an_error():
    assert classify(1, 1, {}, out(), False, CFG) is None
    assert classify(0, 0, {}, out(), False, CFG) is None


def test_rug_alerted_has_priority_and_cost():
    e = classify(1, 0, {"creation_slot_supply_pct": 0.5}, out(rug=True, sim_pnl=-0.9), False, CFG)
    assert e.error_type == "RUG_ALERTE"
    assert e.cost == pytest.approx(0.9)
    assert e.false_positive


def test_bundle_missed():
    e = classify(1, 0, {"creation_slot_supply_pct": 0.2}, out(), False, CFG)
    assert e.error_type == "BUNDLE_RATE"


def test_late_entry():
    e = classify(1, 0, {"mult_since_launch": 4.0}, out(max_return=0.05), False, CFG)
    assert e.error_type == "ENTREE_TARDIVE"


def test_fake_smart_money():
    e = classify(1, 0, {"smart_share": 0.3}, out(max_return=0.5), False, CFG)
    assert e.error_type == "FAUX_SMART_MONEY"


def test_slow_death_and_other():
    assert classify(1, 0, {}, out(max_drawdown=0.4), False, CFG).error_type == "MORT_LENTE"
    assert classify(1, 0, {}, out(max_drawdown=0.1), False, CFG).error_type == "AUTRE"


def test_missed_winner_vs_detected_too_late():
    e = classify(0, 1, {}, out(sim_pnl=1.0), later_positive=False, cfg=CFG)
    assert e.error_type == "GAGNANT_MANQUE"
    assert e.cost == pytest.approx(1.0)
    assert not e.false_positive
    assert classify(0, 1, {}, out(sim_pnl=1.0), later_positive=True, cfg=CFG).error_type == "GAGNANT_DETECTE_TROP_TARD"


def test_dynamic_rule_replaces_other():
    rule = DynamicRule.from_json("CTO_ABANDONNE", {"predicted": 1, "conditions": [
        {"feature": "dev_sold_pct", "op": ">=", "value": 0.9}, {"outcome": "max_return", "op": "<", "value": 0.2}]})
    e = classify(1, 0, {"dev_sold_pct": 1.0}, out(max_return=0.1, max_drawdown=0.1), False, CFG, [rule])
    assert e.error_type == "CTO_ABANDONNE"
    # une règle ne remplace pas un type de base déjà identifié
    e2 = classify(1, 0, {"dev_sold_pct": 1.0}, out(rug=True), False, CFG, [rule])
    assert e2.error_type == "RUG_ALERTE"


def test_dynamic_rule_validation():
    with pytest.raises(ValueError):
        DynamicRule.from_json("X", {"conditions": []})
    with pytest.raises(ValueError):
        DynamicRule.from_json("X", {"conditions": [{"feature": "a", "op": "=~", "value": 1}]})
