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
- **2026-10-09 — Sortie apprise** (commit c7bed47). Un modèle en ligne (apex/trading/exit_model.py) prédit toutes les 10 s, pour chaque token où une position est ouverte, si le prix touchera +30 % avant −20 % dans les 10 min ; deux stratégies l'utilisent (SORTIE_APPRISE, SORTIE_APPRISE_LIBRE), comparées par le bandit aux 5 stratégies fixes. Elles se comportent comme des stratégies à stop suiveur tant que le modèle n'a pas 2 000 situations apprises. À surveiller : `labeler:stats.sortie_apprise.fiabilite` (> 0 = mieux que le hasard) et la place de ces deux stratégies dans `bandit_best_by_policy` / `outcomes_by_policy_24h` après 24–48 h. Si elles restent dernières après 48 h, ajuster `hold_threshold` (0,35) plutôt que de les retirer.
- **2026-10-09 — Smart wallets v2** (commit 153d6b8). Gains réellement encaissés uniquement (positions non vendues ignorées, créateurs exclus) ; robots (> 200 tokens/jour, > 90 % de réussite, flippers, snipers) écartés ; smart = sélectif (≤ 60 tokens/jour), 40–90 % de réussite, gagnant. Base recalculée depuis 48 h de trades : 5 668 smart, 2 608 robots (avant : 8 809 smart dont beaucoup de robots). À surveiller : baisse des erreurs FAUX_SMART_MONEY (≈ 318/24 h avant) et poids des features smart_share / smart_count.
- **2026-10-09 — Panel de 42 stratégies de sortie + classement + évolution automatique** (commits 7225966, d7e6bb9). Familles : objectif fixe, sortie au temps, stop suiveur pur, sécurisation de la mise, paliers, sortie apprise (3 seuils). Simulateur : frais de réseau par vente (`exits.sale_cost`). Simulations seulement sur les décisions alertables ; tokens morts soldés après 30 min. Classement horaire (`apex:exits:board`, commande Telegram /sorties, section `sorties` du rapport) avec marge à 95 %, stabilité sur deux moitiés et verdict honnête. Évolution toutes les 12 h (`apex/trading/evolve.py`) : variantes des 3 meilleures, retrait des variantes perdantes, 12 au plus ; la base n'est jamais retirée. Constat au lancement : sur les 10 % de décisions jugées les meilleures, TOUTES les stratégies perdent environ −10 % par trade ; les écarts entre stratégies sont faibles. Le levier principal reste la sélection des tokens (entrée). Ne pas ajouter de stratégies à la main : l'évolution s'en charge ; surveiller plutôt que la meilleure stratégie passe au-dessus de 0 sur la sélection.
- **2026-10-09 — Mise à l'épreuve de l'entrée** (commit c5364c6). Audit (apex/reporting/entry_audit.py, toutes les 6 h, /entree, section `entree` du rapport) : la seule capitalisation triait mieux que le modèle à chaque point (0,90 contre 0,80 à 10 min) et les 10 % mieux notés étaient annoncés à 28 % pour 17 % réels. Correction : champion et recalage des probabilités sur les décisions ALERTABLES uniquement, LightGBM avec poids 0,2 pour les autres. À surveiller sous 24–48 h : `entree.modele_auc` doit dépasser l'AUC de mc_sol par point, et l'écart de calibration des 10 % mieux notés doit fondre. Indices sans effet mesuré : réseaux sociaux (meta_*), dev_rug_rate / dev_winner_rate, activité globale du marché, heure.
- **2026-10-09 — Gain attendu par stratégie** (commit e2401d7, apex/learning/ev.py). Choix de la sortie token par token + score « ev ». Validé toutes les 6 h sur des tokens jamais vus ; s'active seul (bras AUTO) s'il bat la meilleure stratégie fixe de 1 point deux fois de suite (`apex:ev:stats`, `apex:ev:history`). Premier test : pas d'avantage (−13,4 % contre −13,2 %) → en observation. Si toujours inactif après plusieurs jours, la piste est d'enrichir les indices d'entrée, pas de forcer l'activation.
- **2026-10-09 — Sortie apprise : signaux d'effondrement** (commit e39bcf3) : part des ventes sur 10 s, plus grosse vente, nombre de vendeurs, ventes du créateur, accélération du volume, distance et temps depuis le sommet. À surveiller : `labeler:stats.sortie_apprise.fiabilite` (était ~0,02).
- **2026-10-09 — Garde-fou des trades réels** (commit f02d817) : suspension si les 20 derniers trades RÉELS perdent plus que le seuil, même si les simulations vont bien.
- **2026-10-09 — Étude des nouveaux indices d'entrée** (hors-ligne, réputation des wallets sur J-1, test sur J) : à taille de token égale, smart wallets, achats groupés, robots, mise du créateur et accélération des acheteurs n'apportent rien (AUC conditionnelle 0,47–0,53). Ne pas les réintroduire sans nouvelle preuve. Le propriétaire refuse toute dépense supplémentaire (données payantes, flux plus rapide).
- **2026-10-09 — Terrain en observation** (commit 68af9a1) : décisions d'étude `mig60`, `mig300`, `mig900` (après migration, prix et indices PumpSwap) et `vague2` (token de plus d'1 h qui repart). Aucune alerte, ignorées par le learner, exclues du classement principal et du gain attendu ; classées à part dans `apex:exits:board.terrains` (/sorties). Si un terrain montre une stratégie « solide » (gagne de façon prouvée) sur plusieurs jours, proposer au propriétaire de l'intégrer (ajouter le point à `bandit.decision_points`) — ne PAS l'intégrer sans son accord.
- **2026-10-09 — Sortie apprise** : optimiseur Adam et utilisation seulement si elle bat le taux de base (`ready` dépend de `skill`) ; réapprentissage depuis zéro (version 2 du fichier modèle).
- **2026-10-09 — Audit complet de l'après-midi** : (1) audit des données > 5 min → échantillonnage des transactions absentes (e48a6f4) ; (2) flux public saturé (7 à 60 s de retard, mesuré par connexion indépendante) → retard mesuré en continu (`apex:feed:lag`) et ajouté au délai des ordres simulés ; 2e source gratuite PublicNode ajoutée dans `SOLANA_WS_URLS` (.env, copie de sauvegarde dans /root) → retard ramené à ~5–10 s ; (3) décisions d'étude émises en retard au redémarrage (prix du moment, heure prévue → faux x5000) → corrigé (85896d6) + garde-fou général « jamais plus de 4 min de retard » ; les ~70 décisions d'étude fausses du 09/10 entre 15 h 03 et 15 h 06 UTC (points mig*) restent en base : les ignorer dans les bilans de terrain ; (4) disque 71 % (anciennes images de déploiement + 40 sauvegardes de 420 Mo) → ménage, 12 sauvegardes, ménage automatique après chaque déploiement. Les « prix bas » PumpSwap signalés au début de l'audit étaient de vrais effondrements (rugs), pas un bug du décodeur.
- **2026-10-09 — Décision du propriétaire : flux de données payant à l'état PRÊT.** Il prendra un flux rapide (gRPC Yellowstone ; option la moins chère identifiée : Alchemy en paiement à l'usage, ~75 $/To, soit ~25–45 $/mois pour notre volume) quand le bot passera PRÊT, avant tout trading réel. D'ici là : aucune dépense. Quand l'état devient PRÊT, le rappeler dans le message Telegram et proposer de brancher ce flux (≈ 1 h de travail : client gRPC dans l'ingestor, à côté des sources gratuites).
- **2026-10-09 (soir) — Flux public : 2 connexions décalées par URL** (`ingestion.connections_per_url: 2`, `connection_stagger_s: 20`). Bug confirmé par 3 audits (16 h 08–16 h 25 UTC) : 72–79 % des achats/ventes vus. Cause mesurée hors serveur : le RPC public (api.mainnet-beta) prend 10–60 s de retard puis coupe la connexion toutes les 1–2 min (code 1002) en perdant sa file → ~10 % de trades perdus par connexion ; PublicNode ne transmet plus que ~5 % des transactions, avec 6–8 s de retard en plus. Test sur 1 014 transactions pump.fun relues dans les blocs : une connexion 89–91 %, deux connexions décalées 100 %. Gratuit (aucun crédit Helius). À surveiller : `audit.completude_pct` ≥ 99 ; RAM/CPU de l'ingestor ; si le RPC public refuse la 2e connexion (reconnexions en boucle de ws2), revenir à 1.
