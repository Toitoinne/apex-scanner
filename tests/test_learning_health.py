"""Santé de l'apprentissage : un redémarrage du learner (compteur de récompenses revenu en arrière
depuis un snapshot) ne doit pas déclencher l'alerte « aucune récompense depuis ~1 h »."""
import asyncio

from apex.supervision import service as S
from apex.supervision.service import Supervisor


class FakeR:
    def __init__(self):
        self.starts = []

    async def lrange(self, key, a, b):
        return [str(x).encode() for x in self.starts]


class FakeBus:
    def __init__(self):
        self.state = {}
        self.saved = {}
        self.r = FakeR()

    async def get_json(self, key, default=None):
        return self.state if key == "apex:learner:state" else default

    async def set_json(self, key, val):
        self.saved[key] = val


class FakeDB:
    async def fetchval(self, sql, *a):
        return 1000


def _sup():
    sup = Supervisor.__new__(Supervisor)
    sup.bus, sup.db = FakeBus(), FakeDB()
    sup.sent = []

    async def notify_now(msg):
        sup.sent.append(msg)
    sup.notify_now = notify_now
    return sup


def _run(sup, clock, monkeypatch, t, n_outcomes):
    clock[0] = t
    sup.bus.state = {"n_outcomes": n_outcomes, "last_decision_ts": t, "last_label_ts": t}
    return asyncio.run(sup.learning_health())


def test_restart_counter_reset_no_false_alarm(monkeypatch):
    clock = [0.0]
    monkeypatch.setattr(S.time, "time", lambda: clock[0])
    sup = _sup()
    assert _run(sup, clock, monkeypatch, 10_000, 1500) == []
    # le learner redémarre depuis un snapshot plus ancien (compteur 400) puis reçoit des récompenses
    assert _run(sup, clock, monkeypatch, 10_900, 400) == []
    assert _run(sup, clock, monkeypatch, 13_100, 1200) == []
    assert not sup.sent


def test_real_stall_still_alerts(monkeypatch):
    clock = [0.0]
    monkeypatch.setattr(S.time, "time", lambda: clock[0])
    sup = _sup()
    assert _run(sup, clock, monkeypatch, 10_000, 1500) == []
    out = _run(sup, clock, monkeypatch, 13_100, 1500)
    assert any("aucune récompense" in p for p in out)
    assert sup.bus.saved["apex:learning:health"]["ok"] is False


def test_learner_restart_loop_alerts(monkeypatch):
    """13/10 : learner tué 11 fois en 3 h faute de mémoire, sans alerte (signaux de vie normaux entre deux arrêts)."""
    clock = [0.0]
    monkeypatch.setattr(S.time, "time", lambda: clock[0])
    sup = _sup()
    sup.bus.r.starts = [9_000.0, 8_000.0]
    assert _run(sup, clock, monkeypatch, 10_000, 1500) == []            # 2 démarrages en 1 h : un déploiement, normal
    sup.bus.r.starts = [9_900.0, 9_000.0, 8_000.0, 4_000.0]
    out = _run(sup, clock, monkeypatch, 10_000, 1500)
    assert any("redémarré 3 fois" in p for p in out) and sup.sent
