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
  local key="${1:?}" default="${2:-}" value="" env_current=""
  # Variáveis passadas pelo systemd/bot devem poder sobrescrever o .env
  # sem editar arquivo local. É usado pelo botão manual para furar cooldown.
  env_current="${!key-}"
  if [[ -n "$env_current" ]]; then
    printf '%s' "$env_current"
    return 0
  fi
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
  log "resultado: disabled"
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
START_COMMAND="$(env_value PHONE_WORKER_START_COMMAND '__AUTO_WATCHDOG__')"
COOLDOWN_SECONDS="$(env_value PHONE_WORKER_KICK_COOLDOWN_SECONDS 60)"
FORCE_WAKE="$(env_value PHONE_WORKER_FORCE_WAKE false)"
WAKE_REASON="$(env_value PHONE_WORKER_WATCH_REASON timer)"
STATE_DIR="${STATE_DIR:-$REPO_DIR/data/runtime}"
COOLDOWN_FILE="$STATE_DIR/phone-worker-last-kick"
PENDING_SYNC_FILE="$STATE_DIR/phone-worker-sync-pending.flag"
SYNC_SCRIPT="$REPO_DIR/scripts/sync-phone-worker.sh"

if [[ -z "$PHONE_HOST" || -z "$PHONE_TOKEN" ]]; then
  log "host ou token não configurados; defina PHONE_WORKER_HOST e PHONE_WORKER_TOKEN"
  log "resultado: missing-config"
  exit 0
fi

HEALTH_URL="${PHONE_SCHEME}://${PHONE_HOST}:${PHONE_PORT}/health"

mask_host() {
  local host="${1:-}"
  if [[ "$host" =~ ^100\.([0-9]+)\.([0-9]+)\.([0-9]+)$ ]]; then
    printf '100.%s.x.x' "${BASH_REMATCH[1]}"
  elif [[ "$host" =~ ^([0-9]+)\.([0-9]+)\.([0-9]+)\.([0-9]+)$ ]]; then
    printf '%s.%s.x.x' "${BASH_REMATCH[1]}" "${BASH_REMATCH[2]}"
  else
    printf '%s' "$host"
  fi
}

classify_text() {
  local text="${1:-}"
  text="$(printf '%s' "$text" | tr '[:upper:]' '[:lower:]')"
  if [[ "$text" == *"no route to host"* || "$text" == *"errno 113"* ]]; then
    printf 'no-route'
  elif [[ "$text" == *"connection refused"* ]]; then
    printf 'connection-refused'
  elif [[ "$text" == *"timed out"* || "$text" == *"timeout"* ]]; then
    printf 'timeout'
  elif [[ "$text" == *"permission denied"* || "$text" == *"publickey"* ]]; then
    printf 'auth-failed'
  elif [[ "$text" == *"could not resolve"* || "$text" == *"name or service"* ]]; then
    printf 'dns-failed'
  else
    printf 'failed'
  fi
}

tcp_probe() {
  local host="${1:-}" port="${2:-}" timeout="${3:-4}" label="${4:-tcp}"
  if [[ -z "$host" || -z "$port" ]]; then
    log "probe ${label}: host/porta ausente"
    return 2
  fi
  python3 - "$host" "$port" "$timeout" "$label" <<'PYPROBE' 2>/dev/null || true
import socket, sys, time, errno
host, port, timeout, label = sys.argv[1], int(sys.argv[2]), float(sys.argv[3]), sys.argv[4]
started=time.perf_counter()
try:
    with socket.create_connection((host, port), timeout=timeout):
        ms=(time.perf_counter()-started)*1000
        print(f"probe {label}: open em {ms:.0f}ms")
        sys.exit(0)
except Exception as exc:
    text=str(exc) or type(exc).__name__
    low=text.lower()
    if 'no route to host' in low or getattr(exc, 'errno', None) == 113:
        kind='no-route'
    elif 'refused' in low or getattr(exc, 'errno', None) in {111,61}:
        kind='connection-refused'
    elif 'timed out' in low or isinstance(exc, TimeoutError):
        kind='timeout'
    elif 'network is unreachable' in low or getattr(exc, 'errno', None) == 101:
        kind='network-unreachable'
    else:
        kind=type(exc).__name__
    print(f"probe {label}: {kind} ({text[:100]})")
    sys.exit(1)
PYPROBE
}

health_probe() {
  local tmp code rc detail
  tmp="$(mktemp)"
  code="$(curl --max-time "$HEALTH_TIMEOUT" -sS -o "$tmp" -w '%{http_code}' -H "Authorization: Bearer $PHONE_TOKEN" "$HEALTH_URL" 2>&1)"
  rc=$?
  detail="$(cat "$tmp" 2>/dev/null | head -c 240 | tr '\n' ' ' || true)"
  rm -f "$tmp" 2>/dev/null || true
  if [[ "$rc" -eq 0 && "$code" =~ ^2 ]]; then
    log "health: ok HTTP ${code} em $(mask_host "$PHONE_HOST"):${PHONE_PORT}"
    return 0
  fi
  if [[ "$rc" -eq 0 ]]; then
    log "health: HTTP ${code} em $(mask_host "$PHONE_HOST"):${PHONE_PORT} ${detail:+· $detail}"
    return 1
  fi
  log "health: curl rc=${rc} em $(mask_host "$PHONE_HOST"):${PHONE_PORT} · $(classify_text "$code") · ${code:0:160}"
  return 1
}

