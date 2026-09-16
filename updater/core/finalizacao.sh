# Releases de rollback podem conter node_modules completos. Mantenha apenas
# algumas versões recentes depois que a nova versão já está comprometida.
prune_runtime_releases || true

DURATION="$(human_duration "$SECONDS")"
TOTAL_DURATION="$DURATION"
TOTAL_FROM_RECEIVE_DURATION=""
RECEIVE_TO_UPDATER_DURATION=""
if (( ZIP_PROGRESS_RECEIVED_AT_MS > 0 )); then
  FINAL_NOW_MS="$(update_now_ms)"
  if [[ "$FINAL_NOW_MS" =~ ^[0-9]+$ ]] && (( FINAL_NOW_MS >= ZIP_PROGRESS_RECEIVED_AT_MS )); then
    TOTAL_DURATION="$(format_update_duration_ms "$((FINAL_NOW_MS - ZIP_PROGRESS_RECEIVED_AT_MS))")"
    TOTAL_FROM_RECEIVE_DURATION="$TOTAL_DURATION"
  fi
  if (( ZIP_PROGRESS_UPDATER_DELAY_MS >= 0 )); then
    RECEIVE_TO_UPDATER_DURATION="$(format_update_duration_ms "$ZIP_PROGRESS_UPDATER_DELAY_MS")"
  fi
fi
ROLLBACK_STATUS="não foi necessário"
CHANGED_FILES="$(format_changed_files)"
DIFF_TOTAL_SUMMARY="$(format_diff_total_summary)"
CHANGED_FILES_COUNT="$(printf '%s\n' "$CHANGED_FILES_RAW" | awk 'NF {c++} END {print c+0}')"
# O resumo por phone-worker é caro e só deve ser chamado por fluxos de erro/anexo.
# No update saudável, o resumo público compacto não precisa dessa análise.

# Atualiza o resumo do health no fim, mesmo que o bot não tenha reiniciado.
refresh_bot_health_status >/dev/null 2>&1 || true
normalize_final_health_warning_state

OVERALL_FATAL=0
if [[ "$BOT_HEALTHCHECK_STATUS" == falhou:* ]]; then
  OVERALL_FATAL=1
fi
if (( FRONT_CHANGED == 1 || BACK_CHANGED == 1 )); then
  [[ "${ACTIVITY_HEALTHCHECK_STATUS:-}" == "OK" ]] || OVERALL_FATAL=1
fi
recompute_update_warning_flag

# Trava final: a mensagem pública nunca deve ficar amarela quando a própria
# seção de avisos/saúde não tem aviso real. Detalhes informativos como
# "timer ativo", "unit instalada" ou "0 com falha" são sucesso, não warning.
if (( UPDATE_HAS_WARNINGS == 1 )); then
  if ! has_real_warning_text "$BOT_WARNINGS_STATUS" \
    && ! cogs_have_failures "$BOT_COGS_STATUS" \
    && [[ "$BOT_HEALTHCHECK_STATUS" != falhou:* ]] \
    && [[ "$BOT_HEALTHCHECK_STATUS" != *"sem resposta"* ]] \
    && [[ "$BOT_HEALTHCHECK_STATUS" != "OK com avisos" ]]; then
    logger -t "$LOG_TAG" "auto-update: warning público normalizado para sucesso; sem warning exibível"
    UPDATE_HAS_WARNINGS=0
  fi
fi

if (( OVERALL_FATAL == 0 && UPDATE_HAS_WARNINGS == 0 )); then
  ALERT_TYPE="success"
  ALERT_TITLE="✅ Atualização concluída"
  ALERT_SUMMARY="Atualização aplicada e validada."
elif (( OVERALL_FATAL == 0 )); then
  ALERT_TYPE="warn"
  ALERT_TITLE="⚠️ Atualização concluída com avisos"
  ALERT_SUMMARY="A atualização foi aplicada, mas há avisos que precisam de revisão."
else
  ALERT_TYPE="warn"
  ALERT_TITLE="⚠️ Atualização concluída com alerta"
  ALERT_SUMMARY="A atualização terminou com um alerta. Verifique os pontos abaixo."
fi

APPLY_MODE="reinício completo"
if [[ "$FAST_RELOAD_STATUS" == OK* ]]; then
  APPLY_MODE="recarga controlada de cog"
elif (( BOT_CHANGED == 0 )); then
  APPLY_MODE="sem reinício do bot"
fi

