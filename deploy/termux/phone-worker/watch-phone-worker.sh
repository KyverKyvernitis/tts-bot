#!/data/data/com.termux/files/usr/bin/bash
# Watchdog local do Core Worker/phone-worker.
# Mantém o agent vivo no Termux e é o alvo oficial do Termux:Boot.
set -u

if [ -z "${BASH_VERSION:-}" ]; then
  exec bash "$0" "$@"
fi

ENV_FILE="${PHONE_WORKER_ENV:-$HOME/.phone-worker.env}"
if [[ -f "$ENV_FILE" ]]; then
  set -a
  # shellcheck disable=SC1090
  source "$ENV_FILE"
  set +a
fi

INTERVAL="${PHONE_WORKER_WATCH_INTERVAL_SECONDS:-60}"
WORKER_DIR="${PHONE_WORKER_DIR:-$HOME/phone-worker}"
START_SCRIPT="$WORKER_DIR/start-phone-worker.sh"
WATCH_LOG="${PHONE_WORKER_WATCH_LOG_FILE:-$WORKER_DIR/phone-worker-watch.log}"
WATCH_PID_FILE="${PHONE_WORKER_WATCH_PID_FILE:-$WORKER_DIR/phone-worker-watch.pid}"
WATCH_LOCK_DIR="${PHONE_WORKER_WATCH_LOCK_DIR:-$WORKER_DIR/.phone-worker-watch.lock}"
STATUS_FILE="${PHONE_WORKER_STATUS_FILE:-$WORKER_DIR/phone-worker.status}"
MAX_LOG_BYTES="${PHONE_WORKER_LOG_MAX_BYTES:-1048576}"
SSHD_AUTO_START="${PHONE_WORKER_SSHD_AUTO_START:-true}"
SSHD_PORT="${PHONE_WORKER_SSH_PORT:-8022}"

# Não use fallback para ~/start-phone-worker.sh: versões antigas nesse atalho
# já causaram loops de pip/clang e aquecimento. O instalador cria um wrapper
# em ~/ que aponta para o script oficial dentro de ~/phone-worker.

mkdir -p "$WORKER_DIR"

log() {
  mkdir -p "$(dirname "$WATCH_LOG")"
  printf '[phone-worker-watch] %s\n' "$*" | tee -a "$WATCH_LOG" >/dev/null
}

now_iso() {
  date '+%Y-%m-%dT%H:%M:%S%z' 2>/dev/null || date
}

write_status() {
  mkdir -p "$(dirname "$STATUS_FILE")"
  printf '%s\n' "$1" > "$STATUS_FILE" 2>/dev/null || true
}

rotate_watch_log_if_needed() {
  mkdir -p "$(dirname "$WATCH_LOG")"
  if [[ -f "$WATCH_LOG" ]]; then
    size=$(wc -c < "$WATCH_LOG" 2>/dev/null || echo 0)
    if [[ "$size" -gt "$MAX_LOG_BYTES" ]]; then
      mv -f "$WATCH_LOG" "$WATCH_LOG.1" 2>/dev/null || true
      : > "$WATCH_LOG"
    fi
  fi
}

truthy() {
  local value="${1:-}"
  value="$(printf '%s' "$value" | tr '[:upper:]' '[:lower:]' | tr -d ' \t\r\n"')"
  value="${value//\'/}"
  [[ "$value" == "1" || "$value" == "true" || "$value" == "yes" || "$value" == "y" || "$value" == "on" || "$value" == "sim" ]]
}

sshd_listening() {
  command -v ss >/dev/null 2>&1 || return 1
  ss -lnt 2>/dev/null | grep -Eq "[:.]${SSHD_PORT}[[:space:]]|:${SSHD_PORT}$"
}

ensure_sshd_running() {
  truthy "$SSHD_AUTO_START" || return 0
  command -v sshd >/dev/null 2>&1 || return 0
  if sshd_listening; then
    return 0
  fi
  if command -v pgrep >/dev/null 2>&1 && pgrep -f 'sshd' >/dev/null 2>&1; then
    log "sshd rodando, mas porta ${SSHD_PORT} não apareceu; mantendo processo existente"
    return 0
  fi
  log "sshd parado; tentando iniciar porta ${SSHD_PORT}"
  sshd -p "$SSHD_PORT" >/dev/null 2>&1 || sshd >/dev/null 2>&1 || true
}

# Impede múltiplos watchdogs competindo entre si. Lock velho é removido se o PID
# gravado nele não existir mais.
if ! mkdir "$WATCH_LOCK_DIR" 2>/dev/null; then
  old_pid=""
  [[ -f "$WATCH_PID_FILE" ]] && old_pid="$(cat "$WATCH_PID_FILE" 2>/dev/null | head -n 1)"
  if [[ -n "$old_pid" ]] && kill -0 "$old_pid" 2>/dev/null; then
    log "watchdog já ativo; pid=$old_pid"
    write_status "watchdog_already_online pid=$old_pid $(now_iso)"
    exit 0
  fi
  rm -rf "$WATCH_LOCK_DIR" 2>/dev/null || true
  mkdir "$WATCH_LOCK_DIR" 2>/dev/null || true
fi
trap 'rm -rf "$WATCH_LOCK_DIR" "$WATCH_PID_FILE" 2>/dev/null || true' EXIT INT TERM

printf '%s\n' "$$" > "$WATCH_PID_FILE" 2>/dev/null || true
termux-wake-lock 2>/dev/null || true
write_status "watchdog_running pid=$$ $(now_iso)"
log "watchdog ativo; worker_dir=$WORKER_DIR intervalo=${INTERVAL}s"

failures=0
while true; do
  rotate_watch_log_if_needed
  termux-wake-lock 2>/dev/null || true
  ensure_sshd_running
  if [[ -x "$START_SCRIPT" ]]; then
    if "$START_SCRIPT" >> "$WATCH_LOG" 2>&1; then
      failures=0
      write_status "watchdog_ok pid=$$ $(now_iso)"
    else
      failures=$((failures + 1))
      log "start falhou; falhas=$failures; nova tentativa em ${INTERVAL}s"
      write_status "watchdog_start_failed failures=$failures $(now_iso)"
    fi
  else
    failures=$((failures + 1))
    log "start script não encontrado: $START_SCRIPT"
    write_status "watchdog_missing_start failures=$failures $(now_iso)"
  fi
  sleep "$INTERVAL"
done
