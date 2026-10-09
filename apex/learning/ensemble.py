"""Ensemble par horizon : concurrents en parallèle, champion avec amortissement,
buffer de rejeu (démarrage à chaud des concurrents ombres et LightGBM)."""
from __future__ import annotations

import logging
from collections import deque
from typing import Any

from .models import Competitor, CompetitorSpec

log = logging.getLogger(__name__)

DEFAULT_SPECS = [
    CompetitorSpec(id="rules", kind="rules"),
    # repère honnête : prédit la fréquence moyenne. Tant qu'aucun modèle ne fait mieux,
    # il devient champion et le système ne prétend pas savoir (probas basses → pas d'alerte)
    CompetitorSpec(id="prior", kind="prior"),
    CompetitorSpec(id="logreg", kind="logreg", params={"lr": 0.01}),
    CompetitorSpec(id="arf", kind="arf", params={"n_models": 10}),
    CompetitorSpec(id="hat", kind="hat", params={"grace_period": 200}),
]


# modèles de base jamais éjectés : ce sont les références des comparaisons (ombres, features)
PROTECTED = {"rules", "prior", "logreg", "arf", "hat"}


class HorizonEnsemble:
    def __init__(self, horizon: str, mcfg: dict, calib_method: str = "platt", replay_size: int | None = None):
        self.horizon = horizon
        self.mcfg = mcfg
        self.calib_method = calib_method
        self.competitors: dict[str, Competitor] = {}
        self.champion_id: str | None = None
        self.challenger_streak: dict[str, int] = {}
        self.replay: deque[tuple[dict, int, float, float]] = deque(maxlen=replay_size or mcfg.get("replay_size", 30000))   # (x, y, w, ts)
        self.champion_history: list[tuple[float, str, str]] = []                         # (ts, id, raison)
        for spec in DEFAULT_SPECS:
            params = dict(spec.params)
            if spec.kind == "arf":
                params["n_models"] = mcfg.get("arf_n_models", params["n_models"])
            self.add(CompetitorSpec(**{**spec.to_dict(), "params": params}))
        # démarrage à froid : règles de sécurité + momentum ; les modèles prennent le relais
        # via select_champion() dès qu'ils battent les règles en live (ou dès le backfill)
        self.champion_id = "prior" if horizon.startswith("X") else "rules"
        # (objectifs x5/x10 : les règles de momentum sont faites pour le x2 ; on part du taux de base)

    # ---------- gestion des concurrents ----------
    def add(self, spec: CompetitorSpec, warm_start: bool = False, warm_limit: int | None = None) -> Competitor:
        c = Competitor(spec, window=self.mcfg["champion_window"], calib_method=self.calib_method)
        if warm_start and spec.kind not in ("lgbm", "rules", "prior"):
            rows = list(self.replay)
            if spec.replay_window:
                rows = rows[-spec.replay_window:]
            if warm_limit:
                rows = rows[-warm_limit:]
            for x, y, w, _ in rows:
                c.learn(x, y, w)
        self.competitors[spec.id] = c
        self._enforce_max()
        return c

    def remove(self, cid: str) -> None:
        if cid == self.champion_id:
            raise ValueError("impossible de retirer le champion")
        self.competitors.pop(cid, None)
        self.challenger_streak.pop(cid, None)

    def _enforce_max(self) -> None:
        mx = self.mcfg["max_competitors"]
        while len(self.competitors) > mx:
            cands = [c for c in self.competitors.values()
                     if c.id != self.champion_id and c.spec.shadow_of_correction is None and c.id not in PROTECTED]
            if not cands:
                break
            worst = max(cands, key=lambda c: c.mean_loss())
            self.competitors.pop(worst.id)

    @property
    def champion(self) -> Competitor:
        return self.competitors[self.champion_id]  # type: ignore[index]

    # ---------- prequential ----------
    def predict_all(self, x: dict) -> dict[str, tuple[float, float]]:
        # le champion prédit toujours ; les autres seulement s'ils ont déjà appris (pas de notation « à vide »)
        return {cid: c.predict(x) for cid, c in self.competitors.items() if c.ready or cid == self.champion_id}

    def ensure_defaults(self) -> None:
        """Ajoute les concurrents de base manquants (ex. état restauré d'une version antérieure)."""
        for spec in DEFAULT_SPECS:
            if spec.id not in self.competitors:
                params = dict(spec.params)
                if spec.kind == "arf":
                    params["n_models"] = self.mcfg.get("arf_n_models", params["n_models"])
                # recréé à chaud sur le buffer récent (pas de redémarrage à froid)
                self.add(CompetitorSpec(**{**spec.to_dict(), "params": params}), warm_start=True)

    def evaluate_and_learn(self, x: dict, y: int, preds: dict, w_by_comp: dict[str, float], ts: float,
                           focus: bool = True) -> dict[str, float]:
        """preds = (p_raw, p_cal) — ou p_raw seul — de chaque concurrent AU MOMENT DE LA DÉCISION."""
        losses = {}
        for cid, c in self.competitors.items():
            # p = 0,5 exactement = prédiction par défaut d'un modèle non entraîné (décisions plus
            # anciennes que l'apprentissage) : on ne la note pas, sauf pour le champion
            if cid in preds:
                raw, cal = preds[cid] if isinstance(preds[cid], tuple) else (preds[cid], None)
                if raw != 0.5 or cid == self.champion_id:
                    losses[cid] = c.evaluate(raw, y, cal, focus)
            c.learn(x, y, w_by_comp.get(cid, 1.0))
        self.replay.append((x, y, w_by_comp.get(self.champion_id or "", 1.0), ts))
        return losses

    # ---------- champion ----------
    def select_champion(self, ts: float) -> str | None:
        """Le challenger doit battre le champion d'une marge relative `champion_margin`
        sur la fenêtre récente, `champion_patience` évaluations consécutives."""
        champ = self.champion
        n_min = self.mcfg["min_samples_for_champion"]
        margin, patience = self.mcfg["champion_margin"], self.mcfg["champion_patience"]
        best, best_loss = None, champ.mean_loss()
        for cid, c in self.competitors.items():
            if cid == self.champion_id:
                continue
            if len(c.losses) < n_min:
                self.challenger_streak.pop(cid, None)
                continue
            n = min(len(c.losses), len(champ.losses))
            cl, chl = c.mean_loss(n), champ.mean_loss(n)
            if cl < chl * (1 - margin):
                self.challenger_streak[cid] = self.challenger_streak.get(cid, 0) + 1
                if self.challenger_streak[cid] >= patience and cl < best_loss:
                    best, best_loss = cid, cl
            else:
                self.challenger_streak[cid] = 0
        if best:
            self.promote(best, ts, f"meilleure logloss ({best_loss:.4f} vs {champ.mean_loss():.4f})")
            return best
        return None

    def promote(self, cid: str, ts: float, reason: str) -> None:
        old = self.champion_id
        self.champion_id = cid
        self.challenger_streak.clear()
        self.champion_history.append((ts, cid, reason))
        log.info("[%s] nouveau champion %s (ancien %s) : %s", self.horizon, cid, old, reason)

    def summary(self) -> dict[str, Any]:
        return {
            "horizon": self.horizon, "champion": self.champion_id,
            "competitors": [c.summary() for c in self.competitors.values()],
            "champion_history": self.champion_history[-20:], "replay": len(self.replay),
        }
