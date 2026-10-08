"""Bus d'événements : Redis Streams avec groupes de consommateurs (reprise après crash)."""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, AsyncIterator

import orjson
import redis.asyncio as aioredis

from .events import dumps, loads

log = logging.getLogger(__name__)

# Noms des flux
RAW = "apex:raw"              # créations / trades / migrations (ingestor → features, labeler)
DECISIONS = "apex:decisions"  # vecteurs de features (features → learner, labeler)
LABELS = "apex:labels"        # labels multi-horizons (labeler → learner, notifier)
CLOSED = "apex:closed"        # tokens clôturés (labeler → features : bases wallets/devs)
ALERTS = "apex:alerts"        # alertes tokens (learner → notifier)
CONTROL = "apex:control"      # commandes de correction (supervisor → learner)
CONTROL_ACK = "apex:control_ack"
NOTIFY = "apex:notify"        # rapports / alertes système (supervisor → notifier)
SIGNALS = "apex:signals"      # signaux de vente des positions alertées (labeler → trader)
OUTCOMES = "apex:outcomes"    # PnL par stratégie de sortie (labeler → learner : récompenses du bandit)

# Taille maximale de chaque flux : de quoi reconstruire l'état après un redémarrage (≈ 2 h 10),
# sans jamais remplir la mémoire de Redis (les données durables sont dans PostgreSQL).
STREAM_MAXLEN = {
    DECISIONS: 30_000,     # ≈ 3 h de décisions
    LABELS: 60_000,
    CLOSED: 5_000,
    ALERTS: 5_000,
    CONTROL: 5_000,
    CONTROL_ACK: 5_000,
    NOTIFY: 5_000,
    OUTCOMES: 60_000,
    SIGNALS: 10_000,
}


class Bus:
    def __init__(self, url: str, maxlen: int = 300_000):
        # délais explicites + relance automatique d'une commande en cas de délai dépassé passager
        self.r = aioredis.from_url(url, decode_responses=False, health_check_interval=30,
                                   socket_timeout=15, socket_connect_timeout=5, retry_on_timeout=True,
                                   socket_keepalive=True)
        self.maxlen = maxlen

    async def publish(self, stream: str, ev: Any, maxlen: int | None = None) -> bytes:
        ml = maxlen or STREAM_MAXLEN.get(stream, self.maxlen)
        return await self.r.xadd(stream, {b"d": dumps(ev)}, maxlen=ml, approximate=True)

    async def ensure_group(self, stream: str, group: str, start_id: str = "0") -> None:
        try:
            await self.r.xgroup_create(stream, group, id=start_id, mkstream=True)
        except aioredis.ResponseError as e:
            if "BUSYGROUP" not in str(e):
                raise

    async def consume(
        self, streams: list[str], group: str, consumer: str, count: int = 500, block_ms: int = 1000,
        replay_pending: bool = True, start_id: str = "0",
    ) -> AsyncIterator[tuple[str, bytes, Any]]:
        """Itère (stream, id, event). L'appelant doit appeler ack() après traitement.

        Au démarrage, relit d'abord les messages en attente (non acquittés) pour ne
        rien perdre après un crash.
        """
        for s in streams:
            await self.ensure_group(s, group, start_id)
        # 1) messages en attente de ce consommateur
        pending = {s: "0" for s in streams} if replay_pending else {}
        while pending:
            resp = await self.r.xreadgroup(group, consumer, pending, count=count)
            done = []
            for stream, entries in resp or []:
                s = stream.decode()
                if not entries:
                    done.append(s)
                    continue
                for msg_id, fields in entries:
                    yield s, msg_id, loads(fields[b"d"])
                pending[s] = entries[-1][0]
            for s in done:
                pending.pop(s, None)
            if not resp:
                break
        # 2) nouveaux messages
        while True:
            resp = await self.r.xreadgroup(group, consumer, {s: ">" for s in streams}, count=count, block=block_ms)
            for stream, entries in resp or []:
                s = stream.decode()
                for msg_id, fields in entries:
                    yield s, msg_id, loads(fields[b"d"])

    async def last_delivered(self, stream: str, group: str) -> str | None:
        try:
            for g in await self.r.xinfo_groups(stream):
                if g["name"].decode() == group:
                    lid = g["last-delivered-id"]
                    return lid.decode() if isinstance(lid, bytes) else lid
        except aioredis.ResponseError:
            return None
        return None

    async def history(self, streams: list[str], group: str, seconds: float) -> tuple[list[Any], float]:
        """Événements des `seconds` dernières secondes déjà traités par `group` (jusqu'au
        dernier id délivré), fusionnés par ordre d'id. Sert à reconstruire l'état en
        mémoire après un redémarrage. Retourne (événements, ts du dernier événement traité)."""
        start = f"{int((time.time() - seconds) * 1000)}-0"
        merged: list[tuple[tuple[int, int], Any]] = []
        last_ts = 0.0
        for s in streams:
            end = await self.last_delivered(s, group)
            if not end or end == "0-0":
                continue
            cur = start
            while True:
                batch = await self.r.xrange(s, min=cur, max=end, count=5000)
                if not batch:
                    break
                for mid, fields in batch:
                    ms, seq = (int(x) for x in mid.decode().split("-"))
                    merged.append(((ms, seq), loads(fields[b"d"])))
                    last_ts = max(last_ts, ms / 1000)
                if len(batch) < 5000:
                    break
                ms, seq = (int(x) for x in batch[-1][0].decode().split("-"))
                cur = f"{ms}-{seq + 1}"
        merged.sort(key=lambda kv: kv[0])
        return [ev for _, ev in merged], last_ts

    async def ack(self, stream: str, group: str, *ids: bytes) -> None:
        if ids:
            await self.r.xack(stream, group, *ids)

    # --- clés JSON partagées (état publié, heartbeats) ---
    async def set_json(self, key: str, value: Any, ex: int | None = None) -> None:
        await self.r.set(key, orjson.dumps(value, default=str), ex=ex)

    async def get_json(self, key: str, default: Any = None) -> Any:
        raw = await self.r.get(key)
        return orjson.loads(raw) if raw else default

    async def heartbeat(self, service: str) -> None:
        await self.r.hset("apex:heartbeats", service, str(time.time()))

    async def heartbeats(self) -> dict[str, float]:
        raw = await self.r.hgetall("apex:heartbeats")
        return {k.decode(): float(v) for k, v in raw.items()}


_BACKGROUND: set = set()


def spawn(coro) -> "asyncio.Task":
    """Lance une tâche de fond en gardant une référence forte (sinon Python peut la
    supprimer en silence en cours d'exécution)."""
    t = asyncio.create_task(coro)
    _BACKGROUND.add(t)
    t.add_done_callback(_BACKGROUND.discard)
    return t


async def resilient(name: str, fn, *, delay_s: float = 2.0) -> None:
    """Relance une boucle de service après une erreur passagère (Redis lent, réseau…)
    au lieu de faire tomber tout le service. fn(first: bool) → coroutine."""
    first = True
    while True:
        try:
            await fn(first)
            return
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            log.exception("boucle %s interrompue : relance dans %.0f s", name, delay_s)
            first = False
            await asyncio.sleep(delay_s)


async def heartbeat_loop(bus: Bus, service: str, every_s: float = 10.0) -> None:
    while True:
        try:
            await bus.heartbeat(service)
        except Exception:  # noqa: BLE001
            log.exception("heartbeat")
        await asyncio.sleep(every_s)
