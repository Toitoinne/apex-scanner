"""Messages Telegram en langage simple."""
from apex.reporting import bulletin as BU
from apex.reporting.telegram import fmt_alert

TRADING = {"state": "APPRENTISSAGE", "stats": {"n": 16, "days": 0.11, "total_return": 0.0035, "profit_factor": 1.02},
           "checks": {"positions": [False, "16/100"], "duree": [False, "0.1/7"], "rendement": [False, "x"],
                      "profit_factor": [False, "x"], "jours_positifs": [True, "x"], "drawdown": [True, "x"],
                      "recent": [True, "x"], "sante": [True, "x"]}}
CRIT = {"min_positions": 100, "min_days": 7, "min_return": 0.10, "min_profit_factor": 1.3}


def sample(**kw):
    d = {"hours": 6, "tokens": 21300, "decisions": 57000, "labels": 450000, "alerts": 4,
         "top_alerts": [{"symbol": "PEPE", "best": 3.4}], "paper": {"n": 3, "wins": 2, "pnl_sol": 0.012, "ret": 0.04},
         "paper_total": {"n": 19, "pnl_sol": 0.017}, "errors": [{"type": "GAGNANT_MANQUE", "n": 1993}],
         "actions": [{"action": "revert_recent_correction", "status": "rolled_back"}], "new_models": 4,
         "skill": 0.33, "skill_prev": 0.29, "trading": TRADING, "criteria": CRIT, "problems": []}
    d.update(kw)
    return d


def test_bulletin_is_plain_and_complete():
    txt = BU.render(sample(), "point de 15 h")
    assert txt.startswith("💚 <b>APEX tourne normalement</b>")
    for s in ("21\u202f300 nouveaux tokens", "4 alertes", "$PEPE : jusqu'à x3,4", "2 gagnants", "33% moins",
              "↗ en progrès", "n'a pas signalé un token", "4 nouveaux modèles", "annulé (n'aidait pas)",
              "4/8 critères", "16/100 trades simulés", "Rien à faire"):
        assert s in txt, s
    for jargon in ("logloss", "bandit", "label", "champion", "PLATEAU", "lgbm"):
        assert jargon not in txt.lower(), jargon


def test_bulletin_reports_problems_and_quiet_periods():
    txt = BU.render(sample(problems=["L'apprentissage est ralenti ou arrêté"], alerts=0, top_alerts=[],
                           paper={"n": 0}, skill_prev=None), "point de 21 h")
    assert txt.startswith("🟠") and "L'apprentissage est ralenti" in txt and "Aucune alerte" in txt
    assert "Claude Code va regarder" in txt


def test_skill_and_bar():
    learner = {"ensembles": {"L60": {"champion": "lgbm_v1", "competitors": [
        {"id": "lgbm_v1", "logloss": 0.13}, {"id": "prior", "logloss": 0.2}]}}}
    assert abs(BU.skill_of(learner, "L60") - 0.35) < 1e-9
    assert BU.skill_of({}, "L60") is None
    assert BU.bar(4, 8) == "▓▓▓▓░░░░"


def test_alert_message_is_plain():
    a = {"mint": "Mint111", "name": "Pepe", "symbol": "PEPE", "point": "30", "mc_sol": 61.0, "sol_price_usd": 109,
         "p": 0.34, "scores": {"x2": 0.34, "x10": 0.021}, "policy_description": "tout vendre à x2",
         "reasons": [{"feature": "unique_buyers", "value": 40, "contribution": 0.8},
                     {"feature": "smart_share", "value": 0.2, "contribution": 0.3}], "flags": []}
    txt = fmt_alert(a)
    assert "Simulation : aucun achat réel" in txt and "30 s après son lancement" in txt
    assert "beaucoup d'acheteurs différents" in txt and "x2 en 1 h : 34%" in txt


def test_data_audit_verdict():
    from apex.reporting.data_audit import verdict
    good = {"fraicheur_s": 1.5, "prix_ecart_median_pct": 0.0, "prix_n": 6, "completude_pct": 100.0,
            "completude_vues": 787, "completude_total": 787, "pumpswap_ecart_median_pct": 0.3}
    assert verdict(good).startswith("✅") and "100 % des achats/ventes vus (787/787)" in verdict(good)
    assert verdict({**good, "completude_pct": 80.0}).startswith("⚠️")
    assert verdict({**good, "fraicheur_s": 120}).startswith("⚠️")


def test_bulletin_mentions_exit_learning():
    txt = BU.render(sample(exit_model={"situations_apprises": 450, "pret": False}), "point")
    assert "Quand revendre : en apprentissage (450 situations" in txt
    txt = BU.render(sample(exit_model={"situations_apprises": 5000, "pret": True, "fiabilite": 0.12}), "point")
    assert "il se trompe 12% moins qu'au hasard" in txt
