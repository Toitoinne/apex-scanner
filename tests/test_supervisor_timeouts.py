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


def test_cycle_records_step_durations():
    """Le cycle dure ~45 s depuis le 11/10 : chaque étape est chronométrée pour trouver la lente."""
    saved = {}

    class Bus(FakeBus):
        async def set_json(self, key, val):
            saved[key] = val

    class DB(FakeDB):
        async def executemany(self, *a):
            return None

        async def log_event(self, *a):
            return None

    class Det:
        async def detect(self, primary):
            return []

    sup = Supervisor.__new__(Supervisor)
    sup.bus, sup.db, sup.detector, sup.primary = Bus(), DB(), Det(), "L15"

    async def curves():
        return {}

    async def none():
        return None

    async def empty(*a):
        return []

    async def slow():
        await asyncio.sleep(0.05)
        return []

    async def send(cmd):
        return {"ok": True}
    sup.record_curves, sup.self_reference = curves, none
    sup.guardrails = sup.correct = empty
    sup.learning_health = sup.trading_gate = sup.gc_shadows = lambda: empty()
    sup.evaluate_corrections = slow
    sup.send = send
    summary = asyncio.run(sup.cycle())
    steps = summary["steps_s"]
    assert set(steps) >= {"record_curves", "detect", "evaluate_corrections", "correct", "snapshot"}
    assert max(steps, key=steps.get) == "evaluate_corrections"
    assert saved["apex:supervisor:last_cycle"]["steps_s"] == steps
