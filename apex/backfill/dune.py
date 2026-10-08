"""Téléchargement de l'historique pump.fun via l'API Dune (mis en cache sur disque
pour ne payer les crédits qu'une fois)."""
from __future__ import annotations

import asyncio
import datetime as dt
import gzip
import json
import logging
from pathlib import Path
from typing import Iterator

import httpx

from ..events import Migration, TokenCreated, Trade

log = logging.getLogger("backfill.dune")
API = "https://api.dune.com/api/v1"


class DuneClient:
    def __init__(self, api_key: str):
        self.h = {"X-Dune-Api-Key": api_key}

    async def run(self, query_id: int, params: dict, page: int = 50_000) -> list[dict]:
        async with httpx.AsyncClient(timeout=120, headers=self.h) as c:
            r = await c.post(f"{API}/query/{query_id}/execute", json={"query_parameters": params, "performance": "medium"})
            r.raise_for_status()
            eid = r.json()["execution_id"]
            while True:
                s = (await c.get(f"{API}/execution/{eid}/status")).json()
                state = s.get("state")
                if state == "QUERY_STATE_COMPLETED":
                    break
                if state in ("QUERY_STATE_FAILED", "QUERY_STATE_CANCELLED", "QUERY_STATE_EXPIRED"):
                    raise RuntimeError(f"Dune : {state} {s.get('error')}")
                await asyncio.sleep(5)
            rows: list[dict] = []
            offset = 0
            while True:
                r = await c.get(f"{API}/execution/{eid}/results", params={"limit": page, "offset": offset})
                r.raise_for_status()
                d = r.json()
                batch = d["result"]["rows"]
                rows += batch
                if not d.get("next_offset"):
                    break
                offset = d["next_offset"]
            return rows


async def fetch_days(api_key: str, query_id: int, days: int, sample_mod: int, cache_dir: Path) -> list[Path]:
    cache_dir.mkdir(parents=True, exist_ok=True)
    client = DuneClient(api_key)
    today = dt.datetime.now(dt.timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    paths = []
    for i in range(days, 0, -1):
        start, end = today - dt.timedelta(days=i), today - dt.timedelta(days=i - 1)
        p = cache_dir / f"pump_{start:%Y%m%d}_m{sample_mod}.jsonl.gz"
        if not p.exists():
            log.info("Dune : téléchargement %s", start.date())
            rows = await client.run(query_id, {"start": f"{start:%Y-%m-%d %H:%M:%S}", "end": f"{end:%Y-%m-%d %H:%M:%S}", "sample_mod": sample_mod})
            with gzip.open(p, "wt", encoding="utf-8") as f:
                for r in rows:
                    f.write(json.dumps(r) + "\n")
            log.info("Dune : %d lignes pour %s", len(rows), start.date())
        paths.append(p)
    return paths


def iter_events(paths: list[Path]) -> Iterator:
    for p in paths:
        with gzip.open(p, "rt", encoding="utf-8") as f:
            for line in f:
                r = json.loads(line)
                k = r["kind"]
                ts = float(r["ts"])
                if k == "create":
                    yield TokenCreated(mint=r["mint"], name=r["name"] or "", symbol=r["symbol"] or "", uri=r["uri"] or "",
                                       creator=r["creator"] or "", bonding_curve=r["bonding_curve"] or "",
                                       slot=int(r["slot"] or 0), ts=ts, signature=r["signature"])
                elif k == "trade":
                    yield Trade(mint=r["mint"], signature=r["signature"], slot=int(r["slot"] or 0), ts=ts,
                                trader=r["trader"], is_buy=bool(r["is_buy"]), sol=float(r["sol"]), tokens=float(r["tokens"]),
                                v_sol=float(r["v_sol"]), v_tokens=float(r["v_tokens"]), source="dune")
                elif k == "migration":
                    yield Migration(mint=r["mint"], slot=int(r["slot"] or 0), ts=ts, signature=r["signature"])
