#!/data/data/com.termux/files/usr/bin/bash
# Watchdog local do Core Worker/phone-worker.
# Chama o supervisor start-phone-worker.sh, com backoff e log pequeno.
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
# Patch 41: responsabilidades importantes devem tentar de novo a cada intervalo,
# mesmo depois de falha. Mantemos a variável antiga só por compatibilidade,
# mas o watchdog local não aumenta mais o intervalo sozinho.
MAX_BACKOFF="${PHONE_WORKER_WATCH_MAX_BACKOFF_SECONDS:-60}"
WORKER_DIR="${PHONE_WORKER_DIR:-$HOME/phone-worker}"
START_SCRIPT="$WORKER_DIR/start-phone-worker.sh"
WATCH_LOG="${PHONE_WORKER_WATCH_LOG_FILE:-$WORKER_DIR/phone-worker-watch.log}"
WATCH_PID_FILE="${PHONE_WORKER_WATCH_PID_FILE:-$WORKER_DIR/phone-worker-watch.pid}"
MAX_LOG_BYTES="${PHONE_WORKER_LOG_MAX_BYTES:-1048576}"

if [[ ! -x "$START_SCRIPT" && -x "$HOME/start-phone-worker.sh" ]]; then
  START_SCRIPT="$HOME/start-phone-worker.sh"
fi

log() {
  printf '[phone-worker-watch] %s\n' "$*" | tee -a "$WATCH_LOG" >/dev/null
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

termux-wake-lock 2>/dev/null || true
mkdir -p "$WORKER_DIR"
printf '%s\n' "$$" > "$WATCH_PID_FILE" 2>/dev/null || true
trap 'rm -f "$WATCH_PID_FILE" 2>/dev/null || true' EXIT INT TERM

failures=0
while true; do
  rotate_watch_log_if_needed
  if [[ -x "$START_SCRIPT" ]]; then
    if "$START_SCRIPT" >> "$WATCH_LOG" 2>&1; then
      failures=0
      sleep "$INTERVAL"
    else
      failures=$((failures + 1))
      log "start falhou; nova tentativa em ${INTERVAL}s"
      sleep "$INTERVAL"
    fi
  else
    failures=$((failures + 1))
    log "start script não encontrado: $START_SCRIPT"
    sleep "$INTERVAL"
  fi
done
