#!/usr/bin/env bash
# À lancer sur le VPS (Ubuntu 24.04, en root), dans le dossier où tu as copié apex-migration :
#   bash install.sh
# Sécurise le serveur, installe Docker, restaure le projet ET tout l'apprentissage, puis démarre.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
DEST=/opt/apex

[ "$(id -u)" = 0 ] || { echo "À lancer en root."; exit 1; }
[ "$(uname -m)" = x86_64 ] || { echo "Ce serveur n'est pas x86_64 (amd64) : la base copiée ne serait pas lisible. Prends un CX32/CPX31, pas un CAX."; exit 1; }

echo "1/6  Mises à jour et sécurité (pare-feu : seul SSH est ouvert)…"
export DEBIAN_FRONTEND=noninteractive
apt-get update -q
apt-get upgrade -yq
apt-get install -yq ufw fail2ban unattended-upgrades curl ca-certificates
ufw allow OpenSSH
ufw --force enable
systemctl enable --now fail2ban
dpkg-reconfigure -f noninteractive unattended-upgrades

echo "2/6  Mémoire d'échange de secours (2 Go)…"
if ! swapon --show | grep -q /swapfile; then
  fallocate -l 2G /swapfile && chmod 600 /swapfile && mkswap /swapfile && swapon /swapfile
  echo '/swapfile none swap sw 0 0' >> /etc/fstab
fi

echo "3/6  Installation de Docker…"
if ! command -v docker >/dev/null; then
  curl -fsSL https://get.docker.com | sh
fi
systemctl enable --now docker

echo "4/6  Installation du projet dans $DEST…"
mkdir -p "$DEST"
tar xzf "$HERE/project.tgz" -C "$DEST"
chmod 600 "$DEST/.env"

echo "5/6  Restauration de l'apprentissage (base + modèles)…"
cd "$DEST"
for v in pgdata apexdata; do
  docker volume create "apexscanner_$v" >/dev/null
  if [ -f "$HERE/$v.tgz" ]; then
    docker run --rm -v "apexscanner_$v:/v" -v "$HERE:/b:ro" alpine sh -c "cd /v && tar xzf /b/$v.tgz"
  fi
done
# l'utilisateur du conteneur (uid 10001) doit pouvoir écrire ses snapshots
docker run --rm -v apexscanner_apexdata:/v alpine chown -R 10001:10001 /v

echo "6/6  Construction et démarrage (quelques minutes)…"
docker compose up -d --build
sleep 60
docker compose ps
echo
echo "Installé. Logs : cd $DEST && docker compose logs -f ingestor learner"
echo "Envoie /stats à ton bot Telegram pour vérifier."
