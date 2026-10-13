"""Mémoire du learner : les décisions dont un résultat a été perdu (redémarrage) ne restent pas en mémoire."""
from types import SimpleNamespace

from apex.config import Config
from apex.learning.core import DecisionCache, Learner


def test_stale_pending_decisions_are_forgotten():
    lr = Learner(Config.load())
    now = 1_000_000.0
    d = lambda t: SimpleNamespace(ts=t)    # noqa: E731
    lr.cache = {"vieux": DecisionCache(d=d(now - 3 * 3600), preds={}, champions={}, threshold=0.5, remaining=1),
                "recent": DecisionCache(d=d(now - 600), preds={}, champions={}, threshold=0.5, remaining=3)}
    lr.long_store = {"vieux": {"ts": now - 27 * 3600}, "recent": {"ts": now - 20 * 3600}}
    lr._evict(now)
    assert set(lr.cache) == {"recent"} and set(lr.long_store) == {"recent"}
    lr.cache["vieux2"] = DecisionCache(d=d(now - 3 * 3600), preds={}, champions={}, threshold=0.5, remaining=1)
    lr._evict(now + 30)                     # au plus une fois par minute
    assert "vieux2" in lr.cache
    lr._evict(now + 61)
    assert "vieux2" not in lr.cache


def _dec(i: int, buyers: int):
    from apex.events import Decision
    return Decision(decision_id=f"m{i}:30", mint=f"m{i}", point="30", ts=1000.0 + i,
                    features={"unique_buyers": buyers, "mc_sol": 40.0, "age_s": 30.0},
                    entry_price=1.0, spot_price=1.0, mc_sol=40.0, v_sol=30.0, v_tokens=1e9)


def test_non_alertable_decisions_take_little_memory():
    """13/10 : learner tué toutes les 15 min. Décision non alertable : seule la prédiction du champion est
    gardée, et seul un échantillon fixe attend les labels 6 h/24 h. Les alertables gardent tout."""
    lr = Learner(Config.load())
    for i in range(400):
        lr.on_decision(_dec(i, 3))                   # non alertables (3 acheteurs)
    lr.on_decision(_dec(999, 40))                    # alertable
    small = lr.cache["m0:30"].preds
    assert all(len(v) == 1 for v in small.values())                       # champion seulement
    assert len(lr.cache["m999:30"].preds[lr.primary]) > 1                 # alertable : tous les concurrents
    kept = sum(f"m{i}:30" in lr.long_store for i in range(400))
    assert 60 < kept < 140 and "m999:30" in lr.long_store                 # ~25 % des autres, 100 % des alertables
    from apex.learning.core import keep_long
    assert keep_long("abc:10", 0.25) == keep_long("abc:10", 0.25)         # même choix après un redémarrage


def test_compact_old_state():
    lr = Learner(Config.load())
    lr.cfg.base["models"]["long_store_other_rate"] = 1.0
    for i in range(50):
        lr.on_decision(_dec(i, 3))
    for dc in lr.cache.values():                       # état d'avant : toutes les prédictions gardées
        dc.preds = {h: {"rules": (0.1, 0.1), "prior": (0.2, 0.2), "arf": (0.3, 0.3)} for h in dc.preds}
        dc.champions = {h: "rules" for h in dc.preds}
    lr.cfg.base["models"]["long_store_other_rate"] = 0.25
    lr.compact()
    assert all(list(v) == ["rules"] for dc in lr.cache.values() for v in dc.preds.values())
    assert 3 < len(lr.long_store) < 25
