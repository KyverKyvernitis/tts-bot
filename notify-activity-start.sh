#!/usr/bin/env bash
set -u

HEALTH_URL="http://127.0.0.1:8787/health"
ATTEMPTS=12
SLEEP_SECONDS=2

for _ in $(seq 1 "$ATTEMPTS"); do
  if curl -fsS --max-time 5 "$HEALTH_URL" | grep -Eq '"ok"[[:space:]]*:[[:space:]]*true'; then
    BODY="Backend da Activity respondeu com sucesso no /health.
URL de verificação: $HEALTH_URL"
    /home/ubuntu/bot/alert.sh success "Activity da sinuca online" "$BODY"
    exit 0
  fi
  sleep "$SLEEP_SECONDS"
done

exit 0