PUBLIC_WARNINGS=""
if (( UPDATE_HAS_WARNINGS == 1 )); then
  if has_real_warning_text "$BOT_WARNINGS_STATUS"; then
    PUBLIC_WARNINGS+="${BOT_WARNINGS_STATUS}"
  fi
  if cogs_have_failures "$BOT_COGS_STATUS"; then
    [[ -z "$PUBLIC_WARNINGS" ]] || PUBLIC_WARNINGS+="; "
    PUBLIC_WARNINGS+="$BOT_COGS_STATUS"
  fi
  if has_real_warning_text "$PREFLIGHT_COG_IMPORT_STATUS"; then
    [[ -z "$PUBLIC_WARNINGS" ]] || PUBLIC_WARNINGS+="; "
    PUBLIC_WARNINGS+="$PREFLIGHT_COG_IMPORT_STATUS"
  fi
  if [[ "$BOT_HEALTHCHECK_STATUS" == *"sem resposta"* || "$BOT_HEALTHCHECK_STATUS" == falhou:* ]]; then
    [[ -z "$PUBLIC_WARNINGS" ]] || PUBLIC_WARNINGS+="; "
    PUBLIC_WARNINGS+="$BOT_HEALTHCHECK_STATUS"
  fi
fi
[[ -n "${PUBLIC_WARNINGS//[[:space:]]/}" ]] || PUBLIC_WARNINGS="sem avisos"

read_app_command_sync_status
CHANGED_PROCESSES="$(format_changed_processes)"
UPDATE_DISPLAY_ID="${LOCAL_CANDIDATE_DISPLAY_ID:-}"
if [[ -z "${UPDATE_DISPLAY_ID//[[:space:]]/}" ]]; then
  if (( ROLLBACK_CONTROL_MODE == 1 )); then
    UPDATE_DISPLAY_ID="REV-$(short_commit "${REMOTE_COMMIT:-$ROLLBACK_NEW_COMMIT}")"
  else
    UPDATE_DISPLAY_ID="UPD-$(short_commit "${REMOTE_COMMIT:-$CURRENT_COMMIT}" | tr '[:lower:]' '[:upper:]')"
  fi
fi
RUNTIME_CHECK_MARK="✓"
if [[ "$PREFLIGHT_RUNTIME_STATUS" == adiado:* || "$PREFLIGHT_RUNTIME_STATUS" == falhou:* || "$PREFLIGHT_RUNTIME_STATUS" == "não verificado" ]]; then
  RUNTIME_CHECK_MARK="•"
fi
CHECKS_TEXT="✓ Bot — ${BOT_HEALTHCHECK_STATUS}
✓ Python — ${PREFLIGHT_PY_STATUS}
✓ Bash — ${PREFLIGHT_BASH_STATUS}
✓ Cogs — ${PREFLIGHT_COG_IMPORT_STATUS}
${RUNTIME_CHECK_MARK} Runtime candidato — ${PREFLIGHT_RUNTIME_STATUS}
✓ Comandos — ${APP_COMMAND_SYNC_SUMMARY}"
if [[ -n "$RECEIVE_TO_UPDATER_DURATION" ]]; then
  TIMINGS_TEXT="receive_to_updater=${RECEIVE_TO_UPDATER_DURATION}, ${UPDATER_TIMINGS:-sem etapas}, execution=${DURATION}, total=${TOTAL_DURATION}"
else
  TIMINGS_TEXT="${UPDATER_TIMINGS:-sem etapas}, execution=${DURATION}, total=${TOTAL_DURATION}"
fi
CACHE_TEXT="Node ${NODE_DEP_CACHE_HITS:-0} hit/${NODE_DEP_CACHE_MISSES:-0} miss · TypeScript ${TYPESCRIPT_CACHE_HITS:-0} hit/${TYPESCRIPT_CACHE_MISSES:-0} miss · Python ${PYTHON_INSTALLER_STATUS:-não usado}"
if (( ${READY_FAST_PATH_USED:-0} == 1 )); then
  CACHE_TEXT+=" · READY hit"
fi
TEST_PLAN_TEXT="frontend: ${FRONT_TEST_PLAN_STATUS:-não executado} · backend: ${BACK_TEST_PLAN_STATUS:-não executado}"
DURATION_DISPLAY="$DURATION"
if [[ -n "$TOTAL_FROM_RECEIVE_DURATION" ]]; then
  DURATION_DISPLAY="$TOTAL_FROM_RECEIVE_DURATION desde o envio · $DURATION de execução"
