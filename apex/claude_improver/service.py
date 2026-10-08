"""Amélioration par Claude (section 11).

Déclencheurs : PLATEAU, RÉGRESSION SUR UN TYPE, hausse des erreurs AUTRE, cycle 6 h.
Envoie : diagnostic, courbes, erreurs les plus coûteuses, erreurs AUTRE, features
existantes, historique et taux de réussite des propositions précédentes.
Reçoit (sortie JSON structurée) : features candidates (code + test), nouveaux types
d'erreurs (règle DSL sûre), hypothèse en une phrase.
Le code passe la sandbox puis est ajouté à un concurrent OMBRE ; tout est journalisé,
y compris les rejets et leur raison.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import time
from typing import Any

import anthropic

from .. import bus as B
from ..config import Config, secrets
from ..db import DB
from ..errors.classifier import BASE_TYPES, DynamicRule
from ..events import TokenCreated, Trade
from ..features.state import FEATURE_VERSIONS, TokenState, claude_context, compute_features
from .sandbox import run_isolated

log = logging.getLogger("claude_improver")

SYSTEM_PROMPT = """Tu es l'ingénieur ML d'APEX SCANNER, un détecteur de memecoins pump.fun (Solana) auto-apprenant.
Pour chaque nouveau token, des modèles en ligne prédisent à plusieurs points de décision (30 s, 1, 2, 5, 10 min, migration)
la probabilité que le prix, au prix d'entrée réellement accessible (slippage compris), fasse x2 dans les 60 min sans chuter
de 50 % avant (label principal L60). Les erreurs sont classées : RUG_ALERTE, BUNDLE_RATE, ENTREE_TARDIVE, FAUX_SMART_MONEY,
MORT_LENTE, GAGNANT_MANQUE, GAGNANT_DETECTE_TROP_TARD, AUTRE (+ types dynamiques).

Le système de supervision t'appelle quand il stagne ou régresse. Ton rôle : expliquer le problème en une phrase et proposer
des features qui donneraient aux modèles l'information qui leur manque, et des types d'erreurs pour les schémas non couverts.

CONTRAT D'UNE FEATURE
- Une fonction Python pure `def compute(ctx):` qui renvoie un float (ou None si non calculable).
- `ctx` est un dict : now_rel (s depuis la création), features (dict des features de base déjà calculées), creator, name, symbol,
  trades (liste chronologique de dicts : t [s depuis création], is_buy, sol, tokens, trader, price [SOL/token], slot_rel [slots
  depuis la création ou None]), balances (dict wallet -> tokens détenus).
- Seuls `import math` et `import statistics` sont autorisés. Interdits : while, attributs commençant par _, eval/exec/open/
  getattr/type/globals, tout accès réseau ou fichier. Pas d'état global. Budget : < 5 ms pour 2 000 trades.
- Fournis aussi `def test(compute):` qui construit 2 à 4 ctx synthétiques et vérifie le résultat avec assert.
- Une feature doit apporter une information ABSENTE des features de base listées ; nomme-la en snake_case.

CONTRAT D'UN TYPE D'ERREUR
- name en MAJUSCULES_SNAKE, description, et une règle : predicted (1 = faux positif, 0 = faux négatif, -1 = les deux)
  + conditions (toutes vraies) sur une feature au moment de la décision (source "feature") ou sur le résultat
  (source "outcome" : max_return, max_drawdown, rug, sim_pnl, final_return, time_to_peak_s).
- Ne propose un type que si un schéma récurrent des erreurs AUTRE n'est couvert par aucun type existant.

