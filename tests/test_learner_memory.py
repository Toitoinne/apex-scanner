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
