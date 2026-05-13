#!/usr/bin/env bash
set -Eeuo pipefail

cd /home/ubuntu/bot
source /home/ubuntu/bot/.venv/bin/activate
set -a
source /home/ubuntu/bot/.env
set +a

# Evita corrida no boot: o bot só tenta Wavelink depois que o Lavalink REST
# responde. Se Lavalink estiver desativado no .env, o script sai OK.
if [[ -x /home/ubuntu/bot/scripts/wait-audio-node-ready.py ]]; then
  /home/ubuntu/bot/scripts/wait-audio-node-ready.py --timeout "${AUDIO_NODE_STARTUP_WAIT_SECONDS:-90}"
fi

exec python3 bot.py
