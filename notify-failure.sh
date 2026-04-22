#!/usr/bin/env bash
set -euo pipefail

UNIT_NAME="${1:-tts-bot.service}"
DISPLAY_NAME="${UNIT_NAME%.service}"
FORCE_ALERT="${FORCE_ALERT:-0}"

get_prop() {
  local prop="$1"
  systemctl show "$UNIT_NAME" -p "$prop" --value 2>/dev/null || true
}

ACTIVE_STATE="$(get_prop ActiveState)"
SUB_STATE="$(get_prop SubState)"
RESULT_STATE="$(get_prop Result)"
EXEC_MAIN_CODE="$(get_prop ExecMainCode)"
EXEC_MAIN_STATUS="$(get_prop ExecMainStatus)"

ALERT_TYPE="error"
ALERT_TITLE="Falha fatal no serviço"
SUMMARY="O serviço caiu com erro fatal."

if [[ "$FORCE_ALERT" == "1" ]]; then
  ALERT_TYPE="warn"
  ALERT_TITLE="Teste de alerta do serviço"
  SUMMARY="Alerta manual para validar o webhook do serviço."
else
  should_alert=0

  if [[ "$ACTIVE_STATE" == "failed" || "$RESULT_STATE" == "failed" ]]; then
    should_alert=1
  fi

  if [[ -n "$EXEC_MAIN_STATUS" && "$EXEC_MAIN_STATUS" != "0" ]]; then
    should_alert=1
  fi

  if [[ "$should_alert" != "1" ]]; then
    exit 0
  fi
fi

LOGS="$(journalctl -u "$UNIT_NAME" -n 25 --no-pager 2>/dev/null | tail -n 20)"

BODY="Resumo: $SUMMARY
Serviço: $DISPLAY_NAME
Host: $(hostname)
ActiveState: ${ACTIVE_STATE:-desconhecido}
SubState: ${SUB_STATE:-desconhecido}
Result: ${RESULT_STATE:-desconhecido}
ExecMainCode: ${EXEC_MAIN_CODE:-desconhecido}
ExecMainStatus: ${EXEC_MAIN_STATUS:-desconhecido}
Últimas linhas:
$LOGS
Hora: $(date '+%d/%m/%Y %H:%M:%S')"

/home/ubuntu/bot/alert.sh "$ALERT_TYPE" "$ALERT_TITLE" "$BODY"
