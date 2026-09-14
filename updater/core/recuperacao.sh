# Recuperação transacional, rollback e tratamento de falhas.
# Extraídos do orquestrador sem alteração de comportamento.

rollback_after_failure() {
  local exit_code="${1:-1}"
  local failed_command="${2:-desconhecido}"

  # O contexto primário já foi capturado por on_error() e persistido antes de
  # qualquer rollback. Não recalcule LAST_ERROR_* aqui: isso poderia trocar a
  # causa original por uma mensagem produzida durante a própria restauração.

  # Preserve o diagnóstico da falha ORIGINAL antes de iniciar qualquer ação de
  # rollback. A restauração por release e os healthchecks podem falhar também e
  # atualizar o contexto de erro; o card final deve continuar apontando para a
  # causa que derrubou o candidato, nunca para uma falha secundária do rollback.
  local original_error_code="$LAST_ERROR_CODE"
  local original_error_stderr="$LAST_ERROR_STDERR"
  local original_error_logs="$LAST_ERROR_LOGS"
  local original_error_service_unit="$LAST_ERROR_SERVICE_UNIT"

  local rollback_bot_status="não executado"
  local rollback_front_status="não executado"
  local rollback_back_status="não executado"
  local rollback_python_status="não executado"
  local rollback_activity_status="não executado"
  local rollback_git_status="não executado"
  local rollback_success=1
  local reset_status=1
  local head_after_reset=""
  local rollback_log_file="" rollback_log_offset=0

  rollback_log_file="$(stage_log_file_for "rollback" 2>/dev/null || true)"
  if [[ -f "$RUN_LOG_FILE" ]]; then
    rollback_log_offset="$(stat -c '%s' "$RUN_LOG_FILE" 2>/dev/null || echo 0)"
  fi

  trap - ERR
  set +e

  if (( ROLLBACK_DONE == 1 )); then
    exit "$exit_code"
  fi
  ROLLBACK_DONE=1
  ROLLBACK_IN_PROGRESS=1
  ZIP_RECOVERY_STARTED_MS="$(update_now_ms)"
  zip_recovery_publish "Restaurando código" "Falha original: ${original_error_code:-UPDATE_STAGE_FAILED}" 0

  if (( LOCAL_CANDIDATE_MODE == 1 )); then
    logger -t "$LOG_TAG" "Erro fatal no candidato local. Tentando rollback para $(short_commit "$PREVIOUS_COMMIT") antes de push GitHub"
  elif (( REMOTE_CANDIDATE_MODE == 1 )); then
    logger -t "$LOG_TAG" "Erro fatal no commit do GitHub. Tentando rollback de $(short_commit "$REMOTE_COMMIT") para $(short_commit "$PREVIOUS_COMMIT")"
  else
    logger -t "$LOG_TAG" "Erro fatal após update. Tentando rollback de $(short_commit "$REMOTE_COMMIT") para $(short_commit "$PREVIOUS_COMMIT")"
  fi

  STAGE="rollback git"
  repo_git reset --hard "$PREVIOUS_COMMIT" >/dev/null 2>&1
  reset_status=$?
  head_after_reset="$(repo_git rev-parse HEAD 2>/dev/null || true)"

  if (( reset_status == 0 )) && [[ -n "$head_after_reset" && "$head_after_reset" == "$PREVIOUS_COMMIT" ]]; then
    cleanup_local_candidate_new_files_after_reset
    rollback_git_status="OK: repositório voltou para $(short_commit "$PREVIOUS_COMMIT")"
    if (( LOCAL_CANDIDATE_MODE == 1 )); then
      update_local_candidate_heartbeat "failed" "rollback após falha em $FAILED_STAGE" || true
      ROLLBACK_STATUS="aplicado para $(short_commit "$PREVIOUS_COMMIT"); GitHub não foi alterado"
    elif (( REMOTE_CANDIDATE_MODE == 1 )); then
      mark_remote_commit_rejected "$REMOTE_COMMIT" "health falhou após aplicar; rollback para $(short_commit "$PREVIOUS_COMMIT")"
      ROLLBACK_STATUS="aplicado para $(short_commit "$PREVIOUS_COMMIT"); commit GitHub rejeitado"
    else
      write_dirty_marker "$REMOTE_COMMIT" "$PREVIOUS_COMMIT" "$FAILED_STAGE" "$failed_command"
      ROLLBACK_STATUS="aplicado para $(short_commit "$PREVIOUS_COMMIT"); commit remoto marcado como sujo"
    fi
  else
    rollback_success=0
    rollback_git_status="falhou: reset=$reset_status head=$(short_commit "$head_after_reset") esperado=$(short_commit "$PREVIOUS_COMMIT")"
    ROLLBACK_STATUS="falhou antes de restaurar o commit anterior"
  fi

  if (( rollback_success == 1 )); then
    zip_recovery_publish "Restaurando runtimes" "Código anterior restaurado" 1
    if (( REQUIREMENTS_CHANGED == 1 )); then
      if (( PYTHON_RUNTIME_MUTATED == 1 )); then
        STAGE="rollback runtime Python por release"
        if restore_python_runtime_release "$PREVIOUS_COMMIT"; then
          rollback_python_status="runtime Python anterior reativado sem reinstalação"
        else
          rollback_success=0
          rollback_python_status="falhou ao reativar runtime Python anterior"
        fi
      else
        rollback_python_status="runtime Python novo não chegou a ser ativado"
      fi
    else
      rollback_python_status="não precisou restaurar"
    fi

    if (( FRONT_CHANGED == 1 )); then
      if (( FRONT_RUNTIME_MUTATED == 1 )); then
        STAGE="rollback frontend por release"
        if restore_frontend_runtime_release "$PREVIOUS_COMMIT"; then
          rollback_front_status="${FRONT_STATUS:-}"
        else
          rollback_success=0
          rollback_front_status="falhou: $FRONT_STATUS"
        fi
      else
        rollback_front_status="runtime frontend não chegou a ser alterado; nenhum rebuild necessário"
      fi
    else
      rollback_front_status="não precisou restaurar"
    fi

    if (( BACK_CHANGED == 1 )); then
      if (( BACK_RUNTIME_MUTATED == 1 )); then
        STAGE="rollback backend por release"
        if restore_backend_runtime_release "$PREVIOUS_COMMIT"; then
          rollback_back_status="${BACK_STATUS:-}"
          rollback_activity_status="${ACTIVITY_HEALTHCHECK_STATUS:-}"
        else
          rollback_success=0
          rollback_back_status="falhou: $BACK_STATUS"
          rollback_activity_status="${ACTIVITY_HEALTHCHECK_STATUS:-}"
        fi
      else
        rollback_back_status="runtime backend não chegou a ser alterado; nenhum rebuild necessário"
        if { if declare -F wait_for_health_adaptive >/dev/null 2>&1; then wait_for_health_adaptive "$BACK_HEALTH_URL" 6 1; else wait_for_health "$BACK_HEALTH_URL" 2 2; fi; }; then
          rollback_activity_status="OK"
        else
          rollback_activity_status="não verificada/indisponível"
        fi
      fi
    else
      if { if declare -F wait_for_health_adaptive >/dev/null 2>&1; then wait_for_health_adaptive "$BACK_HEALTH_URL" 6 1; else wait_for_health "$BACK_HEALTH_URL" 2 2; fi; }; then
        rollback_activity_status="OK"
      else
        rollback_activity_status="não verificada/indisponível"
      fi
      rollback_back_status="não precisou reiniciar"
    fi

    zip_recovery_publish "Verificando versão anterior" "Confirmando serviços e estabilidade do bot" 2
    if deploy_bot; then
      rollback_bot_status="$BOT_HEALTHCHECK_STATUS"
    else
      rollback_success=0
      rollback_bot_status="falhou: $BOT_HEALTHCHECK_STATUS"
    fi
  else
    rollback_front_status="não executado porque o git reset falhou"
    rollback_back_status="não executado porque o git reset falhou"
    rollback_python_status="não executado porque o git reset falhou"
    rollback_activity_status="não executado porque o git reset falhou"
    rollback_bot_status="não executado porque o git reset falhou"
  fi

  if [[ -n "$rollback_log_file" && -f "$RUN_LOG_FILE" ]]; then
    tail -c +$((rollback_log_offset + 1)) "$RUN_LOG_FILE" > "$rollback_log_file" 2>/dev/null || true
    chmod 0644 "$rollback_log_file" 2>/dev/null || true
  fi
  if (( rollback_success == 0 )); then
    persist_rollback_failure \
      "$rollback_git_status" \
      "$rollback_front_status" \
      "$rollback_back_status" \
      "$rollback_bot_status" \
      "$rollback_activity_status" \
      "$rollback_python_status" \
      "$rollback_log_file" || true
  fi

  # O diagnóstico reportado deve continuar sendo o da falha que acionou o
  # rollback, não de uma eventual falha secundária ao restaurar/publicar.
  LAST_ERROR_CODE="$original_error_code"
  LAST_ERROR_STDERR="$original_error_stderr"
  LAST_ERROR_LOGS="$original_error_logs"
  LAST_ERROR_SERVICE_UNIT="$original_error_service_unit"

  local duration title summary commit_dirty
  duration="$(human_duration "$SECONDS")"
  if (( rollback_success == 1 )); then
    if (( LOCAL_CANDIDATE_MODE == 1 )); then
      title="Update revertido"
      summary="O ZIP foi testado na VPS, falhou na validação e o bot voltou ao estado anterior. Nenhum commit foi enviado ao GitHub."
      commit_dirty="não; GitHub não foi alterado"
    elif (( REMOTE_CANDIDATE_MODE == 1 )); then
      title="Update do GitHub revertido"
      summary="O commit do GitHub falhou depois da aplicação. A VPS voltou ao último estado saudável e esse commit foi rejeitado."
      commit_dirty="sim; commit GitHub rejeitado"
    else
      title="Rollback aplicado após erro fatal"
      summary="O update falhou, mas o rollback voltou o repositório para o commit anterior e os serviços foram validados."
      commit_dirty="sim"
    fi
  else
    title="Rollback falhou após erro fatal"
    summary="O update falhou e o rollback não conseguiu restaurar completamente o estado anterior. Verificação manual necessária."
    commit_dirty="não confirmado"
  fi

  local body
  body="Resumo: $summary
Host: $HOSTNAME
Branch: $BRANCH
Commit: $(short_commit "$PREVIOUS_COMMIT") ← $(short_commit "$REMOTE_COMMIT")
Mudança: ${COMMIT_SUBJECT:-sem mensagem}
Etapa: ${FAILED_STAGE:-$STAGE}
Código de falha: ${LAST_ERROR_CODE:-UPDATE_STAGE_FAILED}
Comando: $failed_command
Código: $exit_code
Evidência primária: ${UPDATE_FAILURE_FILE:-não persistida}
Validações:
• Git reset: $rollback_git_status
• Bot: $rollback_bot_status
• Cogs: $BOT_COGS_STATUS
• Health: $BOT_HEALTH_DETAIL_STATUS
Serviços:
• Runtime Python: $rollback_python_status
• Frontend: $rollback_front_status
• Backend: $rollback_back_status
• Painel web: $rollback_activity_status
Rollback: $ROLLBACK_STATUS
Commit sujo: $commit_dirty
Ação sugerida: Se o rollback falhou, verifique o serviço manualmente antes de aplicar outro update. Se foi aplicado, faça um novo commit corrigido para liberar o updater novamente.
Stderr:
${LAST_ERROR_STDERR:-nenhuma saída adicional capturada}
Últimas linhas:
${LAST_ERROR_LOGS:-nenhum log adicional encontrado}
Duração: $duration
Hora: $(date '+%d/%m/%Y %H:%M:%S')"

  local recovery_duration rollback_bool final_headline final_summary
  if (( ZIP_RECOVERY_STARTED_MS > 0 )); then
    recovery_duration="$(format_update_duration_ms $(( $(update_now_ms) - ZIP_RECOVERY_STARTED_MS )))"
  else
    recovery_duration=""
  fi
  if (( rollback_success == 1 )); then
    rollback_bool="true"
    final_headline="Atualização não aplicada"
    final_summary="A versão anterior foi restaurada e validada."
  else
    rollback_bool="false"
    final_headline="Recuperação necessária"
    final_summary="O rollback não conseguiu restaurar completamente o estado anterior."
  fi
  if (( LOCAL_CANDIDATE_MODE == 1 )); then
    write_local_candidate_recovery_state \
      "$rollback_bool" "$head_after_reset" "$REMOTE_COMMIT" \
      "${original_error_code:-UPDATE_STAGE_FAILED}" "${FAILED_STAGE:-$STAGE}" \
      "$recovery_duration" "$rollback_bot_status"
  fi

  ZIP_STATUS_UI_JSON="$(
    UI_STATUS=error UI_HEADLINE="$final_headline" UI_SUMMARY="$final_summary" \
    UI_DISPLAY_ID="${LOCAL_CANDIDATE_DISPLAY_ID:-${UPDATE_DISPLAY_ID:-}}" UI_BRANCH="${BRANCH:-main}" \
    UI_FROM="$(short_commit "$PREVIOUS_COMMIT")" UI_TO="$(short_commit "$REMOTE_COMMIT")" \
    UI_FILE_COUNT="$(format_update_file_count "${CHANGED_FILES_COUNT:-0}")" UI_DIFF="${DIFF_TOTAL_SUMMARY:-}" \
    UI_DURATION="$duration" UI_RECOVERY_DURATION="$recovery_duration" UI_HEALTH="$rollback_bot_status" \
    UI_FAILURE_CODE="${original_error_code:-UPDATE_STAGE_FAILED}" UI_ROLLBACK_OK="$rollback_bool" \
    UI_CHECKS="${CHECKS_TEXT:-}" UI_TIMINGS="${TIMINGS_TEXT:-}" UI_CACHE="${CACHE_TEXT:-}" \
    UI_TESTS="${TEST_PLAN_TEXT:-}" UI_FILES="${CHANGED_FILES:-}" UI_PROCESSES="${CHANGED_PROCESSES:-}" \
    python3 - <<'PYROLLBACKFINAL'
import json, os
print(json.dumps({
    "kind": "final",
    "status": "error",
    "headline": os.environ.get("UI_HEADLINE") or "Atualização não aplicada",
    "summary": os.environ.get("UI_SUMMARY") or "",
    "display_id": os.environ.get("UI_DISPLAY_ID") or "",
    "branch": os.environ.get("UI_BRANCH") or "main",
    "from": os.environ.get("UI_FROM") or "",
    "to": os.environ.get("UI_TO") or "",
    "file_count_text": os.environ.get("UI_FILE_COUNT") or "0 arquivos",
    "diff_summary": os.environ.get("UI_DIFF") or "",
    "impact": "",
    "duration": os.environ.get("UI_DURATION") or "",
    "recovery_duration": os.environ.get("UI_RECOVERY_DURATION") or "",
    "bot_health": os.environ.get("UI_HEALTH") or "",
    "github_synced": False,
    "failure_code": os.environ.get("UI_FAILURE_CODE") or "UPDATE_STAGE_FAILED",
    "rollback_ok": (os.environ.get("UI_ROLLBACK_OK") or "false").lower() == "true",
    "checks_text": os.environ.get("UI_CHECKS") or "",
    "timings_text": os.environ.get("UI_TIMINGS") or "",
    "cache_text": os.environ.get("UI_CACHE") or "",
    "tests_text": os.environ.get("UI_TESTS") or "",
    "files_text": os.environ.get("UI_FILES") or "",
    "processes": os.environ.get("UI_PROCESSES") or "",
}, ensure_ascii=False))
PYROLLBACKFINAL
  )"
  notify_zip_status_message "error" "$title" "$summary" || true
  ZIP_STATUS_UI_JSON=""
  send_error "$title" "$body"
  if (( LOCAL_CANDIDATE_MODE == 1 )); then
    # rollback_after_failure termina o trap com exit; sem arquivar aqui o item
    # ficaria em queue/active e poderia ser retomado como se ainda estivesse em
    # aplicação. O histórico de recovery foi salvo acima antes de mover o diretório.
    archive_local_candidate "failed" || true
  fi
  exit "$exit_code"
}

