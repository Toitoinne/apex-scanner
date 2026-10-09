"""Sortie apprise : le modèle apprend quand la hausse continue, et la stratégie vend quand elle s'arrête."""
import math
import random

from apex.config import Config
from apex.events import Decision, TokenCreated, Trade
from apex.labeler.engine import LabelerEngine
from apex.trading.exit_model import ExitLearner, ExitTrackState
from apex.trading.exits import Policy, PolicyState


def _path(rng, trend, n=400):
    """Chemin de prix : tendance persistante (+) ou retournement (−) après une hausse."""
    p, out = 1.0, []
    for i in range(n):
        p *= math.exp(trend * 0.01 + rng.gauss(0, 0.004))
        out.append(p)
    return out


def test_learner_separates_continuing_from_fading_moves():
    rng = random.Random(1)
    ml = ExitLearner({"every_s": 5, "horizon_s": 200, "min_samples": 300})
    for k in range(120):
        trend = 1 if k % 2 else -1
        st, times, prices = ExitTrackState(), [], []
        for i, price in enumerate(_path(rng, trend)):
            t = float(i)
            times.append(t)
            prices.append(price)
            ml.on_price(st, lambda: ExitLearner.features(0.0, times, prices, max(prices), t, price, False, None), t, price)
        ml.close(st, prices[-1])
    assert ml.ready
    up = ml.predict({"log_mult_launch": 1.0, "dd_peak": 0.0, "ret15": 0.15, "ret60": 0.6, "ret300": 2.0,
                     "age": 5.0, "migrated": 0.0})
    down = ml.predict({"log_mult_launch": -1.0, "dd_peak": -0.6, "ret15": -0.15, "ret60": -0.6, "ret300": -2.0,
                       "age": 5.0, "migrated": 0.0})
    assert up > 0.7 and down < 0.3
    assert ml.summary()["fiabilite"] > 0.2          # nettement mieux que le taux de base


def test_learned_policy_sells_when_rise_is_over():
    p = Policy.from_cfg("SORTIE_APPRISE", {"stop_loss": 0.5, "take_profits": [], "learned_exit": True,
                                           "hold_threshold": 0.35, "min_hold_s": 30})
    st = PolicyState(p.name, entry=1.0, t0=0.0, fee=0.0)
    assert st.on_price(p, 10.0, 1.5, hold_p=0.1) == []        # trop tôt : délai minimum
    assert st.on_price(p, 40.0, 1.6, hold_p=0.8) == []        # le modèle croit à la suite : on garde
    acts = st.on_price(p, 50.0, 1.7, hold_p=0.2)              # le modèle juge la hausse finie : on vend
    assert acts and acts[0].kind == "APPRIS" and st.closed and abs(st.pnl() - 0.7) < 1e-9
    # une stratégie classique ignore le modèle
    q = Policy.from_cfg("TP2_SL50", {"stop_loss": 0.5, "take_profits": [[2.0, 1.0]]})
    s2 = PolicyState(q.name, entry=1.0, t0=0.0, fee=0.0)
    assert s2.on_price(q, 50.0, 1.7, hold_p=0.0) == []


def test_labeler_runs_learned_exits_end_to_end():
    cfg = Config.load().data
    cfg["exits"]["learned"] = {"every_s": 2, "horizon_s": 60, "min_samples": 20}
    eng = LabelerEngine(cfg)
    rng = random.Random(3)
    for k in range(6):
        m = f"M{k}"
        eng.on_event(TokenCreated(mint=m, name="m", symbol="M", uri="", creator="dev", bonding_curve="", slot=0,
                                  ts=0.0, signature="c"))
        eng.on_event(Decision(decision_id=f"{m}:10", mint=m, point="10", ts=10.0, features={},
                              entry_price=1e-7, spot_price=1e-7, mc_sol=100, v_sol=30, v_tokens=3e8))
        price = 1e-7
        for i in range(300):
            price *= math.exp(rng.gauss(0.002 if k % 2 else -0.002, 0.01))
            eng.on_event(Trade(mint=m, signature=f"{m}{i}", slot=0, ts=11.0 + i, trader=f"w{i % 7}", is_buy=i % 3 != 0,
                               sol=0.3, tokens=1e6, v_sol=price * 3e8, v_tokens=3e8))
    s = eng.exit_ml.summary()
    assert s["situations_apprises"] > 100 and s["pret"]


def test_crash_precursors_are_visible_to_the_exit_model():
    from apex.trading.exits import DangerDetector, flow_features
    d = DangerDetector("dev")
    for i in range(60):                                   # phase d'achat, volume soutenu
        d.on_trade(float(i), f"a{i}", True, 1.0, 1e6, 1.0 + i / 100)
    d.on_trade(60.0, "dev", True, 1.0, 2e7, 1.6)
    calm = flow_features(d, 60.0)
    for i in range(8):                                    # 8 vendeurs dont le dev et un gros porteur
        d.on_trade(61.0 + i, "dev" if i == 0 else f"s{i}", False, 2.0, 5e7 if i == 3 else 1e6, 1.6 - i / 20)
    rush = flow_features(d, 69.0)
    assert rush["sell_share10"] > 0.8 > calm["sell_share10"]
    assert rush["big_sell30"] >= 0.05 and rush["sellers30"] > calm["sellers30"]
    assert rush["dev_sold_frac"] > 0
    times = [float(i) for i in range(70)]
    prices = [1.0 + i / 100 for i in range(61)] + [1.6 - i / 20 for i in range(9)]
    x = ExitLearner.features(0.0, times, prices, max(prices), 69.0, prices[-1], False, d)
    assert x["from_peak300"] < -0.2 and "sell_share10" in x
