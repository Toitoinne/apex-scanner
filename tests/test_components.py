import base58
import pytest

from apex.alerting.bandit import AlertBandit
from apex.claude_improver.sandbox import UnsafeCode, compile_feature, run_isolated, validate
from apex.config import Config
from apex.features.curve import curve_progress, effective_buy_price, slippage
from apex.ingestor.pump_decoder import encode_trade_for_test, events_from_logs
from apex.safety.filters import SafetyFilters

MINT = base58.b58encode(bytes(range(32))).decode()
USER = base58.b58encode(bytes(range(1, 33))).decode()


def test_decode_trade_event():
    data = encode_trade_for_test(MINT, 500_000_000, 17_000_000_000_000, True, USER, 1700000000, 31_000_000_000, 1_040_000_000_000_000)
    evs = events_from_logs(["Program log: Instruction: Buy", f"Program data: {data}"], "sig", 123, 1.0)
    assert len(evs) == 1
    t = evs[0]
    assert t.mint == MINT and t.trader == USER and t.is_buy
    assert t.sol == pytest.approx(0.5) and t.tokens == pytest.approx(17_000_000)
    assert t.v_sol == pytest.approx(31) and t.v_tokens == pytest.approx(1_040_000_000)


def test_garbage_logs_are_ignored():
    assert events_from_logs(["Program data: !!!", "Program data: AAAA"], "s", 1, 0) == []


def test_curve_math():
    assert curve_progress(1_073_000_000) == pytest.approx(0)
    assert curve_progress(279_900_000) == pytest.approx(1)
    spot = 30 / 1_073_000_000
    assert effective_buy_price(30, 1_073_000_000, 1.0, 0) > spot
    assert slippage(30, 1_073_000_000, 2.0, 100) > slippage(30, 1_073_000_000, 0.5, 100) > 0


def test_safety_filters_block_and_are_frozen():
    cfg = Config.load()
    sf = SafetyFilters(cfg.safety)
    assert sf.check({"creation_slot_supply_pct": 0.5, "mint_authority": 0}).blocked
    assert sf.check({"mint_authority": 1}).blocked
    ok = sf.check({"mint_authority": 0, "freeze_authority": 0, "top10_concentration": 0.2})
    assert not ok.blocked
    with pytest.raises(TypeError):
        sf.cfg["bundle_creation_slot_supply_pct"] = 1.0


def test_runtime_overrides_cannot_touch_safety_or_ingestion():
    cfg = Config.load()
    cfg.set_override("calibration.method", "isotonic")
    assert cfg.get("calibration.method") == "isotonic"
    for key in ("ingestion.sources", "safety.dev_holding_max", "labels.L60.up", "supervision.precision_floor"):
        with pytest.raises(PermissionError):
            cfg.set_override(key, 0)


def test_bandit_learns_best_threshold_and_exit_policy():
    b = AlertBandit(scores_cfg={"x2": [0.3, 0.6]}, points=["30"], policies=["A", "B"],
                    alerts_min=1, alerts_max=1000, seed=3)
    t = 0.0
    for i in range(2000):
        t += 60
        p = 0.7 if i % 4 == 0 else 0.4
        b.observe_decision("30", {"x2": p}, t)
        # tokens à p=0,7 : la stratégie B rapporte, A perd ; tokens à 0,4 : tout perd
        pnl = {"A": -0.2, "B": 0.8} if p > 0.6 else {"A": -0.5, "B": -0.5}
        b.observe_reward("30", {"x2": p}, pnl)
    assert b.arms["x2:0.6@30#B"].mean() > 0.5 > b.arms["x2:0.3@30#B"].mean()
    picks = [b.resample().key for _ in range(30)]
    assert picks.count("x2:0.6@30#B") > 25
    assert b.should_alert("30", {"x2": 0.65}) is (b.active == "x2:0.6@30#B")

def test_sandbox_rejects_dangerous_code():
    for bad in ["import os\ndef compute(ctx):\n    return 1",
                "def compute(ctx):\n    return ctx.__class__",
                "def compute(ctx):\n    while True:\n        pass",
                "def compute(ctx):\n    return open('x').read()",
                "def other(ctx):\n    return 1"]:
        with pytest.raises(UnsafeCode):
            validate(bad)


def test_sandbox_runs_valid_feature_and_test():
    code = ("import statistics\n"
            "def compute(ctx):\n"
            "    buys = [t['sol'] for t in ctx['trades'] if t['is_buy']]\n"
            "    return statistics.pstdev(buys) if len(buys) > 1 else None\n")
    test = ("def test(compute):\n"
            "    ctx = {'trades': [{'sol': 1.0, 'is_buy': True}, {'sol': 3.0, 'is_buy': True}]}\n"
            "    assert abs(compute(ctx) - 1.0) < 1e-9\n"
            "    assert compute({'trades': []}) is None\n")
    fn = compile_feature(code)
    assert fn({"trades": [{"sol": 1.0, "is_buy": True}, {"sol": 3.0, "is_buy": True}]}) == pytest.approx(1.0)
    res = run_isolated(code, test, [{"trades": [{"sol": 2.0, "is_buy": True}, {"sol": 2.0, "is_buy": True}]}], timeout_s=30)
    assert res["ok"], res
    assert res["outputs"] == [0.0]
    bad = run_isolated(code, "def test(compute):\n    assert compute({'trades': []}) == 5\n", [], timeout_s=30)
    assert not bad["ok"]


def test_only_standard_sol_curve_is_accepted():
    from apex.features.curve import is_standard_curve
    assert is_standard_curve(30.0, 1_073_000_000)
    assert is_standard_curve(60.0, 30 * 1_073_000_000 / 60)
    assert not is_standard_curve(0.0, 772_363_902)      # trade sans SOL (autre devise de cotation)
    assert not is_standard_curve(0.3, 600_000_000)      # autre courbe


def test_bandit_sync_arms_keeps_learning_and_adds_low_thresholds():
    b = AlertBandit(scores_cfg={"x2": [0.3]}, points=["30", "migration"], policies=["A"], alerts_min=1, alerts_max=10)
    b.observe_reward("30", {"x2": 0.9}, {"A": 1.0})
    b.sync_arms({"x2": [0.05, 0.3]}, ["30"], ["A"])
    assert set(b.arms) == {"x2:0.05@30#A", "x2:0.3@30#A"}   # bras « migration » retiré, bas seuil ajouté
    assert b.arms["x2:0.3@30#A"].n > 0                      # l'historique des bras existants est conservé


def test_drift_ignores_time_of_day_features():
    import numpy as np
    from apex.supervision.stats import ks_drift
    rng = np.random.default_rng(1)
    ref = {"hour_sin": list(rng.uniform(-1, 1, 2000))}
    rec = {"hour_sin": [0.9] * 200}
    assert ks_drift(rec, ref) == {}


def test_new_bandit_is_prudent_before_it_knows_alert_rates():
    b = AlertBandit(scores_cfg={"x2": [0.03, 0.3, 0.7]}, points=["30"], policies=["A"], alerts_min=10, alerts_max=30)
    assert not b.ready(3600)                       # aucune décision observée : pas d'alerte
    assert b.resample().thr == 0.7                 # sans estimation des fréquences : le seuil le plus prudent
    t = 0.0
    for i in range(200):
        t += 30
        b.observe_decision("30", {"x2": 0.05}, t)
    assert b.ready(3600)