handle_post_deploy_failure() {
  local exit_code="${1:-1}"
  local failed_command="${2:-desconhecido}"
  local failed_line="${3:-?}"
  local failed_function="${4:-main}"
  local display_id="${LOCAL_CANDIDATE_DISPLAY_ID:-}"
  local current_head="${REMOTE_COMMIT:-${CURRENT_COMMIT:-}}"
  local previous_head="${PREVIOUS_COMMIT:-${CURRENT_COMMIT:-}}"
  local safe_description="" body="" event_id=""

  trap - ERR
  set +e
  DELIVERY_PHASE=1
  [[ -n "${display_id//[[:space:]]/}" ]] || display_id="UPD-$(short_commit "$current_head" | tr '[:lower:]' '[:upper:]')"
  printf -v safe_description '%s\n\nIdentificador: %s\nCommit: %s → %s\n\nA confirmação detalhada será reenviada automaticamente.' \
    'A atualização foi aplicada e validada. Uma falha ocorreu somente na etapa de notificação; o código não foi revertido.' \
    "$display_id" "$(short_commit "$previous_head")" "$(short_commit "$current_head")"

  logger -t "$LOG_TAG" "falha pós-deploy ignorada para rollback: etapa=$STAGE função=$failed_function linha=$failed_line comando=$failed_command rc=$exit_code" 2>/dev/null || true

  if (( LOCAL_CANDIDATE_MODE == 1 )); then
    write_local_candidate_state "delivery_degraded" "$current_head" || true
    notify_zip_status_message "success" "✅ Atualização concluída" "$safe_description" || true
  elif (( ROLLBACK_CONTROL_MODE == 1 )); then
    post_direct_update_message "$ROLLBACK_MESSAGE_CHANNEL_ID" "$ROLLBACK_MESSAGE_ID" "success" "✅ Alteração concluída" "$safe_description" "" || true
  else
    notify_zip_status_message "success" "✅ Atualização concluída" "$safe_description" || true
  fi

  body="Resumo: A atualização foi aplicada e permaneceu ativa; somente a finalização visual/log falhou.
Identificador: $display_id
Branch: $BRANCH
Commit: $(short_commit "$previous_head") → $(short_commit "$current_head")
Etapa: $STAGE
Função: $failed_function
Linha: $failed_line
Comando: $failed_command
Código: $exit_code
Rollback: bloqueado porque o deploy já havia sido validado/publicado
Stderr:
${LAST_ERROR_STDERR:-nenhuma saída adicional capturada}
Hora: $(date '+%d/%m/%Y %H:%M:%S')"
  event_id="${display_id}-delivery-degraded-$(short_commit "$current_head")"
  send_alert_reliably "warn" "⚠️ Confirmação final pendente" "$body" "" "" "$event_id" || true
  flush_update_status_outbox || true
  flush_update_alert_outbox || true

  if (( LOCAL_CANDIDATE_MODE == 1 )); then
    archive_local_candidate "done" || true
    trigger_updater_if_queue_pending || true
  elif (( ROLLBACK_CONTROL_MODE == 1 )); then
    archive_rollback_request "done" || true
  fi

  logger -t "$LOG_TAG" "deploy preservado apesar de falha pós-deploy: $display_id" 2>/dev/null || true
  exit 0
}

