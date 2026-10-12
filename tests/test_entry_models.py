"""Concours des modèles (12/10) : modèle de base « capitalisation », nouveaux LightGBM jamais éjectés à
l'arrivée, ménage des jumeaux de test orphelins."""
import asyncio
import random

from apex.config import Config
from apex.learning.ensemble import HorizonEnsemble
from apex.learning.models import Competitor, CompetitorSpec
from apex.supervision.service import Supervisor


def test_mcbase_learns_market_cap_ranking():
    c = Competitor(CompetitorSpec(id="mcbase", kind="mcbase", params={"lr": 0.05}))
    rng = random.Random(0)
    for _ in range(4000):
        mc = rng.uniform(25, 300)
        x = {"mc_sol": mc, "age_s": 300, "bruit": rng.random()}
        c.learn(x, int(rng.random() < mc / 400), 1.0)
    lo = c.predict({"mc_sol": 30, "age_s": 300})[0]
    hi = c.predict({"mc_sol": 250, "age_s": 300})[0]
    assert hi > lo + 0.2
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
