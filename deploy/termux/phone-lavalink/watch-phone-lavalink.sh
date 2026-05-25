#!/data/data/com.termux/files/usr/bin/bash
# Watchdog local do celular. Ele não depende da VPS: quando a internet voltar e
# o Termux estiver vivo, o Lavalink volta sozinho.
set -u

ENV_FILE="${PHONE_LAVALINK_ENV:-$HOME/.phone-lavalink.env}"
if [[ -f "$ENV_FILE" ]]; then
  # shellcheck disable=SC1090
  source "$ENV_FILE"
fi
PHONE_WORKER_ENV_FILE="${PHONE_WORKER_ENV:-$HOME/.phone-worker.env}"
if [[ -f "$PHONE_WORKER_ENV_FILE" ]]; then
  # shellcheck disable=SC1090
  source "$PHONE_WORKER_ENV_FILE"
fi

truthy() { local value="${1:-}"; value="$(printf '%s' "$value" | tr '[:upper:]' '[:lower:]' | tr -d ' 	
"' | tr -d "'")"; [[ "$value" == "1" || "$value" == "true" || "$value" == "yes" || "$value" == "y" || "$value" == "on" || "$value" == "sim" ]]; }
falsey() { local value="${1:-}"; value="$(printf '%s' "$value" | tr '[:upper:]' '[:lower:]' | tr -d ' 	
"' | tr -d "'")"; [[ "$value" == "0" || "$value" == "false" || "$value" == "no" || "$value" == "n" || "$value" == "off" || "$value" == "nao" || "$value" == "não" ]]; }
safe_mode_enabled() {
  truthy "${PHONE_WORKER_SAFE_MODE:-${PHONE_WORKER_BASIC_ONLY:-${PHONE_WORKER_LIGHT_MODE:-false}}}" && return 0
  truthy "${PHONE_WORKER_DISABLE_HEAVY_SERVICES:-false}" && return 0
  if falsey "${PHONE_WORKER_TURBO_DEPS_INSTALL_MODE:-}" || falsey "${PHONE_WORKER_DEPS_INSTALL_MODE:-}"; then
    truthy "${PHONE_WORKER_ALLOW_HEAVY_SERVICES_WITH_DEPS_OFF:-false}" || return 0
  fi
  return 1
}
if safe_mode_enabled; then
  pkill -f '[j]ava.*Lavalink.jar' 2>/dev/null || true
  exit 0
fi

INTERVAL="${PHONE_LAVALINK_WATCH_INTERVAL_SECONDS:-300}"
START_SCRIPT="${PHONE_LAVALINK_START_SCRIPT:-$HOME/start-phone-lavalink.sh}"

termux-wake-lock 2>/dev/null || true

while true; do
  "$START_SCRIPT" || true
  sleep "$INTERVAL"
done
