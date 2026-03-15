#!/usr/bin/env bash
cd /home/ubuntu/bot
source /home/ubuntu/bot/.venv/bin/activate
set -a
source /home/ubuntu/bot/.env
set +a
exec python3 bot.py
