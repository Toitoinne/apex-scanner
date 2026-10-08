"""Décodage des events Anchor du programme pump.fun présents dans les logs
("Program data: <base64>"). Fonctions pures, testées.

Layouts (IDL pump.fun ; seuls les préfixes stables sont lus, les champs ajoutés
ultérieurement par pump.fun sont ignorés) :
  TradeEvent   : mint, sol_amount u64, token_amount u64, is_buy bool, user,
                 timestamp i64, virtual_sol_reserves u64, virtual_token_reserves u64, ...
  CreateEvent  : name str, symbol str, uri str, mint, bonding_curve, user, ...
  CompleteEvent: user, mint, bonding_curve, timestamp i64
"""
from __future__ import annotations

import base64
import hashlib
import struct
from typing import Any

import base58

from ..events import LAMPORTS, Migration, TokenCreated, Trade

TOKEN_DECIMALS = 10**6


def _disc(name: str) -> bytes:
    return hashlib.sha256(f"event:{name}".encode()).digest()[:8]


DISC_TRADE = _disc("TradeEvent")
DISC_CREATE = _disc("CreateEvent")
DISC_COMPLETE = _disc("CompleteEvent")


class _Reader:
    def __init__(self, buf: bytes):
        self.buf = buf
        self.o = 0

    def take(self, n: int) -> bytes:
        if self.o + n > len(self.buf):
            raise ValueError("buffer trop court")
        b = self.buf[self.o:self.o + n]
        self.o += n
        return b

    def pubkey(self) -> str:
        return base58.b58encode(self.take(32)).decode()

    def u64(self) -> int:
        return struct.unpack("<Q", self.take(8))[0]

    def i64(self) -> int:
        return struct.unpack("<q", self.take(8))[0]

    def u8(self) -> int:
        return self.take(1)[0]

    def string(self) -> str:
        (n,) = struct.unpack("<I", self.take(4))
        return self.take(n).decode("utf-8", errors="replace")


def decode_event(data_b64: str) -> tuple[str, dict[str, Any]] | None:
    try:
        raw = base64.b64decode(data_b64)
    except Exception:  # noqa: BLE001
        return None
    if len(raw) < 8:
        return None
    disc, r = raw[:8], _Reader(raw[8:])
    try:
        if disc == DISC_TRADE:
            return "trade", {
                "mint": r.pubkey(), "sol_amount": r.u64(), "token_amount": r.u64(),
                "is_buy": bool(r.u8()), "user": r.pubkey(), "timestamp": r.i64(),
                "virtual_sol_reserves": r.u64(), "virtual_token_reserves": r.u64(),
            }
        if disc == DISC_CREATE:
            return "create", {
                "name": r.string(), "symbol": r.string(), "uri": r.string(),
                "mint": r.pubkey(), "bonding_curve": r.pubkey(), "user": r.pubkey(),
            }
        if disc == DISC_COMPLETE:
            return "complete", {
                "user": r.pubkey(), "mint": r.pubkey(), "bonding_curve": r.pubkey(), "timestamp": r.i64(),
            }
    except (ValueError, struct.error):
        return None
    return None


def events_from_logs(logs: list[str], signature: str, slot: int, recv_ts: float) -> list[Any]:
    """Transforme les logs d'une transaction en événements normalisés."""
    out: list[Any] = []
    idx = 0
    for line in logs:
        if not line.startswith("Program data: "):
            continue
        dec = decode_event(line[len("Program data: "):].strip())
        if dec is None:
            continue
        kind, e = dec
        if kind == "trade":
            out.append(Trade(
                mint=e["mint"], signature=f"{signature}:{idx}", slot=slot, ts=recv_ts,
                trader=e["user"], is_buy=e["is_buy"],
                sol=e["sol_amount"] / LAMPORTS, tokens=e["token_amount"] / TOKEN_DECIMALS,
                v_sol=e["virtual_sol_reserves"] / LAMPORTS,
                v_tokens=e["virtual_token_reserves"] / TOKEN_DECIMALS,
                source="helius",
            ))
        elif kind == "create":
            out.append(TokenCreated(
                mint=e["mint"], name=e["name"], symbol=e["symbol"], uri=e["uri"],
                creator=e["user"], bonding_curve=e["bonding_curve"], slot=slot, ts=recv_ts,
                signature=signature,
            ))
        elif kind == "complete":
            out.append(Migration(mint=e["mint"], slot=slot, ts=recv_ts, signature=signature))
        idx += 1
    return out


def encode_trade_for_test(mint: str, sol: int, tokens: int, is_buy: bool, user: str, ts: int, vsol: int, vtok: int) -> str:
    """Encode un TradeEvent (utilisé par les tests)."""
    body = (
        DISC_TRADE + base58.b58decode(mint) + struct.pack("<QQ", sol, tokens) + bytes([int(is_buy)])
        + base58.b58decode(user) + struct.pack("<qQQ", ts, vsol, vtok)
    )
    return base64.b64encode(body).decode()
