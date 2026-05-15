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
termux-wake-lock 2>/dev/null || true

while true; do
  "$HOME/start-phone-worker.sh" || true
  sleep "$INTERVAL"
done
