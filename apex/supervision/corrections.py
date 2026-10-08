"""Catalogue de corrections automatiques (10.4).

Chaque action produit : le composant visé, les commandes à envoyer au learner,
la commande inverse (annulation), la courbe cible pour mesurer l'effet, et si
elle passe par un concurrent ombre (promotion uniquement si l'ombre fait mieux).
"""
from __future__ import annotations

import random
from dataclasses import dataclass, field
from typing import Any

from .detector import DECALIBRATION, DRIFT, REGRESSION_TYPE, THRESHOLD_LOOSE, THRESHOLD_STRICT
from .stats import PLATEAU, REGRESSION


@dataclass
class Plan:
    action: str
    component: str
    commands: list[dict]
    undo: list[dict]
    target_curve: str
    shadow_id: str | None = None
    params_before: dict = field(default_factory=dict)
    params_after: dict = field(default_factory=dict)
    needs_claude: bool = False
    rollback_stable: bool = False


CANDIDATES: dict[str, list[str]] = {
    PLATEAU: ["add_diverse_competitor", "request_claude_features", "early_lgbm_retrain"],
    REGRESSION: ["revert_recent_correction", "rollback_stable"],
    REGRESSION_TYPE: ["upweight_error_type", "request_claude_targeted_feature"],
    DECALIBRATION: ["switch_calibration"],
    DRIFT: ["shorten_window", "early_lgbm_retrain", "disable_drifted_feature"],
    THRESHOLD_LOOSE: ["bandit_recenter_up"],
    THRESHOLD_STRICT: ["bandit_recenter_down"],
}


def candidates_for(state: str, causes: dict) -> list[str]:
    cands = list(CANDIDATES.get(state, []))
    if "revert_recent_correction" in cands and not causes.get("recent_corrections"):
        cands.remove("revert_recent_correction")
    if "disable_drifted_feature" in cands and not causes.get("features_changed") and not causes.get("drifted"):
        cands.remove("disable_drifted_feature")
    return cands


def build_plan(action: str, state: str, curve: str, causes: dict, learner_state: dict, cfg: Any, corr_id: int, primary: str) -> Plan | None:
    ens = learner_state["ensembles"][primary]
    champion = ens["champion"]
    sid = f"shadow_c{corr_id}"
    tag = {"horizon": primary}
    lo, hi = learner_state["bandit"]["range"]
    if action == "add_diverse_competitor":
        kind = random.choice(["arf", "hat", "logreg"])
        params = {"arf": {"n_models": random.choice([15, 25]), "lambda_value": random.choice([3, 6, 10]), "max_features": random.choice(["sqrt", 0.5])},
                  "hat": {"grace_period": random.choice([50, 100, 400]), "delta": random.choice([1e-4, 1e-6])},
                  "logreg": {"lr": random.choice([0.003, 0.03]), "l2": random.choice([0.0, 1e-3])}}[kind]
        spec = {"id": sid, "kind": kind, "params": params, "shadow_of_correction": corr_id}
        return Plan(action, f"model:{primary}", [{"op": "add_competitor", "spec": spec, "warm_start": True, **tag}],
                    [{"op": "remove_competitor", "id": sid, **tag}], f"logloss:{primary}", shadow_id=sid,
                    params_after={"spec": spec})
    if action == "early_lgbm_retrain":
        window = int(cfg.get("models.lgbm_window") * (0.5 if state == DRIFT else 1.0))
        return Plan(action, f"lgbm:{primary}", [{"op": "retrain_lgbm", "window": window, **tag}], [],
                    f"logloss:{primary}", params_before={"window": cfg.get("models.lgbm_window")}, params_after={"window": window})
    if action in ("request_claude_features", "request_claude_targeted_feature"):
        return Plan(action, "claude", [], [], curve, needs_claude=True)
    if action == "rollback_stable":
        return Plan(action, "global", [], [], "error_rate", rollback_stable=True)
    if action == "revert_recent_correction":
        return Plan(action, "global", [], [], "error_rate", params_after={"revert": causes["recent_corrections"][0]["id"]})
    if action == "upweight_error_type":
        etype = curve.split(":", 1)[1]
        base_w = dict(cfg.get("models.error_weights"))
        new_w = dict(base_w)
        new_w[etype] = round(base_w.get(etype, 1.5) * 2, 2)
        return Plan(action, f"model:{primary}",
                    [{"op": "shadow_of_champion", "new_id": sid, "correction_id": corr_id, "changes": {"error_weights": new_w}, **tag}],
                    [{"op": "remove_competitor", "id": sid, **tag}], f"logloss:{primary}", shadow_id=sid,
                    params_before={"error_weights": base_w}, params_after={"error_weights": new_w, "type": etype})
    if action == "switch_calibration":
        cur = cfg.get("calibration.method")
        new = "isotonic" if cur == "platt" else "platt"
        return Plan(action, "calibration", [{"op": "set_override", "key": "calibration.method", "value": new}],
                    [{"op": "set_override", "key": "calibration.method", "value": cur}], "ece",
                    params_before={"method": cur}, params_after={"method": new})
    if action == "shorten_window":
        win = 5000
        return Plan(action, f"model:{primary}",
                    [{"op": "shadow_of_champion", "new_id": sid, "correction_id": corr_id, "changes": {"replay_window": win}, **tag}],
                    [{"op": "remove_competitor", "id": sid, **tag}], f"logloss:{primary}", shadow_id=sid,
                    params_after={"replay_window": win})
    if action == "disable_drifted_feature":
        drifted = causes.get("features_changed") or causes.get("drifted") or {}
        if not drifted:
            return None
        feat = max(drifted, key=drifted.get)
        cur_dis = next((c["spec"]["disabled_features"] for c in ens["competitors"] if c["id"] == champion), [])
        return Plan(action, f"model:{primary}",
                    [{"op": "shadow_of_champion", "new_id": sid, "correction_id": corr_id,
                      "changes": {"disabled_features": sorted(set(cur_dis) | {feat})}, **tag}],
                    [{"op": "remove_competitor", "id": sid, **tag}], f"logloss:{primary}", shadow_id=sid,
                    params_after={"disabled_feature": feat})
    if action == "bandit_recenter_up":
        nlo = min(0.8, round(lo + 0.1, 2)) if lo > 0 else 0.5
        return Plan(action, "bandit", [{"op": "bandit_range", "lo": nlo, "hi": 1.0}],
                    [{"op": "bandit_range", "lo": lo, "hi": hi}], "alert_precision",
                    params_before={"range": [lo, hi]}, params_after={"range": [nlo, 1.0]})
    if action == "bandit_recenter_down":
        nhi = max(0.3, round(hi - 0.15, 2)) if hi < 1 else 0.6
        return Plan(action, "bandit", [{"op": "bandit_range", "lo": 0.0, "hi": nhi}],
                    [{"op": "bandit_range", "lo": lo, "hi": hi}], "error_rate:GAGNANT_MANQUE",
                    params_before={"range": [lo, hi]}, params_after={"range": [0.0, nhi]})
    return None
