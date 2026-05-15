#!/data/data/com.termux/files/usr/bin/bash
# Instala os scripts do phone-worker no Termux.
set -Eeuo pipefail

SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WORKER_DIR="${PHONE_WORKER_DIR:-$HOME/phone-worker}"

pkg install python tmux curl -y
mkdir -p "$WORKER_DIR"
cp "$SRC_DIR/phone_worker.py" "$WORKER_DIR/phone_worker.py"
cp "$SRC_DIR/start-phone-worker.sh" "$WORKER_DIR/start-phone-worker.sh"
cp "$SRC_DIR/watch-phone-worker.sh" "$WORKER_DIR/watch-phone-worker.sh"
cp "$SRC_DIR/pair-phone-worker.sh" "$WORKER_DIR/pair-phone-worker.sh"
cp "$SRC_DIR/install.sh" "$WORKER_DIR/install.sh"
cp "$SRC_DIR/README.md" "$WORKER_DIR/README.md" 2>/dev/null || true
cp "$SRC_DIR/phone-worker.env.example" "$WORKER_DIR/phone-worker.env.example" 2>/dev/null || true
# Compatibilidade com atalhos antigos em ~/
cp "$SRC_DIR/start-phone-worker.sh" "$HOME/start-phone-worker.sh"
cp "$SRC_DIR/watch-phone-worker.sh" "$HOME/watch-phone-worker.sh"
cp "$SRC_DIR/pair-phone-worker.sh" "$HOME/pair-phone-worker.sh"
chmod +x "$WORKER_DIR/phone_worker.py" "$WORKER_DIR/start-phone-worker.sh" "$WORKER_DIR/watch-phone-worker.sh" "$WORKER_DIR/pair-phone-worker.sh" "$WORKER_DIR/install.sh"
chmod +x "$HOME/start-phone-worker.sh" "$HOME/watch-phone-worker.sh" "$HOME/pair-phone-worker.sh"

if [[ ! -f "$HOME/.phone-worker.env" ]]; then
  cp "$SRC_DIR/phone-worker.env.example" "$HOME/.phone-worker.env"
  chmod 600 "$HOME/.phone-worker.env"
  echo "Criado: $HOME/.phone-worker.env"
  echo "Edite PHONE_WORKER_TOKEN antes de iniciar."
  echo "Depois do pareamento em workers, o script ~/phone-worker/pair-phone-worker.sh preenche CORE_WORKER_* automaticamente."
fi

echo "Instalado. Para iniciar:"
echo "  nano ~/.phone-worker.env"
echo "  ~/phone-worker/pair-phone-worker.sh CORE-XXXX http://IP_TAILSCALE_DA_VPS:10000"
echo "  ~/phone-worker/start-phone-worker.sh"
echo "  tmux new-session -d -s phone-worker-watch '~/phone-worker/watch-phone-worker.sh'"
