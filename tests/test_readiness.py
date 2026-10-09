"""Aptitude au trading réel : prêt, suspension en cas de régression, reprise prouvée."""
import random

from apex.trading import readiness as R

DAY = 86400.0


def series(n, mean, sd=0.6, days=10, seed=1, t0=0.0):
    rng = random.Random(seed)
    return [R.Closed(t0 + i * days * DAY / n, max(-1.0, rng.gauss(mean, sd))) for i in range(n)]


def test_not_ready_without_enough_evidence():
    s = R.compute_stats(series(40, 0.3, days=2))
    v = R.evaluate(s, R.Criteria())
    assert not v.ready and any("positions" in f for f in v.failed())


def test_ready_when_profitable_over_enough_time():
    s = R.compute_stats(series(150, 0.25, sd=0.5, days=9))
    v = R.evaluate(s, R.Criteria())
    assert v.ready, v.failed()
    assert R.next_state(R.LEARNING, v, None, live_enabled=False) == R.READY
    assert R.next_state(R.READY, v, None, live_enabled=True) == R.LIVE


def test_losing_bot_is_never_ready():
    s = R.compute_stats(series(200, -0.05, days=10))
    assert not R.evaluate(s, R.Criteria()).ready


def test_regression_suspends_then_resume_must_be_proven_on_new_positions():
    good = series(150, 0.25, sd=0.5, days=9)
    bad = series(40, -0.4, sd=0.2, days=1, seed=2, t0=9.5 * DAY)
    allv = R.evaluate(R.compute_stats(good + bad), R.Criteria())
    assert allv.regress
    assert R.next_state(R.LIVE, allv, None, live_enabled=True) == R.SUSPENDED
    crit = R.Criteria(min_positions=50, min_days=1.0)
    few = R.evaluate(R.compute_stats(series(20, 0.3, days=1, seed=3, t0=11 * DAY)), crit)
    assert R.next_state(R.SUSPENDED, allv, few, live_enabled=True) == R.SUSPENDED      # pas encore prouvé
    proven = R.evaluate(R.compute_stats(series(80, 0.3, sd=0.4, days=3, seed=4, t0=11 * DAY)), crit)
    assert R.next_state(R.SUSPENDED, allv, proven, live_enabled=True) == R.LIVE


def test_unhealthy_learning_blocks_and_suspends():
    s = R.compute_stats(series(150, 0.25, sd=0.5, days=9))
    v = R.evaluate(s, R.Criteria(), health_ok=False)
    assert not v.ready and v.regress


def test_real_trades_are_judged_separately():
    from apex.trading.readiness import Closed, Criteria, real_guard
    c = Criteria()
    few = [Closed(float(i), -0.5) for i in range(5)]
    assert real_guard(few, c)[0] is False                          # trop peu de trades réels pour juger
    bad = [Closed(float(i), 0.3) for i in range(20)] + [Closed(100.0 + i, -0.3) for i in range(20)]
    stop, why = real_guard(bad, c)
    assert stop and "derniers trades réels" in why                  # les 20 derniers perdent : suspension
    good = [Closed(float(i), 0.05) for i in range(30)]
    assert real_guard(good, c)[0] is False