fi
BODY="Resumo: $ALERT_SUMMARY
Identificador: $UPDATE_DISPLAY_ID
Branch: $BRANCH
Commit: ${SHORT_FROM} → ${SHORT_TO}
Update: $(format_update_file_count "$CHANGED_FILES_COUNT") · $DIFF_TOTAL_SUMMARY
Aplicação: $APPLY_MODE
Processos alterados: $CHANGED_PROCESSES
Duração: $DURATION_DISPLAY
Verificações:
$CHECKS_TEXT
Tempos: $TIMINGS_TEXT
Cache: $CACHE_TEXT
Testes: $TEST_PLAN_TEXT
Arquivos:
$CHANGED_FILES"

if (( UPDATE_HAS_WARNINGS == 1 || OVERALL_FATAL == 1 )); then
  BODY+=$'\n'"Bot: $BOT_HEALTHCHECK_STATUS"
  BODY+=$'\n'"Cogs: $BOT_COGS_STATUS"
  BODY+=$'\n'"Health: $BOT_HEALTH_DETAIL_STATUS"
fi
if (( UPDATE_HAS_WARNINGS == 1 )); then
  BODY+=$'\n'"Avisos: $PUBLIC_WARNINGS"
fi
if [[ -n "${APP_COMMAND_SYNC_WEBHOOK_BLOCK//[[:space:]]/}" ]]; then
  BODY+=$'\n'"$APP_COMMAND_SYNC_WEBHOOK_BLOCK"
fi
BODY+=$'\n'"Hora: $(date '+%d/%m/%Y %H:%M:%S')"
logger -t "$LOG_TAG" "timings: ${UPDATER_TIMINGS:-sem etapas}; execution=$DURATION; total=$TOTAL_DURATION; receive_to_updater=${RECEIVE_TO_UPDATER_DURATION:-n/a}"
if (( LOCAL_CANDIDATE_MODE == 1 || ROLLBACK_CONTROL_MODE == 1 || REMOTE_CANDIDATE_MODE == 1 )); then
  zip_progress_publish "Finalizando..."
fi
DELIVERY_PHASE=1

build_final_status_description \
  "$ALERT_SUMMARY" \
  "$UPDATE_DISPLAY_ID" \
  "$SHORT_FROM" \
  "$SHORT_TO" \
  "$CHANGED_FILES_COUNT" \
  "$DIFF_TOTAL_SUMMARY" \
  "$APPLY_MODE" \
  "$TOTAL_DURATION" \
  "$BOT_HEALTHCHECK_STATUS"
if (( UPDATE_HAS_WARNINGS == 1 || OVERALL_FATAL == 1 )); then
  ZIP_STATUS_DESCRIPTION+=$'\n\nDetalhes enviados no canal de logs.'
fi
ZIP_STATUS_CONTROL_JSON=""
if (( (LOCAL_CANDIDATE_MODE == 1 || REMOTE_CANDIDATE_MODE == 1) && OVERALL_FATAL == 0 )); then
  source_author_id=""
  if (( LOCAL_CANDIDATE_MODE == 1 )) && [[ -f "$LOCAL_CANDIDATE_DIR/manifest.json" ]]; then
    source_author_id="$(json_field_from_file "$LOCAL_CANDIDATE_DIR/manifest.json" discord_status.source_author_id 2>/dev/null || true)"
  fi
  ZIP_STATUS_CONTROL_JSON="$(python3 - "$BRANCH" "$REMOTE_COMMIT" "$PREVIOUS_COMMIT" "$source_author_id" "$UPDATE_DISPLAY_ID" <<'PYCTRL'
import hashlib, json, sys
branch, head, previous, author, display_id = sys.argv[1:6]
print(json.dumps({
    "enabled": True,
    "mode": "rollback",
    "token": hashlib.sha256(f"{display_id}:{head}".encode()).hexdigest()[:16],
    "branch": branch or "main",
    "expected_head": head,
    "revert_commit": head,
    "head_commit": head,
    "update_from": previous,
    "update_to": head,
    "source_author_id": author,
}, ensure_ascii=False))
PYCTRL
)"
fi
FINAL_FILE_COUNT_TEXT="$(format_update_file_count "$CHANGED_FILES_COUNT")"
ZIP_STATUS_UI_JSON="$(
  UI_STATUS="$ALERT_TYPE" \
  UI_SUMMARY="$ALERT_SUMMARY" \
  UI_DISPLAY_ID="$UPDATE_DISPLAY_ID" \
  UI_BRANCH="$BRANCH" \
  UI_FROM="$SHORT_FROM" \
  UI_TO="$SHORT_TO" \
  UI_FILE_COUNT="$FINAL_FILE_COUNT_TEXT" \
  UI_DIFF="$DIFF_TOTAL_SUMMARY" \
  UI_IMPACT="$APPLY_MODE" \
  UI_DURATION="$DURATION" \
  UI_TOTAL_DURATION="$TOTAL_FROM_RECEIVE_DURATION" \
  UI_HEALTH="$BOT_HEALTHCHECK_STATUS" \
  UI_CHECKS="$CHECKS_TEXT" \
  UI_TIMINGS="$TIMINGS_TEXT" \
  UI_CACHE="$CACHE_TEXT" \
  UI_TESTS="$TEST_PLAN_TEXT" \
  UI_FILES="$CHANGED_FILES" \
  UI_PROCESSES="$CHANGED_PROCESSES" \
  python3 - <<'PYFINALUI'
