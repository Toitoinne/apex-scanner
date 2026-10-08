# APEX SCANNER

Détecteur de memecoins pump.fun **auto-apprenant et auto-supervisé** : il apprend de
chaque résultat (boucle 1), surveille lui-même s'il progresse et se corrige (boucle 2),
et apprend quelles corrections marchent (boucle 3). Il **n'exécute aucun ordre** : il
prédit, apprend, se supervise et alerte ; tu décides.

Architecture, schéma de base, clés et coûts : voir [ARCHITECTURE.md](ARCHITECTURE.md).

## Installation (VPS, Docker)

Prérequis : Docker + Docker Compose, un VPS 4 vCPU / 8 Go en Allemagne ou aux Pays-Bas
(≈ 8–16 €/mois). La configuration par défaut est **économe sans perte de performance (≈ 18–30 €/mois au total)** :
voir ARCHITECTURE.md § 5 pour le détail et comment monter en gamme.

```bash
git clone <ton-repo> apex && cd apex
cp .env.example .env        # puis remplis les clés
```

1. **Helius** (offre gratuite : 1 M crédits/mois, 10 req/s) : crée une clé sur dashboard.helius.dev →
   `HELIUS_API_KEY`. Elle sert uniquement à l'enrichissement ciblé (1 crédit/appel). Le flux pump.fun
   complet passe par `SOLANA_WS_URLS` (défaut : RPC public Solana, gratuit). Tu peux y ajouter, séparé
   par une virgule, le websocket d'une autre offre gratuite pour la redondance.
2. **Telegram** : crée un bot avec @BotFather → `TELEGRAM_BOT_TOKEN` ; envoie un message
   au bot puis récupère ton identifiant (@userinfobot) → `TELEGRAM_CHAT_ID`.
3. **Claude** : clé sur console.anthropic.com → `ANTHROPIC_API_KEY`.
4. **Dune** (optionnel, désactivé par défaut) : clé API → `DUNE_API_KEY`. Crée une requête sur dune.com avec le
   contenu de `sql/dune_backfill.sql` (paramètres `start`, `end`, `sample_mod`), vérifie
   les noms de colonnes dans l'explorateur `pumpdotfun_solana.*`, puis mets son id dans
   `config/config.yaml` → `backfill.dune_query_id`.
5. `DASHBOARD_TOKEN` et `POSTGRES_PASSWORD` : valeurs aléatoires longues.

## Lancement

```bash
docker compose up -d db redis migrate
```

Optionnel (si `backfill.provider: dune`) :

```bash
docker compose --profile backfill run --rm backfill
```

Le backfill télécharge l'historique (mis en cache dans le volume `apexdata`), le rejoue
chronologiquement à travers les mêmes composants qu'en production, puis écrit l'état
pré-entraîné, les bases smart wallets/devs et les courbes de référence. S'il échoue ou
n'est pas configuré (cas par défaut), le système démarre sur « règles de sécurité +
momentum », les modèles prennent le relais selon leur précision live, et les courbes de
référence de la boucle 2 sont fixées automatiquement après 48 h de live.

```bash
docker compose up -d
```

```bash
docker compose logs -f learner supervisor
```

Dashboard (lié à 127.0.0.1 sur le VPS) : depuis ton poste, ouvre un tunnel SSH puis
`http://localhost:8080/?token=<DASHBOARD_TOKEN>`.

```bash
ssh -L 8080:localhost:8080 user@ton-vps
```

## Configuration

- `config/config.yaml` : seuils des labels, points de décision, poids des erreurs,
  bandit (grille de seuils, alertes/jour 10–30), supervision (fenêtres, α des tests,
  délai de mesure des effets, limite de corrections/heure, plancher de précision),
  Claude (modèle, appels/jour), rapports.
- `config/safety.yaml` : filtres de sécurité durs. Monté en **lecture seule** ; jamais
  modifié par les boucles automatiques (le système peut seulement proposer un changement
  dans ses rapports).
- Les boucles 2/3 n'écrivent jamais ces fichiers : elles appliquent des surcharges runtime
  journalisées et réversibles (table `corrections`) sur une liste blanche de clés.

## Telegram

Alerte : nom, CA, point de décision, MC, probabilité calibrée, modèle champion, 3 raisons,
drapeaux de risque, slippage estimé 0,5/1/2 SOL, liens DexScreener/Solscan/pump.fun ;
réponses automatiques à +15 et +60 min.

Commandes : `/top` `/stats` `/erreurs` `/etat` `/corrections` `/model` `/features`
`/seuil` `/pause` `/reprendre`.

Rapport toutes les 6 h + rapport quotidien (heure UTC dans la config). Alerte immédiate
uniquement en cas de gel de corrections, de régression forte ou de panne d'un service.

## Développement local (sans Docker)

```bash
python -m venv .venv
```

```bash
.venv/Scripts/pip install -e ".[dev]"
```

```bash
.venv/Scripts/python -m pytest -q
```

Sous Linux/macOS remplace `.venv/Scripts/` par `.venv/bin/`. Pour lancer un service hors
Docker, définis `REDIS_URL`, `DATABASE_URL` et `DATA_DIR` dans `.env`, puis
`python -m apex <ingestor|features|labeler|learner|supervisor|notifier|dashboard>`.

## Tests

Couverture des parties critiques (46 tests) : calcul des labels (cible/stop, horizon,
slippage, rug, trade simulé), classement des erreurs (types de base + règles
dynamiques), tests de tendance (progression, régression, bruit, plateau), mesure de
l'effet des corrections (avant/après et apparié), méta-bandit, dérive KS, décodeur
pump.fun, filtres de sécurité et garde-fous de configuration, bandit d'alerte, sandbox
du code généré, et un test bout-en-bout qui rejoue des tokens synthétiques à travers
features → filtres → labeler → learner → bandit → commandes de correction → snapshot.

## Avertissements

- Données on-chain : la structure des events pump.fun et les tables Dune évoluent ;
  vérifie le décodeur (`ingestor/pump_decoder.py`) et la requête de backfill après
  chaque mise à jour du programme.
- Frais pump.fun (`fees.pump_fee_bps`) : vérifie la valeur actuelle.
- Aucun résultat passé ou simulé ne garantit un résultat futur. Ce logiciel n'est pas
  un conseil en investissement.
