"""Système d'ordres : exécution réaliste, limites de risque, cycle de position, verrou du mode réel."""
import asyncio

import pytest

from apex.trading.engine import REAL, SIM, TraderEngine
from apex.trading.fills import Fees, Market, buy, sell
from apex.trading.risk import Limits, can_open

VS, VT = 40.0, 30.0 * 1_073_000_000 / 40.0          # courbe standard (k = 30 × 1,073 Md)
FEES = Fees()


def test_buy_on_curve_includes_impact_and_all_fees():
    m = Market(ts=0, price=VS / VT, v_sol=VS, v_tokens=VT)
    f = buy(m, 0.1, m.price, 0.15, FEES)
    assert f.ok and f.tokens > 0
    assert f.price > m.price                         # impact + frais
    assert f.fees_sol == pytest.approx(0.1 * 0.005 + 0.0005 + (0.1 - 0.0005 - 0.0005) * 0.0125, rel=1e-6)


def test_buy_fails_like_onchain_when_price_ran_away():
    m = Market(ts=0, price=VS / VT, v_sol=VS, v_tokens=VT)
    f = buy(m, 0.1, expected_price=m.price / 1.5, max_slippage=0.15, fees=FEES)
    assert not f.ok and "slippage" in f.reason
    assert f.sol == pytest.approx(0.0005)            # seuls les frais réseau sont perdus


def test_sell_on_pumpswap_pool_uses_depth():
    m = Market(ts=0, price=4e-7, pool_sol=80.0, migrated=True)
    small = sell(m, 1e6, m.price, 1.0, FEES)
    big = sell(m, 5e7, m.price, 1.0, FEES)
    assert small.ok and big.ok and big.price < small.price      # plus gros = plus d'impact


def test_risk_limits_and_kill_switch():
    lim = Limits(sol_per_trade=0.1, max_open_positions=2, daily_loss_limit_sol=0.3, max_trades_per_day=5)
    assert can_open(lim, killed=False, open_positions=0, realized_today_sol=0, trades_today=0).ok
    assert not can_open(lim, killed=True, open_positions=0, realized_today_sol=0, trades_today=0).ok
    assert not can_open(lim, killed=False, open_positions=2, realized_today_sol=0, trades_today=0).ok
    assert not can_open(lim, killed=False, open_positions=0, realized_today_sol=-0.31, trades_today=0).ok
    assert not can_open(lim, killed=False, open_positions=0, realized_today_sol=0, trades_today=5).ok
    assert not can_open(lim, killed=False, open_positions=0, realized_today_sol=0, trades_today=0, wallet_sol=0.12).ok


def test_full_simulated_cycle_with_latency():
    eng = TraderEngine(limits=Limits(sol_per_trade=0.1), latency_s=2.0)
    alert = {"decision_id": "M:30", "mint": "M", "ts": 100.0, "entry_price": VS / VT * 1.02, "v_sol": VS, "v_tokens": VT,
             "symbol": "M", "policy": "RECUP_TRAIL40"}
    o, why = eng.open(alert, SIM, 100.0, killed=False, realized_today_sol=0, trades_today=0)
    assert o is not None and o.execute_at == 102.0
    assert eng.step(101.0) == []                                  # pas encore exécuté (délai)
    vs2 = 41.0
    eng.on_market("M", Market(ts=102.5, price=vs2 / (30 * 1_073_000_000 / vs2), v_sol=vs2, v_tokens=30 * 1_073_000_000 / vs2))
    (o1, f1), = eng.step(103.0)
    p = eng.positions["M:30"]
    assert f1.ok and p.status == "open" and p.tokens > 0
    # le prix double : signal « vends 50 % »
    vs3 = 60.0
    eng.on_market("M", Market(ts=200.0, price=vs3 / (30 * 1_073_000_000 / vs3), v_sol=vs3, v_tokens=30 * 1_073_000_000 / vs3))
    eng.on_signal({"decision_id": "M:30", "fraction": 0.5, "closed": False, "kind": "PALIER", "price": vs3 / (30 * 1_073_000_000 / vs3)}, 200.0)
    assert eng.step(210.0) == []          # aucun trade après le délai : on attend (au plus 15 s)
    eng.step(218.0)                       # puis exécution sur le dernier état connu
    assert p.status == "open" and p.tokens == pytest.approx(p.tokens_initial * 0.5, rel=1e-6)
    eng.on_signal({"decision_id": "M:30", "fraction": 0.5, "closed": True, "kind": "STOP_SUIVEUR", "price": vs3 / (30 * 1_073_000_000 / vs3)}, 220.0)
    eng.step(240.0)
    assert p.status == "closed" and p.pnl_sol > 0


def test_real_orders_are_never_executed_by_the_simulator():
    eng = TraderEngine()
    eng.open({"decision_id": "R:30", "mint": "R", "ts": 0.0, "entry_price": 1e-7}, REAL, 0.0,
             killed=False, realized_today_sol=0, trades_today=0)
    eng.on_market("R", Market(ts=5, price=1e-7, v_sol=VS, v_tokens=VT))
    assert eng.step(100.0) == [] and len(eng.pending) == 1         # réservé au service (portefeuille réel)


def test_live_venue_reads_actual_amounts_from_confirmed_tx():
    from solders.keypair import Keypair
    from apex.trading.venues import LiveVenue
    kp = Keypair()
    v = LiveVenue("http://rpc", str(kp), builder=None, client=None)  # type: ignore[arg-type]
    tx = {"meta": {"preBalances": [1_000_000_000, 5], "postBalances": [899_000_000, 5],
                   "preTokenBalances": [], "postTokenBalances": [
                       {"mint": "MINT", "owner": v.pubkey, "uiTokenAmount": {"uiAmount": 2_500_000.0}}]},
          "transaction": {"message": {"accountKeys": [{"pubkey": v.pubkey}, {"pubkey": "x"}]}}}
    r = v._deltas(tx, "MINT", "sig", 1.2)
    assert r.ok and r.sol_delta == pytest.approx(-0.101) and r.token_delta == pytest.approx(2_500_000.0)


def test_live_mode_requires_every_condition():
    from apex.config import Config
    from apex.trading.service import TraderService

    class FakeRedis:
        def __init__(self, d): self.d = d
        async def get(self, k): return self.d.get(k)

    class FakeBus:
        def __init__(self, state, d): self.r, self.state = FakeRedis(d), state
        async def get_json(self, k, default=None): return {"state": self.state}

    async def mode(cfg_live, state, flags, wallet):
        svc = TraderService.__new__(TraderService)
        svc.cfg = Config.load()
        svc.cfg.base["trading"]["live_enabled"] = cfg_live
        svc.bus = FakeBus(state, flags)
        svc.live = object() if wallet else None
        return (await svc.mode_now())[0]

    ok = {"apex:trading:armed": "1"}
    assert asyncio.run(mode(True, "ACTIF", ok, True)) == REAL
    assert asyncio.run(mode(False, "ACTIF", ok, True)) == SIM          # non autorisé en configuration
    assert asyncio.run(mode(True, "PRET", ok, True)) == SIM            # pas encore ACTIF
    assert asyncio.run(mode(True, "ACTIF", {}, True)) == SIM           # pas activé par l'utilisateur
    assert asyncio.run(mode(True, "ACTIF", ok, False)) == SIM          # pas de portefeuille
    assert asyncio.run(mode(True, "ACTIF", {**ok, "apex:trading:killed": "1"}, True)) == SIM   # /stop
