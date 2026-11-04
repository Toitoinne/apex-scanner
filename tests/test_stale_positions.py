"""Positions soldées dans le moteur mais restées « ouvertes » en base (04/11 : 2 positions > 26 h)."""
from apex.config import Config
from apex.labeler.engine import LabelerEngine
from apex.trading.exits import PolicyState


def _eng():
    return LabelerEngine(Config.load().data)


def test_closed_in_engine_but_open_in_db_gets_a_sync_closure():
    eng = _eng()
    pol = next(iter(eng.policies))
    st = PolicyState(pol, entry=1e-7, t0=1000.0, last_price=2e-7, last_t=1100.0, peak=2e-7)
    eng.reload_position("d1", "MINTpump", pol, st.to_dict(), "SYM")
    pos = eng.positions["d1"]
    pos.state.close_at_market(eng.policies[pol], 1200.0)        # soldée (ex. pendant le rejeu), signal jeté
    eng.tick(1000.0 + 200_000)
    assert eng.drain()[1] == []                                 # la limite de temps l'ignore : bloquée
    rows = [{"decision_id": "d1", "mint": "MINTpump", "symbol": "SYM", "policy": pol, "state": st.to_dict()}]
    sigs = eng.stale_open(rows, 300_000.0)
    assert len(sigs) == 1 and sigs[0]["closed"] and sigs[0]["kind"] == "SYNCHRO"
    assert abs(sigs[0]["multiple"] - 2.0) < 1e-9 and sigs[0]["pnl_after"] == pos.state.pnl()


def test_closed_state_reloaded_from_db_is_synchronised():
    eng = _eng()
    pol = next(iter(eng.policies))
    st = PolicyState(pol, entry=1e-7, t0=1000.0, last_price=0.5e-7, last_t=1100.0, peak=1e-7)
    st.close_at_market(eng.policies[pol], 1200.0)
    rows = [{"decision_id": "d2", "mint": "M2pump", "symbol": "", "policy": pol, "state": st.to_dict()}]
    sigs = eng.stale_open(rows, 300_000.0)
    assert [s["decision_id"] for s in sigs] == ["d2"]


def test_position_still_followed_is_left_to_the_time_limit():
    eng = _eng()
    pol = next(iter(eng.policies))
    st = PolicyState(pol, entry=1e-7, t0=1000.0, last_price=1e-7, last_t=1100.0, peak=1e-7)
    rows = [{"decision_id": "d3", "mint": "M3pump", "symbol": "", "policy": pol, "state": st.to_dict()},
            {"decision_id": "d4", "mint": "M4pump", "symbol": "", "policy": pol, "state": {}}]
    assert eng.stale_open(rows, 300_000.0) == []
    assert "d3" in eng.positions and "d4" not in eng.positions
