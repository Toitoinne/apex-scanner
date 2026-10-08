# APEX SCANNER — architecture

## 1. Vue d'ensemble

```
                 Helius WS (logsSubscribe pump.fun)      PumpPortal WS (créations, migrations)
                                  │                                   │
                                  └──────────────┬────────────────────┘
                                                 ▼
                                         ┌──────────────┐  dédup (signature), reconnexion auto
                                         │   INGESTOR   │  ── jamais modifié automatiquement
                                         └──────┬───────┘
                                    apex:raw    │  (create / trade / migration)
                    ┌───────────────────────────┼─────────────────────────────┐
                    ▼                                                         ▼
           ┌────────────────┐  apex:decisions                        ┌──────────────┐
           │ FEATURE ENGINE │ ─────────────────────────────────────▶ │   LABELER    │
           │ + FILTRES DURS │   (T+30s,1,2,5,10 min, migration)      │  L2 L5 L15   │
           │ + wallets/devs │ ◀── apex:closed (PnL wallets, rugs) ── │  L60 + rug   │
           └───────┬────────┘                                        └──────┬───────┘
                   │ apex:decisions                                         │ apex:labels
                   ▼                                                        ▼
           ┌───────────────────────────────────────────────────────────────────────┐
           │ LEARNER — BOUCLE 1 (secondes)                                          │
           │  ensembles par horizon : règles, LR, ARF, HAT, LightGBM horaire, ombres│
           │  prequential, ADWIN, champion amorti, calibration Platt/isotonique     │
           │  journal des erreurs, bandit Thompson (seuil × point de décision)      │
           └──────┬──────────────────────────────▲─────────────────────────┬───────┘
         apex:alerts                apex:control │ apex:control_ack         │ PostgreSQL
                  │                              │                          ▼
                  │              ┌───────────────┴──────────────┐   evaluations, errors,
                  │              │ SUPERVISOR — BOUCLES 2 et 3   │◀─ predictions, alerts…
                  │              │ courbes · tendances · états   │
                  │              │ diagnostic · corrections      │──▶ API Claude (sandbox)
                  │              │ effet · table d'efficacité    │
                  │              │ garde-fous · snapshots        │
                  │              └───────────────┬──────────────┘
                  ▼                 apex:notify  ▼
           ┌─────────────────────────────────────────────┐        ┌───────────┐
           │ NOTIFIER (Telegram) alertes, suivis, rapports│        │ DASHBOARD │
           └─────────────────────────────────────────────┘        └───────────┘
```

Chaque service est un processus Python asyncio indépendant (conteneur Docker), relié
par Redis Streams avec groupes de consommateurs. Les cœurs métier (`FeatureEngine`,
`LabelerEngine`, `Learner`) sont **purs** (horloge = timestamps des événements) : le
backfill les rejoue tels quels, ce qui garantit que les features et labels du
pré-entraînement sont identiques à ceux de la production.

### Les trois boucles

| Boucle | Fréquence | Où | Ce qu'elle fait |
|---|---|---|---|
| 1 | secondes | `learning/core.py` | prédit pour TOUS les tokens à chaque point de décision, reçoit chaque label dès son horizon, classe l'erreur, apprend avec un poids ∝ type et coût de l'erreur |
| 2 | 15 min | `supervision/` | calcule les courbes (1 h/6 h/24 h/7 j), teste les tendances (Mann-Kendall + Theil-Sen), classe PROGRESSION / PLATEAU / RÉGRESSION / RÉGRESSION_TYPE / DÉCALIBRATION / DÉRIVE, diagnostique, applique UNE correction par composant (en ombre si possible) |
| 3 | heures/jours | `supervision/meta.py` + `service.py` | mesure l'effet de chaque correction (Wilcoxon apparié pour les ombres, Mann-Whitney avant/après sinon), annule les dégradations, met à jour la table d'efficacité ; le choix des corrections est un bandit contextuel (Thompson Beta) sur (état, régime de marché) |

### Choix techniques notables

- **Ingestion** : `logsSubscribe` Helius sur le programme pump.fun et décodage des events
  Anchor (`TradeEvent`, `CreateEvent`, `CompleteEvent`) : flux complet sans coût au message.
  PumpPortal est branché en parallèle pour les créations/migrations (gratuites) ; ses trades
  sont **facturés 0,01 SOL / 10 000 messages** et donc désactivés par défaut.
