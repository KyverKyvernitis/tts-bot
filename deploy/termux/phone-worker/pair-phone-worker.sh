#!/data/data/com.termux/files/usr/bin/bash
# Pareia o phone-worker atual com o registry Core Workers da VPS.
set -Eeuo pipefail

ENV_FILE="${PHONE_WORKER_ENV:-$HOME/.phone-worker.env}"
if [[ -f "$ENV_FILE" ]]; then
  set -a
  # shellcheck disable=SC1090
  source "$ENV_FILE"
  set +a
fi

WORKER_DIR="${PHONE_WORKER_DIR:-$HOME/phone-worker}"
PYTHON_BIN="${PHONE_WORKER_PYTHON:-python}"
CODE="${1:-}"
VPS_URL="${2:-${CORE_WORKER_VPS_URL:-}}"

if [[ -z "$CODE" ]]; then
  read -r -p "Código CORE-XXXX: " CODE
fi
if [[ -z "$VPS_URL" ]]; then
  read -r -p "URL da VPS/Tailscale (ex: http://100.x.x.x:10000): " VPS_URL
fi

if [[ ! -f "$WORKER_DIR/phone_worker.py" ]]; then
  echo "phone_worker.py não encontrado em $WORKER_DIR" >&2
  exit 1
fi

cd "$WORKER_DIR"
"$PYTHON_BIN" phone_worker.py \
  --pair "$CODE" \
  --vps-url "$VPS_URL" \
  --env-file "$ENV_FILE"
