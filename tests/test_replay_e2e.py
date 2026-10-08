"""Bout-en-bout : tokens synthétiques → features → filtres → labeler → learner → bandit,
avec l'horloge simulée du rejeu (exactement le chemin de production, sans E/S)."""
import random

import pytest

from apex.backfill.replay import Replay
from apex.config import Config
from apex.events import Migration, TokenCreated, Trade


def synth_events(n_tokens=160, seed=1):
    rng = random.Random(seed)
    evs = []
    t = 1_700_000_000.0
    for i in range(n_tokens):
        t += rng.uniform(5, 40)
        mint = f"Mint{i:05d}"
        creator = f"Dev{rng.randint(0, 40)}"
        evs.append(TokenCreated(mint=mint, name=f"tok {i}", symbol=f"T{i}", uri="", creator=creator,
                                bonding_curve="", slot=0, ts=t, signature=f"c{i}"))
        kind = rng.choice(["pump", "dump", "flat", "rug"])
        vs, vt = 30.0, 1_073_000_000.0
        k = vs * vt
        ts = t
        n_tr = rng.randint(40, 160)
        for j in range(n_tr):
            ts += rng.uniform(2, 40)
            if kind == "pump":
                buy = rng.random() < 0.75
            elif kind == "dump":
                buy = rng.random() < 0.35
            elif kind == "rug":
                buy = j < n_tr * 0.6 or rng.random() < 0.1
            else:
                buy = rng.random() < 0.5
            sol = rng.uniform(0.1, 2.0)
            if kind == "rug" and j == int(n_tr * 0.6):
                sol = vs * 0.95       # le dev vend tout
                buy = False
            if buy:
                nvs = vs + sol
                nvt = k / nvs
                tokens = vt - nvt
            else:
                sol = min(sol, vs - 30.5) if vs > 31 else 0.05
                nvs = max(30.01, vs - sol)
                nvt = k / nvs
                tokens = nvt - vt
            vs, vt = nvs, nvt
            trader = creator if (kind == "rug" and j == int(n_tr * 0.6)) else f"W{rng.randint(0, 300)}"
            evs.append(Trade(mint=mint, signature=f"s{i}_{j}", slot=0, ts=ts, trader=trader, is_buy=buy,
                             sol=abs(sol), tokens=abs(tokens), v_sol=vs, v_tokens=vt))
        if kind == "pump" and vs > 80:
            evs.append(Migration(mint=mint, slot=0, ts=ts + 1, signature=f"m{i}"))
    evs.sort(key=lambda e: e.ts)
    return evs


def test_replay_end_to_end(tmp_path):
    cfg = Config.load()
    cfg.base["models"]["min_samples_for_champion"] = 50
    cfg.base["models"]["lgbm_min_samples"] = 100
    rp = Replay(cfg, lgbm=True)
    ref = rp.run(synth_events())
    L = rp.learner
    assert rp.n_decisions > 300
    assert len(rp.log) > 300                       # labels injectés et évalués
    ens = L.ensembles["L60"]
    assert all(c.n_evaluated > 0 for c in ens.competitors.values() if c.spec.kind != "lgbm")
    assert ens.competitors["arf"].n_learned > 100
    assert any(c.spec.kind == "lgbm" for c in ens.competitors.values())
    assert "error_rate" in ref and 0 <= ref["error_rate"]["value"] <= 1
    assert L.bandit.active is not None
    # la boucle « argent » tourne : chaque décision alertable a récompensé les bras via les stratégies simulées
    rewarded = [a for a in L.bandit.arms.values() if a.n > 0]
    assert rewarded and {a.policy for a in rewarded} == set(L.bandit.policies)
    assert "X10_24H" in L.ensembles and L.ensembles["X10_24H"].champion_id == "prior"

    # commandes de correction : ombre du champion avec poids modifiés, puis promotion
    res = L.apply_command({"op": "shadow_of_champion", "new_id": "shadow_c1", "correction_id": 1,
                           "changes": {"error_weights": {"RUG_ALERTE": 8.0}}, "horizon": "L60"})
    assert res["ok"] and "shadow_c1" in ens.competitors
    assert ens.competitors["shadow_c1"].n_learned > 0          # démarrage à chaud sur le buffer
    assert L.apply_command({"op": "promote", "id": "shadow_c1", "horizon": "L60"})["ok"]
    assert ens.champion_id == "shadow_c1"
    with pytest.raises(PermissionError):   # garde-fou : l'ingestion n'est jamais modifiable automatiquement
        L.apply_command({"op": "set_override", "key": "ingestion.sources", "value": []})

    # snapshot / restauration (reprise après crash)
    p = tmp_path / "snap.pkl"
    L.snapshot(p)
    before = L.ensembles["L60"].champion_id
    L.apply_command({"op": "promote", "id": "arf", "horizon": "L60"})
    L.restore(p, keep_runtime=True)
    assert L.ensembles["L60"].champion_id == before
