"""Garde-fou « créateur en série » (09/11) : un créateur relançait le même nom en boucle (« SLNADO » ×6) et le bot
alertait à chaque relance. Une seule alerte par créateur sur 24 h ; jamais sur un nom déjà alerté de ce créateur."""
from apex.config import Config
from apex.events import Decision
from apex.learning.core import Learner

HOT = {"velocity_mc_per_min": 500, "buy_sell_ratio_vol": 5, "unique_buyers": 60, "ret_last_30s": 1.0}


def _learner() -> Learner:
    cfg = Config.load()
    cfg.base["bandit"]["warmup_labels"] = 0
    cfg.base["bandit"]["min_observed_s"] = 0
    L = Learner(cfg)
    L.bandit.recenter(0.0, 0.31)
    L.bandit.active = "x2:0.3@30#RECUP_TRAIL40"
    return L


def _d(i: int, ts: float, creator: str, symbol: str) -> Decision:
    return Decision(decision_id=f"m{i}:30", mint=f"m{i}", point="30", ts=ts, features=dict(HOT), entry_price=1.0,
                    spot_price=1.0, mc_sol=30.0, v_sol=30.0, v_tokens=1e9,
                    meta={"creator": creator, "symbol": symbol, "name": symbol})


def test_one_alert_per_creator_per_day_and_no_relaunch():
    L = _learner()
    assert L.on_decision(_d(1, 1000.0, "dev1", "SLNADO"))[1] is not None
    assert L.on_decision(_d(2, 1200.0, "dev1", "SLNADO"))[1] is None        # relance 3 min après : refusée
    assert L.on_decision(_d(3, 1400.0, "dev1", "MAX"))[1] is None           # autre nom, même créateur, < 24 h
    assert L.on_decision(_d(4, 1600.0, "dev2", "OTHER"))[1] is not None     # autre créateur : normal
    later = 1000.0 + 25 * 3600
    assert L.on_decision(_d(5, later, "dev1", "slnado "))[1] is None         # relance du même nom, même après 24 h
    assert L.on_decision(_d(6, later + 60, "dev1", "NEW"))[1] is not None    # nouveau nom après 24 h : accepté


def test_guard_survives_restart(tmp_path):
    L = _learner()
    assert L.on_decision(_d(1, 1000.0, "dev1", "SLNADO"))[1] is not None
    L.snapshot(tmp_path / "s.pkl")
    L2 = _learner()
    L2.restore(tmp_path / "s.pkl")
    assert L2.creator_block(_d(2, 1100.0, "dev1", "SLNADO")) is not None
