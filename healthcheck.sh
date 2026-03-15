#!/usr/bin/env bash
set -u

LOG_FILE="/home/ubuntu/bot/healthcheck.log"
STATE_FILE="/home/ubuntu/bot/.healthcheck_last_alert"
FAIL_COUNT_FILE="/home/ubuntu/bot/.healthcheck_fail_count"
STARTING_SINCE_FILE="/home/ubuntu/bot/.healthcheck_starting_since"
URL="http://127.0.0.1:10000/health"
SERVICE_NAME="tts-bot"
FAIL_THRESHOLD=2
STARTING_GRACE_SECONDS=60

log() {
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] $1" >> "$LOG_FILE"
}

last_logs() {
  journalctl -u "$SERVICE_NAME" -n 20 --no-pager 2>/dev/null | tail -n 15
}

get_fail_count() {
  if [ -f "$FAIL_COUNT_FILE" ]; then
    cat "$FAIL_COUNT_FILE" 2>/dev/null || echo 0
  else
    echo 0
  fi
}

set_fail_count() {
  echo "$1" > "$FAIL_COUNT_FILE"
}

clear_fail_count() {
  rm -f "$FAIL_COUNT_FILE"
}

get_last_alert() {
  if [ -f "$STATE_FILE" ]; then
    cat "$STATE_FILE" 2>/dev/null || true
  fi
}

set_last_alert() {
  echo "$1" > "$STATE_FILE"
}

clear_last_alert() {
  rm -f "$STATE_FILE"
}

get_starting_since() {
  if [ -f "$STARTING_SINCE_FILE" ]; then
    cat "$STARTING_SINCE_FILE" 2>/dev/null || true
  fi
}

set_starting_since() {
  echo "$1" > "$STARTING_SINCE_FILE"
}

clear_starting_since() {
  rm -f "$STARTING_SINCE_FILE"
}

send_recovery_if_needed() {
  local last_alert
  last_alert="$(get_last_alert)"

  if [ -n "$last_alert" ] && [ "$last_alert" != "recovered" ]; then
    local body="Serviço: $SERVICE_NAME

O serviço voltou ao normal e o /health respondeu healthy."
    /home/ubuntu/bot/alert.sh success "Serviço recuperado" "$body"
    set_last_alert "recovered"
  fi
}

register_failure() {
  local fail_type="$1"
  local title="$2"
  local body="$3"

  local count
  count="$(get_fail_count)"
  count=$((count + 1))
  set_fail_count "$count"

  log "Falha detectada ($fail_type). Contagem: $count/$FAIL_THRESHOLD"

  if [ "$count" -lt "$FAIL_THRESHOLD" ]; then
    return 0
  fi

  local last_alert
  last_alert="$(get_last_alert)"

  if [ "$last_alert" != "$fail_type" ]; then
    /home/ubuntu/bot/alert.sh warn "$title" "$body"
    set_last_alert "$fail_type"
  fi

  log "Limite de falhas atingido para $fail_type. Reiniciando $SERVICE_NAME."
  sudo systemctl restart "$SERVICE_NAME"
}

if ! systemctl is-active --quiet "$SERVICE_NAME"; then
  LOGS="$(last_logs)"
  BODY="Serviço: $SERVICE_NAME

Últimas linhas:
$LOGS

Atingindo limite de falhas, o serviço será reiniciado."
  register_failure "inactive" "Serviço inativo detectado" "$BODY"
  exit 0
fi

HEALTH_JSON="$(curl -fsS --max-time 10 "$URL" 2>/dev/null || true)"

if [ -z "$HEALTH_JSON" ]; then
  LOGS="$(last_logs)"
  BODY="Serviço: $SERVICE_NAME

Últimas linhas:
$LOGS

Atingindo limite de falhas, o serviço será reiniciado."
  register_failure "http_fail" "Endpoint /health não respondeu" "$BODY"
  exit 0
fi

if echo "$HEALTH_JSON" | grep -q '"starting":true'; then
  NOW_TS="$(date +%s)"
  START_TS="$(get_starting_since)"

  if [ -z "$START_TS" ]; then
    set_starting_since "$NOW_TS"
    log "Bot em inicialização. Iniciando janela de tolerância de ${STARTING_GRACE_SECONDS}s."
    exit 0
  fi

  ELAPSED=$((NOW_TS - START_TS))

  if [ "$ELAPSED" -lt "$STARTING_GRACE_SECONDS" ]; then
    log "Bot ainda inicializando (${ELAPSED}s/${STARTING_GRACE_SECONDS}s). Sem reinício."
    exit 0
  fi

  LOGS="$(last_logs)"
  BODY="Serviço: $SERVICE_NAME

Resposta /health:
$HEALTH_JSON

Últimas linhas:
$LOGS

Atingindo limite de falhas, o serviço será reiniciado."
  register_failure "starting_timeout" "Bot preso em inicialização" "$BODY"
  exit 0
fi

clear_starting_since

echo "$HEALTH_JSON" | grep -q '"healthy":true'
IS_HEALTHY=$?

if [ "$IS_HEALTHY" -ne 0 ]; then
  LOGS="$(last_logs)"
  BODY="Serviço: $SERVICE_NAME

Resposta /health:
$HEALTH_JSON

Últimas linhas:
$LOGS

Atingindo limite de falhas, o serviço será reiniciado."
  register_failure "unhealthy" "Healthcheck retornou unhealthy" "$BODY"
  exit 0
fi

log "OK"
clear_fail_count
send_recovery_if_needed
