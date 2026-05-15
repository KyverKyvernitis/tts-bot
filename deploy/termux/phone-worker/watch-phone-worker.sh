#!/data/data/com.termux/files/usr/bin/bash
# Watchdog local do worker auxiliar do celular.
set -u

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
if [[ ! -x "$START_SCRIPT" && -x "$HOME/start-phone-worker.sh" ]]; then
  START_SCRIPT="$HOME/start-phone-worker.sh"
fi
termux-wake-lock 2>/dev/null || true

while true; do
  if [[ -x "$START_SCRIPT" ]]; then
    "$START_SCRIPT" || true
  else
    echo "[phone-worker-watch] start script não encontrado: $START_SCRIPT" >&2
  fi
  sleep "$INTERVAL"
done