- **Prix d'entrée réaliste** : produit constant sur réserves virtuelles + frais ; tous les
  labels sont calculés depuis le prix effectif pour 1 SOL (configurable), pas le spot.
- **Bandit en information complète** : comme on prédit pour tous les tokens, chaque label
  met à jour tous les bras (seuil, point) qui *auraient* alerté → convergence rapide.
- **Ombres démarrées à chaud** : un buffer de rejeu (60 000 exemples) permet d'entraîner
  immédiatement un concurrent ombre ; son effet est mesuré sur les mêmes exemples que le
  champion (test apparié), ce qui rend la boucle 3 statistiquement solide.
- **Démarrage à froid** : un concurrent fixe « règles + momentum » est champion au départ ;
  les modèles le détrônent via la sélection de champion dès qu'ils font mieux en live.
- **Reprise sans perte** : le learner n'acquitte ses messages Redis qu'après une sauvegarde
  d'état (5 min) ; au redémarrage il restaure l'état puis rejoue les messages en attente.
  Les alertes sont dédupliquées par `decision_id` (contrainte UNIQUE).
- **Garde-fous structurels** : `Config.set_override` refuse toute clé `ingestion.*`,
  `safety.*`, `labels.*`, `supervision.*` ; `safety.yaml` est monté en lecture seule et
  `SafetyFilters` est figé (`MappingProxyType`). Le système ne fait que *proposer* des
  changements de filtres dans ses rapports.
