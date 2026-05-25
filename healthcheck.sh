#!/usr/bin/env bash
set -u

LOG_FILE="${HEALTHCHECK_LOG_FILE:-/home/ubuntu/bot/healthcheck.log}"
STATE_FILE="${HEALTHCHECK_LAST_ALERT_FILE:-/home/ubuntu/bot/.healthcheck_last_alert}"
FAIL_COUNT_FILE="${HEALTHCHECK_FAIL_COUNT_FILE:-/home/ubuntu/bot/.healthcheck_fail_count}"
FAIL_KIND_FILE="${HEALTHCHECK_FAIL_KIND_FILE:-/home/ubuntu/bot/.healthcheck_fail_kind}"
FAIL_SINCE_FILE="${HEALTHCHECK_FAIL_SINCE_FILE:-/home/ubuntu/bot/.healthcheck_fail_since}"
STARTING_SINCE_FILE="${HEALTHCHECK_STARTING_SINCE_FILE:-/home/ubuntu/bot/.healthcheck_starting_since}"
LAST_RESTART_FILE="${HEALTHCHECK_LAST_RESTART_FILE:-/home/ubuntu/bot/.healthcheck_last_restart}"
URL="${HEALTHCHECK_URL:-http://127.0.0.1:10000/health}"
SERVICE_NAME="${HEALTHCHECK_SERVICE_NAME:-tts-bot}"
FAIL_THRESHOLD="${HEALTHCHECK_FAIL_THRESHOLD:-2}"
STARTING_GRACE_SECONDS="${HEALTHCHECK_STARTING_GRACE_SECONDS:-180}"
RESTART_COOLDOWN_SECONDS="${HEALTHCHECK_RESTART_COOLDOWN_SECONDS:-600}"
STALE_FAIL_SECONDS="${HEALTHCHECK_STALE_FAIL_SECONDS:-1800}"
CURL_MAX_TIME="${HEALTHCHECK_CURL_MAX_TIME:-8}"

mkdir -p "$(dirname "$LOG_FILE")" 2>/dev/null || true

to_int() {
  local value="${1:-0}" fallback="${2:-0}"
  case "$value" in
    ''|*[!0-9]*) printf '%s' "$fallback" ;;
    *) printf '%s' "$value" ;;
  esac
}

FAIL_THRESHOLD="$(to_int "$FAIL_THRESHOLD" 2)"
STARTING_GRACE_SECONDS="$(to_int "$STARTING_GRACE_SECONDS" 180)"
RESTART_COOLDOWN_SECONDS="$(to_int "$RESTART_COOLDOWN_SECONDS" 600)"
STALE_FAIL_SECONDS="$(to_int "$STALE_FAIL_SECONDS" 1800)"
CURL_MAX_TIME="$(to_int "$CURL_MAX_TIME" 8)"

log() {
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] $1" >> "$LOG_FILE"
}

read_file() {
  local file="$1"
  if [ -f "$file" ]; then
    cat "$file" 2>/dev/null || true
  fi
}

write_file() {
  local file="$1" value="$2"
  printf '%s\n' "$value" > "$file" 2>/dev/null || true
}

rm_state() {
  rm -f "$@" 2>/dev/null || true
}

now_ts() {
  date +%s
}

last_logs() {
  journalctl -u "$SERVICE_NAME" -n 30 --no-pager 2>/dev/null | tail -n 20
}

failure_state_is_stale() {
  local now first_seen
  now="$(now_ts)"
  first_seen="$(to_int "$(read_file "$FAIL_SINCE_FILE")" 0)"
  [ "$first_seen" -gt 0 ] && [ $((now - first_seen)) -gt "$STALE_FAIL_SECONDS" ]
}

clear_failure_state() {
  rm_state "$FAIL_COUNT_FILE" "$FAIL_KIND_FILE" "$FAIL_SINCE_FILE"
}

clear_starting_since() {
  rm_state "$STARTING_SINCE_FILE"
}

set_last_alert() {
  write_file "$STATE_FILE" "$1"
}

get_last_alert() {
  read_file "$STATE_FILE"
}

send_recovery_if_needed() {
  local last_alert
  last_alert="$(get_last_alert)"
  if [ -n "$last_alert" ] && [ "$last_alert" != "recovered" ]; then
    local body="Serviço: $SERVICE_NAME

O serviço voltou ao normal e o /health respondeu healthy.
Falhas antigas foram zeradas para evitar restart loop."
    /home/ubuntu/bot/alert.sh success "Serviço recuperado" "$body" || true
    set_last_alert "recovered"
  fi
}

restart_allowed() {
  local now last
  now="$(now_ts)"
  last="$(to_int "$(read_file "$LAST_RESTART_FILE")" 0)"
  [ "$last" -le 0 ] && return 0
  [ $((now - last)) -ge "$RESTART_COOLDOWN_SECONDS" ]
}

mark_restarted() {
  write_file "$LAST_RESTART_FILE" "$(now_ts)"
}

