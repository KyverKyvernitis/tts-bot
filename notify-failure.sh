#!/usr/bin/env bash
set -u

SERVICE_NAME="tts-bot"

LOGS="$(journalctl -u "$SERVICE_NAME" -n 25 --no-pager 2>/dev/null | tail -n 20)"

BODY="Serviço: $SERVICE_NAME

Últimas linhas:
$LOGS"

/home/ubuntu/bot/alert.sh error "Falha fatal no serviço" "$BODY"
