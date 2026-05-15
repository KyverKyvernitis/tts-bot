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
WORKER_NAME="${3:-${CORE_WORKER_NAME:-}}"
PROFILE_OR_ROLES="${4:-${CORE_WORKER_PROFILE:-midia}}"

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

case "${PROFILE_OR_ROLES,,}" in
  leve|lite)
    ROLES="phone-worker,diagnostics,log-summary"
    ;;
  midia|media)
    ROLES="phone-worker,diagnostics,log-summary,zip-validate,ffmpeg,ffprobe,tts-convert"
    ;;
  completo|full)
    ROLES="phone-worker,diagnostics,log-summary,maintenance-plan,zip-validate,ffmpeg,ffprobe,tts-convert"
    ;;
  bedrock)
    ROLES="phone-worker,diagnostics,log-summary,bedrock,bedrock-logs,bedrock-backup"
    ;;
  *)
    ROLES="$PROFILE_OR_ROLES"
    ;;
esac
CAPABILITIES="${CORE_WORKER_CAPABILITIES:-$ROLES}"

if [[ ! -f "$WORKER_DIR/phone_worker.py" ]]; then
  echo "phone_worker.py não encontrado em $WORKER_DIR" >&2
  exit 1
fi

cd "$WORKER_DIR"
CORE_WORKER_NAME="$WORKER_NAME" \
CORE_WORKER_ROLES="$ROLES" \
CORE_WORKER_CAPABILITIES="$CAPABILITIES" \
"$PYTHON_BIN" phone_worker.py \
  --pair "$CODE" \
  --vps-url "$VPS_URL" \
  --name "$WORKER_NAME" \
  --roles "$ROLES" \
  --capabilities "$CAPABILITIES" \
  --env-file "$ENV_FILE"
