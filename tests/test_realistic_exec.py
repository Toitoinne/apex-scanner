"""Exécution réaliste (16/10) : une vente simulée passe au 1er prix vu ~3 s après le signal, un achat aussi.
SQUEEZE : signal « gros dump » à x1,03, cascade de ventes → −59 % au simulateur d'ordres, +0,8 % à l'entraînement."""
from apex.trading.exits import Policy, PolicyState

P = Policy.from_cfg("T", {"stop_loss": 0.5, "take_profits": [[2.0, 0.5]], "trail": 0.15, "trail_activate": 1.0,
                          "time_limit_s": 3600})


def test_sell_fills_after_delay_at_crashed_price():
    st = PolicyState("T", 1.0, 0.0, fee=0.0, latency_s=3.0, entry_latency_s=3.0)
    assert not st.entered and st.on_price(P, 1.0, 1.0) == []          # achat pas encore passé
    st.on_price(P, 3.5, 1.10)                                           # achat exécuté à 1,10
    assert st.entered and st.entry == 1.10
    out = st.on_price(P, 10.0, 1.13, danger="GROS_DUMP")               # signal : vente lancée, pas encore passée
    assert out == [] and st.pending and not st.closed
    assert st.on_price(P, 11.0, 0.70) == []                              # cascade, délai pas écoulé
    out = st.on_price(P, 13.5, 0.47)                                     # 1er prix après 3 s : vente exécutée
    assert [a.kind for a in out] == ["DANGER"] and st.closed
    assert abs(st.pnl() - (0.47 / 1.10 - 1)) < 1e-9                     # perte réelle, pas le prix du signal


def test_no_double_sell_while_order_pending_and_instant_mode_unchanged():
    st = PolicyState("T", 1.0, 0.0, fee=0.0, latency_s=3.0)
    st.on_price(P, 1.0, 1.0)
    st.on_price(P, 2.0, 0.80)                                            # stop suiveur −15 % : vente lancée
    st.on_price(P, 3.0, 0.40)                                            # stop −50 % : déjà en cours, rien de plus
    assert len(st.pending) == 1 and abs(st.available()) < 1e-12
    st.on_price(P, 5.5, 0.35)
    assert st.closed and abs(st.realized - 0.35) < 1e-9
    inst = PolicyState("T", 1.0, 0.0, fee=0.0)                          # sans délai : comportement d'avant
    inst.on_price(P, 1.0, 1.0)
    assert [a.kind for a in inst.on_price(P, 2.0, 0.80)] == ["STOP_SUIVEUR"] and inst.closed


def test_state_with_pending_survives_restart_and_dead_token():
    st = PolicyState("T", 1.0, 0.0, fee=0.0, latency_s=3.0, entry_latency_s=3.0)
    st.on_price(P, 4.0, 1.0)
    st.on_price(P, 5.0, 0.8)                                             # vente en cours
    st2 = PolicyState.from_dict(st.to_dict())                           # redémarrage
    assert st2.entered and st2.pending
    out = st2.close_at_market(P, 2000.0)                                # plus aucun échange : soldé au dernier prix
    assert out and st2.closed and abs(st2.realized - 0.8) < 1e-9
    never = PolicyState("T", 1.0, 0.0, fee=0.0, entry_latency_s=3.0)
    never.on_price(P, 1.0, 1.2)                                          # aucun prix après le délai
    never.close_at_market(P, 2000.0)
    assert never.closed and abs(never.pnl()) < 1e-9                     # achat au dernier prix : ni gain ni perte