on_error() {
  local exit_code="$?"
  set_updater_priority_profile safe || true
  local failed_line="${1:-${BASH_LINENO[0]:-?}}"
  local failed_function="${2:-${FUNCNAME[1]:-main}}"
  if (( MANUAL_FAILURE_ALERT_SENT == 1 )); then
    exit "$exit_code"
  fi
  local failed_command="${BASH_COMMAND:-desconhecido}"
  FAILED_STAGE="$STAGE"
  LAST_ERROR_LINE="$failed_line"
  LAST_ERROR_FUNCTION="$failed_function"
  register_error_context "$exit_code" "$failed_command"
  persist_primary_failure "$failed_line" "$failed_function" || true

  # Depois do limite transacional, qualquer falha restante pertence apenas à
  # entrega da confirmação. Nunca faça git reset, restart ou marque o commit
  # remoto como rejeitado nessa fase.
  if (( DEPLOYMENT_COMMITTED == 1 )); then
    handle_post_deploy_failure "$exit_code" "$failed_command" "$failed_line" "$failed_function"
  fi

  if (( UPDATE_APPLIED == 1 )) && [[ -n "$PREVIOUS_COMMIT" ]]; then
    rollback_after_failure "$exit_code" "$failed_command"
  fi
  if (( LOCAL_CANDIDATE_MODE == 1 )); then
    if (( UPDATE_APPLIED == 1 )); then
      cleanup_local_candidate_new_files_after_reset || true
    else
      discard_local_candidate_worktree || true
    fi
    update_local_candidate_heartbeat "failed" "falha em $FAILED_STAGE" || true
  fi

  local dirty_status dirty_files
  dirty_status="não"
  dirty_files=""
  if [[ "$FAILED_STAGE" == "verificação de alterações locais" || "$STAGE" == "git pull" ]]; then
    dirty_files="$(collect_local_tracked_changes || true)"
    if [[ -n "${dirty_files//[[:space:]]/}" ]]; then
      dirty_status="sim"
    fi
  fi

  local body
  body="Resumo: O updater falhou antes de concluir a troca de commit.
Host: $HOSTNAME
Branch: $BRANCH
Serviço: tts-bot-updater
Serviço afetado: ${LAST_ERROR_SERVICE_UNIT:-$UPDATER_UNIT}
Commit anterior: $(short_commit "$CURRENT_COMMIT")
Commit alvo: $(short_commit "$REMOTE_COMMIT")
Commit: $(short_commit "$CURRENT_COMMIT") → $(short_commit "$REMOTE_COMMIT")
Update: ${COMMIT_SUBJECT:-sem mensagem}
Etapa: $STAGE
Código de falha: ${LAST_ERROR_CODE:-UPDATE_STAGE_FAILED}
Função: $failed_function
Linha: $failed_line
Comando: $failed_command
Código: $exit_code
Evidência primária: ${UPDATE_FAILURE_FILE:-não persistida}
Rollback: $ROLLBACK_STATUS
Commit sujo: $dirty_status
Arquivos sujos:
${dirty_files:-nenhum arquivo rastreado sujo detectado}
Stderr:
${LAST_ERROR_STDERR:-nenhuma saída adicional capturada}
Últimas linhas:
${LAST_ERROR_LOGS:-nenhum log adicional encontrado}
Hora: $(date '+%d/%m/%Y %H:%M:%S')"
  if (( ROLLBACK_CONTROL_MODE == 1 )); then
    fail_title="Falha ao reverter"
    [[ "$ROLLBACK_REQUEST_ACTION" == "redo" ]] && fail_title="Falha ao reaplicar"
    retry_control=""
    if (( UPDATE_APPLIED == 0 )); then
      retry_control="$(rollback_control_json "$ROLLBACK_REQUEST_ACTION" "$ROLLBACK_EXPECTED_HEAD" "$ROLLBACK_REVERT_COMMIT" 2>/dev/null || true)"
    fi
    post_direct_update_message "$ROLLBACK_MESSAGE_CHANNEL_ID" "$ROLLBACK_MESSAGE_ID" "error" "$fail_title" "O estado local foi mantido quando possível. Verifique o canal técnico/log interno." "$retry_control" || true
    archive_rollback_request "failed"
  else
    if (( LOCAL_CANDIDATE_MODE == 1 )); then
      notify_zip_status_message "error" "Falha ao aplicar atualização" "A VPS foi restaurada quando possível e o candidato foi arquivado. Verifique o canal de logs." || true
      archive_local_candidate "failed"
    else
      notify_zip_status_message "error" "Falha na atualização" "O updater falhou antes de concluir a aplicação. Verifique o canal técnico/log interno." || true
    fi
  fi
  send_error "Falha na atualização automática" "$body"
  exit "$exit_code"
}
