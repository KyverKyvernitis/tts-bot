#!/usr/bin/env bash
set -Eeuo pipefail

REPO_DIR="/home/ubuntu/bot"
BRANCH="main"
SERVICE="tts-bot"
LOG_TAG="tts-bot-updater"

FRONT_DIR="$REPO_DIR/activity /sinuca"
BACK_DIR="$REPO_DIR/activity /sinuca-server"
FRONT_PUBLISH_DIR="/var/www/sinuca"
BACK_PORT="8787"
BACK_HEALTH_URL="http://127.0.0.1:${BACK_PORT}/health"
BOT_HEALTH_URL="http://127.0.0.1:10000/health"

send_info() {
  sudo -u ubuntu /home/ubuntu/bot/alert.sh info "$1" "$2" || true
}

send_warn() {
  sudo -u ubuntu /home/ubuntu/bot/alert.sh warn "$1" "$2" || true
}

send_error() {
  sudo -u ubuntu /home/ubuntu/bot/alert.sh error "$1" "$2" || true
}

human_duration() {
  local total="${1:-0}"
  local m=$((total / 60))
  local s=$((total % 60))
  if (( m > 0 )); then
    printf "%dm %02ds" "$m" "$s"
  else
    printf "%ds" "$s"
  fi
}

run_as_ubuntu() {
  sudo -u ubuntu -H bash -lc "$1"
}

trap 'send_error "Falha no auto update" "O updater falhou durante a execução."' ERR

SECONDS=0
cd "$REPO_DIR"

CURRENT_COMMIT="$(sudo -u ubuntu -H git rev-parse HEAD)"
sudo -u ubuntu -H git fetch origin "$BRANCH"
REMOTE_COMMIT="$(sudo -u ubuntu -H git rev-parse "origin/$BRANCH")"

if [[ "$CURRENT_COMMIT" == "$REMOTE_COMMIT" ]]; then
  logger -t "$LOG_TAG" "Sem mudanças em $BRANCH"
  exit 0
fi

SHORT_FROM="${CURRENT_COMMIT:0:7}"
SHORT_TO="${REMOTE_COMMIT:0:7}"
COMMIT_SUBJECT="$(sudo -u ubuntu -H git log -1 --pretty=%s "$REMOTE_COMMIT")"

CHANGED_FILES_RAW="$(sudo -u ubuntu -H git diff --name-only "$CURRENT_COMMIT" "$REMOTE_COMMIT" || true)"
if [[ -n "$CHANGED_FILES_RAW" ]]; then
  CHANGED_FILES="$(printf '%s\n' "$CHANGED_FILES_RAW" | head -n 20 | sed 's/^/- /')"
else
  CHANGED_FILES="- nenhum arquivo listado"
fi

FRONT_CHANGED=0
BACK_CHANGED=0
BOT_CHANGED=0

if printf '%s\n' "$CHANGED_FILES_RAW" | grep -q '^activity /sinuca/'; then
  FRONT_CHANGED=1
fi
if printf '%s\n' "$CHANGED_FILES_RAW" | grep -q '^activity /sinuca-server/'; then
  BACK_CHANGED=1
fi
if printf '%s\n' "$CHANGED_FILES_RAW" | grep -vq '^activity /sinuca'; then
  BOT_CHANGED=1
fi

logger -t "$LOG_TAG" "Atualizando de $CURRENT_COMMIT para $REMOTE_COMMIT"

sudo -u ubuntu -H git pull --ff-only origin "$BRANCH"

if [[ -x "$REPO_DIR/.venv/bin/pip" && -f "$REPO_DIR/requirements.txt" ]]; then
  sudo -u ubuntu -H "$REPO_DIR/.venv/bin/pip" install -r "$REPO_DIR/requirements.txt"
fi

BOT_HEALTHCHECK_STATUS="não verificado"
if (( BOT_CHANGED == 1 )); then
  systemctl restart "$SERVICE"
  systemctl is-active --quiet "$SERVICE"
  if curl -fsS "$BOT_HEALTH_URL" >/dev/null 2>&1; then
    BOT_HEALTHCHECK_STATUS="OK"
  else
    BOT_HEALTHCHECK_STATUS="falhou"
  fi
else
  if curl -fsS "$BOT_HEALTH_URL" >/dev/null 2>&1; then
    BOT_HEALTHCHECK_STATUS="OK"
  fi
