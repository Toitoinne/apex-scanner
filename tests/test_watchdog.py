from apex.config import Config
from apex.events import Decision, Label
from apex.ingestor.watchdog import FlowWatchdog, overlaps_gap
from apex.learning.core import Learner


def wd():
    return FlowWatchdog(log_sources=["ws0", "ws1"], stall_s=30, backup_after_s=15, release_after_s=300, outage_alert_s=60)


def test_redundant_sources_keep_flow_healthy():
    w = wd()
    for s in ("ws0", "ws1"):
        w.on_connect(s, 0)
    for t in range(0, 100, 5):
        w.on_message("ws1", t)
        w.on_event("ws1", t, False)          # ws0 est mort, ws1 suffit
        a = w.evaluate(t, backup_allowed=True)
        assert a.gap_opened is None and not a.start_backup
    assert "ws0" in w.evaluate(100, True).reconnect   # mais ws0 est relancé


def test_outage_opens_gap_starts_backup_alerts_then_recovers():
    w = wd()
    w.on_connect("ws0", 0)
    w.on_event("ws0", 10, False)
    assert w.evaluate(20, True).gap_opened is None
    a = w.evaluate(40, True)                  # plus rien depuis 30 s
    assert a.gap_opened == 10
    assert a.start_backup                     # déjà 30 s sans événement → secours Helius immédiat
    assert w.evaluate(75, True).alert        # 60 s de panne → alerte immédiate
    w.on_event("helius", 110, False)
    a = w.evaluate(111, True)
    assert a.gap_closed == (10, 111) and a.recovered
    # le secours reste actif tant que les sources gratuites ne sont pas saines depuis 5 min
    for t in range(115, 420, 5):
        w.on_event("ws0", t, False)
        w.on_event("helius", t, False)
        a = w.evaluate(t, True)
        if a.stop_backup:
            break
    assert a.stop_backup and t >= 115 + 300


def test_backup_not_started_when_credits_exhausted():
    w = wd()
    w.on_event("ws0", 0, False)
    alerts = []
    for t in range(25, 200, 5):
        a = w.evaluate(t, backup_allowed=False)
        assert not a.start_backup
        alerts += [a.alert] if a.alert else []
    assert len(alerts) == 1 and "indisponible" in alerts[0]   # une seule alerte, explicite


def test_crosscheck_with_pumpportal_detects_silent_breakage():
    w = wd()
    for t in range(0, 60):
        w.on_event("ws0", t, False)               # des messages, mais aucune création vue
        if t % 10 == 0:
            w.on_event("pumpportal", t, True)     # PumpPortal voit des lancements
    a = w.evaluate(60, True)
    assert a.gap_opened is not None


def test_gap_overlap():
    gaps = [(100.0, 200.0)]
    assert overlaps_gap(gaps, 150, 160)
    assert overlaps_gap(gaps, 0, 101)
    assert not overlaps_gap(gaps, 0, 90)
    assert not overlaps_gap(gaps, 210, 300)
    assert overlaps_gap([(100.0, float("inf"))], 500, 600)


def test_learner_never_alerts_nor_learns_on_data_gap():
    cfg = Config.load()
    cfg.base["bandit"]["warmup_labels"] = 0
    cfg.base["bandit"]["min_observed_s"] = 0
    L = Learner(cfg)
    L.bandit.recenter(0.0, 0.31)
    L.bandit.active = "x2:0.3@30#RECUP_TRAIL40"
    hot = {"velocity_mc_per_min": 500, "buy_sell_ratio_vol": 5, "unique_buyers": 60, "ret_last_30s": 1.0}
    L.gaps = [(1000.0, 1010.0)]
    d = Decision(decision_id="m:30", mint="m", point="30", ts=1030.0, features=hot, entry_price=1, spot_price=1,
                 mc_sol=30, v_sol=30, v_tokens=1e9, meta={"t0": 1000.0})
    _, alert = L.on_decision(d)
    assert alert is None and "m:30" not in L.cache
    # décision saine, mais le trou arrive pendant l'horizon du label : pas d'apprentissage
    L.gaps = []
    d2 = Decision(decision_id="n:30", mint="n", point="30", ts=2030.0, features=hot, entry_price=1, spot_price=1,
                  mc_sol=30, v_sol=30, v_tokens=1e9, meta={"t0": 2000.0})
    _, alert = L.on_decision(d2)
    assert alert is not None
    L.gaps = [(2500.0, 2800.0)]          # 5 min : au-delà de la tolérance L60 (120 s)
    n_before = L.ensembles["L60"].competitors["arf"].n_learned
    L.on_label(Label(decision_id="n:30", mint="n", point="30", horizon="L60", y=1, ts=5700.0, max_return=1,
                     max_drawdown=0, time_to_peak_s=10, rug=False, final_return=1, sim_pnl=1))
    assert L.ensembles["L60"].competitors["arf"].n_learned == n_before
    assert L.skipped_gap == 2


def test_short_gaps_tolerated_on_long_horizons():
    gaps = [(1000.0, 1048.0)]                                  # coupure de 48 s
    assert overlaps_gap(gaps, 900, 4500)                       # sans tolérance : écarté
    assert not overlaps_gap(gaps, 900, 4500, min_len=120)      # L60 : toléré
    assert overlaps_gap([(1000.0, 1300.0)], 900, 4500, min_len=120)   # 5 min : écarté
    assert overlaps_gap([(1000.0, float("inf"))], 900, 4500, min_len=120)
