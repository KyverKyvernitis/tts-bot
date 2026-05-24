#!/usr/bin/env bash
set -Eeuo pipefail

cd /home/ubuntu/bot
source /home/ubuntu/bot/.venv/bin/activate
set -a
source /home/ubuntu/bot/.env
set +a

# A VPS não inicia nem aguarda Lavalink local. Música pesada pertence ao phone worker/Music Agent.

exec python3 bot.py
