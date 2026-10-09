"""Après la migration, le prix d'entrée doit venir de PumpSwap (jamais de la courbe périmée)."""
import pytest

from apex.config import Config
from apex.events import Migration, PriceTick, TokenCreated, Trade
from apex.features.engine import FeatureEngine
from apex.features.market import MarketContext
from apex.features.wallets import WalletIntel
from apex.safety.filters import SafetyFilters


def _engine():
    cfg = Config.load()
    c = cfg.data
    return FeatureEngine(c, SafetyFilters(cfg.safety), WalletIntel(c["features"]),
                         MarketContext(c["features"]["narrative_window_s"]))


def _token_migrated_at_creation(eng):
    eng.on_event(TokenCreated(mint="M", name="m", symbol="M", uri="", creator="dev", bonding_curve="", slot=0,
                              ts=0.0, signature="c"))
    # achat qui remplit toute la courbe dans la même seconde, puis migration
    eng.on_event(Trade(mint="M", signature="s1", slot=0, ts=0.05, trader="dev", is_buy=True, sol=85.0,
                       tokens=793e6, v_sol=115.0, v_tokens=280e6))
    eng.on_event(Migration(mint="M", slot=0, ts=0.1, signature="m"))


def test_no_decision_without_fresh_pumpswap_price():
    eng = _engine()
    _token_migrated_at_creation(eng)
    decs = []
    for t in range(0, 800, 5):
        decs += eng.tick(float(t))
    assert decs == []            # aucun prix PumpSwap : abstention, pas de prix d'entrée inventé


def test_entry_uses_pumpswap_price_after_migration():
    eng = _engine()
    _token_migrated_at_creation(eng)
    curve_price = 115.0 / 280e6
    amm = curve_price * 40      # le token a déjà fortement monté sur PumpSwap
    decs = []
    for t in range(0, 800, 5):
        eng.on_event(PriceTick(mint="M", ts=float(t), price=amm, source="pumpswap", pool_sol=300.0))
        decs += eng.tick(float(t))
    assert decs
    for d in decs:
        assert d.spot_price == pytest.approx(amm)
        assert amm < d.entry_price < amm * 1.02      # frais AMM + léger glissement, pas le prix de la courbe


def test_labeler_follows_fresh_migrations_without_decision():
    from apex.labeler.engine import LabelerEngine
    lab = LabelerEngine(Config.load().data)
    lab.on_event(TokenCreated(mint="M", name="m", symbol="M", uri="", creator="dev", bonding_curve="", slot=0,
                              ts=0.0, signature="c"))
    lab.on_event(Migration(mint="M", slot=0, ts=0.1, signature="m"))
    assert "M" in lab.migrated_mints(60.0)          # suivi sur PumpSwap pour obtenir un prix d'entrée
    assert "M" not in lab.migrated_mints(2000.0)    # puis abandonné s'il n'a donné lieu à aucune décision


def test_learner_forgets_decisions():
    from apex.learning.core import Learner
    lr = Learner(Config.load())
    lr.long_store["M:600"] = {"ts": 0.0, "point": "600", "mint": "M", "scores": {}, "alerted": False,
                              "eligible": True, "preds": {}, "champions": {}}
    assert lr.apply_command({"op": "forget_decisions", "ids": ["M:600", "inconnu"]})["forgotten"] == 1
    assert "M:600" not in lr.long_store


def test_dexscreener_ignored_when_real_pool_price_is_known():
    """DexScreener peut s'écarter de 20 % du prix réel du pool : quand les trades PumpSwap décodés sont
    reçus, ils font foi (sinon des prises de bénéfices se déclenchent sur un prix qui n'existe pas)."""
    from apex.labeler.engine import LabelerEngine
    lab = LabelerEngine(Config.load().data)
    lab.on_event(TokenCreated(mint="M", name="m", symbol="M", uri="", creator="dev", bonding_curve="", slot=0,
                              ts=0.0, signature="c"))
    lab.on_event(PriceTick(mint="M", ts=100.0, price=2.5e-6, source="pumpswap", trader="a", is_buy=True, sol=1, tokens=1e5))
    lab.on_event(PriceTick(mint="M", ts=110.0, price=3.1e-6, source="dexscreener"))
    assert lab.tracks["M"].prices[-1] == 2.5e-6
    lab.on_event(PriceTick(mint="M", ts=500.0, price=3.0e-6, source="dexscreener"))   # plus de trade PumpSwap : utilisé
    assert lab.tracks["M"].prices[-1] == 3.0e-6
