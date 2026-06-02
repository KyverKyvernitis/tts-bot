#!/usr/bin/env bash
set -euo pipefail

SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SRC_DIR/../.." && pwd)"
WORKER_HOME="${CORE_WORKER_HOME:-$HOME/core-worker}"
ENV_FILE="${CORE_WORKER_ENV:-$HOME/.core-worker.env}"

if command -v apt-get >/dev/null 2>&1; then
  sudo apt-get update
  sudo apt-get install -y python3 python3-venv python3-pip ffmpeg curl git rsync
fi

mkdir -p "$WORKER_HOME" "$WORKER_HOME/secrets" "$WORKER_HOME/cache" "$WORKER_HOME/logs"
cp "$REPO_ROOT/deploy/termux/phone-worker/phone_worker.py" "$WORKER_HOME/phone_worker.py"
cp "$REPO_ROOT/deploy/termux/phone-worker/music_agent.py" "$WORKER_HOME/music_agent.py"
cp "$SRC_DIR/start-core-worker.sh" "$WORKER_HOME/start-core-worker.sh"
cp "$SRC_DIR/pair-core-worker.sh" "$WORKER_HOME/pair-core-worker.sh"
chmod +x "$WORKER_HOME/start-core-worker.sh" "$WORKER_HOME/pair-core-worker.sh"

cat > "$WORKER_HOME/requirements-worker.txt" <<'REQ'
aiohttp
discord.py[voice]>=2.7.1,<2.8
PyNaCl
davey
yt-dlp[default]
gTTS
edge-tts
psutil
google-cloud-texttospeech
requests>=2.31.0
REQ

python3 -m venv "$WORKER_HOME/.venv"
"$WORKER_HOME/.venv/bin/python" -m pip install -U pip wheel
"$WORKER_HOME/.venv/bin/python" -m pip install -r "$WORKER_HOME/requirements-worker.txt"

if [ ! -f "$ENV_FILE" ]; then
  cp "$SRC_DIR/core-worker.env.example" "$ENV_FILE"
  sed -i "s|CORE_WORKER_NAME=Meu-PC|CORE_WORKER_NAME=$(hostname)|" "$ENV_FILE" || true
fi

mkdir -p "$HOME/.config/systemd/user"
cp "$SRC_DIR/core-worker.service" "$HOME/.config/systemd/user/core-worker.service"
systemctl --user daemon-reload || true

echo "Instalado em $WORKER_HOME"
echo "Edite $ENV_FILE, pareie com ~/core-worker/pair-core-worker.sh e então rode:"
echo "systemctl --user enable --now core-worker.service"
