#!/usr/bin/env bash
# Sincroniza os arquivos do phone-worker do repositório da VPS para o Termux.
# Nunca deve quebrar o update principal: em falha, deixa sync pendente.
set -u

REPO_DIR="${REPO_DIR:-/home/ubuntu/bot}"
ENV_FILE="${ENV_FILE:-$REPO_DIR/.env}"
STATE_DIR="${STATE_DIR:-$REPO_DIR/data/runtime}"
LOCK_FILE="${TMPDIR:-/tmp}/phone-worker-sync.lock"
PENDING_FILE="$STATE_DIR/phone-worker-sync-pending.flag"
STATUS_FILE="$STATE_DIR/phone-worker-sync-status.txt"
LOG_PREFIX="[phone-worker-sync]"

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

write_status() {
  local status="${1:-desconhecido}"
  mkdir -p "$STATE_DIR" 2>/dev/null || true
  {
    printf 'status=%s\n' "$status"
    printf 'at=%s\n' "$(date -Is 2>/dev/null || date)"
  } > "$STATUS_FILE" 2>/dev/null || true
  log "$status"
}

mark_pending() {
  local reason="${1:-motivo desconhecido}"
  mkdir -p "$STATE_DIR" 2>/dev/null || true
  {
    printf 'reason=%s\n' "$reason"
    printf 'at=%s\n' "$(date -Is 2>/dev/null || date)"
  } > "$PENDING_FILE" 2>/dev/null || true
}

clear_pending() {
  rm -f "$PENDING_FILE" 2>/dev/null || true
}

exec 9>"$LOCK_FILE"
if ! flock -n 9; then
  write_status "já existe sync do phone-worker em andamento"
  exit 0
fi

WORKER_ENABLED="$(env_value PHONE_WORKER_ENABLED false)"
if ! truthy "$WORKER_ENABLED"; then
  clear_pending
  write_status "desativado por PHONE_WORKER_ENABLED=false"
  exit 0
fi

PHONE_HOST="$(env_value PHONE_WORKER_HOST "$(env_value AUX_LAVALINK_HOST "$(env_value PHONE_LAVALINK_HOST "")")")"
PHONE_PORT="$(env_value PHONE_WORKER_PORT 8766)"
PHONE_TOKEN="$(env_value PHONE_WORKER_TOKEN "")"
PHONE_SCHEME="$(env_value PHONE_WORKER_SCHEME http)"
HEALTH_TIMEOUT="$(env_value PHONE_WORKER_HEALTH_TIMEOUT_SECONDS 4)"
SSH_USER="$(env_value PHONE_WORKER_SSH_USER "$(env_value PHONE_LAVALINK_SSH_USER "")")"
SSH_PORT="$(env_value PHONE_WORKER_SSH_PORT "$(env_value PHONE_LAVALINK_SSH_PORT 8022)")"
SSH_CONNECT_TIMEOUT="$(env_value PHONE_WORKER_SSH_CONNECT_TIMEOUT_SECONDS 5)"
TERMUX_HOME="$(env_value PHONE_WORKER_TERMUX_HOME /data/data/com.termux/files/home)"
START_COMMAND="$(env_value PHONE_WORKER_START_COMMAND "$TERMUX_HOME/start-phone-worker.sh")"
case "$START_COMMAND" in
  ~/*) START_COMMAND="$TERMUX_HOME/${START_COMMAND#~/}" ;;
esac
SRC_DIR="$REPO_DIR/deploy/termux/phone-worker"
DEST_WORKER_DIR="$TERMUX_HOME/phone-worker"
HEALTH_URL="${PHONE_SCHEME}://${PHONE_HOST}:${PHONE_PORT}/health"

if [[ -z "$PHONE_HOST" || -z "$SSH_USER" ]]; then
  mark_pending "host ou ssh user ausente"
  write_status "sync pendente: PHONE_WORKER_HOST/PHONE_WORKER_SSH_USER ausente"
  exit 0
fi
if [[ -z "$PHONE_TOKEN" ]]; then
  mark_pending "token ausente"
  write_status "sync pendente: PHONE_WORKER_TOKEN ausente"
  exit 0
fi
if [[ ! -d "$SRC_DIR" ]]; then
  mark_pending "diretório fonte ausente"
  write_status "sync pendente: deploy/termux/phone-worker ausente"
  exit 0
fi
if ! command -v ssh >/dev/null 2>&1 || ! command -v scp >/dev/null 2>&1; then
  mark_pending "ssh/scp ausente"
  write_status "sync pendente: ssh/scp ausente na VPS"
  exit 0
fi

SSH_BASE=(
  ssh
  -p "$SSH_PORT"
  -o BatchMode=yes
  -o ConnectTimeout="$SSH_CONNECT_TIMEOUT"
  -o ServerAliveInterval=5
  -o StrictHostKeyChecking=accept-new
  "$SSH_USER@$PHONE_HOST"
)
SCP_BASE=(
  scp
  -P "$SSH_PORT"
  -o BatchMode=yes
  -o ConnectTimeout="$SSH_CONNECT_TIMEOUT"
  -o StrictHostKeyChecking=accept-new
)

if ! "${SSH_BASE[@]}" "mkdir -p '$DEST_WORKER_DIR'" >/dev/null 2>&1; then
  mark_pending "celular offline ou ssh indisponível"
  write_status "sync pendente: celular offline/SSH indisponível"
  exit 0
fi

copied=0
copy_file() {
  local src="$1" dest="$2"
  [[ -f "$src" ]] || return 0
  if "${SCP_BASE[@]}" "$src" "$SSH_USER@$PHONE_HOST:$dest" >/dev/null 2>&1; then
    copied=$((copied + 1))
    return 0
  fi
  return 1
}

if ! copy_file "$SRC_DIR/phone_worker.py" "$DEST_WORKER_DIR/phone_worker.py"; then
  mark_pending "falha ao copiar phone_worker.py"
  write_status "sync pendente: falha ao copiar phone_worker.py"
  exit 0
fi
copy_file "$SRC_DIR/start-phone-worker.sh" "$TERMUX_HOME/start-phone-worker.sh" || true
copy_file "$SRC_DIR/watch-phone-worker.sh" "$TERMUX_HOME/watch-phone-worker.sh" || true
copy_file "$SRC_DIR/install.sh" "$DEST_WORKER_DIR/install.sh" || true
copy_file "$SRC_DIR/README.md" "$DEST_WORKER_DIR/README.md" || true
copy_file "$SRC_DIR/phone-worker.env.example" "$DEST_WORKER_DIR/phone-worker.env.example" || true

remote_cmd="chmod +x '$TERMUX_HOME/start-phone-worker.sh' '$TERMUX_HOME/watch-phone-worker.sh' 2>/dev/null || true; tmux kill-session -t phone-worker 2>/dev/null || true; pkill -f '[p]hone_worker.py' 2>/dev/null || true; sleep 1; '$START_COMMAND'"
if ! "${SSH_BASE[@]}" "$remote_cmd" >/dev/null 2>&1; then
  mark_pending "arquivos copiados, mas reinício falhou"
  write_status "sync pendente: copiado, mas reinício falhou"
  exit 0
fi

sleep 2
if curl --max-time "$HEALTH_TIMEOUT" -fsS -H "Authorization: Bearer $PHONE_TOKEN" "$HEALTH_URL" >/dev/null 2>&1; then
  clear_pending
  write_status "atualizado no celular (${copied} arquivo(s)); health OK"
else
  mark_pending "health falhou após sync"
  write_status "sync pendente: copiado, mas health falhou"
fi

exit 0
