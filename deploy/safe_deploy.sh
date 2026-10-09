#!/usr/bin/env bash
# Déploiement SÉCURISÉ d'une version du code (utilisé par le suivi Claude Code et par les mises à jour).
#   safe_deploy.sh <branche|commit>
# 1. le commit doit descendre de la version en production (pas de réécriture d'historique)
# 2. zones interdites : filtres de sécurité, limites de risque, exécution réelle, scripts de
#    déploiement, .env, docker-compose ; clés de config trading.* et risk.* intouchables
# 3. construction de l'image candidate + TOUS les tests doivent passer
# 4. déploiement, puis contrôle de santé pendant 4 min ; au moindre problème : RETOUR AUTOMATIQUE
#    à la version précédente (code + image)
# 5. résultat envoyé sur Telegram et journalisé
set -euo pipefail
REF="${1:-}"
[[ "$REF" =~ ^[A-Za-z0-9._/-]{1,100}$ ]] || { echo "référence invalide"; exit 2; }
exec 9>/var/lock/apex-deploy.lock
flock -n 9 || { echo "un déploiement est déjà en cours"; exit 3; }
cd /opt/apex
LOG=/var/log/apex-deploys.log
SERVICES="ingestor features labeler learner supervisor notifier dashboard trader"

notify() {
  local tok chat
  tok=$(grep -E '^TELEGRAM_BOT_TOKEN=' .env | head -1 | cut -d= -f2- | tr -d '\r')
  chat=$(grep -E '^TELEGRAM_CHAT_ID=' .env | head -1 | cut -d= -f2- | tr -d '\r')
  curl -s -o /dev/null "https://api.telegram.org/bot${tok}/sendMessage" --data-urlencode "chat_id=${chat}" \
       --data-urlencode "disable_web_page_preview=true" --data-urlencode "text=$1" || true
}
say() { echo "$(date -u '+%F %T') $*" | tee -a "$LOG"; }
refuse() { say "REFUSÉ $REF : $1"; notify "🚫 Déploiement refusé ($REF) : $1"; exit 1; }

git fetch -q origin '+refs/heads/*:refs/remotes/origin/*'
NEW=$(git rev-parse --verify -q "origin/$REF^{commit}" || git rev-parse --verify -q "$REF^{commit}" || true)
[ -n "$NEW" ] || refuse "référence introuvable sur GitHub"
OLD=$(git rev-parse HEAD)
[ "$NEW" != "$OLD" ] || { echo "déjà en production"; exit 0; }
git merge-base --is-ancestor "$OLD" "$NEW" || refuse "ce commit ne part pas de la version en production (${OLD:0:7})"
MSG=$(git log -1 --format=%s "$NEW")

# --- zones interdites ---
BAD=$(git diff --name-only "$OLD" "$NEW" | grep -E '^(config/safety\.yaml|apex/safety/|apex/trading/risk\.py|apex/trading/venues\.py|deploy/|\.env|docker-compose\.yml)' || true)
[ -z "$BAD" ] || refuse "fichiers protégés modifiés : $(echo $BAD)"

TMP=$(mktemp -d); trap 'rm -rf "$TMP"' EXIT
chmod 755 "$TMP"   # lisible par l'utilisateur non-root (apex) du conteneur candidat
git archive "$NEW" | tar -x -C "$TMP"
git show "$OLD:config/config.yaml" > "$TMP/old_config.yaml"

say "construction de l'image candidate ${NEW:0:7} : $MSG"
docker build -q -t apex-scanner:candidate "$TMP" >/dev/null || refuse "la construction de l'image a échoué"

docker run --rm -v "$TMP:/w:ro" --entrypoint python apex-scanner:candidate -c "
import yaml, sys
a = yaml.safe_load(open('/w/old_config.yaml')); b = yaml.safe_load(open('/w/config/config.yaml'))
bad = [k for k in ('trading', 'risk') if a.get(k) != b.get(k)]
sys.exit('clés protégées modifiées : ' + ', '.join(bad) if bad else 0)" || refuse "configuration : sections trading/risk protégées"

say "tests…"
if ! docker run --rm -v "$TMP/config:/app/config:ro" --entrypoint python apex-scanner:candidate \
      -m pytest -q -p no:cacheprovider > "$TMP/tests.txt" 2>&1; then
  refuse "tests en échec : $(tail -3 "$TMP/tests.txt" | tr '\n' ' ')"
fi
say "tests OK : $(tail -1 "$TMP/tests.txt")"

# --- déploiement ---
docker tag apex-scanner:latest apex-scanner:previous
git reset -q --hard "$NEW"
docker tag apex-scanner:candidate apex-scanner:latest
docker compose run --rm migrate >/dev/null 2>&1 || true
T0=$(date +%s)
docker compose up -d --force-recreate $SERVICES >/dev/null 2>&1
say "déployé ${NEW:0:7}, contrôle de santé (4 min)…"
sleep 240

# --- contrôle de santé ---
PROBLEM=$(docker compose exec -T redis redis-cli hgetall apex:heartbeats | paste - - | awk -v now="$(date +%s)" '
  { split($2, a, "."); if (now - a[1] > 120) printf "%s muet depuis %d s ; ", $1, now - a[1] }')
for s in $SERVICES; do
  [ "$(docker compose ps -q "$s" | xargs -r docker inspect -f '{{.State.Running}}' 2>/dev/null)" = "true" ] || PROBLEM="$PROBLEM$s arrêté ; "
  n=$(docker compose logs --no-color --since "$(( $(date +%s) - T0 ))s" "$s" 2>&1 | grep -c Traceback || true)
  [ "$n" -le 3 ] || PROBLEM="$PROBLEM$s : $n erreurs ; "
done

if [ -n "$PROBLEM" ]; then
  say "ÉCHEC de santé : $PROBLEM → retour à ${OLD:0:7}"
  git reset -q --hard "$OLD"
  docker tag apex-scanner:previous apex-scanner:latest
  docker compose up -d --force-recreate $SERVICES >/dev/null 2>&1
  notify "↩️ Déploiement annulé automatiquement : « $MSG » (${NEW:0:7}). Problème détecté : $PROBLEM La version précédente (${OLD:0:7}) est rétablie."
  exit 1
fi
say "SUCCÈS ${NEW:0:7}"
# ménage : anciennes versions de l'image et cache de construction (chaque déploiement en laisse ~0,7 Go)
docker image prune -f >/dev/null 2>&1 || true
docker builder prune -f --filter until=12h >/dev/null 2>&1 || true
notify "🛠 Mise à jour déployée : « $MSG » (${NEW:0:7}). Tests OK, santé OK après 4 min. Annulable : la version précédente est ${OLD:0:7}."