import json, os
print(json.dumps({
    "kind": "final",
    "status": os.environ.get("UI_STATUS") or "success",
    "summary": os.environ.get("UI_SUMMARY") or "",
    "display_id": os.environ.get("UI_DISPLAY_ID") or "",
    "branch": os.environ.get("UI_BRANCH") or "main",
    "from": os.environ.get("UI_FROM") or "",
    "to": os.environ.get("UI_TO") or "",
    "file_count_text": os.environ.get("UI_FILE_COUNT") or "arquivos",
    "diff_summary": os.environ.get("UI_DIFF") or "",
    "impact": os.environ.get("UI_IMPACT") or "",
    "duration": os.environ.get("UI_DURATION") or "",
    "total_duration": os.environ.get("UI_TOTAL_DURATION") or "",
    "bot_health": os.environ.get("UI_HEALTH") or "",
    "github_synced": True,
    "checks_text": os.environ.get("UI_CHECKS") or "",
    "timings_text": os.environ.get("UI_TIMINGS") or "",
    "cache_text": os.environ.get("UI_CACHE") or "",
    "tests_text": os.environ.get("UI_TESTS") or "",
    "files_text": os.environ.get("UI_FILES") or "",
    "processes": os.environ.get("UI_PROCESSES") or "",
}, ensure_ascii=False))
PYFINALUI
)"
if (( LOCAL_CANDIDATE_MODE == 1 )); then
  write_local_candidate_state "finalizing_delivery" "$REMOTE_COMMIT"
fi
FINAL_STATUS_DELIVERY_RC=0
notify_zip_status_message "$ALERT_TYPE" "$ALERT_TITLE" "$ZIP_STATUS_DESCRIPTION" || FINAL_STATUS_DELIVERY_RC=$?
ZIP_STATUS_UI_JSON=""
flush_update_status_outbox || true

FINAL_ALERT_EVENT_ID="${UPDATE_DISPLAY_ID:-update}-final-${SHORT_TO:-unknown}"
FINAL_ALERT_DELIVERY_RC=0
FINAL_RAW_LOG=""
if [[ -f "${RUN_LOG_FILE:-}" && -s "${RUN_LOG_FILE:-}" ]]; then
  FINAL_RAW_LOG="$RUN_LOG_FILE"
fi
send_alert_reliably "$ALERT_TYPE" "$ALERT_TITLE" "$BODY" "$FINAL_RAW_LOG" "bot-updater.log" "$FINAL_ALERT_EVENT_ID" || FINAL_ALERT_DELIVERY_RC=$?
flush_update_alert_outbox || true
if (( LOCAL_CANDIDATE_MODE == 1 )); then
  # "delivery_scheduled" significa que cada saída foi entregue ou persistida
  # em outbox. Se nem a entrega nem a persistência funcionarem, o candidato é
  # arquivado como degradado para o reconciliador do bot reconstruir a saída.
  if (( FINAL_STATUS_DELIVERY_RC == 0 && FINAL_ALERT_DELIVERY_RC == 0 )); then
    write_local_candidate_state "delivery_scheduled" "$REMOTE_COMMIT"
  else
    write_local_candidate_state "delivery_degraded" "$REMOTE_COMMIT"
    logger -t "$LOG_TAG" "entrega final degradada: status_rc=$FINAL_STATUS_DELIVERY_RC alert_rc=$FINAL_ALERT_DELIVERY_RC" 2>/dev/null || true
  fi
  archive_local_candidate "done"
  trigger_updater_if_queue_pending
fi
logger -t "$LOG_TAG" "$ALERT_TITLE"
