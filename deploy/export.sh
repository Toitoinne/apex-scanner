#!/usr/bin/env bash
# À lancer sur TON PC (Git Bash), dans le dossier du projet : bash deploy/export.sh
# Arrête proprement le système, puis prépare le dossier apex-migration/ avec :
#   project.tgz  : le code, la config et le .env (tes clés)
#   pgdata.tgz   : la base (apprentissage, wallets, devs, alertes, paper trading…)
#   apexdata.tgz : les modèles appris (snapshots du learner)
set -euo pipefail
cd "$(dirname "$0")/.."
OUT=apex-migration
mkdir -p "$OUT"

echo "1/4  Sauvegarde finale de l'état du learner…"
docker compose exec -T redis redis-cli XADD apex:control '*' d '{"op":"snapshot","stable":true,"label":"migration","cmd_id":"migration"}' >/dev/null || true
sleep 20

echo "2/4  Arrêt du système (les données restent sur ce PC)…"
docker compose stop

echo "3/4  Archive du code et de la configuration…"
tar --exclude=.venv --exclude=data --exclude=.pytest_cache --exclude='__pycache__' --exclude="$OUT" \
    --exclude='*.egg-info' -czf "$OUT/project.tgz" .

echo "4/4  Archive des données (base + modèles)…"
MSYS_NO_PATHCONV=1 docker run --rm -v apexscanner_pgdata:/v:ro -v "$(pwd -W 2>/dev/null || pwd)/$OUT:/b" alpine \
    tar czf /b/pgdata.tgz -C /v .
MSYS_NO_PATHCONV=1 docker run --rm -v apexscanner_apexdata:/v:ro -v "$(pwd -W 2>/dev/null || pwd)/$OUT:/b" alpine \
    tar czf /b/apexdata.tgz -C /v .
cp deploy/install.sh "$OUT/"

echo
ls -lh "$OUT"
echo
echo "Export terminé. Le système est ARRÊTÉ sur ce PC (ne le relance pas : deux instances"
echo "enverraient des alertes en double et consommeraient deux fois les crédits Helius)."