register_failure() {
  local fail_type="$1" title="$2" body="$3"
  local now previous_type count first_seen last_alert elapsed cooldown_left
  now="$(now_ts)"

  if failure_state_is_stale; then
    log "Estado de falha antigo expirou; zerando contador antes de registrar $fail_type."
    clear_failure_state
  fi

  previous_type="$(read_file "$FAIL_KIND_FILE")"
  if [ "$previous_type" != "$fail_type" ]; then
    count=0
    write_file "$FAIL_KIND_FILE" "$fail_type"
    write_file "$FAIL_SINCE_FILE" "$now"
  else
    count="$(to_int "$(read_file "$FAIL_COUNT_FILE")" 0)"
    first_seen="$(to_int "$(read_file "$FAIL_SINCE_FILE")" 0)"
    if [ "$first_seen" -le 0 ]; then
      write_file "$FAIL_SINCE_FILE" "$now"
    fi
  fi

  count=$((count + 1))
  # Evita contador absurdo sobreviver a uma crise e virar loop eterno.
  if [ "$count" -gt "$FAIL_THRESHOLD" ]; then
    count="$FAIL_THRESHOLD"
  fi
  write_file "$FAIL_COUNT_FILE" "$count"

  log "Falha detectada ($fail_type). Contagem: $count/$FAIL_THRESHOLD"

  if [ "$count" -lt "$FAIL_THRESHOLD" ]; then
    return 0
  fi

  last_alert="$(get_last_alert)"
  if [ "$last_alert" != "$fail_type" ]; then
    /home/ubuntu/bot/alert.sh warn "$title" "$body" || true
    set_last_alert "$fail_type"
  fi

  if ! restart_allowed; then
    last="$(to_int "$(read_file "$LAST_RESTART_FILE")" 0)"
    elapsed=$((now - last))
    cooldown_left=$((RESTART_COOLDOWN_SECONDS - elapsed))
    [ "$cooldown_left" -lt 0 ] && cooldown_left=0
    log "Limite de falhas atingido para $fail_type, mas restart está em cooldown (${cooldown_left}s restantes)."
    return 0
  fi

  log "Limite de falhas atingido para $fail_type. Reiniciando $SERVICE_NAME."
  mark_restarted
  sudo systemctl restart "$SERVICE_NAME" || true
}

if ! systemctl is-active --quiet "$SERVICE_NAME"; then
  LOGS="$(last_logs)"
  BODY="Serviço: $SERVICE_NAME

Últimas linhas:
$LOGS

O serviço está inativo. O healthcheck só reinicia após falhas consecutivas e cooldown."
  clear_starting_since
  register_failure "inactive" "Serviço inativo detectado" "$BODY"
  exit 0
fi

HEALTH_JSON="$(curl -fsS --max-time "$CURL_MAX_TIME" "$URL" 2>/dev/null || true)"

if [ -z "$HEALTH_JSON" ]; then
  LOGS="$(last_logs)"
  BODY="Serviço: $SERVICE_NAME

Últimas linhas:
$LOGS

O endpoint /health não respondeu dentro de ${CURL_MAX_TIME}s."
  clear_starting_since
  register_failure "http_fail" "Endpoint /health não respondeu" "$BODY"
  exit 0
fi

if echo "$HEALTH_JSON" | grep -q '"starting":true'; then
  NOW_TS="$(now_ts)"
  START_TS="$(to_int "$(read_file "$STARTING_SINCE_FILE")" 0)"
  if [ "$START_TS" -le 0 ]; then
    write_file "$STARTING_SINCE_FILE" "$NOW_TS"
    clear_failure_state
    log "Bot em inicialização. Iniciando janela de tolerância de ${STARTING_GRACE_SECONDS}s."
    exit 0
  fi
  ELAPSED=$((NOW_TS - START_TS))
  if [ "$ELAPSED" -lt "$STARTING_GRACE_SECONDS" ]; then
    clear_failure_state
    log "Bot ainda inicializando (${ELAPSED}s/${STARTING_GRACE_SECONDS}s). Sem reinício."
    exit 0
  fi
  LOGS="$(last_logs)"
  BODY="Serviço: $SERVICE_NAME

Resposta /health:
$HEALTH_JSON

Últimas linhas:
$LOGS

O bot ficou em starting por ${ELAPSED}s."
  register_failure "starting_timeout" "Bot preso em inicialização" "$BODY"
  exit 0
fi

clear_starting_since

if ! echo "$HEALTH_JSON" | grep -q '"healthy":true'; then
  LOGS="$(last_logs)"
  BODY="Serviço: $SERVICE_NAME

Resposta /health:
$HEALTH_JSON

Últimas linhas:
$LOGS

O /health respondeu, mas não está healthy."
  register_failure "unhealthy" "Healthcheck retornou unhealthy" "$BODY"
  exit 0
fi

log "OK"
clear_failure_state
send_recovery_if_needed
