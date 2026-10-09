"""BOUCLE 1 — cœur d'apprentissage : prédire → observer → noter → apprendre.

Indépendant des E/S : utilisé par le service live (learner) et par le rejeu
backfill. Applique aussi les commandes de correction envoyées par la boucle 2.
"""
from __future__ import annotations

import copy
import logging
import pickle
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..alerting.bandit import AlertBandit
from ..alerting.slippage import slippage_table
from ..config import Config
from ..errors.classifier import DynamicRule, ErrorRecord, classify
from ..events import Decision, Label, Outcome
from ..ingestor.watchdog import overlaps_gap
from .ensemble import HorizonEnsemble
from ..trading.exits import build_panel
from .models import CompetitorSpec, train_lgbm

log = logging.getLogger(__name__)


@dataclass
class DecisionCache:
    d: Decision
    preds: dict[str, dict[str, tuple[float, float]]]      # horizon -> cid -> (raw, cal)
    champions: dict[str, str]
    threshold: float
    alerted: bool = False
    remaining: int = 0


@dataclass
class LabelResult:
    evaluations: list[tuple] = field(default_factory=list)   # (decision_id, horizon, cid, is_champ, p, y, loss, err_type, cost)
    error: ErrorRecord | None = None
    error_ctx: dict | None = None
    alert_result: dict | None = None


