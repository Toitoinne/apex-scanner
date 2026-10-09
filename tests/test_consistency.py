"""Vérification automatique de cohérence : recalcul d'un trade à partir de ses ventes, doublons."""
from apex.reporting.consistency import recompute, summarize


def test_recompute_from_fills():
    pnl, dbl = recompute([{"fraction": 0.5, "multiple": 2.0}, {"fraction": 0.5, "multiple": 1.0}], 0.0, 0.0)
    assert abs(pnl - 0.5) < 1e-9 and not dbl
    pnl, dbl = recompute([{"fraction": 1.0, "multiple": 0.5}, {"fraction": 1.0, "multiple": 1.0}], 0.02, 0.005)
    assert dbl and abs(pnl - (0.5 * 0.98 - 0.005 - 1)) < 1e-9      # la 2e vente (rejeu) est ignorée et signalée
    assert recompute([{"fraction": 0.5, "multiple": 2.0}], 0.0, 0.0)[0] is None    # position pas encore soldée


def test_summary_is_plain():
    ok = {k: {"ok": True, "n": 0, "part": 0.0} for k in
          ("calcul", "doublons", "prix_ventes", "simulateur", "bloquees", "impossibles", "achats_rates")}
    probs, res = summarize(ok)
    assert not probs and res.startswith("✅")
    bad = {**ok, "doublons": {"ok": False, "n": 2}, "prix_ventes": {"ok": False, "n": 9, "part": 0.3}}
    probs, res = summarize(bad)
    assert len(probs) == 2 and "vendus deux fois" in res and "30%" in res
