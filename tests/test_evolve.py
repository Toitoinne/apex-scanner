"""Évolution des stratégies de sortie : les réglages apprennent, la base reste."""
import random

from apex.config import Config
from apex.labeler.engine import LabelerEngine
from apex.learning.core import Learner
from apex.trading.evolve import BOUNDS, evolve, mutate
from apex.trading.exits import Policy, build_panel

BASE = build_panel(Config.load()["exits"])


def row(pol, rang, mean, n=1000, verdict="pas prouvée"):
    return {"pol": pol, "rang": rang, "mean": mean, "n": n, "verdict": verdict}


def test_mutations_stay_valid_and_bounded():
    rng = random.Random(0)
    for name, cfg in BASE.items():
        for _ in range(20):
            c, changes = mutate(cfg, rng)
            assert changes
            Policy.from_cfg("X", c)
            for k, (lo, hi) in BOUNDS.items():
                if c.get(k):
                    assert lo <= c[k] <= hi
            assert all(m >= 1.1 for m, _ in c.get("take_profits", []))


def test_best_breed_losers_die_base_is_kept():
    names = list(BASE)
    board = [row(n, i + 1, 0.1 - i * 0.01) for i, n in enumerate(names)]
    ev, added, removed = evolve(board, BASE, {}, gen=1, rng=random.Random(1))
    assert len(added) == 6 and not removed
    assert {ev[a]["parent"] for a in added} == set(names[:3])          # enfants des 3 meilleures
    # génération suivante : une variante en tête, une autre perd en bas du classement
    a1, a2 = added[0], added[1]
    board2 = [row(a1, 1, 0.2)] + [row(n, i + 2, 0.05 - i * 0.01) for i, n in enumerate(names)] + \
             [row(a2, len(names) + 2, -0.3, verdict="perd")]
    ev2, added2, removed2 = evolve(board2, BASE, ev, gen=2, rng=random.Random(2))
    assert a2 in removed2 and a1 in ev2 and a2 not in ev2
    assert any(ev2[x]["parent"] == a1 for x in added2)                 # la meilleure variante a des enfants
    assert len(ev2) <= 12 and not set(removed2) & set(BASE)             # la base n'est jamais retirée


def test_new_variants_reach_labeler_and_bandit():
    ev, added, _ = evolve([row(n, i + 1, 0.1) for i, n in enumerate(BASE)], BASE, {}, 1, random.Random(3))
    eng = LabelerEngine(Config.load().data)
    eng.set_evolved(ev)
    assert set(added) <= eng.active_policies and set(added) <= set(eng.policies)
    eng.set_evolved({})                                                  # retirée : plus simulée sur les nouvelles décisions
    assert not set(added) & eng.active_policies and set(added) <= set(eng.policies)
    lr = Learner(Config.load())
    lr.set_evolved(ev)
    assert any(a.policy == added[0] for a in lr.bandit.arms.values())
    lr.set_evolved({})
    assert not any(a.policy == added[0] for a in lr.bandit.arms.values())
