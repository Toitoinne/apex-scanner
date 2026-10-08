from apex.labeler.labels import is_rug, label_hit, outcome, simulated_trade


def path(*pts):
    return [(float(t), float(p)) for t, p in pts]


def test_target_hit_before_stop():
    p = path((10, 1.1), (20, 1.35), (30, 0.5))
    assert label_hit(p, 0, 1.0, up=0.30, down=0.30, horizon_s=120) == 1


def test_stop_hit_first_is_negative():
    p = path((10, 0.69), (20, 2.0))
    assert label_hit(p, 0, 1.0, up=0.30, down=0.30, horizon_s=120) == 0


def test_target_after_horizon_is_negative():
    p = path((10, 1.1), (130, 2.0))
    assert label_hit(p, 0, 1.0, up=0.30, down=0.30, horizon_s=120) == 0


def test_points_before_decision_are_ignored():
    p = path((-5, 5.0), (10, 1.0))
    assert label_hit(p, 0, 1.0, up=0.30, down=0.30, horizon_s=120) == 0


def test_entry_price_includes_slippage():
    # spot 1.0 mais prix d'entrée effectif 1.2 : +30 % depuis l'entrée = 1.56
    p = path((10, 1.5))
    assert label_hit(p, 0, 1.2, up=0.30, down=0.30, horizon_s=120) == 0
    assert label_hit(path((10, 1.57)), 0, 1.2, up=0.30, down=0.30, horizon_s=120) == 1


def test_l60_double_without_halving():
    p = path((100, 0.6), (1000, 2.1))
    assert label_hit(p, 0, 1.0, up=1.0, down=0.5, horizon_s=3600) == 1
    p2 = path((100, 0.49), (1000, 2.1))
    assert label_hit(p2, 0, 1.0, up=1.0, down=0.5, horizon_s=3600) == 0


def test_outcome_stats():
    p = path((10, 1.5), (20, 3.0), (40, 0.8), (50, 1.2))
    o = outcome(p, 0, 1.0, 1.0, up=1.0, down=0.5, horizon_s=3600)
    assert o.y == 1
    assert abs(o.max_return - 2.0) < 1e-9
    assert abs(o.max_drawdown - 0.2) < 1e-9
    assert o.time_to_peak_s == 20
    assert abs(o.final_return - 0.2) < 1e-9


def test_rug_from_peak_within_window():
    assert is_rug(path((10, 3.0), (60, 0.29)), 0, 1.0, drop=0.9, window_s=600)
    assert not is_rug(path((10, 3.0), (60, 0.31)), 0, 1.0, drop=0.9, window_s=600)
    assert not is_rug(path((10, 3.0), (700, 0.1)), 0, 1.0, drop=0.9, window_s=600)


def test_simulated_trade_tp_sl_and_fee():
    assert abs(simulated_trade(path((10, 2.5)), 0, 1.0, 1.0, 0.5, 3600) - 1.0) < 1e-9
    assert abs(simulated_trade(path((10, 0.4)), 0, 1.0, 1.0, 0.5, 3600) - (-0.6)) < 1e-9
    assert abs(simulated_trade(path((10, 1.2)), 0, 1.0, 1.0, 0.5, 3600, sell_fee_bps=100) - (1.2 * 0.99 - 1)) < 1e-9
    assert simulated_trade([], 0, 1.0, 1.0, 0.5, 3600) == 0.0
