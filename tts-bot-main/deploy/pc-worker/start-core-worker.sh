#!/usr/bin/env bash
set -euo pipefail

WORKER_HOME="${CORE_WORKER_HOME:-$HOME/core-worker}"
ENV_FILE="${CORE_WORKER_ENV:-$HOME/.core-worker.env}"
PYTHON="${WORKER_HOME}/.venv/bin/python"

if [ -f "$ENV_FILE" ]; then
  set -a
  # shellcheck disable=SC1090
  . "$ENV_FILE"
  set +a
fi

export CORE_WORKER_SOURCE="${CORE_WORKER_SOURCE:-linux-pc-worker}"
export CORE_WORKER_DEVICE_TYPE="${CORE_WORKER_DEVICE_TYPE:-linux_pc}"
export CORE_WORKER_RUNTIME_MODE="${CORE_WORKER_RUNTIME_MODE:-linux-pc}"
export PHONE_WORKER_ENV="$ENV_FILE"
export PHONE_WORKER_DIR="$WORKER_HOME"
export PHONE_WORKER_HOST="${CORE_WORKER_HOST:-${PHONE_WORKER_HOST:-127.0.0.1}}"
export PHONE_WORKER_PORT="${CORE_WORKER_PORT:-${PHONE_WORKER_PORT:-8766}}"
export CORE_WORKER_PROFILE="${CORE_WORKER_PROFILE:-turbo}"

mkdir -p "$WORKER_HOME" "$WORKER_HOME/logs" "$WORKER_HOME/secrets" "$WORKER_HOME/cache"
cd "$WORKER_HOME"

if [ ! -x "$PYTHON" ]; then
  python3 -m venv "$WORKER_HOME/.venv"
fi

exec "$PYTHON" "$WORKER_HOME/phone_worker.py" \
  --host "$PHONE_WORKER_HOST" \
  --port "$PHONE_WORKER_PORT" \
  --token "${PHONE_WORKER_TOKEN:-${CORE_WORKER_TOKEN:-}}"
