"""Concours des modèles (12/10) : modèle de base « capitalisation », nouveaux LightGBM jamais éjectés à
l'arrivée, ménage des jumeaux de test orphelins."""
import asyncio
import random

from apex.config import Config
from apex.learning.ensemble import HorizonEnsemble
from apex.learning.models import Competitor, CompetitorSpec
from apex.supervision.service import Supervisor


def test_mcbase_batch_model_is_monotone_in_market_cap():
    from apex.learning.models import train_mcbase
    c = Competitor(CompetitorSpec(id="mcbase", kind="mcbase"))
    assert not c.ready and c.predict({"mc_sol": 50, "age_s": 30})[0] == 0.5     # pas encore entraîné
    rng = random.Random(0)
    rows = []
    for _ in range(6000):
        mc, age = rng.uniform(25, 300), rng.choice([10, 60, 300, 600])
        rows.append(({"mc_sol": mc, "age_s": age}, int(rng.random() < (mc / 400) ** 2 + 0.02), 1.0))
    c.booster = train_mcbase(rows)
    assert c.ready
    for age in (10, 60, 300, 600):              # à âge égal : jamais moins bien noté avec une capitalisation plus forte
        ps = [c.predict_raw({"mc_sol": mc, "age_s": age}) for mc in range(25, 300, 5)]
        assert all(b >= a - 1e-9 for a, b in zip(ps, ps[1:])) and ps[-1] > ps[0] + 0.1
    c.learn({"mc_sol": 50, "age_s": 30}, 1, 1.0)                              # pas d'apprentissage en ligne
    assert set(c.filter_x({"mc_sol": 50, "age_s": 10, "autre": 1})) == {"lmc", "lage", "lmc_lage"}


def test_new_competitor_survives_full_ensemble():
    mcfg = {**Config.load().data["models"], "max_competitors": 8, "min_samples_for_champion": 10}
    ens = HorizonEnsemble("L60", mcfg)
    for i in range(3):                                   # places occupées par des jumeaux de test protégés
        ens.add(CompetitorSpec(id=f"shadow_c{i}", kind="logreg", shadow_of_correction=i))
    new = ens.add(CompetitorSpec(id="lgbm_v1", kind="lgbm"))
    assert "lgbm_v1" in ens.competitors                  # avant : « perte infinie » → éjecté à la seconde
    old = ens.add(CompetitorSpec(id="lgbm_v0", kind="lgbm"))
    for _ in range(20):
        old.evaluate(0.9, 0)                             # évalué et mauvais : c'est lui qui part
    ens.add(CompetitorSpec(id="lgbm_v2", kind="lgbm"))
    assert "lgbm_v0" not in ens.competitors and {"lgbm_v1", "lgbm_v2"} <= set(ens.competitors)
    assert new is ens.competitors["lgbm_v1"]


class _DB:
    def __init__(self):
        self.events = []

    async def fetch(self, q, ids):
        return [{"id": 32, "status": "degradation"}, {"id": 200, "status": "evaluating"}]

    async def log_event(self, *a):
        self.events.append(a)


def test_orphan_shadows_are_removed():
    sup = Supervisor.__new__(Supervisor)
    sent = []
    state = {"ensembles": {"L60": {"competitors": [
        {"id": "shadow_c32", "spec": {"shadow_of_correction": 32}},
        {"id": "shadow_c32_ctl", "spec": {"shadow_of_correction": 32}},
        {"id": "shadow_c200", "spec": {"shadow_of_correction": 200}},
        {"id": "shadow_c146", "spec": {"shadow_of_correction": None}},    # adopté : gardé
        {"id": "rules", "spec": {}}]}}}

    async def learner_state():
        return state

    async def send(cmd, timeout=120):
        sent.append(cmd["id"])
        return {"ok": True}

    sup.learner_state, sup.send, sup.db = learner_state, send, _DB()
    out = asyncio.run(sup.gc_shadows())
    assert sorted(sent) == ["shadow_c32", "shadow_c32_ctl"] and out