class Learner:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        c = cfg.data
        self.primary = c["labels"]["primary"]
        self.horizons = c["models"]["horizons"]
        self.ensembles = {h: HorizonEnsemble(h, c["models"], c["calibration"]["method"]) for h in self.horizons}
        self.evolved: dict[str, dict] = {}
        self.ev = None                  # modèles « gain attendu par stratégie » ACTIFS (sinon None : pas de choix AUTO)
        self.bandit = self._new_bandit()
        self.short = [h for h in self.horizons if c["labels"][h]["horizon_s"] <= 3600]
        self.long = [h for h in self.horizons if h not in self.short]
        # décisions en attente de labels LONGS (6 h / 24 h) et d'Outcome : prédictions et scores
        # seulement (les features sont relues en base au moment du label)
        self.long_store: dict[str, dict] = {}
        self.cache: dict[str, DecisionCache] = {}
        self.mint_preds: dict[str, dict[str, float]] = {}
        self.alerted_mints: set[str] = set()
        self.alerts_day: tuple[int, int] = (0, 0)              # (jour, nombre)
        self.dynamic_rules: list[DynamicRule] = []
        self.paused = False
        self.counter = 0
        self.lgbm_version = 0
        self.last_candidate = False
        self.alerts_hour: tuple[int, int] = (0, 0)              # (heure, nombre) : alertes étalées sur la journée
        self.gaps: list[tuple[float, float]] = []    # trous de données (ingestor)
        self.skipped_gap = 0
        self.n_outcomes = 0                  # récompenses reçues par le bandit
        self.last_decision_ts = 0.0
        self.last_label_ts = 0.0

    def _new_bandit(self) -> AlertBandit:
        b = self.cfg.data["bandit"]
        scores = {k: v["thresholds"] for k, v in b.get("scores", {"x2": {"thresholds": b["thresholds"]}}).items()
                  if k != "ev" or getattr(self, "ev", None) is not None}
        allp = build_panel(self.cfg.data.get("exits", {}), getattr(self, "evolved", {}))
        policies = b.get("policies") or ["TP2_SL50"]
        if policies == "all":
            policies = list(allp)
        if getattr(self, "ev", None) is not None:
            policies = list(policies) + ["AUTO"]      # stratégie choisie token par token
        return AlertBandit(scores_cfg=scores, points=[str(p) for p in b["decision_points"]], policies=policies,
                           alerts_min=b["alerts_per_day"]["min"], alerts_max=b["alerts_per_day"]["max"],
                           discount=b["discount"])

    def score_horizons(self) -> dict[str, str]:
        b = self.cfg.data["bandit"]
        return {k: v["horizon"] for k, v in b.get("scores", {"x2": {"horizon": self.primary}}).items()
                if v.get("horizon") in self.ensembles}

    # ------------------------------------------------------------------
    def _primary_threshold(self) -> float:
        a = self.bandit.active_arm()
        return a.thr if a.score == "x2" else self.cfg.get("errors.default_threshold", 0.13)

    def on_decision(self, d: Decision) -> tuple[list[tuple], dict | None]:
        """Retourne (lignes de prédiction, alerte éventuelle)."""
        x = d.features
        self.last_decision_ts = time.time()
        preds: dict[str, dict[str, tuple[float, float]]] = {}
        champions = {}
        rows = []
        for h, ens in self.ensembles.items():
            preds[h] = ens.predict_all(x)
            champions[h] = ens.champion_id
            raw, cal = preds[h][ens.champion_id]
            rows.append((d.decision_id, d.mint, d.point, h, ens.champion_id, raw, cal, True))
        p = preds[self.primary][champions[self.primary]][1]
        thr = self._primary_threshold()
        if self.gaps and overlaps_gap(self.gaps, float(d.meta.get("t0", d.ts)), d.ts):
            # features calculées sur un historique incomplet : ni alerte, ni apprentissage
            self.skipped_gap += 1
            self.last_candidate = False
            return rows, None
        dc = DecisionCache(d=d, preds=preds, champions=champions, threshold=thr, remaining=len(self.short))
        self.cache[d.decision_id] = dc
        self.mint_preds.setdefault(d.mint, {})[d.point] = p
        scores = {sc: preds[h][champions[h]][1] for sc, h in self.score_horizons().items()}
        # seuls les tokens réellement alertables servent à évaluer les politiques d'alerte
        eligible = (not d.blocked and d.features.get("unique_buyers", 0) >= self.cfg.get("bandit.min_buyers_to_alert", 10))
        auto_policy = None
        if self.ev is not None:
            auto_policy, ev = self.ev.best(x) if eligible else (None, -1.0)
            scores["ev"] = ev
        self.long_store[d.decision_id] = {
            "ts": d.ts, "point": d.point, "mint": d.mint, "scores": scores, "alerted": False, "eligible": eligible,
            "preds": {h: preds[h] for h in self.long}, "champions": {h: champions[h] for h in self.long},
            "auto_policy": auto_policy,
        }
        if eligible:
            self.bandit.observe_decision(d.point, scores, d.ts)
        alert = None
        arm = self.bandit.active_arm()
        ratio = self.cfg.get("features.candidate_p_ratio", 0.6)
        self.last_candidate = p >= ratio * thr or (arm.score != "x2" and scores.get(arm.score, 0.0) >= ratio * arm.thr)
        warm = self.ensembles[self.primary].champion.n_evaluated >= self.cfg.get("bandit.warmup_labels", 0)
        # pas d'alerte sur un token sans activité réelle (illiquide, non tradable)
        warm = warm and d.features.get("unique_buyers", 0) >= self.cfg.get("bandit.min_buyers_to_alert", 10)
        warm = warm and self.bandit.ready(self.cfg.get("bandit.min_observed_s", 3600))
        if warm and not d.blocked and not self.paused and d.mint not in self.alerted_mints and self.bandit.should_alert(d.point, scores):
            day, hour = int(d.ts // 86400), int(d.ts // 3600)
            n = self.alerts_day[1] if self.alerts_day[0] == day else 0
            nh = self.alerts_hour[1] if getattr(self, "alerts_hour", (0, 0))[0] == hour else 0
            if n < self.cfg.get("bandit.alerts_per_day.max", 30) and nh < self.cfg.get("bandit.alerts_per_hour_max", 4):
                self.alerts_day = (day, n + 1)
                self.alerts_hour = (hour, nh + 1)
                self.alerted_mints.add(d.mint)
                dc.alerted = True
                self.long_store[d.decision_id]["alerted"] = True
                alert = self._alert_payload(dc, p, scores)
        self._evict(d.ts)
        return rows, alert

    def _alert_payload(self, dc: DecisionCache, p: float, scores: dict[str, float]) -> dict:
        d = dc.d
        fee = self.cfg.get("fees.pumpswap_fee_bps") if d.meta.get("migrated") else self.cfg.get("fees.pump_fee_bps")
        arm = self.bandit.active_arm()
        policy = arm.policy
        if policy == "AUTO":        # stratégie choisie pour CE token par le modèle de gain attendu
            policy = (self.long_store.get(d.decision_id) or {}).get("auto_policy") or "TP2_SL50"
        pol_cfg = build_panel(self.cfg.data.get("exits", {}), self.evolved).get(policy) or {}
        return {
            "decision_id": d.decision_id, "mint": d.mint, "point": d.point, "ts": d.ts,
            "entry_price": d.entry_price, "v_sol": d.v_sol, "v_tokens": d.v_tokens,
            "migrated": bool(d.meta.get("migrated")), "score": arm.score, "scores": {k: round(v, 4) for k, v in scores.items()},
            "policy": policy, "policy_description": pol_cfg.get("description", policy)
            + (" (stratégie choisie pour ce token)" if arm.policy == "AUTO" else ""),
            "arm_mean_pnl": round(arm.mean(), 4), "arm_n": round(arm.n, 1),
            "name": d.meta.get("name"), "symbol": d.meta.get("symbol"), "mc_sol": d.mc_sol,
            "p": p, "model": dc.champions[self.primary], "arm": self.bandit.active,
            "horizons": {h: round(dc.preds[h][dc.champions[h]][1], 3) for h in self.horizons},
            "reasons": self.top_reasons(d.features),
            "flags": d.safety_flags,
            "slippage": slippage_table(d.v_sol, d.v_tokens, [0.5, 1.0, 2.0], fee),
            "sol_price_usd": d.features.get("sol_price_usd"),
        }

    def top_reasons(self, x: dict[str, float], k: int = 3) -> list[dict]:
        """Contributions de la régression logistique en ligne (lisible) : poids × valeur standardisée."""
        ens = self.ensembles[self.primary]
        lr = ens.competitors.get("logreg")
        if lr is None or lr.n_learned < 50:
            return []
        try:
            scaler, model = list(lr.model.steps.values())
            xf = lr.filter_x(x)
            contribs = []
            for f, v in xf.items():
                mean = scaler.means.get(f, 0.0)
                var = scaler.vars.get(f, 0.0)
                z = (v - mean) / (var ** 0.5) if var > 0 else 0.0
                contribs.append((model.weights.get(f, 0.0) * z, f, v))
            contribs.sort(reverse=True)
            return [{"feature": f, "value": round(v, 4), "contribution": round(c, 3)} for c, f, v in contribs[:k] if c > 0]
        except Exception:  # noqa: BLE001
            return []

    # ------------------------------------------------------------------
    def sample_weight(self, y: int, err: ErrorRecord | None, spec: CompetitorSpec) -> float:
        m = self.cfg.data["models"]
        weights = spec.error_weights or m["error_weights"]
        pos_w = spec.positive_class_weight or m["positive_class_weight"]
        w = pos_w if y == 1 else 1.0
        if err is not None:
            w *= weights.get(err.error_type, weights.get("AUTRE", 1.5)) * (1 + min(err.cost, 2.0))
        return min(w, m["max_sample_weight"])

    def on_outcome(self, o: Outcome) -> dict | None:
        """Récompense du bandit : PnL simulé de chaque stratégie de sortie pour cette décision."""
        st = self.long_store.get(o.decision_id)
        if st is None or st.get("rewarded"):
            return None
        st["rewarded"] = True       # un même Outcome rejoué après redémarrage n'est compté qu'une fois
        self.n_outcomes += 1
        if st.get("eligible", True) and not (self.gaps and overlaps_gap(self.gaps, st["ts"], o.ts, min_len=1800)):
            pnl = o.pnl
            if st.get("auto_policy") in pnl:
                pnl = {**pnl, "AUTO": pnl[st["auto_policy"]]}      # récompense du choix token par token
            self.bandit.observe_reward(st["point"], st["scores"], pnl)
        return {"alerted": st["alerted"]}

    def on_label_long(self, lb: Label, x: dict[str, float]) -> LabelResult:
        """Labels 6 h / 24 h : prédictions gardées en mémoire, features relues en base."""
        res = LabelResult()
        st = self.long_store.get(lb.decision_id)
        if st is None or lb.horizon not in self.ensembles or not x:
            return res
        tol = (self.cfg.get("labels.gap_tolerance_s") or {}).get(lb.horizon, 0)
        hz = self.cfg.get(f"labels.{lb.horizon}.horizon_s", 86400)
        if self.gaps and overlaps_gap(self.gaps, st["ts"], st["ts"] + hz, min_len=tol):
            self.skipped_gap += 1
            return res
        ens = self.ensembles[lb.horizon]
        preds = st["preds"].get(lb.horizon, {})
        champ = st["champions"].get(lb.horizon)
        w_by = {cid: self.sample_weight(lb.y, None, c.spec) for cid, c in ens.competitors.items()}
        losses = ens.evaluate_and_learn(x, lb.y, dict(preds), w_by, lb.ts, focus=st.get("eligible", True))
        for cid, loss in losses.items():
            if cid == champ:
                res.evaluations.append((lb.decision_id, lb.horizon, cid, True, preds[cid][1], lb.y, loss, None, None))
        self.counter += 1
        return res

    def on_label(self, lb: Label) -> LabelResult:
        res = LabelResult()
        dc = self.cache.get(lb.decision_id)
        if dc is None or lb.horizon not in self.ensembles or lb.horizon in self.long:
            return res
        ens = self.ensembles[lb.horizon]
        x = dc.d.features
        hz = self.cfg.get(f"labels.{lb.horizon}.horizon_s", 3600)
        tol = (self.cfg.get("labels.gap_tolerance_s") or {}).get(lb.horizon, 0)
        if self.gaps and overlaps_gap(self.gaps, dc.d.ts, dc.d.ts + hz, min_len=tol):
            # label calculé sur un chemin de prix trop incomplet : on ne l'apprend pas,
            # mais le résultat de l'alerte est quand même communiqué (marqué incomplet)
            self.skipped_gap += 1
            if dc.alerted and lb.horizon in (self.primary, "L15"):
                res.alert_result = {"decision_id": lb.decision_id, "pnl": lb.sim_pnl, "y": lb.y, "horizon": lb.horizon,
                                    "max_return": lb.max_return, "error_type": None, "incomplete": True}
            dc.remaining -= 1
            if dc.remaining <= 0:
                self.cache.pop(lb.decision_id, None)
            return res
        preds = dc.preds.get(lb.horizon, {})
        champ = dc.champions[lb.horizon]
        out = {"max_return": lb.max_return, "max_drawdown": lb.max_drawdown, "rug": lb.rug,
               "sim_pnl": lb.sim_pnl, "final_return": lb.final_return, "time_to_peak_s": lb.time_to_peak_s}
        err: ErrorRecord | None = None
        if lb.horizon == self.primary and champ in preds:
            p = preds[champ][1]
            predicted = int(p >= dc.threshold)
            later = self._later_positive(dc.d.mint, dc.d.point, dc.threshold)
            err = classify(predicted, lb.y, x, out, later, self.cfg.data["errors"], self.dynamic_rules)
            res.error = err
            if err is not None:
                res.error_ctx = {"p": p, "model": champ, "alerted": dc.alerted, "outcome": out,
                                 "threshold": dc.threshold, "features": x}
            if dc.alerted:
                res.alert_result = {"decision_id": lb.decision_id, "pnl": lb.sim_pnl, "y": lb.y, "horizon": lb.horizon,
                                    "max_return": lb.max_return, "error_type": err.error_type if err else None}
        w_by = {cid: self.sample_weight(lb.y, err, c.spec) for cid, c in ens.competitors.items()}
        losses = ens.evaluate_and_learn(x, lb.y, dict(preds), w_by, lb.ts, focus=self.alertable(dc.d))
        record_all = lb.horizon == self.primary
        for cid, loss in losses.items():
            if record_all or cid == champ:
                et = err.error_type if (err and cid == champ) else None
                cost = err.cost if (err and cid == champ) else None
                res.evaluations.append((lb.decision_id, lb.horizon, cid, cid == champ, preds[cid][1], lb.y, loss, et, cost))
        if lb.horizon == "L15" and dc.alerted:
            res.alert_result = {"decision_id": lb.decision_id, "pnl": lb.sim_pnl, "y": lb.y, "horizon": "L15",
                                "max_return": lb.max_return}
        dc.remaining -= 1
        if dc.remaining <= 0:
            self.cache.pop(lb.decision_id, None)
        self.counter += 1
        self.last_label_ts = time.time()
        return res

    def _later_positive(self, mint: str, point: str, thr: float) -> bool:
        order = [str(p) for p in self.cfg.data["decision_points_s"]] + ["migration"]
        mp = self.mint_preds.get(mint, {})
        try:
            i = order.index(point)
        except ValueError:
            return False
        return any(mp.get(q, 0) >= thr for q in order[i + 1:])

    def _evict(self, now: float) -> None:
        if len(self.long_store) > 50_000 and int(now) % 60 == 0:
            for did in [k for k, v in self.long_store.items() if now - v["ts"] > 26 * 3600]:
                self.long_store.pop(did, None)
        if len(self.cache) < 200_000:
            return
        for did in [k for k, v in self.cache.items() if now - v.d.ts > 7200]:
            self.cache.pop(did, None)
        if len(self.mint_preds) > 100_000:
            for m in list(self.mint_preds)[:50_000]:
                self.mint_preds.pop(m, None)

    def set_ev(self, models: Any) -> None:
        """Active (ou coupe, models=None) le choix de stratégie token par token : bras AUTO et score ev."""
        if (models is None) == (self.ev is None) and models is self.ev:
            return
        self.ev = models
        fresh = self._new_bandit()
        self.bandit.sync_arms(fresh.scores_cfg, fresh.points, fresh.policies)

    def set_evolved(self, evolved: dict[str, dict]) -> None:
        """Nouvelles variantes de stratégies de sortie : bras du bandit ajoutés / retirés."""
        if evolved == self.evolved:
            return
        self.evolved = dict(evolved)
        fresh = self._new_bandit()
        self.bandit.sync_arms(fresh.scores_cfg, fresh.points, fresh.policies)

    def periodic(self, now: float) -> list[str]:
        """Sélection des champions + rééchantillonnage du bandit."""
        events = []
        for h, ens in self.ensembles.items():
            ens.ensure_defaults()
            new = ens.select_champion(now)
            if new:
                events.append(f"[{h}] nouveau champion : {new}")
        return events

    # ------------------------------------------------------------------
    def alertable(self, d: Any) -> bool:
        """Décision qui peut réellement donner une alerte (non bloquée, assez d'acheteurs)."""
        return not d.blocked and d.features.get("unique_buyers", 0) >= self.cfg.get("bandit.min_buyers_to_alert", 10)

    def lgbm_rows(self, horizon: str, window: int | None = None) -> list[tuple[dict, int, float]]:
        """Fenêtre récente ; les décisions NON alertables gardent un poids réduit (on apprend surtout
        à trier les tokens qui peuvent donner une alerte, sans jeter le reste de l'information)."""
        w = window or self.cfg.get("models.lgbm_window", 50000)
        mb = self.cfg.get("bandit.min_buyers_to_alert", 10)
        other = self.cfg.get("models.non_alertable_weight", 0.2)
        return [(x, y, wt if x.get("unique_buyers", 0) >= mb else wt * other)
                for x, y, wt, _ in list(self.ensembles[horizon].replay)[-w:]]

    def install_lgbm(self, horizon: str, booster: Any, feats: list[str], cid: str | None = None) -> str:
        ens = self.ensembles[horizon]
        self.lgbm_version += 1
        cid = cid or f"lgbm_v{self.lgbm_version}"
        # on garde les 2 versions précédentes : pour L60, les prédictions d'un LightGBM ne sont
        # notées qu'au bout de 60 min, il faut donc lui laisser plus d'une heure pour faire ses preuves
        olds = sorted((k for k, c in ens.competitors.items()
                       if c.spec.kind == "lgbm" and k != ens.champion_id and c.spec.shadow_of_correction is None),
                      key=lambda k: int(k.split("_v")[-1]) if k.split("_v")[-1].isdigit() else 0)
        for old in olds[:-2]:
            ens.competitors.pop(old)
        c = ens.add(CompetitorSpec(id=cid, kind="lgbm"))
        c.booster, c.lgbm_features = booster, feats
        return cid

    # ------------------------------------------------------------------
    # Commandes de correction (boucle 2) — chaque commande est réversible
    def apply_command(self, cmd: dict) -> dict:
        op = cmd["op"]
        h = cmd.get("horizon", self.primary)
        ens = self.ensembles.get(h)
        if op == "add_competitor":
            spec = CompetitorSpec(**cmd["spec"])
            if spec.id in ens.competitors:
                return {"ok": False, "error": "id existant"}
            ens.add(spec, warm_start=cmd.get("warm_start", True))
            return {"ok": True, "competitor": spec.id}
        if op == "shadow_of_champion":
            base = ens.champion
            changes = cmd.get("changes", {})
            if base.spec.kind in ("lgbm", "rules", "prior"):
                base = ens.competitors.get("arf") or base
            spec = base.clone_spec(cmd["new_id"], shadow_of_correction=cmd.get("correction_id"), **changes)
            ens.add(spec, warm_start=True, warm_limit=self.cfg.get("models.shadow_warm_start", 10000))
            return {"ok": True, "competitor": spec.id, "base": base.id}
        if op == "remove_competitor":
            if cmd["id"] not in ens.competitors:
                return {"ok": True, "absent": True}
            ens.remove(cmd["id"])
            return {"ok": True}
        if op == "promote":
            if cmd["id"] not in ens.competitors:
                return {"ok": False, "error": "inconnu"}
            prev = ens.champion_id
            ens.promote(cmd["id"], time.time(), cmd.get("reason", "correction"))
            ens.competitors[cmd["id"]].spec.shadow_of_correction = None
            return {"ok": True, "previous": prev}
        if op == "set_override":
            before = self.cfg.overrides.get(cmd["key"])
            self.cfg.set_override(cmd["key"], cmd["value"])
            self._on_config_change(cmd["key"])
            return {"ok": True, "before": before}
        if op == "clear_override":
            self.cfg.clear_override(cmd["key"])
            self._on_config_change(cmd["key"])
            return {"ok": True}
        if op == "set_calibration":
            before = None
            for e in self.ensembles.values():
                for c in e.competitors.values():
                    before = c.calibrator.method
                    c.calibrator.method = cmd["method"]
            return {"ok": True, "before": before}
        if op == "bandit_range":
            before = [self.bandit.thr_lo, self.bandit.thr_hi]
            self.bandit.recenter(cmd["lo"], cmd["hi"])
            return {"ok": True, "before": before}
        if op == "add_error_type":
            rule = DynamicRule.from_json(cmd["name"], cmd["rule"])
            self.dynamic_rules = [r for r in self.dynamic_rules if r.name != rule.name] + [rule]
            return {"ok": True}
        if op == "enable_claude_feature":
            spec_changes = {"extra_features": sorted(set(ens.champion.spec.extra_features) | {cmd["feature_id"]})}
            base = ens.champion if ens.champion.spec.kind not in ("lgbm", "rules", "prior") else ens.competitors["arf"]
            # deux JUMEAUX partis du même point (même modèle de base, même démarrage à chaud) :
            # l'un reçoit la feature, l'autre non. L'effet mesuré est l'écart entre les deux,
            # pas l'écart avec un modèle entraîné depuis bien plus longtemps.
            limit = self.cfg.get("models.shadow_warm_start", 10000)
            spec = base.clone_spec(cmd["new_id"], shadow_of_correction=cmd.get("correction_id"), **spec_changes)
            ctl = base.clone_spec(cmd["new_id"] + "_ctl", shadow_of_correction=cmd.get("correction_id"),
                                  extra_features=list(base.spec.extra_features))
            ens.add(spec, warm_start=True, warm_limit=limit)
            ens.add(ctl, warm_start=True, warm_limit=limit)
            return {"ok": True, "competitor": spec.id, "control": ctl.id}
        if op == "forget_decisions":
            # décisions reconnues comme fausses (ex. prix d'entrée périmé) : leurs labels et
            # outcomes à venir ne doivent plus servir ni aux modèles ni au bandit
            n = 0
            for did in cmd.get("ids", []):
                n += self.long_store.pop(did, None) is not None
                n += self.cache.pop(did, None) is not None
            return {"ok": True, "forgotten": n}
        if op == "reset_bandit":
            self.bandit = self._new_bandit()
            for st in self.long_store.values():
                st["rewarded"] = False
            return {"ok": True}
        if op == "adopt":
            # l'ombre a fait ses preuves face à son modèle d'origine : elle devient un concurrent normal
            if cmd["id"] not in ens.competitors:
                return {"ok": False, "error": "inconnu"}
            ens.competitors[cmd["id"]].spec.shadow_of_correction = None
            return {"ok": True}
        if op == "reset_eval_window":
            for cid, c in ens.competitors.items():
                if cid != ens.champion_id or cmd.get("include_champion"):
                    c.reset_eval()
            ens.challenger_streak.clear()
            return {"ok": True}
        if op == "pause":
            self.paused = bool(cmd.get("value", True))
            return {"ok": True}
        return {"ok": False, "error": f"commande inconnue {op}"}

    def _on_config_change(self, key: str) -> None:
        if key.startswith("bandit.thresholds"):
            pass
        if key == "calibration.method":
            m = self.cfg.get("calibration.method")
            for e in self.ensembles.values():
                for c in e.competitors.values():
                    c.calibrator.method = m

    # ------------------------------------------------------------------
    def state_summary(self) -> dict:
        return {
            "ts": time.time(), "paused": self.paused, "config_version": self.cfg.version(),
            "overrides": self.cfg.overrides, "ensembles": {h: e.summary() for h, e in self.ensembles.items()},
            "bandit": self.bandit.summary(), "alerts_today": self.alerts_day[1],
            "dynamic_error_types": [r.name for r in self.dynamic_rules], "cache": len(self.cache),
            "n_labels": self.counter,
            "n_outcomes": getattr(self, "n_outcomes", 0),
            "last_decision_ts": getattr(self, "last_decision_ts", 0.0),
            "last_label_ts": getattr(self, "last_label_ts", 0.0),
            "skipped_data_gap": self.skipped_gap,
            "warmup_done": self.ensembles[self.primary].champion.n_evaluated >= self.cfg.get("bandit.warmup_labels", 0),
        }

    def snapshot(self, path: Path) -> None:
        state = {
            "ensembles": self.ensembles, "bandit": self.bandit, "overrides": copy.deepcopy(self.cfg.overrides),
            "dynamic_rules": self.dynamic_rules, "alerted_mints": self.alerted_mints, "alerts_day": self.alerts_day,
            "lgbm_version": self.lgbm_version, "counter": self.counter, "cache": self.cache, "mint_preds": self.mint_preds,
            "long_store": self.long_store,
        }
        tmp = path.with_suffix(".tmp")
        with open(tmp, "wb") as f:
            pickle.dump(state, f, protocol=pickle.HIGHEST_PROTOCOL)
        tmp.replace(path)

    def restore(self, path: Path, keep_runtime: bool = False) -> None:
        """keep_runtime=True : retour à un état stable des MODÈLES en gardant les décisions
        en attente de labels (pas de perte des prédictions en cours)."""
        with open(path, "rb") as f:
            st = pickle.load(f)  # noqa: S301 — fichiers produits par ce système uniquement
        self.ensembles = st["ensembles"]
        self.bandit = st["bandit"]
        self.evolved = getattr(self, "evolved", {}) or {}
        self.ev = getattr(self, "ev", None)
        if not hasattr(self.bandit, "scores_cfg"):
            self.bandit = self._new_bandit()      # ancien format (seuil seul) : nouveau bandit
        else:
            fresh = self._new_bandit()
            self.bandit.sync_arms(fresh.scores_cfg, fresh.points, fresh.policies)
        for h in self.horizons:                   # nouveaux horizons ajoutés à la config
            if h not in self.ensembles:
                self.ensembles[h] = HorizonEnsemble(h, self.cfg.data["models"], self.cfg.data["calibration"]["method"])
        for e in self.ensembles.values():
            # la config des modèles vient TOUJOURS du fichier courant, pas de la copie sauvegardée
            e.mcfg = self.cfg.data["models"]
            for c in e.competitors.values():
                c.losses = type(c.losses)(c.losses, maxlen=e.mcfg["champion_window"])
                c.hits = type(c.hits)(c.hits, maxlen=e.mcfg["champion_window"])
            e.ensure_defaults()
        self.cfg.overrides = st["overrides"]
        self.dynamic_rules = st["dynamic_rules"]
        self.lgbm_version = st["lgbm_version"]
        if not keep_runtime:
            self.alerted_mints = st["alerted_mints"]
            self.alerts_day = st["alerts_day"]
            self.counter = st["counter"]
            self.cache = st["cache"]
            self.mint_preds = st["mint_preds"]
            self.long_store = st.get("long_store", {})


async def train_lgbm_async(learner: Learner, horizon: str) -> str | None:
    import asyncio

    rows = learner.lgbm_rows(horizon)
    if len(rows) < learner.cfg.get("models.lgbm_min_samples", 2000):
        return None
    booster, feats = await asyncio.to_thread(train_lgbm, rows)
    return learner.install_lgbm(horizon, booster, feats)
