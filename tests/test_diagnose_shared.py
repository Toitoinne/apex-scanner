"""Superviseur : le diagnostic commun (requêtes lourdes) n'est calculé qu'une fois par cycle,
au lieu d'une fois par état anormal (cycle de 50–80 s depuis le 11/10, étape « correct » ~25 s)."""
import asyncio

from apex.supervision.detector import PLATEAU, REGRESSION, Detected, Detector


class FakeMetrics:
    def __init__(self):
        self.calls = 0

    async def feature_samples(self, *a):
        self.calls += 1
        return []


class FakeDB:
    def __init__(self):
        self.fetch_calls = []

    async def fetch(self, sql, *a):
        self.fetch_calls.append(sql)
        if "FROM corrections" in sql:
            return [{"id": len(self.fetch_calls), "action": "x", "component": "c", "ts": None}]
        if "GROUP BY error_type" in sql:
            return [{"error_type": "GAGNANT_MANQUE", "r_recent": 0.2, "r_before": 0.1}]
        return []

    async def fetchrow(self, sql, *a):
        return None


def _detector():
    det = Detector.__new__(Detector)
    det.db, det.m, det.c = FakeDB(), FakeMetrics(), {"drift_ks_alpha": 0.01}
    return det


def test_diagnose_shares_heavy_queries_within_a_cycle():
    det = _detector()
    shared = {}
    states = [Detected(PLATEAU, "error_rate", p_value=0.5), Detected(REGRESSION, "logloss:L60", p_value=0.01),
              Detected(PLATEAU, "logloss:L60", p_value=0.2)]
    results = [asyncio.run(det.diagnose(d, "L60", shared)) for d in states]
    assert det.m.calls == 2                       # recent + healthy, une seule fois
    heavy = [s for s in det.db.fetch_calls if "FROM evaluations" in s]
    assert len(heavy) == 2                        # types d'erreur + modèles, une seule fois
    # les corrections récentes restent relues à chaque état
    assert len([s for s in det.db.fetch_calls if "FROM corrections" in s]) == 3
    # même contenu qu'un diagnostic sans cache, et copies indépendantes
    ref = asyncio.run(_detector().diagnose(states[1], "L60"))
    assert results[1][1] == ref[1]
    assert results[1][0]["error_types_delta"] == ref[0]["error_types_delta"]
    results[0][0]["error_types_delta"]["X"] = 1
    assert "X" not in results[2][0]["error_types_delta"]
