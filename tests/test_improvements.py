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



# Vrais events PumpSwap relevés sur la blockchain le 14/10/2026 (token / WSOL).
REAL_BUY = ("Z/RSHyz1d3cKqs9qAAAAALbp5tcgAgAAsmhVQAAAAAAAAAAAAAAAALJoVUAAAAAAX+79AMCzAAAy0UapEAAAALJoVUAAAAAAFAAAAAAAAABajCAA"
            "AAAAAAUAAAAAAAAAFyMIAAAAAADyqrI/AAAAAJgekj8AAAAAf8R6dAVarQSXgVsFZinYaF4gnNXsc8XzbK+2oKV0EhHQilODgYCnW1UDK/Cup4N9"
            "XBNCscLgnMuEVy7L1WC05Av2djVK8hXl7KnioM4pqSV0y7vkoEoZ1rva1xzWqIR8q6rXBgM6U7vhB+XLDeHtyV1fV6rYKkX9wmj1/OcWMLgAAAAA"
            "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAYzKBwKDyjea6TUnIZSjJ"
            "HS4vAgrcQ4tipXDv4teC0rNfAAAAAAAAAKmamgAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAZbbILLQBAAAVAAAAYnV5X2V4"
            "YWN0X3F1b3RlX2luX3YyAAAAAAAAAAAAAAAAAAAAAIgTAAAAAAAAixEEAAAAAAAyYSsQBAAAAAAAAAAAAAAAAZbYyiO+igMAXwAAAAAAAACpmpoA"
            "AAAAANlOWQgAAAAA")
REAL_SELL = ("Pi83CqUD3CoKqs9qAAAAAMy/87pDAAAA2BoKCwAAAAAe8PnVoAAAAAAAAAAAAAAAIcw8N5uOAAA7iPJMFgAAANwmfAwAAAAAFAAAAAAAAABvZAYA"
             "AAAAAAUAAAAAAAAAHJkBAAAAAABtwnUMAAAAAEbMVQwAAAAAMy4WuwIyXOAJR1Rj0wHjSGm3VIaKaZGyex3CtN1Gshp6QcW+MdtYtJcc6e/WPsk8"
             "+j6y1Q8tTXg/hye3Sg0DCrKY5pmKhNNwdsvvAi1XA3s85CGM1MKQduYjjNv2ER98V+CnndPfygdxcIvC+G0niZv47rhyG39btW84aUWbSC1KwvjQ"
             "3Vy8l+MonBl8tQYqVPPZVrnOblEV+WVnqlyz5nfZFZVfiIBzHOtKdaDMlsF0+kCVxOHZlnrPxChFrmeuyLeVY4g8HzdhjyMOwULv583s/xoFggnu"
             "/bissqxwNbxfAAAAAAAAAAtdHgAAAAAAAAAAAAAAAAAAAAAAAAAAAIgTAAAAAAAAjswAAAAAAABydwsJBAAAAAAAAAAAAAAAAV08afcbdwMAAAAA"
             "AAAAAAAAAAAAAAAAADOgLA4AAAAA")


def test_pumpswap_real_events_sol_amount():
    # le montant SOL est quote_amount_in / out (u64[6]), pas les frais LP en points de base (u64[7] = 20)
    b, = events_from_amm_logs([f"Program data: {REAL_BUY}"])
    s, = events_from_amm_logs([f"Program data: {REAL_SELL}"])
    assert b["is_buy"] and not s["is_buy"]
    assert b["sol"] == pytest.approx(1.079339186)
    assert s["sol"] == pytest.approx(0.209463004)        # le vendeur a bien reçu ~0,207 SOL (après frais)
    assert b["tokens"] == pytest.approx(2340084.443574)
    assert b["price"] == pytest.approx((71.559467314 + 1.079339186) / (197637231.734367 - 2340084.443574))
    assert s["price"] == pytest.approx((95.780243515 - 0.209463004) / (156797297.806369 + 290899.345356))

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
    eng.sell_latency = eng.buy_latency = 0.0
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


def test_claude_sample_contexts_without_parallel_workers():
    """Le tirage des décisions pour tester les features de Claude passe par fetch_serial : en parallèle,
    PostgreSQL saturait la mémoire partagée du conteneur et la proposition restait bloquée."""
    import asyncio

    from apex.claude_improver.service import Improver

    class FakeDB:
        def __init__(self):
            self.serial = []

        async def fetch_serial(self, q, *a):
            self.serial.append(a)
            return []

        async def fetch(self, q, *a):
            raise AssertionError("requête parallèle interdite pour le tirage des décisions")

    imp = object.__new__(Improver)
    imp.db = FakeDB()
    assert asyncio.run(imp.sample_contexts(7)) == []
    assert imp.db.serial == [(7,)]


def test_metadata_fetch_survives_invalid_url():
    import asyncio

    from apex.features.metadata import MetadataFetcher

    async def run():
        f = MetadataFetcher()
        try:
            return await f.fetch("https://bad.example:https://x.json"), f.stats
        finally:
            await f.client.aclose()

    meta, stats = asyncio.run(run())
    assert meta is None and stats["fail"] == 1
