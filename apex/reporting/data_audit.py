"""Audit des données réelles : le bot voit-il la VRAIE blockchain, en temps réel, sans trou ?

Compare ce que le bot a enregistré avec l'état lu directement sur Solana (RPC public, sans crédits Helius) :
  1. fraîcheur   : âge du dernier trade reçu, et retard de réception par rapport à l'heure du bloc
  2. prix courbe : état de la bonding curve on-chain vs dernier trade vu par le bot au même slot
  3. complétude  : achats/ventes pump.fun réussis on-chain vs vus par le bot (fenêtre de 3 min). Les
                   transactions qui ne font que LIRE la courbe (autres programmes) ne sont pas des trades.
  4. prix après migration : réserves du pool PumpSwap on-chain vs dernier prix PumpSwap du bot
Lancé par `python -m apex.reporting.data_audit` (commande « audit » du suivi). Sortie : JSON + résumé simple.
"""
from __future__ import annotations

import asyncio
import base64
import json
import statistics
import struct
import time

import base58
import httpx

from ..bus import Bus
from ..config import secrets
from ..db import DB

RPC = "https://api.mainnet-beta.solana.com"
PUMP = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
TRADE_INSTRUCTIONS = ("Instruction: Buy", "Instruction: Sell")


def verdict(r: dict) -> str:
    """Résumé en une ligne, en langage simple."""
    ok = (r.get("fraicheur_s", 99) < 10 and r.get("prix_ecart_median_pct", 0) < 1.5
          and r.get("completude_pct", 100) >= 99 and r.get("pumpswap_ecart_median_pct", 0) < 2)
    head = "✅ Données vérifiées" if ok else "⚠️ Données à vérifier"
    parts = [f"dernier trade reçu il y a {r.get('fraicheur_s', '?')} s"]
    if "retard_median_s" in r:
        parts.append(f"trades reçus {r['retard_median_s']} s après la blockchain en général (95 % en moins de {r['retard_p95_s']} s)")
    if "prix_ecart_median_pct" in r:
        parts.append(f"prix identiques à la blockchain à {r['prix_ecart_median_pct']:.1f} % près ({r['prix_n']} tokens)")
    if "completude_pct" in r:
        parts.append(f"{r['completude_pct']:.0f} % des achats/ventes vus ({r['completude_vues']}/{r['completude_total']})")
    if "pumpswap_ecart_median_pct" in r:
        parts.append(f"prix après migration à {r['pumpswap_ecart_median_pct']:.1f} % de la blockchain ({r['pumpswap_n']} tokens)")
    return f"{head} : " + " ; ".join(parts)