- **Code généré par Claude** : liste blanche AST (pas d'import hors math/statistics, pas de
  `while`, pas de dunder, pas d'`open/eval/getattr`…), test unitaire + exécution sur de vrais
  tokens dans un sous-processus `python -I` avec limites CPU/mémoire, puis exécution en
  production avec builtins restreints et désactivation automatique si lente ou en erreur.
  La feature n'entre que dans un concurrent ombre ; elle n'atteint le champion que si
  l'ombre bat le champion (test apparié).

## 2. Schéma de base de données (`sql/001_schema.sql`)

| Table | Type | Rôle |
|---|---|---|
| `tokens` | table | métadonnées, migration, pic de MC, rug, issue |
| `trades` | hypertable, rétention 3 j | trades bruts (purge après agrégation) |
| `candles_1m` | agrégat continu | bougies minute conservées |
| `market_context` | hypertable | température, lancements/h, migrations/h, prix SOL |
| `decisions` | hypertable | features (JSONB) + versions + filtres au point de décision |
| `predictions` | hypertable, 30 j | probabilité brute/calibrée du champion par horizon |
| `labels` | hypertable | y, rendement max, drawdown, temps au pic, rug, PnL simulé |
| `evaluations` | hypertable, 30 j | prequential : une ligne par modèle × label (logloss, type d'erreur, coût) — base de toutes les courbes |
| `errors` / `error_types` | hypertable / table | journal des erreurs (features, contexte, coût) ; types dynamiques (règle DSL) |
| `alerts` | table | alertes, message Telegram, résultats +15/+60 min |
| `wallets` / `devs` | tables | smart wallets, bots, graphe de financement (2 sauts), devs ruggers |
| `curve_points` / `reference_curves` | hypertable / table | courbes suivies ; références issues du backfill |
| `learning_states` / `diagnoses` | hypertable / table | états détectés et diagnostics (confiance) |
| `corrections` / `efficacy` / `frozen_components` | tables | journal complet des corrections (avant/après, courbes au moment de l'action, effet) ; table d'efficacité ; gels |
| `snapshots` | table | snapshots (modèles, config, bandit), marqués stables ou non |
| `claude_proposals` / `claude_features` | tables | propositions de Claude, y compris rejetées et pourquoi |
| `system_events` | hypertable | journal système (rapports) |

## 3. Arborescence

```
apex/
  __main__.py              point d'entrée : python -m apex <service>
  config.py                config YAML + secrets .env + surcharges runtime (liste blanche)
  events.py  bus.py  db.py événements normalisés, Redis Streams, PostgreSQL
  ingestor/                pump_decoder.py (events Anchor) · service.py (Helius + PumpPortal)
  features/                curve.py (bonding curve, slippage) · state.py (features versionnées)
                           wallets.py (smart/bots/devs, graphe de financement) · market.py
                           engine.py (planification des points de décision) · service.py
  safety/filters.py        filtres durs (lecture seule)
  labeler/                 labels.py (labels, rug, trade simulé) · engine.py · service.py
  errors/classifier.py     journal des erreurs (types de base + règles dynamiques)
  learning/                models.py · ensemble.py · calibration.py · core.py (boucle 1) · service.py
  alerting/                bandit.py (Thompson) · slippage.py
  supervision/             stats.py (tendances, effets) · metrics.py (courbes) · detector.py
                           corrections.py (catalogue) · meta.py (boucle 3) · service.py
  claude_improver/         sandbox.py · service.py (API Claude, sortie JSON structurée)
  reporting/               reports.py (rapports 6 h / quotidien) · telegram.py (alertes, commandes)
  dashboard/               app.py (FastAPI) · static/index.html (Chart.js)
  backfill/                dune.py · replay.py (rejeu chronologique) · run.py
config/config.yaml  config/safety.yaml
sql/001_schema.sql  sql/dune_backfill.sql
tests/                     labels, erreurs, tendances, effets, décodeur, sandbox, bandit, e2e
```

## 4. Clés API nécessaires

| Clé | Usage | Obligatoire |
|---|---|---|
| `HELIUS_API_KEY` | websocket logsSubscribe (flux), RPC (autorités mint/freeze, graphe de financement) | oui |
| `TELEGRAM_BOT_TOKEN` + `TELEGRAM_CHAT_ID` | alertes, rapports, commandes | oui (sinon logs) |
| `ANTHROPIC_API_KEY` | amélioration par Claude (section 11) | recommandé |
| `DUNE_API_KEY` | backfill 7–14 jours | recommandé (sinon démarrage à froid) |
| `PUMPPORTAL_API_KEY` | trades PumpPortal (payants) | non |
| `DASHBOARD_TOKEN` | protection du dashboard | recommandé |

## 5. Coût mensuel — configuration économe sans perte de performance (par défaut)

| Poste | Choix | Coût |
|---|---|---|
| VPS | 4 vCPU / 8 Go (ex. Hetzner CX32 ou CPX31, Allemagne) | ≈ 8–16 €/mois |
| Flux pump.fun complet | websocket(s) gratuit(s) `SOLANA_WS_URLS` (défaut : RPC public Solana) — PAS Helius : ses websockets coûtent 20 crédits/Mo et le flux complet (plusieurs Go/jour) dépasserait le million de crédits gratuits | 0 € |
| Enrichissement RPC | Helius gratuit (1 M crédits/mois, 10 req/s) : 1 crédit/appel, **ciblé** sur les candidats, plafonné à 30 000 crédits/jour et 5 appels/s | 0 € |
| PumpPortal | créations/migrations uniquement | 0 € |
| Backfill | désactivé : verrou de démarrage + références établies sur les 24 premières heures | 0 € |
| API Claude | Opus 5.5, effort `medium`, 2 appels/jour maximum | ≈ 8–15 $/mois |
| **Total** | | **≈ 18–30 €/mois** |

### Pourquoi les performances ne sont pas réduites

| Économie | Risque | Compensation gratuite |
|---|---|---|
| Pas de graphe de financement pour tous les tokens | moins bonne détection des faux acheteurs | **enrichissement ciblé** : le graphe à 2 sauts et la vérification mint/freeze sont faits pour les seuls tokens qui peuvent être alertés (≥ 12 acheteurs dans les 30 premières s, ou proba du champion ≥ 60 % du seuil). Les ~95 % de tokens restants ne seraient jamais alertés : l'information y serait inutile. La feature `enriched` indique au modèle quels tokens ont été enrichis, pour qu'il ne confonde pas « non vérifié » et « indépendant ». |
| Idem | bundles/bots coordonnés non vus | **groupes d'opérateurs gratuits** : des wallets qui achètent dans le même slot sur au moins 2 tokens différents sont fusionnés en un seul acteur ; appris en continu depuis le flux déjà reçu, sur TOUS les tokens. |
| Pas de backfill | alertes de mauvaise qualité au démarrage | **verrou de démarrage** : aucune alerte avant 3 000 labels L60 (≈ 1–2 h de flux pump.fun). Avec des milliers de décisions par heure, les modèles en ligne apprennent vite. Les courbes de référence sont fixées après 24 h. |
| Claude moins souvent | progression plus lente des features | 2 appels/jour, réservés aux vrais problèmes (plateau, régression d'un type, erreurs AUTRE en hausse) ; le reste des boucles 2/3 fonctionne sans Claude. |
| Petit VPS | — | modèles à pleine taille (ARF 10 arbres, 8 concurrents) : tient dans 8 Go. |

Reste une différence assumée : la toute première alerte arrive après le verrou (1–2 h)
au lieu d'immédiatement. Pour lever le quota d'enrichissement si ton plan le permet :
`features.enrichment_daily_credit_budget`. Le volume réel du flux est mesuré
(`apex:ingestor:stats` → `volume`, avec la projection en crédits Helius) : tu sais ainsi
exactement ce que coûterait un passage du flux sur Helius.

### Continuité du flux (parade anti-panne)

1. **Redondance** : toutes les URL de `SOLANA_WS_URLS` tournent en parallèle ; chaque
   événement est dédupliqué par signature, donc une source qui tombe ne coupe rien.
2. **Chien de garde** (`ingestor/watchdog.py`, toutes les 2 s) : une source ouverte mais
   muette 30 s est reconnectée de force ; le flux est aussi recoupé avec PumpPortal (si
   PumpPortal voit des lancements et les logs aucun, le flux est déclaré cassé).
3. **Secours Helius automatique** : si TOUTES les sources gratuites sont en panne depuis
   15 s, le flux bascule sur le websocket Helius, puis revient sur le gratuit après 5 min
   de stabilité. Les crédits consommés (20/Mo) et ceux de l'enrichissement partagent un
   plafond mensuel commun de 980 000 (< 1 M gratuits) : le secours ne peut jamais te faire
   dépasser l'offre gratuite. L'enrichissement est limité à ~750 k/mois pour laisser
   ~230 k au secours (plusieurs dizaines d'heures de flux de secours par mois, selon le
   volume réel mesuré par l'ingestor).
4. **Trous de données** : toute interruption est enregistrée ; un token dont l'historique
   chevauche un trou n'est jamais alerté, et un label dont l'horizon chevauche un trou
   n'est jamais appris (pas de faux apprentissage sur des prix incomplets).
5. **Alertes** : Telegram immédiat après 60 s de panne (en précisant si le secours est
   actif), puis message de rétablissement ; état détaillé dans `/stats`.

## 6. Performance visée et mesure

- Point de décision → alerte < 5 s : tick du feature engine toutes les 200 ms, prédiction
  en mémoire (ms). La latence est mesurée (`alert_latency_s`, `decision_latency_ms`).
- Label → modèle mis à jour < 1 s : labeler tick 250 ms, apprentissage incrémental
  (`label_to_update_latency_s`).
- Cycle d'auto-supervision < 30 s : requêtes indexées sur hypertables ; durée journalisée.

## 7. BSC (four.meme) — étape suivante

Les événements portent un champ `chain` ; il suffit d'ajouter un adaptateur d'ingestion
(logs EVM du contrat four.meme) et les constantes de courbe correspondantes. Le reste de
la chaîne (features génériques, labels, boucles 1–3) est indépendant de la chaîne.

## Mises à jour

Toute modification passe par `deploy/safe_deploy.sh` : zones protégées, tests dans une image candidate, contrôle de santé de 4 min et retour automatique à la version précédente en cas de problème.
- Le suivi Claude Code (4×/jour) déploie ses corrections par ce même circuit et en rend compte sur Telegram.

## Journal des évolutions

Tenu par le suivi Claude Code : chaque évolution y est notée (date, changement, hypothèse, chiffre à surveiller), puis son bilan est fait aux passages suivants (annulation si elle dégrade les résultats).

- **2026-10-08 — Prix d'entrée après migration** (commit 1727a53). Bug : les décisions prises sur un token déjà migré utilisaient le prix périmé de la bonding curve, alors que la suite du prix venait de PumpSwap → gains simulés fictifs (PnL médian +375 %, ~75 % des « x10 » des outcomes étaient faux). Correction : prix PumpSwap récent (< 60 s) ou abstention. Les décisions antérieures concernées ont été oubliées par le learner, purgées de la base, et le bandit a été remis à zéro. À surveiller : PnL moyen des outcomes du même ordre que le paper (quelques % par décision, pas des dizaines) ; labeler.migrated_followed > 0.
