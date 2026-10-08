"""Exécution des ordres.

- PumpPortalBuilder : construit la vraie transaction (API Local de PumpPortal, frais 0,5 %) et
  vérifie qu'elle est valide. En simulation, la transaction est construite puis JETÉE (preuve
  que la chaîne fonctionne, sans jamais rien envoyer).
- LiveVenue : signe avec le portefeuille DÉDIÉ de l'utilisateur, envoie, attend la confirmation
  et lit les montants réellement échangés. Utilisé uniquement quand le trading réel est activé.
  La clé privée n'est jamais journalisée.
"""
from __future__ import annotations

import asyncio
import base64
import logging
import time
from dataclasses import dataclass

import httpx

log = logging.getLogger("trader.venue")
PUMPPORTAL_LOCAL = "https://pumpportal.fun/api/trade-local"


class PumpPortalBuilder:
    def __init__(self, client: httpx.AsyncClient):
        self.client = client
        self.stats = {"ok": 0, "fail": 0, "last_error": ""}

    async def build(self, public_key: str, action: str, mint: str, amount: float | str, in_sol: bool,
                    slippage_pct: float, priority_fee_sol: float) -> bytes | None:
        from solders.transaction import VersionedTransaction
        body = {"publicKey": public_key, "action": action, "mint": mint, "amount": amount,
                "denominatedInSol": "true" if in_sol else "false", "slippage": round(slippage_pct, 1),
                "priorityFee": priority_fee_sol, "pool": "auto"}
        try:
            r = await self.client.post(PUMPPORTAL_LOCAL, json=body, timeout=8)
            if r.status_code != 200:
                raise RuntimeError(f"HTTP {r.status_code} {r.text[:120]}")
            VersionedTransaction.from_bytes(r.content)          # transaction valide et décodable
            self.stats["ok"] += 1
            return r.content
        except Exception as e:  # noqa: BLE001
            self.stats["fail"] += 1
            self.stats["last_error"] = str(e)[:200]
            return None


@dataclass
class LiveResult:
    ok: bool
    signature: str = ""
    sol_delta: float = 0.0      # variation du solde SOL du portefeuille (négatif à l'achat)
    token_delta: float = 0.0    # variation du solde du token
    error: str = ""
    latency_s: float = 0.0


class LiveVenue:
    def __init__(self, rpc_url: str, private_key_b58: str, builder: PumpPortalBuilder, client: httpx.AsyncClient):
        from solders.keypair import Keypair
        self._kp = Keypair.from_base58_string(private_key_b58)
        self.pubkey = str(self._kp.pubkey())
        self.rpc_url, self.builder, self.client = rpc_url, builder, client

    async def _rpc(self, method: str, params: list):
        r = await self.client.post(self.rpc_url, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params}, timeout=10)
        d = r.json()
        if "error" in d:
            raise RuntimeError(str(d["error"])[:200])
        return d.get("result")

    async def balance_sol(self) -> float:
        return (await self._rpc("getBalance", [self.pubkey, {"commitment": "confirmed"}]))["value"] / 1e9

    async def execute(self, action: str, mint: str, amount: float | str, in_sol: bool, slippage_pct: float,
                      priority_fee_sol: float) -> LiveResult:
        from solders.transaction import VersionedTransaction
        t0 = time.time()
        raw = await self.builder.build(self.pubkey, action, mint, amount, in_sol, slippage_pct, priority_fee_sol)
        if raw is None:
            return LiveResult(False, error=f"construction refusée : {self.builder.stats['last_error']}")
        unsigned = VersionedTransaction.from_bytes(raw)
        signed = VersionedTransaction(unsigned.message, [self._kp])
        try:
            sig = await self._rpc("sendTransaction", [base64.b64encode(bytes(signed)).decode(),
                                                      {"encoding": "base64", "preflightCommitment": "processed", "maxRetries": 3}])
        except Exception as e:  # noqa: BLE001
            return LiveResult(False, error=f"envoi refusé : {e}")
        for _ in range(80):                                     # ~40 s
            await asyncio.sleep(0.5)
            st = (await self._rpc("getSignatureStatuses", [[sig]]))["value"][0]
            if st and st.get("err"):
                return LiveResult(False, sig, error=f"transaction échouée on-chain : {st['err']}", latency_s=time.time() - t0)
            if st and st.get("confirmationStatus") in ("confirmed", "finalized"):
                break
        else:
            return LiveResult(False, sig, error="non confirmée après 40 s", latency_s=time.time() - t0)
        tx = await self._rpc("getTransaction", [sig, {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0,
                                                      "commitment": "confirmed"}])
        return self._deltas(tx, mint, sig, time.time() - t0)

    def _deltas(self, tx: dict, mint: str, sig: str, latency: float) -> LiveResult:
        meta = tx["meta"]
        keys = [k["pubkey"] if isinstance(k, dict) else k for k in tx["transaction"]["message"]["accountKeys"]]
        i = keys.index(self.pubkey)
        sol_delta = (meta["postBalances"][i] - meta["preBalances"][i]) / 1e9

        def tok(bals: list) -> float:
            return sum(float(b["uiTokenAmount"]["uiAmount"] or 0) for b in bals or []
                       if b.get("mint") == mint and b.get("owner") == self.pubkey)
        return LiveResult(True, sig, sol_delta, tok(meta.get("postTokenBalances")) - tok(meta.get("preTokenBalances")),
                          latency_s=latency)
