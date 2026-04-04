#!/usr/bin/env bash
set -u

SERVER_DIR="/home/ubuntu/bot/activity /sinuca-server"
ENV_FILE="$SERVER_DIR/.env"

cd "$SERVER_DIR" || exit 1

if [ -f "$ENV_FILE" ]; then
  set -a
  . "$ENV_FILE"
  set +a
fi

exec /usr/bin/node dist/index.js
