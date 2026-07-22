#!/usr/bin/env bash
set -euo pipefail

SERVER_DIR="/home/ubuntu/bot/activity/sinuca-server"
ROOT_ENV_FILE="/home/ubuntu/bot/.env"
ENV_FILE="$SERVER_DIR/.env"
NODE_BIN="/home/ubuntu/.nvm/versions/node/v20.20.2/bin/node"
ENTRYPOINT="$SERVER_DIR/dist/index.js"

cd "$SERVER_DIR"

set -a
if [ -f "$ROOT_ENV_FILE" ]; then
  # shellcheck disable=SC1091
  . "$ROOT_ENV_FILE"
fi
if [ -f "$ENV_FILE" ]; then
  # shellcheck disable=SC1090
  . "$ENV_FILE"
fi
set +a

if [ ! -x "$NODE_BIN" ]; then
  NODE_BIN="$(command -v node || true)"
fi
if [ -z "$NODE_BIN" ] || [ ! -x "$NODE_BIN" ]; then
  echo "[osaka-dashboard] Node.js não encontrado." >&2
  exit 1
fi
if [ ! -f "$ENTRYPOINT" ]; then
  echo "[osaka-dashboard] Build ausente: $ENTRYPOINT" >&2
  exit 1
fi

exec "$NODE_BIN" "$ENTRYPOINT"
