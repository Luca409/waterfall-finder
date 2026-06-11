#!/usr/bin/env bash
# Run on a fresh Ubuntu 24.04 DigitalOcean droplet (as root or with sudo).
set -euo pipefail

APP_DIR="${APP_DIR:-/opt/waterfall-finder}"

apt-get update
apt-get install -y ca-certificates curl git

# Docker
install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
chmod a+r /etc/apt/keyrings/docker.asc
echo \
  "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu \
  $(. /etc/os-release && echo "$VERSION_CODENAME") stable" \
  > /etc/apt/sources.list.d/docker.list
apt-get update
apt-get install -y docker-ce docker-ce-cli containerd.io docker-compose-plugin

mkdir -p "$APP_DIR"
echo "App directory: $APP_DIR"
echo "Copy your project files here, then run:"
echo "  cd $APP_DIR && docker compose up -d --build"
