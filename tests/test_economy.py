"""Mode économe sans perte de performance : enrichissement ciblé, groupes d'opérateurs
gratuits, budget de crédits, verrou de démarrage."""
from apex.config import Config
from apex.events import Decision
from apex.features.wallets import WalletIntel
from apex.learning.core import Learner


def test_operator_groups_learned_for_free_from_same_slot_buys():
    intel = WalletIntel({})
    buyers = ["A", "B", "C", "D"]
    assert intel.independent_clusters(buyers) == 4
    # A et B achètent dans le même slot sur un seul token : pas encore un groupe
    intel.update_from_closed({"mint": "m1", "wallets": {}, "slot_groups": [["A", "B"]]})
    assert intel.independent_clusters(buyers) == 4
    # ... puis sur un deuxième token : même opérateur
    intel.update_from_closed({"mint": "m2", "wallets": {}, "slot_groups": [["A", "B", "C"]]})
    assert intel.independent_clusters(buyers) == 3
    intel.update_from_closed({"mint": "m3", "wallets": {}, "slot_groups": [["B", "C"]]})
    assert intel.independent_clusters(buyers) == 2


def test_daily_credit_budget_is_enforced():
    intel = WalletIntel({"enrichment_daily_credit_budget": 3, "enrichment_hourly_burst": 24})
    assert [intel.spend() for _ in range(5)] == [True, True, True, False, False]


def test_budget_is_paced_over_the_day():
    # 2 400 crédits/jour, burst 1,5 → 150 max par heure : le quota ne peut pas partir en 1 h
    intel = WalletIntel({"enrichment_daily_credit_budget": 2400, "enrichment_hourly_burst": 1.5})
    spent = sum(intel.spend() for _ in range(1000))
    assert spent == 150
    assert not intel.budget_left_this_hour()


def test_priority_queue_serves_best_candidates_first():
    intel = WalletIntel({}, rpc_url="http://rpc")
    intel.request_funding("faible", priority=0.2)
    intel.request_funding("modele", priority=1.7)
    intel.request_funding("moyen", priority=0.5)
    order = [intel._queue.get_nowait()[2] for _ in range(3)]
    assert order == ["modele", "moyen", "faible"]


def test_buyer_quality_does_not_trigger_rpc_lookups():
    intel = WalletIntel({}, rpc_url="http://rpc")
    intel.buyer_quality(["A", "B"], 0.0)
    assert intel._queue.qsize() == 0          # seul l'enrichissement ciblé déclenche des appels
    intel.request_funding("A")
    assert intel._queue.qsize() == 1


def _decision(i: int, p_feats: dict) -> Decision:
    return Decision(decision_id=f"m{i}:30", mint=f"m{i}", point="30", ts=1000.0 + i, features=p_feats,
                    entry_price=1.0, spot_price=1.0, mc_sol=30.0, v_sol=30.0, v_tokens=1e9)


def test_no_alert_before_warmup():
    cfg = Config.load()
    cfg.base["bandit"]["warmup_labels"] = 10
    cfg.base["bandit"]["min_observed_s"] = 0
    L = Learner(cfg)
    L.bandit.recenter(0.0, 0.31)
    L.bandit.active = "x2:0.3@30#RECUP_TRAIL40"
    hot = {"velocity_mc_per_min": 500, "buy_sell_ratio_vol": 5, "unique_buyers": 60, "ret_last_30s": 1.0}
    _, alert = L.on_decision(_decision(1, hot))
    assert alert is None                      # modèle pas encore assez entraîné
    L.ensembles["L60"].champion.n_evaluated = 10
    _, alert = L.on_decision(_decision(2, hot))
    assert alert is not None


def test_untrained_models_are_not_scored_on_blind_predictions():
    cfg = Config.load()
    L = Learner(cfg)
    ens = L.ensembles["L60"]
    x = {"velocity_mc_per_min": 1.0}
    preds = ens.predict_all(x)
    assert "rules" in preds                   # champion de départ : toujours présent
    assert "arf" not in preds and "logreg" not in preds   # pas encore entraînés : pas notés
    for _ in range(60):
        ens.evaluate_and_learn(x, 0, {}, {}, 0.0)
    assert "arf" in ens.predict_all(x)


def test_pool_cap_blocks_enrichment():
    intel = WalletIntel({"enrichment_daily_credit_budget": 1000, "enrichment_hourly_burst": 24})
    assert intel.spend()
    intel.pool_blocked = True
    assert not intel.spend()


def test_prior_competitor_tracks_base_rate():
    cfg = Config.load()
    ens = Learner(cfg).ensembles["L60"]
    for i in range(2000):
        ens.evaluate_and_learn({}, int(i % 50 == 0), {}, {}, 0.0)   # 2 % de positifs
    assert 0.015 < ens.competitors["prior"].prior_rate() < 0.03


def test_no_alert_on_illiquid_token():
    cfg = Config.load()
    cfg.base["bandit"]["warmup_labels"] = 0
    cfg.base["bandit"]["min_observed_s"] = 0
    L = Learner(cfg)
    L.bandit.recenter(0.0, 0.31)
    L.bandit.active = "x2:0.3@30#RECUP_TRAIL40"
    hot = {"velocity_mc_per_min": 500, "buy_sell_ratio_vol": 5, "ret_last_30s": 1.0, "unique_buyers": 6}
    assert L.on_decision(_decision(1, hot))[1] is None          # 6 acheteurs : pas d'alerte
    hot["unique_buyers"] = 60
    assert L.on_decision(_decision(2, hot))[1] is not None


def test_base_models_recreated_warm_and_lgbm_versions_kept():
    cfg = Config.load()
    L = Learner(cfg)
    ens = L.ensembles["L60"]
    for i in range(300):
        ens.evaluate_and_learn({"a": float(i % 7)}, int(i % 20 == 0), {}, {}, 0.0)
    ens.competitors.pop("arf")
    ens.ensure_defaults()
    assert ens.competitors["arf"].n_learned == 300          # recréé à chaud sur le buffer
    for v in range(5):
        L.install_lgbm("L60", None, [])
    lgbms = sorted(k for k, c in ens.competitors.items() if c.spec.kind == "lgbm")
    assert lgbms == ["lgbm_v3", "lgbm_v4", "lgbm_v5"]       # les 3 dernières versions sont gardées


def test_restore_applies_current_model_config(tmp_path):
    cfg = Config.load()
    L = Learner(cfg)
    L.ensembles["L60"].mcfg = {**L.ensembles["L60"].mcfg, "max_competitors": 3}   # ancienne config figée
    p = tmp_path / "s.pkl"
    L.snapshot(p)
    L2 = Learner(Config.load())
    L2.restore(p)
    assert L2.ensembles["L60"].mcfg["max_competitors"] == cfg.get("models.max_competitors")


def test_claude_feature_tested_against_twin_control():
    cfg = Config.load()
    L = Learner(cfg)
    ens = L.ensembles["L60"]
    for i in range(200):
        ens.evaluate_and_learn({"a": float(i % 5)}, int(i % 25 == 0), {}, {}, 0.0)
    res = L.apply_command({"op": "enable_claude_feature", "feature_id": "cx_test", "new_id": "shadow_c99",
                           "correction_id": 99, "horizon": "L60"})
    assert res["ok"] and res["control"] == "shadow_c99_ctl"
    a, b = ens.competitors["shadow_c99"], ens.competitors["shadow_c99_ctl"]
    assert a.n_learned == b.n_learned == 200                 # même point de départ
    assert "cx_test" in a.spec.extra_features and "cx_test" not in b.spec.extra_features