async def rpc(c: httpx.AsyncClient, method: str, params: list) -> dict | list | None:
    for attempt in range(4):
        r = await c.post(RPC, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
        if r.status_code == 429:
            await asyncio.sleep(2 * (attempt + 1))
            continue
        return r.json().get("result")
    return None


async def is_trade(c: httpx.AsyncClient, sig: str) -> bool:
    tx = await rpc(c, "getTransaction", [sig, {"maxSupportedTransactionVersion": 1, "commitment": "confirmed"}])
    logs = ((tx or {}).get("meta") or {}).get("logMessages") or []
    return any(PUMP in l for l in logs) and any(t in l for l in logs for t in TRADE_INSTRUCTIONS)


async def audit() -> dict:
    db = await DB.connect(secrets().database_url, max_size=2)
    bus = Bus(secrets().redis_url)
    out: dict = {"ts": time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime())}
    last = await db.fetchval("SELECT max(ts) FROM trades WHERE ts > now() - interval '10 minutes'")
    out["fraicheur_s"] = round(time.time() - last.timestamp(), 1) if last else 999
    async with httpx.AsyncClient(timeout=20) as c:
        # 2. prix : on lit la courbe on-chain, puis le dernier trade du bot à un slot <= celui de la lecture
        rows = await db.fetch(
            """SELECT t.mint, k.bonding_curve FROM (SELECT mint, count(*) n FROM trades WHERE ts > now() - interval '2 minutes'
               GROUP BY mint ORDER BY n DESC LIMIT 6) t JOIN tokens k USING (mint) WHERE k.bonding_curve <> ''""")
        gaps = []
        for r in rows:
            res = await rpc(c, "getAccountInfo", [r["bonding_curve"], {"encoding": "base64", "commitment": "confirmed"}])
            if not res or not res.get("value"):
                continue
            slot = res["context"]["slot"]
            vtok, vsol = struct.unpack_from("<QQ", base64.b64decode(res["value"]["data"][0]), 8)
            await asyncio.sleep(2)          # laisse au bot le temps de recevoir les trades de ce slot
            b = await db.fetchrow("""SELECT v_sol, v_tokens FROM trades WHERE mint=$1 AND slot <= $2 AND ts > now() - interval '10 minutes'
                                     ORDER BY slot DESC, ts DESC LIMIT 1""", r["mint"], slot)
            if b and b["v_tokens"] and vtok:
                gaps.append(abs((b["v_sol"] / b["v_tokens"]) / ((vsol / 1e9) / (vtok / 1e6)) - 1) * 100)
        if gaps:
            out["prix_ecart_median_pct"] = round(statistics.median(gaps), 2)
            out["prix_n"] = len(gaps)
        # 3. complétude et retard de réception, fenêtre [now-4 min, now-1 min]
        t1 = time.time() - 60
        t0 = t1 - 180
        rows = await db.fetch(
            """SELECT t.mint, k.bonding_curve FROM (SELECT mint, count(*) n FROM trades
               WHERE ts BETWEEN to_timestamp($1) AND to_timestamp($2) GROUP BY mint
               HAVING count(*) BETWEEN 15 AND 300 ORDER BY count(*) DESC LIMIT 3) t JOIN tokens k USING (mint)
               WHERE k.bonding_curve <> ''""", t0, t1)
        seen = total = 0
        delays = []
        for r in rows:
            sigs = await rpc(c, "getSignaturesForAddress", [r["bonding_curve"], {"limit": 1000, "commitment": "confirmed"}]) or []
            bot = {x["signature"].split(":")[0]: x["ts"].timestamp() for x in await db.fetch(
                """SELECT signature, ts FROM trades WHERE mint=$1
                   AND ts BETWEEN to_timestamp($2) - interval '5 seconds' AND to_timestamp($3) + interval '30 seconds'""",
                r["mint"], t0, t1)}
            for s in sigs:
                if s.get("err") is not None or not s.get("blockTime") or not (t0 <= s["blockTime"] < t1):
                    continue
                if s["signature"] in bot:
                    seen += 1
                    total += 1
                    delays.append(bot[s["signature"]] - s["blockTime"])
                elif await is_trade(c, s["signature"]):      # absent : on ne compte que les vrais achats/ventes
                    total += 1
        if total:
            out.update(completude_pct=round(100 * seen / total, 1), completude_vues=seen, completude_total=total)
        if delays:
            delays.sort()
            out["retard_median_s"] = round(delays[len(delays) // 2], 1)
            out["retard_p95_s"] = round(delays[int(len(delays) * 0.95)], 1)
        # 4. après migration : réserves du pool PumpSwap on-chain vs dernier prix PumpSwap du bot
        pools = (await bus.get_json("apex:pools:active", []) or [])[:12]
        g = []
        for p in pools:
            tick = await db.fetchrow("""SELECT price FROM price_ticks WHERE mint=$1 AND source='pumpswap'
                                        AND ts > now() - interval '10 minutes' ORDER BY ts DESC LIMIT 1""", p["mint"])
            acc = await rpc(c, "getAccountInfo", [p["pool"], {"encoding": "base64"}])
            if not tick or not acc or not acc.get("value"):
                continue
            data = base64.b64decode(acc["value"]["data"][0])
            base_ta, quote_ta = base58.b58encode(data[139:171]).decode(), base58.b58encode(data[171:203]).decode()
            bal = await rpc(c, "getMultipleAccounts", [[base_ta, quote_ta], {"encoding": "jsonParsed"}])
            try:
                amt = [float(v["data"]["parsed"]["info"]["tokenAmount"]["uiAmount"]) for v in bal["value"]]
            except (TypeError, KeyError):
                continue
            if amt[0] > 0:
                g.append(abs(tick["price"] / (amt[1] / amt[0]) - 1) * 100)
            if len(g) >= 5:
                break
        if g:
            out["pumpswap_ecart_median_pct"] = round(statistics.median(g), 2)
            out["pumpswap_n"] = len(g)
    out["resume"] = verdict(out)
    return out


if __name__ == "__main__":
    print(json.dumps(asyncio.run(audit()), ensure_ascii=False))
