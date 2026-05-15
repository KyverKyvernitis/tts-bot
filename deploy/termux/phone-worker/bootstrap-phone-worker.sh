#!/data/data/com.termux/files/usr/bin/bash
# Bootstrap/repair local para transformar um Termux em Core Worker pareado.
# Uso:
#   bash bootstrap-phone-worker.sh CORE-XXXX http://100.x.x.x:10000 "Xiaomi Worker 2" midia
set -Eeuo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WORKER_DIR="${PHONE_WORKER_DIR:-$HOME/phone-worker}"
CODE="${1:-}"
VPS_URL="${2:-${CORE_WORKER_VPS_URL:-}}"
WORKER_NAME="${3:-${CORE_WORKER_NAME:-}}"
PROFILE="${4:-${CORE_WORKER_PROFILE:-midia}}"

log() { printf '[core-worker-bootstrap] %s\n' "$*"; }

if [[ -z "$CODE" ]]; then
  read -r -p "Código CORE-XXXX: " CODE
fi
if [[ -z "$VPS_URL" ]]; then
  read -r -p "URL da VPS/Tailscale (ex: http://100.x.x.x:10000): " VPS_URL
fi
if [[ -z "$WORKER_NAME" ]]; then
  MANUFACTURER="$(getprop ro.product.manufacturer 2>/dev/null || true)"
  MODEL="$(getprop ro.product.model 2>/dev/null || true)"
  WORKER_NAME="${MANUFACTURER} ${MODEL}"
  WORKER_NAME="${WORKER_NAME# }"
  WORKER_NAME="${WORKER_NAME% }"
  [[ -n "$WORKER_NAME" ]] || WORKER_NAME="Core Phone Worker"
fi

log "instalando dependências básicas"
pkg install python tmux curl -y || true
pkg install termux-api -y || true

log "copiando/reparando scripts em $WORKER_DIR"
mkdir -p "$WORKER_DIR"
for f in phone_worker.py start-phone-worker.sh watch-phone-worker.sh pair-phone-worker.sh bootstrap-phone-worker.sh install.sh README.md phone-worker.env.example; do
  if [[ -f "$SCRIPT_DIR/$f" ]]; then
    cp "$SCRIPT_DIR/$f" "$WORKER_DIR/$f"
  fi
done
for f in start-phone-worker.sh watch-phone-worker.sh pair-phone-worker.sh bootstrap-phone-worker.sh; do
  if [[ -f "$WORKER_DIR/$f" ]]; then
    chmod +x "$WORKER_DIR/$f"
    cp "$WORKER_DIR/$f" "$HOME/$f" 2>/dev/null || true
    chmod +x "$HOME/$f" 2>/dev/null || true
  fi
done
chmod +x "$WORKER_DIR/phone_worker.py" "$WORKER_DIR/install.sh" 2>/dev/null || true

if [[ ! -f "$HOME/.phone-worker.env" && -f "$WORKER_DIR/phone-worker.env.example" ]]; then
  cp "$WORKER_DIR/phone-worker.env.example" "$HOME/.phone-worker.env"
  chmod 600 "$HOME/.phone-worker.env"
fi

log "pareando como '$WORKER_NAME' perfil '$PROFILE'"
bash "$WORKER_DIR/pair-phone-worker.sh" "$CODE" "$VPS_URL" "$WORKER_NAME" "$PROFILE"

log "reiniciando worker"
pkill -f '[p]hone_worker.py' 2>/dev/null || true
sleep 1
bash "$WORKER_DIR/start-phone-worker.sh"
sleep 2

log "teste de heartbeat"
cd "$WORKER_DIR"
python phone_worker.py --heartbeat-once || true
log "pronto. Abra o painel workers e aperte Atualizar."
