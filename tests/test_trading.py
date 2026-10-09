"""Stratégies de sortie, détection de danger, simulation par décision et positions alertées."""
import pytest

from apex.config import Config
from apex.events import Decision, PriceTick, TokenCreated, Trade
from apex.labeler.engine import LabelerEngine
from apex.trading.exits import DangerDetector, Policy, simulate

RECUP = Policy("RECUP", stop_loss=0.5, take_profits=((2.0, 0.5),), trail=0.4)
TP2 = Policy("TP2", stop_loss=0.5, take_profits=((2.0, 1.0),), time_limit_s=3600, danger_exit=False)


def path(*pts):
    return [(float(t), float(p)) for t, p in pts]


def test_recup_then_let_it_run_captures_a_moonshot():
    # x2 → on vend 50 % ; montée à x10 ; repli de 40 % depuis x10 → on vend le reste à x6
    st = simulate(RECUP, path((10, 1.5), (20, 2.0), (30, 5.0), (40, 10.0), (50, 6.0)), entry=1.0, t0=0, fee=0.0)
    assert st.closed
    assert st.pnl() == pytest.approx(0.5 * 2.0 + 0.5 * 6.0 - 1)          # +300 %
    tp = simulate(TP2, path((10, 1.5), (20, 2.0), (30, 5.0), (40, 10.0)), entry=1.0, t0=0, fee=0.0)
    assert tp.pnl() == pytest.approx(1.0)                                  # +100 % seulement


def test_stop_loss_before_any_take_profit():
    st = simulate(RECUP, path((10, 0.8), (20, 0.49), (30, 3.0)), entry=1.0, t0=0, fee=0.0)
    assert st.closed and st.pnl() == pytest.approx(-0.51)


def test_after_recovering_stake_a_dump_cannot_lose_money():
    st = simulate(RECUP, path((10, 2.0), (20, 1.2)), entry=1.0, t0=0, fee=0.0)
    assert st.closed                                     # stop suiveur −40 % depuis x2 = x1,2
    assert st.pnl() == pytest.approx(0.5 * 2 + 0.5 * 1.2 - 1)              # +60 %


def test_danger_exit_and_fees():
    st = simulate(RECUP, path((10, 1.5), (20, 1.4)), entry=1.0, t0=0, fee=0.02, dangers=[(20, "DEV_VEND")])
    assert st.closed and st.pnl() == pytest.approx(1.4 * 0.98 - 1)


def test_time_limit_and_mark_to_market():
    st = simulate(TP2, path((100, 1.3), (3700, 1.1)), entry=1.0, t0=0, fee=0.0)
    assert st.closed and st.pnl() == pytest.approx(0.1)


def test_danger_detector():
    d = DangerDetector("dev")
    assert d.on_trade(0, "dev", True, 1.0, 50e6, 1.0) is None
    assert d.on_trade(1, "dev", False, 0.5, 30e6, 1.0) == "DEV_VEND"
    assert d.on_trade(2, "whale", False, 2.0, 40e6, 1.0) == "GROS_DUMP"
    p = DangerDetector("x")
    for i in range(5):
        p.on_trade(10 + i, f"b{i}", True, 0.1, 1e6, 1.0)
    sig = None
    for i in range(5):
        sig = p.on_trade(20 + i, f"s{i}", False, 1.0, 1e6, 0.7) or sig
    assert sig == "PANIQUE"


def _engine():
    cfg = Config.load()
    return LabelerEngine(cfg.data)


def _trade(t, price, buy=True, trader="w"):
    vs = 30.0 * (price / (30 / 1_073_000_000)) ** 0.5
    return Trade(mint="M", signature=f"s{t}", slot=0, ts=float(t), trader=trader, is_buy=buy, sol=0.5,
                 tokens=1e6, v_sol=vs, v_tokens=30.0 * 1_073_000_000 / vs)


def test_engine_outcome_per_policy_and_live_position_signals():
    eng = _engine()
    eng.on_event(TokenCreated(mint="M", name="m", symbol="M", uri="", creator="dev", bonding_curve="", slot=0,
                              ts=0.0, signature="c"))
    p0 = 30 / 1_073_000_000
    eng.on_event(_trade(5, p0 * 1.2))
    d = Decision(decision_id="M:30", mint="M", point="30", ts=30.0, features={}, entry_price=p0 * 1.25,
                 spot_price=p0 * 1.2, mc_sol=30, v_sol=30, v_tokens=1e9)
    eng.on_event(d)
    eng.open_position({"decision_id": "M:30", "mint": "M", "policy": "RECUP_TRAIL40", "ts": 30.0, "symbol": "M"})
    eng.on_event(_trade(60, p0 * 2.6))          # > x2 depuis l'entrée → palier
    eng.on_event(PriceTick(mint="M", ts=500.0, price=p0 * 12.5))   # après migration : x10
    eng.on_event(PriceTick(mint="M", ts=900.0, price=p0 * 6.0))    # repli > 40 % → stop suiveur
    outs, sigs = eng.drain()
    kinds = [s["kind"] for s in sigs]
    assert kinds == ["PALIER", "STOP_SUIVEUR"]
    assert sigs[-1]["closed"] and sigs[-1]["pnl_after"] > 1.0
    # toutes les stratégies sont déjà clôturées (stops suiveurs) : l'Outcome est émis sans attendre 24 h
    assert len(outs) == 1
    o = outs[0]
    assert set(o.pnl) == set(eng.policies)
    assert o.pnl["RECUP_TRAIL40"] > o.pnl["TP2_SL50"] > 0
    assert o.reached["x2"] == pytest.approx(30.0) and "x5" in o.reached


def test_price_guard_rejects_isolated_spikes_but_accepts_confirmed_moves():
    from apex.trading.pricefilter import PriceGuard
    g = PriceGuard(max_jump=5.0)
    g.seed("M", 1.0)
    assert g.accept("M", 3.0)                 # x3 : plausible
    assert not g.accept("M", 3000.0)          # x1000 isolé : rejeté
    assert g.accept("M", 3.2)                 # retour à la normale
    assert not g.accept("M", 40.0)            # saut x12 ...
    assert g.accept("M", 42.0)                # ... confirmé par le relevé suivant : accepté


def test_danger_window_sums_slide():
    from apex.trading.exits import DangerDetector
    d = DangerDetector("dev")
    for i in range(10):                       # gros achats anciens : ils doivent sortir de la fenêtre de 30 s
        d.on_trade(float(i), f"a{i}", True, 10.0, 1e6, 1.0)
    assert d._buys == 100.0
    sig = None
    for i in range(6):                        # 40 s plus tard : ventes en panique, prix −30 %
        sig = d.on_trade(50.0 + i, f"s{i}", False, 2.0, 1e5, 1.0 - 0.06 * (i + 1)) or sig
    assert d._buys == 0.0 and abs(d._sells - 12.0) < 1e-9
    assert sig == "PANIQUE"
