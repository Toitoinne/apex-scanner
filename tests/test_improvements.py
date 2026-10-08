"""PumpSwap, métadonnées, alerte crédit Claude, danger après migration."""
import base58
import pytest

from apex.config import Config
from apex.events import Decision, PriceTick, TokenCreated
from apex.features.metadata import candidate_urls, meta_features
from apex.ingestor.pumpswap_decoder import encode_for_test, events_from_amm_logs
from apex.labeler.engine import LabelerEngine

POOL = base58.b58encode(bytes(range(32))).decode()
USER = base58.b58encode(bytes(range(1, 33))).decode()


def test_pumpswap_buy_and_sell_decoding():
    # pool : 184 M tokens / 78,84 SOL ; achat de 1 M tokens pour 0,43 SOL
    buy = encode_for_test(True, 1700000000, 1_000_000 * 10**6, 184_000_000 * 10**6, int(78.84e9), int(0.43e9), POOL, USER)
    sell = encode_for_test(False, 1700000001, 2_000_000 * 10**6, 184_000_000 * 10**6, int(78.84e9), int(0.85e9), POOL, USER)
    evs = events_from_amm_logs(["Program log: x", f"Program data: {buy}", f"Program data: {sell}"])
    assert [e["is_buy"] for e in evs] == [True, False]
    b, s = evs
    assert b["pool"] == POOL and b["user"] == USER
    assert b["price"] == pytest.approx((78.84 + 0.43) / (184e6 - 1e6))
    assert s["price"] == pytest.approx((78.84 - 0.85) / (184e6 + 2e6))
    assert b["price"] * 1e9 == pytest.approx(433, rel=0.01)          # capitalisation ≈ 433 SOL


def test_metadata_features():
    f = meta_features({"twitter": "https://x.com/abc/status/1", "telegram": "", "website": "https://x.com/abc",
                       "description": "un vrai projet"})
    assert f["meta_fetched"] == 1 and f["meta_has_twitter"] == 1 and f["meta_n_socials"] == 2
    assert f["meta_twitter_is_post"] == 1 and f["meta_website_is_social"] == 1
    assert meta_features(None) == {"meta_fetched": 0.0}
    urls = candidate_urls("https://ipfs.io/ipfs/QmABC")
    assert urls[0] == "https://pump.mypinata.cloud/ipfs/QmABC" and not any("ipfs.io/" in u for u in urls)
    assert candidate_urls("https://metadata.j7tracker.io/x.json") == ["https://metadata.j7tracker.io/x.json"]


def test_claude_credit_errors_are_detected():
    from apex.claude_improver.service import Improver

    class E(Exception):
        def __init__(self, m, code=400):
            self.message, self.status_code = m, code
    assert Improver.classify_api_error(E("Your credit balance is too low to access the Anthropic API.")) == "credit"
    assert Improver.classify_api_error(E("invalid x-api-key", 401)) == "auth"
    assert Improver.classify_api_error(E("overloaded", 529)) is None


def test_danger_detected_on_pumpswap_trades_after_migration():
    eng = LabelerEngine(Config.load().data)
    eng.on_event(TokenCreated(mint="M", name="m", symbol="M", uri="", creator="dev", bonding_curve="", slot=0,
                              ts=0.0, signature="c"))
    p = 4e-7
    eng.on_event(PriceTick(mint="M", ts=10.0, price=p, source="pumpswap", trader="dev", is_buy=True, sol=5, tokens=50e6))
    eng.on_event(Decision(decision_id="M:migration", mint="M", point="migration", ts=20.0, features={},
                          entry_price=p * 1.02, spot_price=p, mc_sol=400, v_sol=0, v_tokens=0))
    eng.open_position({"decision_id": "M:migration", "mint": "M", "policy": "RECUP_TRAIL40", "ts": 20.0, "symbol": "M"})
    eng.on_event(PriceTick(mint="M", ts=30.0, price=p * 0.97, source="pumpswap", trader="dev", is_buy=False, sol=4, tokens=40e6))
    _, sigs = eng.drain()
    assert sigs and sigs[0]["kind"] == "DANGER" and sigs[0]["reason"] == "DEV_VEND"
