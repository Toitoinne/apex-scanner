#!/usr/bin/env bash
# Filet de sécurité du suivi Claude Code (routine cloud 6/11/16/21 h UTC) : lancé par cron 45 min
# après chaque passage ; si le passage n'a envoyé aucun rapport Telegram, prévient le propriétaire.
set -uo pipefail
cd /opt/apex
LOG=/var/log/apex-followup.log
H=$(date -u +%H); START="$(date -u +%F) ${H}:00:00"
SINCE=$(awk -v s="$START" '($1" "$2) >= s' "$LOG" 2>/dev/null)
echo "$SINCE" | grep -q " notify$" && exit 0
if [ -n "$SINCE" ]; then
  WHY="il s'est connecté au serveur mais n'a pas envoyé son rapport (il a pu échouer en cours de route)"
else
  WHY="il ne s'est pas connecté au serveur (routine non lancée, quota de l'abonnement Claude atteint, ou erreur)"
fi
tok=$(grep -E '^TELEGRAM_BOT_TOKEN=' .env | head -1 | cut -d= -f2- | tr -d '\r')
chat=$(grep -E '^TELEGRAM_CHAT_ID=' .env | head -1 | cut -d= -f2- | tr -d '\r')
curl -s -o /dev/null "https://api.telegram.org/bot${tok}/sendMessage" --data-urlencode "chat_id=${chat}" \
  --data-urlencode "text=⚠️ Le suivi Claude Code de ${H} h UTC n'a pas donné de nouvelles : ${WHY}. Le bot, lui, continue de tourner. Détails des passages : https://claude.ai/code/routines"
