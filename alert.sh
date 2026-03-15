#!/usr/bin/env bash
set -u

ENV_FILE="/home/ubuntu/bot/.env"
HOSTNAME="$(hostname)"
NOW="$(date '+%Y-%m-%d %H:%M:%S')"

if [ -f "$ENV_FILE" ]; then
  set -a
  . "$ENV_FILE"
  set +a
fi

TYPE="${1:-info}"
TITLE="${2:-Sem título}"
BODY="${3:-}"

case "$TYPE" in
  error)   EMOJI="❌" ;;
  warn)    EMOJI="⚠️" ;;
  success) EMOJI="✅" ;;
  update)  EMOJI="🔄" ;;
  *)       EMOJI="ℹ️" ;;
esac

escape_json() {
  printf '%s' "$1" | sed 's/\\/\\\\/g; s/"/\\"/g; :a;N;$!ba;s/\n/\\n/g'
}

if [ -z "${ALERT_WEBHOOK_URL:-}" ]; then
  exit 0
fi

MESSAGE="$EMOJI $TITLE
Host: $HOSTNAME
Hora: $NOW

$BODY"

ESCAPED_MESSAGE="$(escape_json "$MESSAGE")"

curl -fsS -H "Content-Type: application/json" \
  -X POST \
  -d "{\"content\":\"$ESCAPED_MESSAGE\"}" \
  "$ALERT_WEBHOOK_URL" >/dev/null 2>&1 || true
