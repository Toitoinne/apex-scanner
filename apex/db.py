"""Accès PostgreSQL/TimescaleDB (asyncpg)."""
from __future__ import annotations

import datetime as dt
import json
import logging
from pathlib import Path
from typing import Any, Iterable

import asyncpg

log = logging.getLogger(__name__)
SQL_DIR = Path(__file__).resolve().parent.parent / "sql"


def ts(x: float) -> dt.datetime:
    return dt.datetime.fromtimestamp(x, tz=dt.timezone.utc)


async def _init_conn(conn: asyncpg.Connection) -> None:
    await conn.set_type_codec("jsonb", encoder=lambda v: json.dumps(v, default=str), decoder=json.loads, schema="pg_catalog")
    await conn.set_type_codec("json", encoder=lambda v: json.dumps(v, default=str), decoder=json.loads, schema="pg_catalog")


class DB:
    def __init__(self, pool: asyncpg.Pool):
        self.pool = pool

    @classmethod
    async def connect(cls, url: str, min_size: int = 1, max_size: int = 10) -> "DB":
        pool = await asyncpg.create_pool(url, min_size=min_size, max_size=max_size, init=_init_conn)
        return cls(pool)

    async def migrate(self) -> None:
        async with self.pool.acquire() as conn:
            for f in sorted(SQL_DIR.glob("0*.sql")):
                log.info("migration %s", f.name)
                await conn.execute(f.read_text(encoding="utf-8"))

    async def fetch(self, q: str, *args: Any) -> list[asyncpg.Record]:
        async with self.pool.acquire() as conn:
            return await conn.fetch(q, *args)

    async def fetch_serial(self, q: str, *args: Any) -> list[asyncpg.Record]:
        """Requête lourde exécutée SANS travailleurs parallèles : en parallèle, PostgreSQL échange les
        résultats intermédiaires par la mémoire partagée (/dev/shm, 64 Mo dans le conteneur) et échoue
        alors avec « could not resize shared memory segment … No space left on device »."""
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute("SET LOCAL max_parallel_workers_per_gather = 0")
                return await conn.fetch(q, *args)

    async def iter_serial(self, q: str, *args: Any, chunk: int = 2000):
        """Comme fetch_serial, mais par paquets (curseur) : une requête de plusieurs centaines de Mo n'est jamais
        entièrement chargée en mémoire."""
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute("SET LOCAL max_parallel_workers_per_gather = 0")
                cur = await conn.cursor(q, *args)
                while True:
                    rows = await cur.fetch(chunk)
                    if not rows:
                        break
                    yield rows

    async def fetchrow(self, q: str, *args: Any) -> asyncpg.Record | None:
        async with self.pool.acquire() as conn:
            return await conn.fetchrow(q, *args)

    async def fetchval(self, q: str, *args: Any) -> Any:
        async with self.pool.acquire() as conn:
            return await conn.fetchval(q, *args)

    async def execute(self, q: str, *args: Any) -> str:
        async with self.pool.acquire() as conn:
            return await conn.execute(q, *args)

    async def executemany(self, q: str, rows: Iterable[tuple]) -> None:
        rows = list(rows)
        if not rows:
            return
        async with self.pool.acquire() as conn:
            await conn.executemany(q, rows)

    async def copy(self, table: str, columns: list[str], rows: list[tuple]) -> None:
        if not rows:
            return
        async with self.pool.acquire() as conn:
            await conn.copy_records_to_table(table, records=rows, columns=columns)

    async def log_event(self, level: str, kind: str, message: str, data: dict | None = None) -> None:
        await self.execute(
            "INSERT INTO system_events (level, kind, message, data) VALUES ($1,$2,$3,$4)",
            level, kind, message, data or {},
        )
