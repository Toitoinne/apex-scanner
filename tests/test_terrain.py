"""Terrain en observation : décisions d'étude après migration et sur les 2es vagues, sans toucher à l'existant."""
from apex.config import Config
from apex.events import Decision, Migration, PriceTick, TokenCreated, Trade
from apex.features.engine import FeatureEngine
from apex.features.market import MarketContext
from apex.features.wallets import WalletIntel
from apex.learning.core import Learner
from apex.safety.filters import SafetyFilters


def _engine():
    cfg = Config.load()
    c = cfg.data
    return FeatureEngine(c, SafetyFilters(cfg.safety), WalletIntel(c["features"]), MarketContext(c["features"]["narrative_window_s"]))


def test_post_migration_study_points():
    eng = _engine()
    eng.on_event(TokenCreated(mint="M", name="m", symbol="M", uri="", creator="dev", bonding_curve="", slot=0, ts=0.0, signature="c"))
    eng.on_event(Trade(mint="M", signature="s", slot=0, ts=100.0, trader="w", is_buy=True, sol=80.0, tokens=7e8,
                       v_sol=115.0, v_tokens=2.8e8))
    eng.on_event(Migration(mint="M", slot=0, ts=200.0, signature="m"))
    pts = []
    for t in range(200, 1200, 5):
        eng.on_event(PriceTick(mint="M", ts=float(t), price=5e-7, source="pumpswap", trader=f"b{t}", is_buy=True, sol=1.0,
                               tokens=2e6, pool_sol=90.0))
        pts += [d.point for d in eng.tick(float(t))]
    assert {"mig60", "mig300", "mig900"} <= set(pts)
    d = [d for d in eng.tick(1200.0)]
    assert not d


def test_second_wave_trigger():
    eng = _engine()
    eng.on_event(TokenCreated(mint="W", name="w", symbol="W", uri="", creator="dev", bonding_curve="", slot=0, ts=0.0, signature="c"))
    p = 4e-8
    for i in range(30):                      # vie normale, puis calme
        eng.on_event(Trade(mint="W", signature=f"a{i}", slot=0, ts=10.0 + i, trader=f"old{i}", is_buy=True, sol=0.5,
                           tokens=1e7, v_sol=p * 1e9 * 1.1, v_tokens=1.1e9))
    pts = []
    for i in range(40):                      # 1 h plus tard : nouveaux acheteurs, +40 % en quelques minutes
        q = p * (1 + 0.4 * i / 39)
        eng.on_event(Trade(mint="W", signature=f"b{i}", slot=0, ts=3700.0 + i * 5, trader=f"new{i}", is_buy=True, sol=1.0,
                           tokens=1e7, v_sol=q * 1.1e9, v_tokens=1.1e9))
        pts += [d.point for d in eng.tick(3700.0 + i * 5)]
    assert pts.count("vague2") == 1


def test_study_points_do_not_touch_current_learning():
    lr = Learner(Config.load())
    d = Decision(decision_id="M:mig60", mint="M", point="mig60", ts=1.0, features={"unique_buyers": 50},
                 entry_price=1e-6, spot_price=1e-6, mc_sol=1000, v_sol=90, v_tokens=9e7)
    rows, alert = lr.on_decision(d)
    assert rows == [] and alert is None and "M:mig60" not in lr.long_store


def test_no_late_decisions_after_a_pause():
    """Un point de décision traité très en retard (redémarrage) est abandonné : il porterait l'heure
    prévue avec les prix du moment."""
    eng = _engine()
    eng.on_event(TokenCreated(mint="L", name="l", symbol="L", uri="", creator="dev", bonding_curve="", slot=0, ts=0.0, signature="c"))
    for i in range(20):
        eng.on_event(Trade(mint="L", signature=f"s{i}", slot=0, ts=1.0 + i, trader=f"w{i}", is_buy=True, sol=0.5,
                           tokens=1e7, v_sol=31.0 + i, v_tokens=1.0e9))
    late = eng.tick(1000.0)                 # on ne revient qu'à t = 1000 s : 10 s … 600 s sont en retard
    assert [d.point for d in late] == ["600"] or late == []
    assert all(1000.0 - (d.ts) <= 300 for d in late)