fi

ACTIVITY_LINES="não houve mudanças na Activity."

if (( FRONT_CHANGED == 1 || BACK_CHANGED == 1 )); then
  FRONT_STATUS="não alterado"
  BACK_STATUS="não alterado"
  ACTIVITY_HEALTHCHECK_STATUS="não verificado"

  if (( FRONT_CHANGED == 1 )); then
    if [[ -d "$FRONT_DIR" ]]; then
      run_as_ubuntu "cd \"$FRONT_DIR\" && [ -d node_modules ] || npm install"
      run_as_ubuntu "cd \"$FRONT_DIR\" && npm run build"
      rm -rf "${FRONT_PUBLISH_DIR:?}/"*
      cp -r "$FRONT_DIR/dist/." "$FRONT_PUBLISH_DIR/"
      FRONT_STATUS="frontend publicado em $FRONT_PUBLISH_DIR"
    else
      FRONT_STATUS="frontend não encontrado em $FRONT_DIR"
    fi
  fi

  if (( BACK_CHANGED == 1 )); then
    if [[ -d "$BACK_DIR" ]]; then
      run_as_ubuntu "cd \"$BACK_DIR\" && [ -d node_modules ] || npm install"
      run_as_ubuntu "cd \"$BACK_DIR\" && npm run build"

      fuser -k "${BACK_PORT}/tcp" >/dev/null 2>&1 || true

      run_as_ubuntu "cd \"$BACK_DIR\" && set -a && [ -f \"$REPO_DIR/.env\" ] && . \"$REPO_DIR/.env\" || true && [ -f .env ] && . ./.env || true && set +a && nohup node dist/index.js >> sinuca-server.log 2>&1 &"

      sleep 3

      if curl -fsS "$BACK_HEALTH_URL" >/dev/null 2>&1; then
        ACTIVITY_HEALTHCHECK_STATUS="OK"
        BACK_STATUS="backend publicado e validado em $BACK_HEALTH_URL"
      else
        ACTIVITY_HEALTHCHECK_STATUS="falhou"
        BACK_STATUS="backend reiniciado, mas healthcheck falhou em $BACK_HEALTH_URL"
      fi
    else
      BACK_STATUS="backend não encontrado em $BACK_DIR"
    fi
  elif curl -fsS "$BACK_HEALTH_URL" >/dev/null 2>&1; then
    ACTIVITY_HEALTHCHECK_STATUS="OK"
  fi

  ACTIVITY_LINES="frontend
$FRONT_STATUS

backend
$BACK_STATUS

healthcheck
$ACTIVITY_HEALTHCHECK_STATUS"
fi

DURATION="$(human_duration "$SECONDS")"

compact_multiline() {
  local value="${1:-}"
  if [ -z "${value//[[:space:]]/}" ]; then
    printf '—'
  else
    printf '%s' "$value"
  fi
}

CHANGED_FILES_BLOCK="$(compact_multiline "${CHANGED_FILES:-}")"
ACTIVITY_BLOCK="$(compact_multiline "${ACTIVITY_LINES:-Sem mudanças na Activity}")"

if [[ "${BOT_HEALTHCHECK_STATUS,,}" == *"ok"* ]]; then
  ALERT_TYPE="success"
  ALERT_TITLE="Update aplicado com sucesso"
  ALERT_SUMMARY="O update foi aplicado e o healthcheck passou."
else
  ALERT_TYPE="warn"
  ALERT_TITLE="Update aplicado com alerta"
  ALERT_SUMMARY="O update foi aplicado, mas o healthcheck falhou logo depois."
fi

BODY="$(cat <<EOF
Resumo: $ALERT_SUMMARY
Host: $HOSTNAME
Branch: $BRANCH
Commit: ${SHORT_FROM} → ${SHORT_TO}
Mudança: $COMMIT_SUBJECT
Arquivos: $CHANGED_FILES_BLOCK
Activity: $ACTIVITY_BLOCK
Duração: $DURATION
Hora: $(date '+%d/%m/%Y %H:%M:%S')
EOF
)"
/home/ubuntu/bot/alert.sh "$ALERT_TYPE" "$ALERT_TITLE" "$BODY"
logger -t "$LOG_TAG" "$ALERT_TITLE"
