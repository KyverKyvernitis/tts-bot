#!/usr/bin/env bash
# Mantém o phone-worker do celular acordado sem virar dependência do bot.
# Roda na VPS via systemd timer. Se o celular estiver offline, falha rápido.
set -u

REPO_DIR="${REPO_DIR:-/home/ubuntu/bot}"
ENV_FILE="${ENV_FILE:-$REPO_DIR/.env}"
LOCK_FILE="${TMPDIR:-/tmp}/phone-worker-watch.lock"
LOG_PREFIX="[phone-worker-watch]"

log() {
  printf '%s %s\n' "$LOG_PREFIX" "$*"
}

env_value() {
  local key="${1:?}" default="${2:-}" value=""
  if [[ -f "$ENV_FILE" ]]; then
    value="$(grep -E "^[[:space:]]*(export[[:space:]]+)?${key}=" "$ENV_FILE" 2>/dev/null | tail -n 1 | sed -E 's/^[[:space:]]*export[[:space:]]+//' | cut -d= -f2- || true)"
    value="${value%$'\r'}"
    value="${value#\"}"; value="${value%\"}"
    value="${value#\'}"; value="${value%\'}"
  fi
  if [[ -n "$value" ]]; then
    printf '%s' "$value"
  else
    printf '%s' "$default"
  fi
}

truthy() {
  local value="${1:-}"
  value="$(printf '%s' "$value" | tr '[:upper:]' '[:lower:]' | tr -d ' \t\r\n\"')"
  value="${value//\'/}"
  [[ "$value" == "1" || "$value" == "true" || "$value" == "yes" || "$value" == "y" || "$value" == "on" || "$value" == "sim" ]]
}

falsey() {
  local value="${1:-}"
  value="$(printf '%s' "$value" | tr '[:upper:]' '[:lower:]' | tr -d ' \t\r\n\"')"
  value="${value//\'/}"
  [[ "$value" == "0" || "$value" == "false" || "$value" == "no" || "$value" == "n" || "$value" == "off" || "$value" == "não" || "$value" == "nao" ]]
}

exec 9>"$LOCK_FILE"
if ! flock -n 9; then
  log "já existe uma verificação em andamento"
  exit 0
fi

WORKER_ENABLED="$(env_value PHONE_WORKER_ENABLED false)"
if ! truthy "$WORKER_ENABLED"; then
  log "PHONE_WORKER_ENABLED=false; nada a fazer"
  exit 0
fi

PHONE_HOST="$(env_value PHONE_WORKER_HOST "$(env_value AUX_LAVALINK_HOST "$(env_value PHONE_LAVALINK_HOST "")")")"
PHONE_PORT="$(env_value PHONE_WORKER_PORT 8766)"
PHONE_TOKEN="$(env_value PHONE_WORKER_TOKEN "")"
PHONE_SCHEME="$(env_value PHONE_WORKER_SCHEME http)"
HEALTH_TIMEOUT="$(env_value PHONE_WORKER_HEALTH_TIMEOUT_SECONDS 4)"
START_WAIT="$(env_value PHONE_WORKER_START_WAIT_SECONDS 4)"
SSH_USER="$(env_value PHONE_WORKER_SSH_USER "$(env_value PHONE_LAVALINK_SSH_USER "")")"
SSH_PORT="$(env_value PHONE_WORKER_SSH_PORT "$(env_value PHONE_LAVALINK_SSH_PORT 8022)")"
SSH_CONNECT_TIMEOUT="$(env_value PHONE_WORKER_SSH_CONNECT_TIMEOUT_SECONDS 5)"
START_COMMAND="$(env_value PHONE_WORKER_START_COMMAND '/data/data/com.termux/files/home/start-phone-worker.sh')"
COOLDOWN_SECONDS="$(env_value PHONE_WORKER_KICK_COOLDOWN_SECONDS 60)"
STATE_DIR="${STATE_DIR:-$REPO_DIR/data/runtime}"
COOLDOWN_FILE="$STATE_DIR/phone-worker-last-kick"
PENDING_SYNC_FILE="$STATE_DIR/phone-worker-sync-pending.flag"
SYNC_SCRIPT="$REPO_DIR/scripts/sync-phone-worker.sh"

if [[ -z "$PHONE_HOST" || -z "$PHONE_TOKEN" ]]; then
  log "host ou token não configurados; defina PHONE_WORKER_HOST e PHONE_WORKER_TOKEN"
  exit 0
fi

HEALTH_URL="${PHONE_SCHEME}://${PHONE_HOST}:${PHONE_PORT}/health"

worker_health_ok() {
  curl --max-time "$HEALTH_TIMEOUT" -fsS -H "Authorization: Bearer $PHONE_TOKEN" "$HEALTH_URL" >/dev/null 2>&1
}

try_pending_sync() {
  if [[ -f "$PENDING_SYNC_FILE" && -x "$SYNC_SCRIPT" ]]; then
    log "sync pendente detectado; tentando atualizar phone-worker no celular"
    REPO_DIR="$REPO_DIR" ENV_FILE="$ENV_FILE" STATE_DIR="$STATE_DIR" "$SYNC_SCRIPT" || true
  fi
}

if worker_health_ok; then
  try_pending_sync
  if [[ -f "$PENDING_SYNC_FILE" ]]; then
    log "worker online em ${PHONE_HOST}:${PHONE_PORT}; sync ainda pendente"
  else
    log "worker online em ${PHONE_HOST}:${PHONE_PORT}"
  fi
  exit 0
fi

log "worker offline; tentando recuperação leve"

mkdir -p "$STATE_DIR" 2>/dev/null || true
now_epoch="$(date +%s)"
last_kick="0"
if [[ -f "$COOLDOWN_FILE" ]]; then
  last_kick="$(cat "$COOLDOWN_FILE" 2>/dev/null || echo 0)"
fi
if [[ "$last_kick" =~ ^[0-9]+$ ]] && (( now_epoch - last_kick < COOLDOWN_SECONDS )); then
  log "cooldown ativo; VPS segue local"
  exit 0
fi
printf '%s' "$now_epoch" > "$COOLDOWN_FILE" 2>/dev/null || true

if [[ -z "$SSH_USER" ]]; then
  log "PHONE_WORKER_SSH_USER vazio; aguardando watchdog local do celular"
  exit 0
fi
if ! command -v ssh >/dev/null 2>&1; then
  log "ssh não encontrado na VPS"
  exit 0
fi

log "acionando Termux via SSH em ${SSH_USER}@${PHONE_HOST}:${SSH_PORT}"
ssh -p "$SSH_PORT" \
  -o BatchMode=yes \
  -o ConnectTimeout="$SSH_CONNECT_TIMEOUT" \
  -o ServerAliveInterval=5 \
  -o StrictHostKeyChecking=accept-new \
  "$SSH_USER@$PHONE_HOST" \
  "$START_COMMAND" >/dev/null 2>&1 || {
    log "não consegui acionar o phone-worker por SSH; fallback local continua"
    exit 0
  }

sleep "$START_WAIT"
if worker_health_ok; then
  log "worker voltou"
  try_pending_sync
else
  log "celular respondeu ao SSH, mas worker ainda não ficou online"
fi

exit 0