worker_health_ok() {
  curl --max-time "$HEALTH_TIMEOUT" -fsS -H "Authorization: Bearer $PHONE_TOKEN" "$HEALTH_URL" >/dev/null 2>&1
}

try_pending_sync() {
  if [[ -f "$PENDING_SYNC_FILE" && -x "$SYNC_SCRIPT" ]]; then
    log "sync pendente detectado; tentando atualizar phone-worker no celular"
    REPO_DIR="$REPO_DIR" ENV_FILE="$ENV_FILE" STATE_DIR="$STATE_DIR" "$SYNC_SCRIPT" || true
  fi
}

log "config: host=$(mask_host "$PHONE_HOST") porta=$PHONE_PORT ssh_user=$([[ -n "$SSH_USER" ]] && printf configurado || printf vazio) ssh_port=$SSH_PORT motivo=${WAKE_REASON:-timer}"
tcp_probe "$PHONE_HOST" "$PHONE_PORT" "$HEALTH_TIMEOUT" "worker-http" | while IFS= read -r line; do log "$line"; done
if health_probe; then
  try_pending_sync
  if [[ -f "$PENDING_SYNC_FILE" ]]; then
    log "worker online em $(mask_host "$PHONE_HOST"):${PHONE_PORT}; sync ainda pendente"
  else
    log "worker online em $(mask_host "$PHONE_HOST"):${PHONE_PORT}"
  fi
  log "resultado: online"
  exit 0
fi

log "worker offline/inacessível; tentando recuperação leve"

mkdir -p "$STATE_DIR" 2>/dev/null || true
now_epoch="$(date +%s)"
last_kick="0"
if [[ -f "$COOLDOWN_FILE" ]]; then
  last_kick="$(cat "$COOLDOWN_FILE" 2>/dev/null || echo 0)"
fi
if ! truthy "$FORCE_WAKE" && [[ "$last_kick" =~ ^[0-9]+$ ]] && (( now_epoch - last_kick < COOLDOWN_SECONDS )); then
  log "cooldown ativo; VPS segue local"
  log "resultado: cooldown"
  exit 0
fi
if truthy "$FORCE_WAKE"; then
  log "wake forçado solicitado (${WAKE_REASON:-manual}); ignorando cooldown"
fi
printf '%s' "$now_epoch" > "$COOLDOWN_FILE" 2>/dev/null || true

if [[ -z "$SSH_USER" ]]; then
  log "PHONE_WORKER_SSH_USER vazio; aguardando watchdog local do celular"
  log "resultado: no-ssh-user"
  exit 0
fi
if ! command -v ssh >/dev/null 2>&1; then
  log "ssh não encontrado na VPS"
  log "resultado: no-ssh-client"
  exit 0
fi

tcp_probe "$PHONE_HOST" "$SSH_PORT" "$SSH_CONNECT_TIMEOUT" "ssh" | while IFS= read -r line; do log "$line"; done

if [[ "$START_COMMAND" == "__AUTO_WATCHDOG__" ]]; then
  START_COMMAND='sh -c "cd; termux-wake-lock >/dev/null 2>&1 || true; if [ -x phone-worker/watch-phone-worker.sh ]; then nohup bash phone-worker/watch-phone-worker.sh >/dev/null 2>&1 & elif [ -x watch-phone-worker.sh ]; then nohup bash watch-phone-worker.sh >/dev/null 2>&1 & elif [ -x phone-worker/start-phone-worker.sh ]; then nohup bash phone-worker/start-phone-worker.sh >/dev/null 2>&1 & elif [ -x start-phone-worker.sh ]; then nohup bash start-phone-worker.sh >/dev/null 2>&1 & else exit 42; fi"'
fi

log "acionando Termux via SSH em ${SSH_USER}@$(mask_host "$PHONE_HOST"):${SSH_PORT}"
ssh_out="$(mktemp)"
if ! ssh -p "$SSH_PORT" \
  -o BatchMode=yes \
  -o ConnectTimeout="$SSH_CONNECT_TIMEOUT" \
  -o ConnectionAttempts=1 \
  -o ServerAliveInterval=5 \
  -o StrictHostKeyChecking=accept-new \
  "$SSH_USER@$PHONE_HOST" \
  "$START_COMMAND" >"$ssh_out" 2>&1; then
    ssh_detail="$(cat "$ssh_out" 2>/dev/null | head -c 300 | tr '\n' ' ' || true)"
    rm -f "$ssh_out" 2>/dev/null || true
    log "não consegui acionar o phone-worker por SSH; motivo=$(classify_text "$ssh_detail") ${ssh_detail:+· $ssh_detail}"
    log "resultado: ssh-failed"
    exit 0
fi
rm -f "$ssh_out" 2>/dev/null || true

sleep "$START_WAIT"
tcp_probe "$PHONE_HOST" "$PHONE_PORT" "$HEALTH_TIMEOUT" "worker-http-pos-ssh" | while IFS= read -r line; do log "$line"; done
if health_probe; then
  log "worker voltou"
  log "resultado: woke"
  try_pending_sync
else
  log "celular respondeu ao SSH, mas worker ainda não ficou online ou token/porta não bateram"
  log "resultado: ssh-ok-worker-offline"
fi

exit 0
