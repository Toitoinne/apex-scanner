"""Smart wallets v2 : gains encaissés, robots / snipers / flippers / créateurs écartés."""
from apex.config import Config
from apex.events import TokenCreated, Trade
from apex.features.wallets import classify
from apex.labeler.engine import LabelerEngine

CFG = {"smart_wallet_min_trades": 8, "smart_wallet_min_winrate": 0.4, "smart_wallet_max_tokens_per_day": 60,
       "bot_tokens_per_day": 200}
NOW = 10 * 86400.0


def st(n, wins, pnl, flips=0, snipes=0, days=2):
    return {"n_trades": n, "n_wins": wins, "pnl": pnl, "fast_flips": flips, "snipes": snipes, "first": NOW - days * 86400}


def test_classification():
    assert classify(st(20, 12, 5.0), NOW, CFG) == (True, False)          # sélectif, gagnant : smart
    assert classify(st(20, 12, -1.0), NOW, CFG)[0] is False               # perdant au total
    assert classify(st(5, 4, 5.0), NOW, CFG)[0] is False                  # pas assez de recul
    assert classify(st(2000, 1100, 500.0), NOW, CFG) == (False, True)     # achète tout : robot
    assert classify(st(267, 261, 300.0, days=2), NOW, CFG) == (False, True)   # 98 % de réussite : irréaliste
    assert classify(st(30, 18, 5.0, flips=25), NOW, CFG) == (False, True)     # revend en quelques secondes
    assert classify(st(30, 18, 5.0, snipes=25), NOW, CFG) == (False, True)    # sniper de la 1re seconde


def test_summary_counts_only_realized_gains_and_excludes_creator():
    eng = LabelerEngine(Config.load().data)
    eng.on_event(TokenCreated(mint="M", name="m", symbol="M", uri="", creator="dev", bonding_curve="", slot=100,
                              ts=0.0, signature="c"))

    def tr(t, w, buy, sol, tok, slot=200):
        eng.on_event(Trade(mint="M", signature=f"{w}{t}", slot=slot, ts=float(t), trader=w, is_buy=buy, sol=sol,
                           tokens=tok, v_sol=40.0, v_tokens=8e8))
    tr(1, "dev", True, 5.0, 1e8, slot=100)        # créateur : exclu
    tr(2, "sniper", True, 1.0, 3e7, slot=101)     # achète dans le slot de création (+1)
    tr(3, "holder", True, 2.0, 5e7)               # n'a rien revendu : pas jugé (gain non encaissé)
    tr(4, "trader", True, 1.0, 2e7)
    tr(100, "trader", False, 3.0, 2e7)            # revend tout : +2 SOL encaissés
    tr(5, "sniper", False, 1.5, 3e7)              # revend en 3 s : flipper + sniper
    s = eng._summary(eng.tracks["M"])
    assert set(s["wallets"]) == {"trader", "sniper"}
    assert abs(s["wallets"]["trader"] - 2.0) < 1e-9 and abs(s["wallets"]["sniper"] - 0.5) < 1e-9
    assert s["snipers"] == ["sniper"] and s["fast_flippers"] == ["sniper"]
