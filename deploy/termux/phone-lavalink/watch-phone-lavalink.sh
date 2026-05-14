#!/data/data/com.termux/files/usr/bin/bash
# Watchdog local do celular. Ele não depende da VPS: quando a internet voltar e
# o Termux estiver vivo, o Lavalink volta sozinho.
set -u

ENV_FILE="${PHONE_LAVALINK_ENV:-$HOME/.phone-lavalink.env}"
if [[ -f "$ENV_FILE" ]]; then
  # shellcheck disable=SC1090
  source "$ENV_FILE"
fi

INTERVAL="${PHONE_LAVALINK_WATCH_INTERVAL_SECONDS:-60}"
START_SCRIPT="${PHONE_LAVALINK_START_SCRIPT:-$HOME/start-phone-lavalink.sh}"

termux-wake-lock 2>/dev/null || true

while true; do
  "$START_SCRIPT" || true
  sleep "$INTERVAL"
done