Sois concret et parcimonieux : 1 à 3 features de qualité valent mieux que 10 features fragiles. Appuie-toi sur les erreurs
fournies (valeurs des features, résultats) pour justifier chaque proposition dans sa description."""

OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "hypothesis": {"type": "string"},
        "features": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "description": {"type": "string"},
                    "code": {"type": "string"},
                    "test_code": {"type": "string"},
                    "target_error_types": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["name", "description", "code", "test_code", "target_error_types"],
                "additionalProperties": False,
            },
        },
        "error_types": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "description": {"type": "string"},
                    "predicted": {"type": "integer"},
                    "conditions": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "source": {"type": "string", "enum": ["feature", "outcome"]},
                                "key": {"type": "string"},
                                "op": {"type": "string", "enum": [">", ">=", "<", "<=", "==", "!="]},
                                "value": {"type": "number"},
                            },
                            "required": ["source", "key", "op", "value"],
                            "additionalProperties": False,
                        },
                    },
                },
                "required": ["name", "description", "predicted", "conditions"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["hypothesis", "features", "error_types"],
    "additionalProperties": False,
}


def _slug(name: str) -> str:
    return re.sub(r"[^a-z0-9_]", "_", name.lower())[:40].strip("_") or "feature"


class Improver:
    def __init__(self, cfg: Config, db: DB, bus: Any):
        self.cfg, self.db, self.bus = cfg, db, bus
        self.c = cfg["claude"]
        self.client = anthropic.AsyncAnthropic(api_key=secrets().anthropic_api_key or None)
        self._lock = asyncio.Lock()
        self._last_run = 0.0

    # ------------------------------------------------------------------
    async def _budget_ok(self) -> bool:
        n = await self.db.fetchval("SELECT count(*) FROM claude_proposals WHERE ts > now() - interval '24 hours'")
        return n < self.c["max_calls_per_day"] and time.time() - self._last_run > 1800

    async def build_context(self, trigger: str, problem_type: str, diagnosis: dict) -> str:
        primary = self.cfg["labels"]["primary"]
        curves = await self.db.fetch(
            """SELECT DISTINCT ON (curve, win) curve, win, value, n FROM curve_points
               WHERE model_id='champion' ORDER BY curve, win, ts DESC""")
        states = await self.db.fetch(
            "SELECT DISTINCT ON (curve) curve, state, slope, p_value FROM learning_states ORDER BY curve, ts DESC")
        costly = await self.db.fetch(
            """SELECT error_type, point, p, cost, features, outcome, market FROM errors
               WHERE ts > now() - interval '24 hours' ORDER BY cost DESC NULLS LAST LIMIT $1""", self.c["max_errors_in_prompt"] // 2)
        autre = await self.db.fetch(
            """SELECT point, p, cost, features, outcome, market FROM errors WHERE error_type='AUTRE'
               AND ts > now() - interval '24 hours' ORDER BY ts DESC LIMIT $1""", self.c["max_errors_in_prompt"])
        history = await self.db.fetch(
            """SELECT problem_type, count(*) n, count(*) FILTER (WHERE status='champion') ok,
                      count(*) FILTER (WHERE status IN ('rejected','sandbox_failed','disabled')) ko
               FROM claude_features GROUP BY problem_type""")
        recent = await self.db.fetch(
            "SELECT feature_id, name, description, status, reason FROM claude_features ORDER BY created_at DESC LIMIT 20")
        etypes = await self.db.fetch("SELECT name, description FROM error_types WHERE active")

        def rnd(d: dict) -> dict:
            return {k: (round(v, 5) if isinstance(v, float) else v) for k, v in (d or {}).items()}

        payload = {
            "declencheur": trigger, "probleme": problem_type, "horizon_principal": primary,
            "diagnostic": diagnosis,
            "etats_des_courbes": [dict(r) for r in states],
            "courbes": [dict(r) for r in curves],
            "erreurs_les_plus_couteuses": [{**{k: r[k] for k in ("error_type", "point", "p", "cost")},
                                             "features": rnd(r["features"]), "resultat": rnd(r["outcome"])} for r in costly],
            "erreurs_AUTRE": [{"point": r["point"], "p": r["p"], "cost": r["cost"], "features": rnd(r["features"]),
                               "resultat": rnd(r["outcome"])} for r in autre],
            "features_de_base": sorted(FEATURE_VERSIONS),
            "types_erreurs_existants": BASE_TYPES + [r["name"] for r in etypes],
            "taux_de_reussite_de_tes_propositions_par_probleme": [dict(r) for r in history],
            "tes_dernieres_propositions": [dict(r) for r in recent],
        }
        return ("Voici l'état du système. Propose des améliorations ciblées sur le problème indiqué. "
                "Tiens compte de ce qui a échoué précédemment (raison fournie) pour proposer mieux.\n\n"
                + json.dumps(payload, ensure_ascii=False, default=str))

    async def call_claude(self, user: str) -> tuple[dict | None, dict]:
        async with self.client.beta.messages.stream(
            model=self.c["model"],
            max_tokens=32000,
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
            thinking={"type": "adaptive"},
            output_config={"effort": self.c["effort"], "format": {"type": "json_schema", "schema": OUTPUT_SCHEMA}},
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user}],
        ) as stream:
            msg = await stream.get_final_message()
        usage = {"input_tokens": msg.usage.input_tokens, "output_tokens": msg.usage.output_tokens,
                 "model": msg.model, "stop_reason": msg.stop_reason}
        if msg.stop_reason in ("refusal", "max_tokens"):
            return None, usage
        text = next((b.text for b in msg.content if b.type == "text"), None)
        return (json.loads(text) if text else None), usage

    async def sample_contexts(self, n: int = 25) -> list[dict]:
        """Reconstruit de vrais ctx à partir des trades récents (rétention 3 jours)."""
        decs = await self.db.fetch(
            """SELECT d.mint, d.ts, t.name, t.symbol, t.creator, t.created_at, t.created_slot FROM decisions d
               JOIN tokens t USING (mint) WHERE d.ts > now() - interval '24 hours' ORDER BY random() LIMIT $1""", n)
        out = []
        for d in decs:
            trades = await self.db.fetch(
                "SELECT * FROM trades WHERE mint=$1 AND ts <= $2 ORDER BY ts LIMIT 3000", d["mint"], d["ts"])
            created = TokenCreated(mint=d["mint"], name=d["name"] or "", symbol=d["symbol"] or "", uri="",
                                   creator=d["creator"] or "", bonding_curve="", slot=d["created_slot"] or 0,
                                   ts=d["created_at"].timestamp(), signature="")
            st = TokenState(created=created)
            for r in trades:
                st.apply(Trade(mint=r["mint"], signature=r["signature"], slot=r["slot"] or 0, ts=r["ts"].timestamp(),
                               trader=r["trader"], is_buy=r["is_buy"], sol=r["sol"], tokens=r["tokens"],
                               v_sol=r["v_sol"], v_tokens=r["v_tokens"]))
            now = d["ts"].timestamp()
            out.append(claude_context(st, now, compute_features(st, now, None, None, self.cfg["features"])))
        return out

    # ------------------------------------------------------------------
    # ------------------------------------------------------------------
    @staticmethod
    def classify_api_error(e: Exception) -> str | None:
        """Erreurs qui demandent une action de l'utilisateur (crédit épuisé, clé invalide)."""
        msg = str(getattr(e, "message", "") or e).lower()
        if "credit balance" in msg or "billing" in msg or "purchase credits" in msg:
            return "credit"
        if isinstance(e, anthropic.AuthenticationError) or getattr(e, "status_code", None) == 401:
            return "auth"
        if isinstance(e, anthropic.PermissionDeniedError):
            return "permission"
        return None

    async def report_api_problem(self, e: Exception) -> None:
        kind = self.classify_api_error(e)
        if kind is None or self.bus is None:
            return
        await self.bus.set_json("apex:claude:status", {"ok": False, "problem": kind, "ts": time.time()})
        last = float(await self.bus.r.get("apex:claude:last_alert") or 0)
        if time.time() - last < 6 * 3600:
            return                      # une alerte toutes les 6 h au plus
        await self.bus.r.set("apex:claude:last_alert", str(time.time()))
        text = {
            "credit": "💳 <b>Crédit API Claude épuisé.</b> Recharge sur console.anthropic.com (Billing). "
                      "En attendant, le scanner continue de tourner et d'apprendre, mais sans nouvelles idées de Claude.",
            "auth": "🔑 <b>Clé API Claude refusée</b> (invalide ou révoquée). Vérifie ANTHROPIC_API_KEY dans le .env du serveur.",
            "permission": "⛔ <b>Accès API Claude refusé</b> pour ce modèle ou cette organisation. Vérifie ton compte Anthropic.",
        }[kind]
        await self.bus.publish(B.NOTIFY, {"type": "urgent", "text": text})

    async def health_check(self) -> dict:
        """Vérification légère (quelques tokens) que l'API Claude répond et que le crédit n'est pas épuisé."""
        try:
            await self.client.messages.create(
                model=self.c["model"], max_tokens=64, output_config={"effort": "low"},
                messages=[{"role": "user", "content": "Réponds juste OK."}])
            st = {"ok": True, "ts": time.time()}
            await self.bus.set_json("apex:claude:status", st)
            return st
        except anthropic.APIStatusError as e:
            await self.report_api_problem(e)
            return {"ok": False, "error": e.status_code}
        except anthropic.APIConnectionError:
            return {"ok": False, "error": "connexion"}

    async def resume(self, supervisor: Any) -> None:
        """Au démarrage : termine le traitement des propositions interrompues (redémarrage
        pendant le test des features) et donne leur modèle ombre aux features validées."""
        async with self._lock:
            for p in await self.db.fetch(
                    "SELECT id, problem_type, raw FROM claude_proposals WHERE status='received' ORDER BY id"):
                raw = p["raw"] or {}
                known = {r["feature_id"] for r in await self.db.fetch(
                    "SELECT feature_id FROM claude_features WHERE proposal_id=$1", p["id"])}
                samples = await self.sample_contexts()
                accepted = 0
                for f in raw.get("features", [])[:5]:
                    fid = "cx_" + _slug(f["name"]) + "_" + hashlib.sha1(f["code"].encode()).hexdigest()[:6]
                    if fid not in known:
                        accepted += await self._handle_feature(f, p["id"], p["problem_type"], samples, supervisor)
                for et in raw.get("error_types", [])[:3]:
                    await self._handle_error_type(et, supervisor)
                await self.db.execute("UPDATE claude_proposals SET status=$2 WHERE id=$1", p["id"],
                                      f"processed(resumed):{accepted}_features_shadow")
                log.info("proposition #%s reprise après interruption", p["id"])
            orphans = await self.db.fetch(
                """SELECT f.feature_id, f.problem_type FROM claude_features f WHERE f.status='shadow' AND NOT EXISTS (
                     SELECT 1 FROM corrections c WHERE c.params_after->>'feature_id' = f.feature_id
                       AND c.status IN ('applying','evaluating','improvement','promoted'))""")
            for o in orphans:
                await supervisor.add_claude_feature_shadow(o["feature_id"], f"CLAUDE:{o['problem_type']}")
                log.info("feature %s : modèle ombre créé (reprise)", o["feature_id"])

    async def run(self, trigger: str, problem_type: str, diagnosis: dict, supervisor: Any = None) -> dict | None:
        if self._lock.locked() or not await self._budget_ok():
            return None
        async with self._lock:
            self._last_run = time.time()
            user = await self.build_context(trigger, problem_type, diagnosis)
            try:
                result, usage = await self.call_claude(user)
            except anthropic.RateLimitError:
                log.warning("API Claude : rate limit")
                return None
            except anthropic.APIStatusError as e:
                log.error("API Claude : %s %s", e.status_code, e.message)
                await self.report_api_problem(e)
                return None
            except anthropic.APIConnectionError:
                log.error("API Claude : connexion impossible")
                return None
            pid = await self.db.fetchval(
                """INSERT INTO claude_proposals (ts, trigger, problem_type, hypothesis, n_features, n_error_types, status, raw, usage)
                   VALUES (now(),$1,$2,$3,$4,$5,$6,$7,$8) RETURNING id""",
                trigger, problem_type, (result or {}).get("hypothesis"), len((result or {}).get("features", [])),
                len((result or {}).get("error_types", [])), "received" if result else "empty", result or {}, usage)
            if not result:
                return None
            samples = await self.sample_contexts()
            accepted = 0
            for f in result.get("features", [])[:5]:
                accepted += await self._handle_feature(f, pid, problem_type, samples, supervisor)
            for et in result.get("error_types", [])[:3]:
                await self._handle_error_type(et, supervisor)
            await self.db.execute("UPDATE claude_proposals SET status=$2 WHERE id=$1", pid, f"processed:{accepted}_features_shadow")
            await self.db.log_event("info", "claude", f"proposition #{pid} ({trigger}) : {result.get('hypothesis')}",
                                    {"accepted_features": accepted})
            return result

    async def _handle_feature(self, f: dict, pid: int, problem_type: str, samples: list[dict], supervisor: Any) -> int:
        fid = "cx_" + _slug(f["name"]) + "_" + hashlib.sha1(f["code"].encode()).hexdigest()[:6]
        res = await asyncio.to_thread(run_isolated, f["code"], f["test_code"], samples, self.c["sandbox_timeout_s"])
        status, reason = "shadow", None
        if not res.get("ok"):
            status, reason = "sandbox_failed", res.get("error", "")[:1000]
        else:
            vals = [v for v in res["outputs"] if v is not None]
            if samples and len(vals) < max(3, len(samples) // 4):
                status, reason = "rejected", "renvoie None sur la plupart des tokens réels"
            elif len(set(round(v, 9) for v in vals)) <= 1 and len(samples) >= 5:
                status, reason = "rejected", "valeur constante sur les tokens réels"
        await self.db.execute(
            """INSERT INTO claude_features (feature_id, version, name, description, code, test_code, proposal_id, problem_type, status, reason)
               VALUES ($1,1,$2,$3,$4,$5,$6,$7,$8,$9) ON CONFLICT (feature_id) DO NOTHING""",
            fid, f["name"], f["description"], f["code"], f["test_code"], pid, problem_type, status, reason)
        if status == "shadow" and supervisor is not None:
            await asyncio.sleep(70)     # laisse le feature engine charger la feature
            await supervisor.add_claude_feature_shadow(fid, f"CLAUDE:{problem_type}")
            return 1
        return 0

    async def _handle_error_type(self, et: dict, supervisor: Any) -> None:
        name = _slug(et["name"]).upper()
        if name in BASE_TYPES:
            return
        rule = {"predicted": None if et["predicted"] == -1 else et["predicted"],
                "conditions": [{c["source"]: c["key"], "op": c["op"], "value": c["value"]} for c in et["conditions"]]}
        try:
            DynamicRule.from_json(name, rule)
        except (ValueError, KeyError) as e:
            await self.db.log_event("info", "claude", f"type d'erreur rejeté {name} : {e}")
            return
        await self.db.execute(
            """INSERT INTO error_types (name, description, rule, created_by) VALUES ($1,$2,$3,'claude')
               ON CONFLICT (name) DO UPDATE SET rule=$3, description=$2, active=TRUE""", name, et["description"], rule)
        if supervisor is not None:
            await supervisor.send({"op": "add_error_type", "name": name, "rule": rule})
