"""Redémarrages : rejouer la même journée avec un arrêt au milieu doit donner EXACTEMENT les mêmes
résultats que sans arrêt. C'est la famille de bugs la plus fréquente (trades rouverts et réécrits,
décisions prises en retard, positions orphelines) : on la verrouille ici."""
from apex.config import Config
from apex.events import Decision, TokenCreated, Trade
from apex.features.engine import FeatureEngine
from apex.features.market import MarketContext
from apex.features.wallets import WalletIntel
from apex.labeler.engine import LabelerEngine
from apex.safety.filters import SafetyFilters

K = 30.0 * 1.073e9          # produit constant de la bonding curve (réserves virtuelles)


def _trade(mint: str, t: float, mult: float, i: int, buy: bool = True) -> Trade:
    p = 30.0 / 1.073e9 * mult
    v_sol = (K * p) ** 0.5
    return Trade(mint=mint, signature=f"{mint}{i}", slot=0, ts=t, trader=f"w{i % 37}", is_buy=buy, sol=0.3,
                 tokens=1e6, v_sol=v_sol, v_tokens=K / v_sol)


def _day() -> list:
    """Deux tokens alertés : M monte à x1,7 puis s'effondre AVANT l'arrêt ; N s'effondre APRÈS l'arrêt."""
    ev: list = []
    for mint, t0, crash in (("M", 0.0, 70.0), ("N", 100.0, 300.0)):
        ev.append(TokenCreated(mint=mint, name=mint, symbol=mint, uri="", creator="dev", bonding_curve="", slot=0,
                               ts=t0, signature=f"c{mint}"))
        mult = 1.2
        for i in range(250):
            t = t0 + 2 + i * 2
            if t < crash:
                mult = min(1.7, mult * 1.01)
            elif t < crash + 6:
                mult *= 0.6
            ev.append(_trade(mint, t, mult, i, buy=t < crash))
        d_t = t0 + 10
        ev.append(Decision(decision_id=f"{mint}:10", mint=mint, point="10", ts=d_t, features={"unique_buyers": 40},
                           entry_price=30.0 / 1.073e9 * 1.25, spot_price=30.0 / 1.073e9 * 1.22, mc_sol=40,
                           v_sol=33, v_tokens=9.7e8))
        ev.append({"decision_id": f"{mint}:10", "mint": mint, "policy": "TP2_SL50", "ts": d_t + 1, "symbol": mint,
                   "entry_price": 30.0 / 1.073e9 * 1.25})
    return sorted(ev, key=lambda e: e["ts"] if isinstance(e, dict) else e.ts)


def _feed(eng: LabelerEngine, events: list) -> None:
    for ev in events:
        if isinstance(ev, dict):
            eng.open_position(ev)
        else:
            eng.on_event(ev)


def _sig(signals: list) -> list:
    return [(s["decision_id"], s["kind"], s["closed"], round(s["multiple"], 4)) for s in signals]


def test_labeler_restart_gives_same_sell_signals():
    cfg = Config.load().data
    events = _day()
    a = LabelerEngine(cfg)
    _feed(a, events)
    _, ref = a.drain()
    T = 200.0
    before = [e for e in events if (e["ts"] if isinstance(e, dict) else e.ts) <= T]
    after = [e for e in events if (e["ts"] if isinstance(e, dict) else e.ts) > T]
    b = LabelerEngine(cfg)
    _feed(b, before)
    _, sent_before = b.drain()
    done = {s["decision_id"] for s in sent_before if s["closed"]}
    c = LabelerEngine(cfg)                         # redémarrage : nouvel état, rejeu de l'historique
    c.restore(before, T, done)
    assert "M:10" not in c.positions               # trade terminé avant l'arrêt : jamais rouvert
    assert "N:10" in c.positions                   # trade en cours : repris
    _feed(c, after)
    _, sent_after = c.drain()
    assert _sig(sent_before) + _sig(sent_after) == _sig(ref)
    assert not any(s["kind"] == "TEMPS" for s in sent_after)    # aucune clôture fabriquée au prix d'achat


def _fe() -> FeatureEngine:
    cfg = Config.load()
    c = cfg.data
    return FeatureEngine(c, SafetyFilters(cfg.safety), WalletIntel(c["features"]), MarketContext(c["features"]["narrative_window_s"]))


def _market_events() -> list:
    ev: list = []
    for k, t0 in enumerate((0.0, 150.0, 290.0)):
        m = f"T{k}"
        ev.append(TokenCreated(mint=m, name=m, symbol=m, uri="", creator="dev", bonding_curve="", slot=0, ts=t0,
                               signature=f"c{m}"))
        ev += [_trade(m, t0 + 1 + i, 1.0 + i / 200, i) for i in range(700)]
    return sorted(ev, key=lambda e: e.ts)


def _run(eng: FeatureEngine, events: list, t_from: int, t_to: int) -> list:
    out, i = [], 0
    events = list(events)
    for t in range(t_from, t_to + 1):
        while i < len(events) and events[i].ts <= t:
            eng.on_event(events[i])
            i += 1
        out += [(d.decision_id, d.ts) for d in eng.tick(float(t))]
    return out


def test_feature_restart_no_duplicate_nor_late_decision():
    events = _market_events()
    ref = _run(_fe(), events, 0, 1100)
    T = 400
    b = _fe()
    first = _run(b, [e for e in events if e.ts <= T], 0, T)
    c = _fe()                                      # redémarrage : rejeu de l'historique, puis points échus marqués
    for e in [e for e in events if e.ts <= T]:
        c.on_event(e)
    c.mark_elapsed(float(T))
    second = _run(c, [e for e in events if e.ts > T], T + 1, 1100)
    ids = [d for d, _ in first + second]
    assert len(ids) == len(set(ids))               # aucune décision en double
    assert sorted(ids) == sorted(d for d, _ in ref)  # ni perdue
    planned = {f"T{k}": t0 for k, t0 in enumerate((0.0, 150.0, 290.0))}
    for did, ts in first + second:                 # aucune décision prise en retard
        mint, pt = did.split(":")
        if pt.isdigit():
            assert ts - (planned[mint] + int(pt)) <= 240
