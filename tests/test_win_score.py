"""Note « gagnant probable » (14/10) : entraînement, validation sur tokens jamais vus, activation prudente,
choix d'alerte resserré une fois active."""
import random

from apex.config import Config
from apex.learning.core import Learner
from apex.learning.ev import Data, should_activate_win, train
from apex.reporting.telegram import fmt_alert


def _rows(n=8000, seed=1):
    rng = random.Random(seed)
    rows = []
    for i in range(n):
        q = rng.random()                                    # « qualité » cachée du token
        win = rng.random() < (0.6 if q > 0.97 else 0.08)
        pnl = rng.uniform(0.1, 0.8) if win else rng.uniform(-0.3, -0.05)
        rows.append(({"qualite": q, "bruit": rng.random()}, {"SUIVEUR15_DES_X1": pnl, "TP2_SL50": -0.1}, float(i),
                     f"m{i}"))
    return rows


def test_win_model_ranks_and_validates():
    m = train(Data.from_rows(_rows()), None, min_rows=1000, rounds=30, win_policy="SUIVEUR15_DES_X1", win_rounds=80)
    assert m.win_booster is not None
    assert m.win_rank({"qualite": 0.99, "bruit": 0.5}) > 0.9 > m.win_rank({"qualite": 0.2, "bruit": 0.5})
    s = m.stats
    assert s["win_auc"] > 0.6 and s["win_top"]["0.01"]["moyen"] > 0 > s["win_moyen_tous"]


def test_win_activation_needs_two_winning_validations():
    good = {"win_top": {"0.01": {"n": 80, "moyen": 0.04}}}
    bad = {"win_top": {"0.01": {"n": 80, "moyen": -0.02}}}
    few = {"win_top": {"0.01": {"n": 10, "moyen": 0.3}}}
    assert not should_activate_win([good])
    assert should_activate_win([bad, good, good])
    assert not should_activate_win([good, bad]) and not should_activate_win([few, few])


def test_learner_uses_win_score_exclusively_once_active():
    lr = Learner(Config.load())
    n_before = len(lr.bandit.arms)
    m = train(Data.from_rows(_rows()), None, min_rows=1000, rounds=20, win_policy="SUIVEUR15_DES_X1", win_rounds=40)
    lr.set_win(m)
    arms = list(lr.bandit.arms.values())
    assert {a.score for a in arms} == {"win"} and {a.policy for a in arms} == {"SUIVEUR15_DES_X1"}
    assert len(arms) == 4 * 8 < n_before
    lr.set_win(None)
    assert not any(a.score == "win" for a in lr.bandit.arms.values())


def test_alert_message_mentions_win():
    a = {"mc_sol": 50, "mint": "M", "point": "60", "p": 0.2, "scores": {"x2": 0.2, "win": 0.995}, "name": "T",
         "symbol": "T", "policy": "SUIVEUR15_DES_X1"}
    assert "gagnant probable" in fmt_alert(a) and "top 0,5 %" in fmt_alert(a)
    assert "gagnant probable" not in fmt_alert({**a, "scores": {"x2": 0.2}})
