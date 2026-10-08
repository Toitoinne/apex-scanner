"""Décodage des events du programme PumpSwap (AMM où migrent les tokens pump.fun).

BuyEvent / SellEvent : timestamp i64, puis 13 u64, puis pool (Pubkey), user (Pubkey), …
  u64[0] = quantité de tokens (achetée ou vendue), u64[4] = réserve tokens du pool,
  u64[5] = réserve SOL du pool (avant le trade), u64[7] = SOL payés / reçus.
Prix après trade = réserve SOL / réserve tokens, ajustées du trade. Tokens à 6 décimales, SOL à 9.
"""
from __future__ import annotations

import base64
import hashlib
import struct

import base58

PUMPSWAP_PROGRAM_ID = "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA"


def _disc(name: str) -> bytes:
    return hashlib.sha256(f"event:{name}".encode()).digest()[:8]


DISC_BUY = _disc("BuyEvent")
DISC_SELL = _disc("SellEvent")


def decode_amm_event(data_b64: str) -> dict | None:
    try:
        raw = base64.b64decode(data_b64)
    except Exception:  # noqa: BLE001
        return None
    if raw[:8] not in (DISC_BUY, DISC_SELL) or len(raw) < 8 + 176:
        return None
    b = raw[8:]
    ts = struct.unpack_from("<q", b, 0)[0]
    u = struct.unpack_from("<13Q", b, 8)
    is_buy = raw[:8] == DISC_BUY
    tokens = u[0] / 1e6
    sol = u[7] / 1e9
    base = u[4] / 1e6
    quote = u[5] / 1e9
    base_after = base - tokens if is_buy else base + tokens
    quote_after = quote + sol if is_buy else quote - sol
    if base_after <= 0 or quote_after <= 0:
        return None
    return {
        "is_buy": is_buy, "ts": ts, "tokens": tokens, "sol": sol,
        "price": quote_after / base_after, "pool_sol": quote_after,
        "pool": base58.b58encode(b[112:144]).decode(), "user": base58.b58encode(b[144:176]).decode(),
    }


def events_from_amm_logs(logs: list[str]) -> list[dict]:
    out = []
    for line in logs:
        if line.startswith("Program data: "):
            ev = decode_amm_event(line[len("Program data: "):].strip())
            if ev:
                out.append(ev)
    return out


def encode_for_test(is_buy: bool, ts: int, tokens_raw: int, pool_base_raw: int, pool_quote_raw: int,
                    sol_raw: int, pool: str, user: str) -> str:
    u = [tokens_raw, 0, 0, 0, pool_base_raw, pool_quote_raw, 0, sol_raw, 0, 0, 0, 0, 0]
    body = ((DISC_BUY if is_buy else DISC_SELL) + struct.pack("<q", ts) + struct.pack("<13Q", *u)
            + base58.b58decode(pool) + base58.b58decode(user) + bytes(64))
    return base64.b64encode(body).decode()
