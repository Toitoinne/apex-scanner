"""ÉVOLUTION des stratégies de sortie (cœur pur, testé) : les réglages apprennent.

Toutes les 12 h, à partir du classement (apex/reporting/exit_board.py) :
  1. les meilleures stratégies (assez de cas) donnent naissance à des VARIANTES aux réglages
     légèrement modifiés (objectif, stop, stop suiveur, délai, seuil de la sortie apprise…) ;
  2. les variantes qui perdent de façon prouvée ou restent en bas du classement sont retirées ;
  3. la population de variantes est plafonnée.
Les stratégies de base (config + panel) ne sont JAMAIS retirées : elles servent de référence stable.
Chaque variante est simulée sur toutes les décisions alertables et jugée sur ses résultats réels.
"""
from __future__ import annotations

import random
from typing import Any

# (clé, borne basse, borne haute) des réglages numériques que l'on fait varier
BOUNDS = {
    "stop_loss": (0.1, 0.6), "trail": (0.08, 0.6), "trail_activate": (1.0, 5.0), "time_stop_s": (60, 1800),
    "time_stop_mult": (0.03, 0.6), "hold_threshold": (0.1, 0.7), "breakeven_at": (1.1, 2.5), "min_hold_s": (10, 300),
}
LABELS = {"stop_loss": "stop", "trail": "stop suiveur", "trail_activate": "suiveur dès", "time_stop_s": "délai",
          "time_stop_mult": "hausse exigée", "hold_threshold": "seuil de sortie apprise", "breakeven_at": "sécurise dès",
          "min_hold_s": "attente minimale"}


def readable(k: str, v: float) -> str:
    """Réglage en langage simple (pour les descriptions affichées sur Telegram)."""
    txt = {"stop_loss": f"stop −{v:.0%}", "trail": f"stop suiveur −{v:.0%}", "trail_activate": f"suiveur dès x{v:.2f}",
           "time_stop_s": f"délai {v / 60:.0f} min", "time_stop_mult": f"hausse exigée +{v:.0%}",
           "hold_threshold": f"vend si chances de hausse < {v:.0%}", "breakeven_at": f"sécurise la mise dès x{v:.2f}",
           "min_hold_s": f"attend au moins {v:.0f} s"}[k]
    return txt.replace(".", ",")


def _clip(k: str, v: float) -> float:
    lo, hi = BOUNDS[k]
    return round(min(hi, max(lo, v)), 3 if k not in ("time_stop_s", "min_hold_s") else 0)


def mutate(cfg: dict, rng: random.Random, strength: float = 0.25) -> tuple[dict, list[str]]:
    """Variante : 1 à 3 réglages modifiés de ±`strength` (multiplicatif). Retourne (cfg, changements lisibles)."""
    c = {k: (list(map(list, v)) if k == "take_profits" else v) for k, v in cfg.items()}
    keys = [k for k in BOUNDS if c.get(k)]
    if c.get("take_profits"):
        keys.append("take_profits")
    changes = []
    for k in rng.sample(keys, k=min(len(keys), rng.randint(1, 3))):
        f = rng.uniform(1 - strength, 1 + strength)
        if k == "take_profits":
            i = rng.randrange(len(c["take_profits"]))
            m = round(max(1.1, c["take_profits"][i][0] * f), 2)
            c["take_profits"][i][0] = m
            c["take_profits"].sort(key=lambda x: x[0])
            changes.append(f"palier à x{m:g}".replace(".", ","))
        else:
            c[k] = _clip(k, c[k] * f)
            changes.append(readable(k, c[k]))
    return c, changes


def evolve(board: list[dict], base: dict[str, dict], evolved: dict[str, dict], gen: int, rng: random.Random,
           cap: int = 12, parents: int = 3, children: int = 2, min_n: int = 300) -> tuple[dict, list[str], list[str]]:
    """board : classement (rang, pol, n, mean, verdict…). Retourne (variantes, ajoutées, retirées)."""
    evolved = dict(evolved)
    judged = [r for r in board if r.get("n", 0) >= min_n]
    removed = []
    # 1. retirer les variantes qui perdent de façon prouvée ou sont dans la moitié basse
    half = len(judged) // 2
    for r in judged:
        if r["pol"] in evolved and (r["verdict"] == "perd" and r["rang"] > parents or r["rang"] > max(half, parents)):
            evolved.pop(r["pol"])
            removed.append(r["pol"])
    # 2. les meilleures donnent naissance à des variantes
    allp = {**base, **evolved}
    added = []
    for r in [r for r in judged if r["pol"] in allp][:parents]:
        for k in range(children):
            cfg, changes = mutate(allp[r["pol"]], rng)
            name = f"EVO{gen}_{len(added) + 1}"
            parent_desc = allp[r["pol"]].get("description", r["pol"])
            cfg["description"] = f"variante de « {parent_desc} » ({', '.join(changes)})"
            cfg["parent"] = r["pol"]
            evolved[name] = cfg
            added.append(name)
    # 3. plafond : on garde les variantes les mieux classées, puis les plus récentes (pas encore jugées)
    if len(evolved) > cap:
        rank_of = {r["pol"]: r["rang"] for r in board if r.get("n", 0) >= min_n}
        order = sorted(evolved, key=lambda n: (rank_of.get(n, 10_000 - int(n.split("_")[0][3:] or 0))))
        for n in order[cap:]:
            evolved.pop(n)
            if n in added:
                added.remove(n)
            else:
                removed.append(n)
    return evolved, added, removed


def describe(added: list[str], removed: list[str], evolved: dict[str, Any]) -> str:
    parts = []
    if added:
        parts.append(f"{len(added)} nouvelle(s) variante(s) créée(s) à partir des meilleures")
    if removed:
        parts.append(f"{len(removed)} variante(s) retirée(s) (moins bonnes)")
    return (" ; ".join(parts) or "aucun changement") + f" — {len(evolved)} variante(s) en test"
