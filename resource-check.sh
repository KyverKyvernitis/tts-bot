#!/usr/bin/env bash
set -u

LOG_FILE="/home/ubuntu/bot/resource-check.log"
STATE_FILE="/home/ubuntu/bot/.resource_alert_state"

RAM_THRESHOLD=85
DISK_THRESHOLD=85
DISK_PATH="/"

log() {
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] $1" >> "$LOG_FILE"
}

get_state() {
  if [ -f "$STATE_FILE" ]; then
    cat "$STATE_FILE" 2>/dev/null || true
  fi
}

set_state() {
  echo "$1" > "$STATE_FILE"
}

clear_state() {
  rm -f "$STATE_FILE"
}

RAM_USED_PERCENT="$(free | awk '/Mem:/ {printf "%.0f", ($3/$2)*100}')"
DISK_USED_PERCENT="$(df -P "$DISK_PATH" | awk 'NR==2 {gsub("%","",$5); print $5}')"

ALERTS=()

if [ "$RAM_USED_PERCENT" -ge "$RAM_THRESHOLD" ]; then
  ALERTS+=("RAM:${RAM_USED_PERCENT}%")
fi

if [ "$DISK_USED_PERCENT" -ge "$DISK_THRESHOLD" ]; then
  ALERTS+=("DISK:${DISK_USED_PERCENT}%")
fi

CURRENT_STATE="$(printf '%s\n' "${ALERTS[@]}" | paste -sd ',' -)"
LAST_STATE="$(get_state)"

if [ -n "$CURRENT_STATE" ]; then
  if [ "$CURRENT_STATE" != "$LAST_STATE" ]; then
    BODY="RAM: ${RAM_USED_PERCENT}%
Disco ($DISK_PATH): ${DISK_USED_PERCENT}%

Limites:
RAM >= ${RAM_THRESHOLD}%
Disco >= ${DISK_THRESHOLD}%"
    log "Alerta enviado: $CURRENT_STATE"
    /home/ubuntu/bot/alert.sh warn "Recursos altos na VPS" "$BODY"
    set_state "$CURRENT_STATE"
  fi
  exit 0
fi

if [ -n "$LAST_STATE" ]; then
  BODY="RAM: ${RAM_USED_PERCENT}%
Disco ($DISK_PATH): ${DISK_USED_PERCENT}%"
  log "Recursos normalizados."
  /home/ubuntu/bot/alert.sh success "Recursos normalizados" "$BODY"
fi

clear_state
log "OK"
