# Avisos nos servidores TTS: uma única chamada curta por fase, sem bloquear deploy.
voice_notices_start() {
  (( ${VOICE_NOTICES_STARTED:-0} == 0 )) || return 0
  VOICE_NOTICES_STARTED=1
  local helper="$UPDATER_SOURCE_DIR/../utilitarios/avisos_servidores.py"
  # A cópia executada mora no diretório temporário; usar origem do repo para
  # encontrar o helper, pois o snapshot do updater só contém updater/core/*.
  helper="$REPO_DIR/updater/utilitarios/avisos_servidores.py"
  if [[ -f "$helper" ]]; then
    REPO_DIR="$REPO_DIR" BOT_HEALTH_URL="$BOT_HEALTH_URL" \
      python3 "$helper" start "$UPDATE_RUNTIME_RUN_ID" || true
  fi
}

voice_notices_finish() {
  (( ${VOICE_NOTICES_STARTED:-0} == 1 )) || return 0
  (( ${VOICE_NOTICES_FINISHED:-0} == 0 )) || return 0
  VOICE_NOTICES_FINISHED=1
  local helper="$REPO_DIR/updater/utilitarios/avisos_servidores.py"
  if [[ -f "$helper" ]]; then
    REPO_DIR="$REPO_DIR" BOT_HEALTH_URL="$BOT_HEALTH_URL" \
      python3 "$helper" "${1:-failure}" "$UPDATE_RUNTIME_RUN_ID" || true
  fi
}
