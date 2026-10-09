"""Gain attendu par stratégie : sait-il choisir token par token quand c'est possible, et ne
prend-il la main qu'après avoir fait ses preuves ?"""
import random

from apex.config import Config
from apex.events import Outcome
from apex.learning.core import Learner
from apex.learning.ev import should_activate, summary, train


def _rows(n=6000, seed=0):
    rng = random.Random(seed)
    rows = []
    for i in range(n):
        speed = rng.random()
        # tokens rapides : vendre vite rapporte ; tokens lents : garder rapporte
        fast = 0.3 if speed > 0.5 else -0.2
        slow = -0.2 if speed > 0.5 else 0.3
        rows.append(({"speed": speed, "noise": rng.random()},
                     {"VITE": fast + rng.gauss(0, 0.2), "LENT": slow + rng.gauss(0, 0.2)}, float(i)))
    return rows


def test_learns_which_strategy_for_which_token():
    m = train(_rows(), ["VITE", "LENT"], min_rows=1000, rounds=60)
    s = m.stats
    assert s["auto_moyen"] > s["meilleure_fixe_moyen"] + 0.2          # nettement mieux qu'une stratégie unique
    assert m.best({"speed": 0.9, "noise": 0.5})[0] == "VITE"
    assert m.best({"speed": 0.1, "noise": 0.5})[0] == "LENT"
    assert "tokens jamais vus" in summary(s)


def test_activation_requires_proof_twice():
    good = {"auto_moyen": 0.05, "meilleure_fixe_moyen": 0.0, "n_test": 5000}
    bad = {"auto_moyen": -0.1, "meilleure_fixe_moyen": -0.08, "n_test": 5000}
    assert not should_activate([good])
    assert should_activate([bad, good, good])
    assert not should_activate([good, bad])
    assert not should_activate([{**good, "n_test": 100}, {**good, "n_test": 100}])


def test_auto_arm_reward_and_alert_policy():
    lr = Learner(Config.load())
    assert not any(a.policy == "AUTO" for a in lr.bandit.arms.values())
    m = train(_rows(), ["VITE", "LENT"], min_rows=1000, rounds=30)
    lr.set_ev(m)
    assert any(a.policy == "AUTO" for a in lr.bandit.arms.values())
    assert any(a.score == "ev" for a in lr.bandit.arms.values())
    lr.long_store["M:10"] = {"ts": 0.0, "point": "10", "mint": "M", "scores": {"x2": 0.9, "ev": 0.3}, "alerted": False,
                             "eligible": True, "preds": {}, "champions": {}, "auto_policy": "VITE"}
    lr.on_outcome(Outcome(decision_id="M:10", mint="M", point="10", ts=10.0, pnl={"VITE": 0.4, "LENT": -0.3},
                          max_return=0.5, reached={}))
    arm = next(a for a in lr.bandit.arms.values() if a.policy == "AUTO" and a.score == "x2" and a.point == "10")
    assert arm.n > 0 and arm.mean() > 0                                  # récompensé avec le gain de la stratégie choisie
    lr.set_ev(None)
    assert not any(a.policy == "AUTO" for a in lr.bandit.arms.values())
