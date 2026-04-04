#!/usr/bin/env bash
set -Eeuo pipefail

REPO_DIR="/home/ubuntu/bot"
BRANCH="main"
SERVICE="tts-bot"
LOG_TAG="tts-bot-updater"

cd "$REPO_DIR"

CURRENT_COMMIT="$(sudo -u ubuntu -H git rev-parse HEAD)"
sudo -u ubuntu -H git fetch origin "$BRANCH"
REMOTE_COMMIT="$(sudo -u ubuntu -H git rev-parse "origin/$BRANCH")"

if [[ "$CURRENT_COMMIT" == "$REMOTE_COMMIT" ]]; then
  logger -t "$LOG_TAG" "Sem mudanças em $BRANCH"
  exit 0
fi

logger -t "$LOG_TAG" "Atualizando de $CURRENT_COMMIT para $REMOTE_COMMIT"

sudo -u ubuntu -H git pull --ff-only origin "$BRANCH"

if [[ -x "$REPO_DIR/.venv/bin/pip" && -f "$REPO_DIR/requirements.txt" ]]; then
  sudo -u ubuntu -H "$REPO_DIR/.venv/bin/pip" install -r "$REPO_DIR/requirements.txt"
fi

systemctl restart "$SERVICE"
systemctl is-active --quiet "$SERVICE"

logger -t "$LOG_TAG" "Update aplicado e serviço reiniciado com sucesso"
