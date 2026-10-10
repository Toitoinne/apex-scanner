"""Une coupure passagère de Redis ne doit perdre ni label, ni récompense, ni vente (et ne rien doubler)."""
import asyncio

from apex import bus as B
from apex.config import Config
from apex.events import Label, Outcome
from apex.labeler.service import LabelerService


class FlakyBus:
    def __init__(self):
        self.sent: list[tuple[str, object]] = []
        self.fail_after: int | None = None     # nombre de publications réussies avant la panne

    async def publish(self, stream, ev, maxlen=None):
        if self.fail_after is not None:
            if self.fail_after <= 0:
                raise TimeoutError("Timeout connecting to server")
            self.fail_after -= 1
        self.sent.append((stream, ev))


class FakeDB:
    def __init__(self):
        self.labels = 0
        self.outcomes = 0
        self.updates = 0

    async def executemany(self, sql, rows):
        if "INTO labels" in sql:
            self.labels += len(rows)
        else:
            self.outcomes += len(rows)

    async def execute(self, sql, *args):
        self.updates += 1


def _label(i):
    return Label(decision_id=f"M{i}:30", mint=f"M{i}", point="30", horizon="L2", y=0, ts=1.0, max_return=0.1,
                 max_drawdown=-0.1, time_to_peak_s=5.0, rug=False, final_return=0.0, sim_pnl=-0.1)


def _outcome(i):
    return Outcome(decision_id=f"M{i}:30", mint=f"M{i}", point="30", ts=1.0, pnl={"TP2_SL50": -0.1},
                   max_return=0.1, reached={})


def test_coupure_redis_rien_perdu_rien_double():
    async def run():
        bus, db = FlakyBus(), FakeDB()
        svc = LabelerService(Config.load(), bus, db)
        sig = {"decision_id": "M9:30", "t": 2.0, "kind": "STOP", "fraction": 1.0, "multiple": 0.5,
               "pnl_after": -0.5, "closed": True}
        bus.fail_after = 3 + 2 + 1         # la panne tombe entre l'envoi du signal de vente et son message Telegram
        try:
            await svc.publish([_label(i) for i in range(3)], [{"mint": "M1"}], [], [_outcome(i) for i in range(2)], [sig])
        except TimeoutError:
            pass
        bus.fail_after = None              # Redis revient
        await svc.publish([_label(3)], [], [], [], [])
        streams = [s for s, _ in bus.sent]
        assert streams.count(B.LABELS) == 4
        assert streams.count(B.OUTCOMES) == 2
        assert streams.count(B.SIGNALS) == 1     # la vente n'est pas renvoyée deux fois au système d'ordres
        assert streams.count(B.NOTIFY) == 1
        assert streams.count(B.CLOSED) == 1
        assert db.labels == 4 and db.outcomes == 2
        assert db.updates == 1                   # la vente n'est pas inscrite deux fois
        assert all(not d for d in svc._q.values())
    asyncio.run(run())
