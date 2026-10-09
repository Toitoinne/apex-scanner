"""Audit des données réelles : le bot voit-il la VRAIE blockchain, en temps réel, sans trou ?

Compare, au même instant, ce que le bot a enregistré avec des sources indépendantes :
  1. fraîcheur   : âge du dernier trade reçu
  2. prix        : état de la bonding curve lu directement sur Solana (RPC public, sans crédits Helius)
  3. complétude  : transactions réussies sur la blockchain vs transactions vues par le bot (fenêtre de 3 min)
  4. après migration : prix PumpSwap du bot vs DexScreener
Lancé par `python -m apex.reporting.data_audit` (commande « audit » du suivi). Sortie : JSON + résumé simple.
"""
from __future__ import annotations

import asyncio
import base64
import json
import statistics
import struct
import time

import httpx

from ..config import secrets
from ..db import DB

RPC = "https://api.mainnet-beta.solana.com"


def verdict(r: dict) -> str:
    """Résumé en une ligne, en langage simple."""
    ok = (r.get("fraicheur_s", 99) < 10 and r.get("prix_ecart_median_pct", 99) < 2
          and r.get("completude_pct", 0) >= 97 and r.get("pumpswap_ecart_median_pct", 0) < 5)
    head = "✅ Données vérifiées" if ok else "⚠️ Données à vérifier"
    parts = [f"retard {r.get('fraicheur_s', '?')} s"]
    if "prix_ecart_median_pct" in r:
        parts.append(f"prix identiques à la blockchain à {r['prix_ecart_median_pct']:.1f} % près ({r['prix_n']} tokens)")
    if "completude_pct" in r:
        parts.append(f"{r['completude_pct']:.0f} % des transactions vues ({r['completude_vues']}/{r['completude_total']})")
    if "pumpswap_ecart_median_pct" in r:
        parts.append(f"prix après migration à {r['pumpswap_ecart_median_pct']:.1f} % de DexScreener")
    return f"{head} : " + " ; ".join(parts)


async def rpc(c: httpx.AsyncClient, method: str, params: list) -> dict:
    for attempt in range(3):
        r = await c.post(RPC, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
        if r.status_code == 429:
            await asyncio.sleep(2 * (attempt + 1))
            continue
        return r.json().get("result") or {}
    return {}


async def audit() -> dict:
    db = await DB.connect(secrets().database_url, max_size=2)
    out: dict = {"ts": time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime())}
    last = await db.fetchval("SELECT max(ts) FROM trades WHERE ts > now() - interval '10 minutes'")
    out["fraicheur_s"] = round(time.time() - last.timestamp(), 1) if last else 999
    async with httpx.AsyncClient(timeout=20) as c:
        # 2. prix : dernier trade vu par le bot vs état on-chain de la bonding curve
        rows = await db.fetch(
            """SELECT t.mint, k.bonding_curve FROM (SELECT mint, count(*) n FROM trades WHERE ts > now() - interval '2 minutes'
               GROUP BY mint ORDER BY n DESC LIMIT 6) t JOIN tokens k USING (mint) WHERE k.bonding_curve <> ''""")
        gaps = []
        for r in rows:
            b = await db.fetchrow("SELECT v_sol, v_tokens FROM trades WHERE mint=$1 ORDER BY ts DESC LIMIT 1", r["mint"])
            acc = (await rpc(c, "getAccountInfo", [r["bonding_curve"], {"encoding": "base64", "commitment": "confirmed"}])).get("value")
            if not acc or not b or not b["v_tokens"]:
                continue
            vtok, vsol = struct.unpack_from("<QQ", base64.b64decode(acc["data"][0]), 8)
            if vtok:
                gaps.append(abs((b["v_sol"] / b["v_tokens"]) / ((vsol / 1e9) / (vtok / 1e6)) - 1) * 100)
            await asyncio.sleep(0.3)
        if gaps:
            out["prix_ecart_median_pct"] = round(statistics.median(gaps), 2)
            out["prix_n"] = len(gaps)
        # 3. complétude : transactions réussies on-chain vs vues par le bot (fenêtre [now-4 min, now-1 min])
        t1 = time.time() - 60
        t0 = t1 - 180
        rows = await db.fetch(
            """SELECT t.mint, k.bonding_curve FROM (SELECT mint, count(*) n FROM trades
               WHERE ts BETWEEN to_timestamp($1) AND to_timestamp($2) GROUP BY mint
               HAVING count(*) BETWEEN 15 AND 300 ORDER BY count(*) DESC LIMIT 4) t JOIN tokens k USING (mint)
               WHERE k.bonding_curve <> ''""", t0, t1)
        seen = total = 0
        for r in rows:
            sigs = await rpc(c, "getSignaturesForAddress", [r["bonding_curve"], {"limit": 1000, "commitment": "confirmed"}])
            chain = {s["signature"] for s in (sigs or []) if s.get("err") is None and s.get("blockTime")
                     and t0 <= s["blockTime"] < t1}
            bot = {x["signature"].split(":")[0] for x in await db.fetch(
                """SELECT DISTINCT signature FROM trades WHERE mint=$1
                   AND ts BETWEEN to_timestamp($2) - interval '3 seconds' AND to_timestamp($3) + interval '3 seconds'""",
                r["mint"], t0, t1)}
            seen += len(chain & bot)
            total += len(chain)
            await asyncio.sleep(0.3)
        if total:
            out.update(completude_pct=round(100 * seen / total, 1), completude_vues=seen, completude_total=total)
        # 4. après migration : prix PumpSwap du bot vs DexScreener
        rows = await db.fetch(
            """SELECT mint, (array_agg(price ORDER BY ts DESC))[1] p FROM price_ticks
               WHERE ts > now() - interval '30 seconds' AND source = 'pumpswap' GROUP BY mint LIMIT 8""")
        if rows:
            r = await c.get("https://api.dexscreener.com/tokens/v1/solana/" + ",".join(x["mint"] for x in rows))
            best: dict[str, tuple[float, float]] = {}
            for d in r.json() or []:
                if d.get("dexId") == "pumpswap" and (d.get("quoteToken") or {}).get("symbol") == "SOL":
                    liq = (d.get("liquidity") or {}).get("usd") or 0
                    if liq > best.get(d["baseToken"]["address"], (0, -1))[1]:
                        best[d["baseToken"]["address"]] = (float(d["priceNative"]), liq)
            g = [abs(x["p"] / best[x["mint"]][0] - 1) * 100 for x in rows if x["mint"] in best and best[x["mint"]][0]]
            if g:
                out["pumpswap_ecart_median_pct"] = round(statistics.median(g), 2)
                out["pumpswap_n"] = len(g)
    out["resume"] = verdict(out)
    return out


if __name__ == "__main__":
    print(json.dumps(asyncio.run(audit()), ensure_ascii=False))
