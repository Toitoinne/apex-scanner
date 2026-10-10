"""Superviseur : une tâche bloquée (requête PostgreSQL sans fin) ne doit plus figer toute la boucle
(constaté le 10/10 : plus aucun cycle pendant des heures, santé et aptitude jamais remises à jour)."""
import asyncio

from apex.supervision import service as S
from apex.supervision.service import Supervisor, bounded


async def _hang():
    await asyncio.sleep(3600)


def test_bounded_abandons_hanging_task():
    assert asyncio.run(bounded("bloquée", _hang(), 0.05)) is None
    assert asyncio.run(bounded("ok", asyncio.sleep(0, result=42), 1)) == 42


def test_bounded_swallows_errors():
    async def boom():
        raise RuntimeError("x")
    assert asyncio.run(bounded("erreur", boom(), 1)) is None


class FakeR:
    async def set(self, *a, **k):
        return None


class FakeBus:
    r = FakeR()

    async def get_json(self, key, default=None):
        return default

    async def set_json(self, key, val):
        return None


class FakeDB:
    async def fetchval(self, *a):
        return None


class FakeCfg:
    def get(self, key, default=None):
        return 10 ** 12


def test_loop_survives_hanging_tasks(monkeypatch):
    monkeypatch.setattr(S, "TASK_TIMEOUT_S", 0.05)
    monkeypatch.setattr(S.B, "spawn", lambda coro: coro.close())
    sup = Supervisor.__new__(Supervisor)
    sup.bus, sup.db, sup.cfg, sup.improver = FakeBus(), FakeDB(), FakeCfg(), None
    sup.s = {"cycle_s": 0.01}
    done = []

    async def noop():
        return None

    async def board():
        done.append("board")
    sup.load_meta = noop
    cycles = []

    def cycle():
        cycles.append(1)
        return _hang()
    sup.cycle = cycle                      # cycle bloqué
    sup.bulletin_if_due = noop
    sup.consistency_check = noop
    sup.exit_board = board
    monkeypatch.setattr(S.EA, "run", lambda *a: _hang())   # audit de l'entrée bloqué

    async def go():
        try:
            await asyncio.wait_for(sup.run(), 1.0)
        except asyncio.TimeoutError:
            pass
    asyncio.run(go())
    # malgré le cycle et l'audit bloqués, la boucle continue et tourne plusieurs fois
    assert done == ["board"]
    assert len(cycles) >= 2
