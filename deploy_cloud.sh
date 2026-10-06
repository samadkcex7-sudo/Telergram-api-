#!/usr/bin/env bash
set -euo pipefail

APP_DIR=/opt/hyperliquid-whale-alert
REPO_URL=${REPO_URL:-https://github.com/samadkcex7-sudo/Telergram-api-.git}
ENV_FILE=/etc/hyperliquid-whale-alert/.env

sudo apt-get update
sudo apt-get install -y ca-certificates curl git
if ! command -v docker >/dev/null 2>&1; then
  curl -fsSL https://get.docker.com | sudo sh
fi
sudo systemctl enable --now docker

sudo mkdir -p "$APP_DIR" /etc/hyperliquid-whale-alert
if [ -d "$APP_DIR/.git" ]; then
  sudo git -C "$APP_DIR" fetch --all --prune
  sudo git -C "$APP_DIR" reset --hard origin/main
else
  sudo git clone "$REPO_URL" "$APP_DIR"
fi

if [ ! -f "$ENV_FILE" ]; then
  echo "Create $ENV_FILE with TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, and optional settings, then rerun this script." >&2
  sudo install -m 600 /dev/null "$ENV_FILE"
  exit 2
fi
sudo chmod 600 "$ENV_FILE"
sudo cp "$APP_DIR/hyperliquid-whale-alert.service" /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now hyperliquid-whale-alert.service
sudo systemctl --no-pager --full status hyperliquid-whale-alert.service
