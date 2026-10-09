"""Panel de stratégies de sortie : nouvelles règles, coût par vente, choix efficace, classement honnête."""
from apex.alerting.bandit import AlertBandit
from apex.config import Config
from apex.events import Decision, TokenCreated, Trade
from apex.labeler.engine import LabelerEngine
from apex.reporting.exit_board import rank, render
from apex.trading.exits import Policy, PolicyState, build_panel


def test_panel_is_large_and_valid():
    pols = build_panel(Config.load()["exits"])
    assert len(pols) >= 40
    for name, c in pols.items():
        Policy.from_cfg(name, c)                      # chaque stratégie est lisible
    for keep in ("TP2_SL50", "RECUP_TRAIL40", "RECUP_TRAIL25", "ECHELLE", "MOONSHOT", "SORTIE_APPRISE", "SORTIE_APPRISE_LIBRE"):
        assert keep in pols                           # rien de l'existant ne disparaît


def test_time_stop_breakeven_and_sale_cost():
    t = Policy.from_cfg("T", {"stop_loss": 0.35, "take_profits": [[2.0, 0.5]], "time_stop_s": 300, "time_stop_mult": 0.1})
    s = PolicyState("T", 1.0, 0.0, fee=0.0)
    assert s.on_price(t, 100, 1.05) == []
    acts = s.on_price(t, 301, 1.05)                   # pas +10 % après 5 min : sortie au temps
    assert acts[0].kind == "TEMPS" and s.closed
    s = PolicyState("T", 1.0, 0.0, fee=0.0)
    assert s.on_price(t, 301, 1.2) == [] and not s.closed     # a assez monté : on garde
    b = Policy.from_cfg("B", {"stop_loss": 0.35, "take_profits": [[3.0, 1.0]], "breakeven_at": 1.5})
    s = PolicyState("B", 1.0, 0.0, fee=0.0)
    s.on_price(b, 10, 1.6)
    acts = s.on_price(b, 20, 0.99)                    # était à x1,6 : sortie au prix d'entrée, pas à −35 %
    assert acts[0].kind == "STOP" and abs(s.pnl() + 0.01) < 1e-9
    e = Policy.from_cfg("E", {"stop_loss": 0.5, "take_profits": [[1.5, 0.5], [2.0, 0.5]]})
    s = PolicyState("E", 1.0, 0.0, fee=0.0, sale_cost=0.005)
    s.on_price(e, 10, 2.0)                            # deux ventes : deux fois les frais de réseau
    assert s.closed and abs(s.pnl() - (2.0 - 1.0 - 0.01)) < 1e-9     # 2 ventes à x2, moins 2 × 0,5 %


def test_bandit_alert_rates_are_shared_per_slot():
    b = AlertBandit(scores_cfg={"x2": [0.1, 0.5]}, points=["10"], policies=[f"P{i}" for i in range(40)],
                    alerts_min=0, alerts_max=1000)
    for i in range(100):
        b.observe_decision("10", {"x2": 0.3}, float(i * 60))
    a1, a2 = b.arms["x2:0.1@10#P0"], b.arms["x2:0.1@10#P39"]
    assert b.apd(a1) == b.apd(a2) > 0                  # même créneau, quelle que soit la stratégie
    assert b.apd(b.arms["x2:0.5@10#P0"]) == 0
    b.observe_reward("10", {"x2": 0.3}, {"P0": 0.5, "P39": -0.2})
    assert b.arms["x2:0.1@10#P0"].mean() > 0 > b.arms["x2:0.1@10#P39"].mean()
    assert b.arms["x2:0.5@10#P0"].n == 0
    assert b.ready(min_observed_s=3600)


def _labeler():
    eng = LabelerEngine(Config.load().data)
    eng.on_event(TokenCreated(mint="M", name="m", symbol="M", uri="", creator="dev", bonding_curve="", slot=0,
                              ts=0.0, signature="c"))
    return eng


def test_only_alertable_decisions_are_simulated_and_dead_tokens_finalize_early():
    eng = _labeler()
    eng.on_event(Decision(decision_id="M:10", mint="M", point="10", ts=10.0, features={"unique_buyers": 2},
                          entry_price=1e-7, spot_price=1e-7, mc_sol=100, v_sol=30, v_tokens=3e8))
    eng.on_event(Decision(decision_id="M:15", mint="M", point="15", ts=15.0, features={"unique_buyers": 40},
                          entry_price=1e-7, spot_price=1e-7, mc_sol=100, v_sol=30, v_tokens=3e8))
    tr = eng.tracks["M"]
    assert set(tr.sims) == {"M:15"} and len(tr.sims["M:15"]) == len(eng.policies)
    eng.on_event(Trade(mint="M", signature="s", slot=0, ts=20.0, trader="w", is_buy=True, sol=0.3, tokens=1e6,
                       v_sol=0.9e-7 * 3e8, v_tokens=3e8))
    eng.tick(20.0 + 1801 + 60)                        # plus aucun échange depuis 30 min : simulations soldées
    outs, _ = eng.drain()
    assert [o.decision_id for o in outs] == ["M:15"] and not tr.sims


def test_board_is_honest():
    rows = [
        {"pol": "BON", "n": 2000, "mean": 0.08, "sd": 0.5, "med": 0.0, "win": 0.4, "h1": 0.07, "h2": 0.09},
        {"pol": "TP2_SL50", "n": 2000, "mean": -0.05, "sd": 0.5, "med": -0.1, "win": 0.2, "h1": -0.05, "h2": -0.05},
        {"pol": "CHANCE", "n": 30, "mean": 0.3, "sd": 2.0, "med": -0.1, "win": 0.1, "h1": 0.9, "h2": -0.3},
        {"pol": "MAUVAIS", "n": 2000, "mean": -0.2, "sd": 0.3, "med": -0.2, "win": 0.1, "h1": -0.2, "h2": -0.2},
    ]
    r = {x["pol"]: x for x in rank(rows, top_k=2)}
    assert r["BON"]["verdict"] == "solide"
    assert r["CHANCE"]["verdict"] == "pas prouvée"       # en tête par chance : 30 cas, instable
    assert r["MAUVAIS"]["verdict"] == "perd"
    txt = render({"selection": rank(rows, top_k=2), "toutes": rows, "heures": 48})
    assert "1 stratégie(s) gagnent de façon prouvée" in txt and "+8,0% par trade" in txt


def test_sorties_message_with_stored_board():
    """/sorties : le classement relu depuis Redis contient des nombres sous forme de texte (ex. win)."""
    from apex.reporting import exit_board as EB
    r = {"pol": "TP2_SL50", "description": "x", "rang": 1, "n": 100, "mean": -0.1, "lo": "-0.12", "hi": -0.08,
         "win": "0.17662973460448338057", "h1": None, "h2": -0.1, "verdict": "perd"}
    msg = EB.render({"selection": [r], "toutes": [r], "heures": 48, "terrains": {"mig60": [r]}})
    assert "18% gagnants" in msg
