"""Journal des erreurs : classement automatique de chaque prédiction fausse.

Types de base : RUG_ALERTE, BUNDLE_RATE, ENTREE_TARDIVE, FAUX_SMART_MONEY,
MORT_LENTE, GAGNANT_MANQUE, GAGNANT_DETECTE_TROP_TARD, AUTRE.
Des types supplémentaires (créés par Claude via la boucle 2) sont décrits par une
règle DSL sûre (pas de code) et évalués quand aucun type de base ne s'applique.
"""
from __future__ import annotations

import operator
from dataclasses import dataclass
from typing import Any, Mapping

BASE_TYPES = [
    "RUG_ALERTE", "BUNDLE_RATE", "ENTREE_TARDIVE", "FAUX_SMART_MONEY", "MORT_LENTE",
    "GAGNANT_MANQUE", "GAGNANT_DETECTE_TROP_TARD", "AUTRE",
]
DISPLAY = {
    "RUG_ALERTE": "RUG_ALERTÉ", "BUNDLE_RATE": "BUNDLE_RATÉ", "ENTREE_TARDIVE": "ENTRÉE_TARDIVE",
    "FAUX_SMART_MONEY": "FAUX_SMART_MONEY", "MORT_LENTE": "MORT_LENTE", "GAGNANT_MANQUE": "GAGNANT_MANQUÉ",
    "GAGNANT_DETECTE_TROP_TARD": "GAGNANT_DÉTECTÉ_TROP_TARD", "AUTRE": "AUTRE",
}
FALSE_POSITIVE_TYPES = {"RUG_ALERTE", "BUNDLE_RATE", "ENTREE_TARDIVE", "FAUX_SMART_MONEY", "MORT_LENTE"}
FALSE_NEGATIVE_TYPES = {"GAGNANT_MANQUE", "GAGNANT_DETECTE_TROP_TARD"}

OPS = {">": operator.gt, ">=": operator.ge, "<": operator.lt, "<=": operator.le, "==": operator.eq, "!=": operator.ne}


@dataclass(frozen=True)
class ErrorRecord:
    error_type: str
    cost: float
    false_positive: bool


@dataclass(frozen=True)
class DynamicRule:
    name: str
    predicted: int | None          # 1 = faux positif, 0 = faux négatif, None = les deux
    conditions: tuple[tuple[str, str, str, float], ...]   # (source, clé, op, valeur) ; source ∈ {feature, outcome}

    @classmethod
    def from_json(cls, name: str, rule: Mapping[str, Any]) -> "DynamicRule":
        conds = []
        for c in rule.get("conditions", []):
            src = "outcome" if "outcome" in c else "feature"
            key = c.get("outcome") or c.get("feature")
            if c["op"] not in OPS or not isinstance(key, str):
                raise ValueError(f"condition invalide : {c}")
            conds.append((src, key, c["op"], float(c["value"])))
        if not conds:
            raise ValueError("règle vide")
        pred = rule.get("predicted")
        return cls(name=name, predicted=None if pred is None else int(pred), conditions=tuple(conds))

    def matches(self, predicted: int, features: Mapping[str, float], out: Mapping[str, float]) -> bool:
        if self.predicted is not None and self.predicted != predicted:
            return False
        for src, key, op, val in self.conditions:
            pool = out if src == "outcome" else features
            if key not in pool or not OPS[op](float(pool[key]), val):
                return False
        return True


def classify(
    predicted: int,
    y: int,
    features: Mapping[str, float],
    out: Mapping[str, float],          # max_return, max_drawdown, rug, sim_pnl, final_return...
    later_positive: bool,
    cfg: Mapping[str, Any],
    dynamic_rules: list[DynamicRule] | tuple = (),
) -> ErrorRecord | None:
    """Retourne None si la prédiction est correcte."""
    if predicted == y:
        return None
    notional = cfg.get("cost_notional_sol", 1.0)
    pnl = float(out.get("sim_pnl", 0.0))
    if predicted == 1:
        cost = max(0.0, -pnl) * notional
        if out.get("rug"):
            t = "RUG_ALERTE"
        elif features.get("creation_slot_supply_pct", 0) >= cfg.get("bundle_slot_supply_pct", 0.15) \
                or features.get("sniper_share", 0) >= 0.5:
            t = "BUNDLE_RATE"
        elif features.get("mult_since_launch", 1) >= cfg.get("late_entry_runup", 3.0) \
                and out.get("max_return", 0) < cfg.get("late_entry_max_return", 0.10):
            t = "ENTREE_TARDIVE"
        elif features.get("smart_share", 0) >= cfg.get("fake_smart_share", 0.2):
            t = "FAUX_SMART_MONEY"
        elif out.get("max_drawdown", 0) >= cfg.get("slow_death_min_dd", 0.3):
            t = "MORT_LENTE"
        else:
            t = "AUTRE"
        fp = True
    else:
        cost = max(0.0, pnl) * notional
        t = "GAGNANT_DETECTE_TROP_TARD" if later_positive else "GAGNANT_MANQUE"
        fp = False
    if t in ("AUTRE",) or (dynamic_rules and t == "GAGNANT_MANQUE"):
        for rule in dynamic_rules:
            if rule.matches(predicted, features, out):
                t = rule.name
                break
    return ErrorRecord(error_type=t, cost=cost, false_positive=fp)
