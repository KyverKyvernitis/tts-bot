#!/usr/bin/env bash
set -u

HEALTH_URL="${ACTIVITY_HEALTH_URL:-http://127.0.0.1:8787/health}"
SERVICE_NAME="${ACTIVITY_SERVICE_NAME:-sinuca-activity-server}"
ATTEMPTS="${ACTIVITY_NOTIFY_ATTEMPTS:-12}"
SLEEP_SECONDS="${ACTIVITY_NOTIFY_SLEEP_SECONDS:-2}"
STABLE_RECHECK_DELAY="${ACTIVITY_NOTIFY_STABLE_RECHECK_DELAY:-2}"
COOLDOWN_SECONDS="${ACTIVITY_NOTIFY_COOLDOWN_SECONDS:-900}"
STATE_FILE="${ACTIVITY_NOTIFY_STATE_FILE:-/tmp/sinuca-activity-online.state}"

read_state() {
  LAST_NOTIFIED_AT=0
  LAST_BOOT_ID=""
  LAST_PID=""

  if [ ! -f "$STATE_FILE" ]; then
    return 0
  fi

  while IFS='=' read -r key value; do
    case "$key" in
      notified_at) LAST_NOTIFIED_AT="$value" ;;
      boot_id) LAST_BOOT_ID="$value" ;;
      pid) LAST_PID="$value" ;;
    esac
  done < "$STATE_FILE"
}

write_state() {
  umask 077
  cat > "$STATE_FILE" <<STATE
notified_at=$NOW_TS
boot_id=$BOOT_ID
pid=$CURRENT_PID
STATE
}

current_boot_id() {
  cat /proc/sys/kernel/random/boot_id 2>/dev/null || echo "unknown"
}

systemd_service_exists() {
  systemctl cat "$SERVICE_NAME" >/dev/null 2>&1
}

port_pid() {
  fuser -n tcp 8787 2>/dev/null | tr ' ' '\n' | awk 'NF { print $1; exit }'
}

current_pid() {
  local pid=""

  if systemd_service_exists; then
    pid="$(systemctl show -p MainPID --value "$SERVICE_NAME" 2>/dev/null || true)"
    case "$pid" in
      ''|0) pid="" ;;
    esac
  fi

  if [ -z "$pid" ]; then
    pid="$(port_pid)"
  fi

  printf '%s' "$pid"
}

service_active() {
  if systemd_service_exists; then
    systemctl is-active --quiet "$SERVICE_NAME"
    return $?
  fi

  [ -n "$(port_pid)" ]
}

health_ok() {
  curl -fsS --max-time 5 "$HEALTH_URL" | grep -Eq '"ok"[[:space:]]*:[[:space:]]*true'
}

recent_notification() {
  if [ "$LAST_BOOT_ID" != "$BOOT_ID" ]; then
    return 1
  fi

  if [ -n "$CURRENT_PID" ] && [ -n "$LAST_PID" ] && [ "$CURRENT_PID" = "$LAST_PID" ]; then
    return 0
  fi

  if [ "$LAST_NOTIFIED_AT" -gt 0 ] 2>/dev/null; then
    local elapsed=$((NOW_TS - LAST_NOTIFIED_AT))
    if [ "$elapsed" -lt "$COOLDOWN_SECONDS" ]; then
      return 0
    fi
  fi

  return 1
}

NOW_TS="$(date +%s)"
BOOT_ID="$(current_boot_id)"
CURRENT_PID="$(current_pid)"
read_state

if recent_notification; then
  exit 0
fi

for _ in $(seq 1 "$ATTEMPTS"); do
  if service_active && health_ok; then
    sleep "$STABLE_RECHECK_DELAY"

    CURRENT_PID="$(current_pid)"
    if service_active && health_ok; then
      BODY="Backend da Activity respondeu com sucesso no /health.
URL de verificação: $HEALTH_URL"
      /home/ubuntu/bot/alert.sh success "Activity da sinuca online" "$BODY"
      write_state
      exit 0
    fi
  fi

  sleep "$SLEEP_SECONDS"
done

exit 0
