#!/bin/bash
# DigitalOcean droplet first-boot setup for Waterfall Finder.
set -euo pipefail
exec > /var/log/waterfall-finder-setup.log 2>&1

APP_DIR=/opt/waterfall-finder
REPO_URL=https://github.com/Luca409/waterfall-finder.git
DEPLOY_PUBKEY="ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIBed5n9iQUPgV4B058W3gdCOwBHaAyEaPVnNcBiA7Qvb github-actions-waterfall-finder"

mkdir -p /root/.ssh
chmod 700 /root/.ssh
grep -qxF "$DEPLOY_PUBKEY" /root/.ssh/authorized_keys 2>/dev/null || echo "$DEPLOY_PUBKEY" >> /root/.ssh/authorized_keys
chmod 600 /root/.ssh/authorized_keys

export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y ca-certificates curl git

install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
chmod a+r /etc/apt/keyrings/docker.asc
echo \
  "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu \
  $(. /etc/os-release && echo "$VERSION_CODENAME") stable" \
  > /etc/apt/sources.list.d/docker.list
apt-get update
apt-get install -y docker-ce docker-ce-cli containerd.io docker-compose-plugin

if [ -d "$APP_DIR/.git" ]; then
  git -C "$APP_DIR" pull --ff-only
else
  git clone "$REPO_URL" "$APP_DIR"
fi

cd "$APP_DIR"
docker compose up -d --build

echo "Waterfall Finder deployed at http://$(curl -s http://169.254.169.254/metadata/v1/interfaces/public/0/ipv4/address):8080"
