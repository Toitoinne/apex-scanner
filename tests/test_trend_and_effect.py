import numpy as np

from apex.supervision.meta import EfficacyTable
from apex.supervision.stats import (DEGRADATION, IMPROVEMENT, NO_EFFECT, PLATEAU, PROGRESSION, REGRESSION, STABLE,
                                    INSUFFICIENT, classify_curve, effect_before_after, effect_paired, ks_drift, trend,
                                    two_proportion_test)

rng = np.random.default_rng(0)


def test_trend_detects_significant_decrease():
    t = np.arange(24)
    v = 0.5 - 0.01 * t + rng.normal(0, 0.005, 24)
    tr = trend(t, v)
    assert tr.direction == "down" and tr.p_value < 0.05 and tr.slope < 0
    assert classify_curve(tr, lower_is_better=True, hours_since_progress=0, plateau_hours=6) == PROGRESSION


def test_trend_detects_regression():
    t = np.arange(24)
    tr = trend(t, 0.2 + 0.01 * t + rng.normal(0, 0.005, 24))
    assert classify_curve(tr, lower_is_better=True, hours_since_progress=0, plateau_hours=6) == REGRESSION
    # pour une courbe « plus haut = mieux », la même pente est une progression
    assert classify_curve(tr, lower_is_better=False, hours_since_progress=0, plateau_hours=6) == PROGRESSION


def test_noise_is_not_a_trend_then_plateau():
    t = np.arange(24)
    tr = trend(t, 0.3 + rng.normal(0, 0.02, 24))
    assert tr.direction == "flat"
    assert classify_curve(tr, True, hours_since_progress=2, plateau_hours=6) == STABLE
    assert classify_curve(tr, True, hours_since_progress=8, plateau_hours=6) == PLATEAU


def test_insufficient_points():
    tr = trend([0, 1], [0.1, 0.2])
    assert classify_curve(tr, True, 100, 6) == INSUFFICIENT


def test_two_proportion():
    d, p = two_proportion_test(300, 1000, 200, 1000)
    assert d < 0 and p < 0.001
    assert two_proportion_test(0, 0, 1, 10) == (0.0, 1.0)


def test_effect_before_after():
    before = rng.normal(0.30, 0.05, 500)
    assert effect_before_after(before, rng.normal(0.25, 0.05, 500), lower_is_better=True)[0] == IMPROVEMENT
    assert effect_before_after(before, rng.normal(0.35, 0.05, 500), lower_is_better=True)[0] == DEGRADATION
    assert effect_before_after(before, rng.normal(0.30, 0.05, 500), lower_is_better=True)[0] == NO_EFFECT
    assert effect_before_after(before[:5], before[:5], True)[0] == NO_EFFECT


def test_effect_paired_shadow():
    champ = rng.gamma(2, 0.3, 2000)
    better = champ * 0.9 + rng.normal(0, 0.01, 2000)
    worse = champ * 1.1 + rng.normal(0, 0.01, 2000)
    v, gain, p = effect_paired(champ, better)
    assert v == IMPROVEMENT and gain > 0.05
    assert effect_paired(champ, worse)[0] == DEGRADATION
    assert effect_paired(champ, champ)[0] == NO_EFFECT
    assert effect_paired(champ[:10], better[:10])[0] == NO_EFFECT


def test_ks_drift_flags_shifted_feature_only():
    ref = {"a": list(rng.normal(0, 1, 2000)), "b": list(rng.normal(0, 1, 2000))}
    rec = {"a": list(rng.normal(1.0, 1, 500)), "b": list(rng.normal(0, 1, 500))}
    d = ks_drift(rec, ref, alpha=0.01)
    assert "a" in d and "b" not in d


def test_meta_bandit_prefers_what_worked():
    t = EfficacyTable(explore=0.0, seed=1)
    for _ in range(20):
        t.update("PLATEAU", "hot", "good", IMPROVEMENT, 0.05)
        t.update("PLATEAU", "hot", "bad", DEGRADATION, -0.05)
    picks = [t.choose("PLATEAU", "hot", ["good", "bad"]) for _ in range(50)]
    assert picks.count("good") > 45
    # contexte jamais vu : les stats globales de l'état guident quand même
    picks = [t.choose("PLATEAU", "cold", ["good", "bad"]) for _ in range(50)]
    assert picks.count("good") > 35
