#!/usr/bin/env bash
set -u

SERVER_DIR="/home/ubuntu/bot/activity /sinuca-server"
ENV_FILE="$SERVER_DIR/.env"
NODE_BIN="/home/ubuntu/.nvm/versions/node/v20.20.2/bin/node"

cd "$SERVER_DIR" || exit 1

if [ -f "$ENV_FILE" ]; then
  set -a
  . "$ENV_FILE"
  set +a
fi

exec "$NODE_BIN" dist/index.js
