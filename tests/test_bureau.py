"""Page « bureau » du dashboard : l'API répond même quand des données manquent (démarrage, panne d'un service)."""
import asyncio

from apex.config import Config
from apex.dashboard import app as A


class _Bus:
    def __init__(self, data):
        self.data = data

    async def get_json(self, k, default=None):
        return self.data.get(k, default)

    async def heartbeats(self):
        return {"ingestor": 0.0, "learner": 0.0}


class _DB:
    async def fetch(self, q, *a):
        return []

    async def fetchrow(self, q, *a):
        return None


def test_bureau_api_with_partial_data():
    A.state.update(cfg=Config.load(), db=_DB(), bus=_Bus({
        "apex:ingestor:stats": {"events": 100, "late": 0},
        "apex:learner:state": {"ensembles": {"L60": {"champion": "mcbase", "competitors": [
            {"id": "mcbase", "logloss": 0.19}, {"id": "prior", "logloss": 0.22}]}}, "lgbm_version": 300},
        "apex:trading:status": {"state": "APPRENTISSAGE", "checks": {"a": [True, ""], "b": [False, ""]}}}))
    A.state.pop("bureau_prev", None)
    d = asyncio.run(A.bureau(None))
    assert d["chercheur"]["champion"] == "mcbase" and 0.1 < d["chercheur"]["gain_vs_hasard"] < 0.2
    assert d["trader"]["criteres_ok"] == 1 and d["trader"]["criteres"] == 2
    assert d["facteur"]["dernieres"] == [] and d["fil"] == [] and d["guetteur"]["par_min"] is None
    A.state["bus"] = _Bus({})                                   # tout vide : pas de plantage
    assert asyncio.run(A.bureau(None))["chercheur"]["champion"] is None


def test_bureau_page_exists():
    html = (A.STATIC / "bureau.html").read_text(encoding="utf-8")
    assert "/api/bureau" in html and "token" in html
