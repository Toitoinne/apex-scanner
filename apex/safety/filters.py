"""FILTRES DE SÉCURITÉ DURS — appliqués avant tout modèle, jamais désappris.

Les seuils viennent de safety.yaml (lecture seule). Aucune API n'existe pour les
modifier à chaud : la boucle 2 peut seulement produire une *proposition*
(`propose_change`) qui apparaît dans le rapport.
"""
from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Mapping


@dataclass(frozen=True)
class SafetyResult:
    blocked: bool
    flags: tuple[str, ...]


class SafetyFilters:
    def __init__(self, cfg: Mapping):
        self._cfg = MappingProxyType(dict(cfg))   # figé

    @property
    def cfg(self) -> Mapping:
        return self._cfg

    def check(self, f: Mapping[str, float]) -> SafetyResult:
        c = self._cfg
        flags: list[str] = []
        blocking: list[str] = []

        def hit(name: str, block: bool = True) -> None:
            flags.append(name)
            if block:
                blocking.append(name)

        if f.get("creation_slot_supply_pct", 0) > c["bundle_creation_slot_supply_pct"]:
            hit("BUNDLE_MASSIF")
        if f.get("mint_authority") == 1 and c.get("mint_authority_active") == "block":
            hit("MINT_AUTHORITY_ACTIVE")
        if f.get("freeze_authority") == 1 and c.get("freeze_authority_active") == "block":
            hit("FREEZE_AUTHORITY_ACTIVE")
        n_tok = f.get("dev_n_tokens", 0)
        n_rugs = f.get("dev_rug_rate", 0) * n_tok
        if n_rugs >= c["dev_rug_count_max"] or (n_tok >= 3 and f.get("dev_rug_rate", 0) > c["dev_rug_rate_max"]):
            hit("DEV_RUGGER_RECIDIVISTE")
        if f.get("top10_concentration", 0) > c["top10_concentration_max"]:
            hit("CONCENTRATION_EXTREME")
        if f.get("dev_holding_pct", 0) > c["dev_holding_max"]:
            hit("DEV_DETIENT_TROP")
        # drapeaux informatifs (non bloquants), affichés dans les alertes
        if f.get("dev_sold_pct", 0) > 0.5:
            hit("DEV_A_VENDU", block=False)
        if f.get("sniper_share", 0) > 0.4:
            hit("SNIPERS_DOMINANTS", block=False)
        if f.get("wash_score", 0) > 0.3:
            hit("WASH_TRADING", block=False)
        if f.get("bot_share", 0) > 0.5:
            hit("BOTS_DOMINANTS", block=False)
        if "mint_authority" not in f:
            hit("AUTORITES_NON_VERIFIEES", block=False)
        return SafetyResult(blocked=bool(blocking), flags=tuple(flags))


def propose_change(key: str, current: float, suggested: float, evidence: str) -> dict:
    """Proposition destinée au rapport humain ; n'est JAMAIS appliquée automatiquement."""
    return {"key": key, "current": current, "suggested": suggested, "evidence": evidence, "applied": False}
