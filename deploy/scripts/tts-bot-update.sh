#!/usr/bin/env bash
set -Eeuo pipefail

REPO_DIR="/home/ubuntu/bot"
BRANCH="main"
SERVICE="tts-bot"
CALLKEEPER_SERVICE="callkeeper"
LOG_TAG="tts-bot-updater"

cd "$REPO_DIR"

CURRENT_COMMIT="$(sudo -u ubuntu -H git rev-parse HEAD)"
sudo -u ubuntu -H git fetch origin "$BRANCH"
REMOTE_COMMIT="$(sudo -u ubuntu -H git rev-parse "origin/$BRANCH")"

if [[ "$CURRENT_COMMIT" == "$REMOTE_COMMIT" ]]; then
  logger -t "$LOG_TAG" "Sem mudanças em $BRANCH"
  exit 0
fi

CHANGED_FILES_RAW="$(sudo -u ubuntu -H git diff --name-only "$CURRENT_COMMIT" "$REMOTE_COMMIT" || true)"
CALLKEEPER_CHANGED=0
if printf '%s\n' "$CHANGED_FILES_RAW" | grep -Eq '^(callkeeper_service\.py|callkeeper_runtime/|deploy/systemd/callkeeper\.service|config\.py|db\.py|requirements\.txt)$'; then
  CALLKEEPER_CHANGED=1
fi

logger -t "$LOG_TAG" "Atualizando de $CURRENT_COMMIT para $REMOTE_COMMIT"

sudo -u ubuntu -H git pull --ff-only origin "$BRANCH"

if [[ -x "$REPO_DIR/.venv/bin/pip" && -f "$REPO_DIR/requirements.txt" ]]; then
  sudo -u ubuntu -H "$REPO_DIR/.venv/bin/pip" install -r "$REPO_DIR/requirements.txt"
fi

systemctl restart "$SERVICE"
systemctl is-active --quiet "$SERVICE"

if (( CALLKEEPER_CHANGED == 1 )); then
  if [[ -f "$REPO_DIR/deploy/systemd/callkeeper.service" ]]; then
    cp "$REPO_DIR/deploy/systemd/callkeeper.service" /etc/systemd/system/callkeeper.service
    systemctl daemon-reload
    systemctl enable "$CALLKEEPER_SERVICE" >/dev/null 2>&1 || true
  fi
  systemctl restart "$CALLKEEPER_SERVICE"
  systemctl is-active --quiet "$CALLKEEPER_SERVICE"
fi

logger -t "$LOG_TAG" "Update aplicado e serviços reiniciados com sucesso"
