#!/usr/bin/env bash
set -euo pipefail

if [ $# -lt 2 ]; then
  echo "uso: $0 CORE-XXXX https://sua-vps:10000 [nome] [perfil]" >&2
  exit 2
fi

CODE="$1"
VPS_URL="$2"
NAME="${3:-${CORE_WORKER_NAME:-$(hostname)}}"
PROFILE="${4:-${CORE_WORKER_PROFILE:-turbo}}"
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
export CORE_WORKER_PROFILE="$PROFILE"
export PHONE_WORKER_ENV="$ENV_FILE"
export PHONE_WORKER_DIR="$WORKER_HOME"

mkdir -p "$WORKER_HOME" "$WORKER_HOME/secrets"
cd "$WORKER_HOME"

if [ ! -x "$PYTHON" ]; then
  python3 -m venv "$WORKER_HOME/.venv"
  "$PYTHON" -m pip install -U pip wheel
  "$PYTHON" -m pip install -r "$WORKER_HOME/requirements-worker.txt"
fi

exec "$PYTHON" "$WORKER_HOME/phone_worker.py" \
  --pair "$CODE" \
  --vps-url "$VPS_URL" \
  --name "$NAME" \
  --env-file "$ENV_FILE" \
  --host "${CORE_WORKER_HOST:-127.0.0.1}" \
  --port "${CORE_WORKER_PORT:-8766}"
