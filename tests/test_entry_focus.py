"""Audit de l'entrée et apprentissage centré sur les décisions alertables."""
import numpy as np

from apex.config import Config
from apex.learning.core import Learner
from apex.learning.models import Competitor, CompetitorSpec
from apex.reporting.entry_audit import auc, calibration, feature_report, render


def test_auc_feature_report_and_calibration():
    rng = np.random.default_rng(0)
    y = (rng.random(4000) < 0.1).astype(float)
    good = y * 1.0 + rng.normal(0, 0.7, 4000)            # indice utile
    noise = rng.normal(0, 1, 4000)                        # indice sans effet
    inverse = -good                                       # utile, sens inverse
    half = np.arange(4000) >= 2000
    assert 0.7 < auc(good, y) <= 1 and abs(auc(noise, y) - 0.5) < 0.05 and auc(inverse, y) < 0.3
    rep = {r["feature"]: r for r in feature_report({"good": good, "noise": noise, "inverse": inverse}, y, half)}
    assert rep["good"]["stable"] and rep["good"]["lift_top10"] > 2
    assert rep["inverse"]["stable"] and rep["inverse"]["pouvoir"] == rep["good"]["pouvoir"]
    assert rep["noise"]["pouvoir"] < 0.05
    p = np.clip(y * 0.5 + 0.05, 0, 1)                     # surconfiant sur les positifs
    rows, ece = calibration(p, y)
    assert rows[0]["annonce"] >= rows[-1]["annonce"] and ece >= 0
    txt = render({"n": 4000, "heures": 48, "taux_base": 0.1, "modele_auc": 0.75, "calibration": rows,
                  "features": list(rep.values()), "inutiles": ["noise"]})
    assert "Qualité de tri du bot : 0,75" in txt


def test_non_alertable_decisions_do_not_drive_champion_or_calibration():
    c = Competitor(CompetitorSpec(id="logreg", kind="logreg", params={"lr": 0.01}))
    c.evaluate(0.9, 0, focus=False)
    assert len(c.losses) == 0 and c.n_evaluated == 0
    c.evaluate(0.9, 0, focus=True)
    assert len(c.losses) == 1


def test_lgbm_rows_downweight_non_alertable():
    lr = Learner(Config.load())
    ens = lr.ensembles[lr.primary]
    ens.replay.append(({"unique_buyers": 40}, 1, 2.0, 0.0))
    ens.replay.append(({"unique_buyers": 2}, 0, 2.0, 0.0))
    rows = lr.lgbm_rows(lr.primary)
    assert rows[-2][2] == 2.0 and abs(rows[-1][2] - 0.4) < 1e-9
