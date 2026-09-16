SECONDS=0
cd "$REPO_DIR"
prepare_update_delivery_dirs || true
mkdir -p "$CANDIDATE_QUEUE_CANCELLED_DIR" "$CANDIDATE_ROOT/cancelled" 2>/dev/null || true
startup_phase_started_ms="$(update_now_ms)"
if ! guard_updater_disk_space; then
  log_update_operation_timing_ms "startup.disk_guard" "$startup_phase_started_ms"
  exit 0
fi
log_update_operation_timing_ms "startup.disk_guard" "$startup_phase_started_ms"

# Claim de rollback/ZIP vem antes de manutenção, outboxes e refresh da fila.
# Esses trabalhos são recuperáveis e não devem adicionar dezenas de segundos
# entre o arquivo recebido e o início real da atualização. O bot possui seu
# próprio reconciliador de outbox; a manutenção pesada continua no caminho
# ocioso/remoto e ao final das entregas.
startup_phase_started_ms="$(update_now_ms)"
if load_pending_rollback_request; then
  log_update_operation_timing_ms "startup.queue_claim" "$startup_phase_started_ms"
  logger -t "$LOG_TAG" "Controle de update recebido: $ROLLBACK_REQUEST_ACTION $ROLLBACK_REQUEST_ID"
  prepare_rollback_request_update
elif load_pending_local_candidate; then
  log_update_operation_timing_ms "startup.queue_claim" "$startup_phase_started_ms"
  logger -t "$LOG_TAG" "Candidato local recebido: $LOCAL_CANDIDATE_ID"
  prepare_local_candidate_update
else
  log_update_operation_timing_ms "startup.queue_scan" "$startup_phase_started_ms"
  prune_update_artifacts || true
  flush_update_status_outbox || true
  flush_update_alert_outbox || true
  refresh_pending_queue_messages || true
  STAGE="commit atual"
  CURRENT_COMMIT="$(repo_git rev-parse HEAD)"
  PREVIOUS_COMMIT="$CURRENT_COMMIT"

  STAGE="fetch remoto"
  repo_git fetch origin "$BRANCH"
  if ! load_repo_ref_snapshot "$BRANCH"; then return 1; fi
  CURRENT_COMMIT="$GIT_REFS_CURRENT"
  PREVIOUS_COMMIT="$CURRENT_COMMIT"
  REMOTE_COMMIT="$GIT_REFS_REMOTE"
  COMMIT_SUBJECT="$GIT_REFS_REMOTE_SUBJECT"
  record_remote_fetch_state "$REMOTE_COMMIT" || true
  mark_update_timing "fetch"

  if [[ -f "$DIRTY_MARKER_FILE" ]]; then
    MARKED_FAILED_COMMIT="$(marker_value FAILED_REMOTE_COMMIT)"
    if [[ -n "$MARKED_FAILED_COMMIT" && "$REMOTE_COMMIT" == "$MARKED_FAILED_COMMIT" ]]; then
      logger -t "$LOG_TAG" "Commit remoto $(short_commit "$REMOTE_COMMIT") continua marcado como sujo após rollback fatal; aguardando um novo commit no GitHub."
      exit 0
    fi
    clear_dirty_marker
  fi

  if remote_commit_is_rejected "$REMOTE_COMMIT"; then
    logger -t "$LOG_TAG" "Commit remoto $(short_commit "$REMOTE_COMMIT") já foi rejeitado; aguardando novo commit no GitHub ou ZIP."
    exit 0
  fi

  if [[ "$CURRENT_COMMIT" == "$REMOTE_COMMIT" ]]; then
    logger -t "$LOG_TAG" "Sem mudanças em $BRANCH"
    exit 0
  fi

  REMOTE_CANDIDATE_MODE=1
  set_updater_priority_profile fast
  SHORT_FROM="$(short_commit "$CURRENT_COMMIT")"
  SHORT_TO="$(short_commit "$REMOTE_COMMIT")"

  if ! load_git_diff_snapshot "$REPO_DIR" --base "$CURRENT_COMMIT" --target "$REMOTE_COMMIT"; then
    LAST_ERROR_CODE="REMOTE_DIFF_SNAPSHOT_FAILED"
    return 1
  fi
  mark_update_timing "diff"

  classify_changed_files

  eval "$(create_direct_update_message "applying" "$(zip_progress_title "Conferindo commit do GitHub")" "$UPDATE_STAGE_EMOJI **Conferindo commit do GitHub**")"
  zip_progress_publish "Conferindo commit do GitHub"

  remote_ready_fast_path=0
  STAGE="reutilização READY do commit remoto"
  if reuse_ready_artifacts_for_commit "$REMOTE_COMMIT"; then
    remote_ready_fast_path=1
    PREFLIGHT_PY_STATUS="validado no READY anterior"
    PREFLIGHT_BASH_STATUS="validado no READY anterior"
    PREFLIGHT_COG_IMPORT_STATUS="validado no READY anterior"
    logger -t "$LOG_TAG" "commit remoto $(short_commit "$REMOTE_COMMIT") reutilizou READY antes de criar worktree" 2>/dev/null || true
    mark_update_timing "remote_ready_reuse"
    zip_progress_done_and_publish "READY anterior confirmado" "Aplicando na VPS"
  else
    STAGE="validação do commit remoto em staging"
    if ! validate_remote_commit_in_staging "$REMOTE_COMMIT"; then
      reject_remote_commit_without_live_apply "preflight falhou no staging remoto"
    fi
    mark_update_timing "remote_preflight"

    STAGE="preparação de artefatos do commit remoto"
    set_updater_priority_profile safe
    zip_progress_done_and_publish "Commit conferido" "Validando runtime em isolamento"
    if ! prepare_local_candidate_runtime_artifacts_in_worktree; then
      reject_remote_commit_without_live_apply "validação/build isolado falhou antes da promoção: ${LAST_ERROR_CODE:-REMOTE_READY_FAILED}: ${LAST_ERROR_STDERR:-erro desconhecido}"
    fi
    mark_update_timing "remote_ready"
    zip_progress_done_and_publish "Commit READY em isolamento" "Aplicando na VPS"
  fi

  set_updater_priority_profile safe
  STAGE="preservação do runtime anterior"
  if ! capture_runtime_release_snapshot "$PREVIOUS_COMMIT"; then
    reject_remote_commit_without_live_apply "não foi possível preservar o runtime atual para rollback sem rebuild: ${LAST_ERROR_STDERR:-erro desconhecido}"
  fi

  STAGE="limpeza de artefatos gerados"
  cleanup_known_generated_update_artifacts

  STAGE="verificação de alterações locais"
  clear_local_changes_marker_if_clean
  fail_local_changes_before_pull

  logger -t "$LOG_TAG" "Aplicando commit remoto validado de $CURRENT_COMMIT para $REMOTE_COMMIT"

  STAGE="aplicação do commit GitHub"
  set_updater_priority_profile fast
  repo_git merge --ff-only "$REMOTE_COMMIT"
  UPDATE_APPLIED=1
  mark_update_timing "apply"
  set_updater_priority_profile safe
  zip_progress_done "Aplicado na VPS"
fi

FAILED_STAGE=""

if (( LOCAL_CANDIDATE_RESUME_DELIVERY_ONLY == 1 )); then
  # O commit já foi validado e publicado numa execução anterior. Repetir o
  # pipeline aqui reiniciava o bot novamente apenas porque a confirmação final
  # tinha falhado. Nesta retomada, apenas confirmamos o estado atual e seguimos
  # para a entrega idempotente.
  mark_deployment_committed
  zip_progress_publish "Recuperando confirmação final"
  PREFLIGHT_PY_STATUS="validado na execução anterior"
  PREFLIGHT_BASH_STATUS="validado na execução anterior"
  PREFLIGHT_COG_IMPORT_STATUS="validado na execução anterior"
  PREFLIGHT_RUNTIME_STATUS="validado na execução anterior"
  if refresh_bot_health_status; then
    BOT_HEALTHCHECK_STATUS="OK"
  else
    BOT_HEALTHCHECK_STATUS="commit publicado; health indisponível na recuperação"
    UPDATE_HAS_WARNINGS=1
  fi
  read_app_command_sync_status
  zip_progress_done "Estado publicado confirmado"
else
  if (( LOCAL_CANDIDATE_MODE == 1 || ROLLBACK_CONTROL_MODE == 1 || REMOTE_CANDIDATE_MODE == 1 )); then
    zip_progress_publish "Validando arquivos"
  fi
  if (( LOCAL_CANDIDATE_MODE == 1 )); then
    STAGE="limpeza de artefatos gerados pós-promoção"
    cleanup_known_generated_update_artifacts
  fi
  run_preflight_checks
  mark_update_timing "preflight"
  if (( LOCAL_CANDIDATE_MODE == 1 || ROLLBACK_CONTROL_MODE == 1 || REMOTE_CANDIDATE_MODE == 1 )); then
    zip_progress_done "Arquivos validados"
    zip_progress_publish "$(zip_progress_next_apply_stage)"
  fi

  deploy_bot
  mark_update_timing "bot"
  deploy_frontend
  mark_update_timing "frontend"
  deploy_backend
  mark_update_timing "backend"
  run_core_worker_post_update_automation
  mark_update_timing "worker"
  STAGE="verificação pós-build do repositório"
  ensure_no_unstaged_tracked_changes
  if (( LOCAL_CANDIDATE_MODE == 1 || ROLLBACK_CONTROL_MODE == 1 || REMOTE_CANDIDATE_MODE == 1 )); then
    zip_progress_done_apply_stage
    zip_progress_publish "Verificando comandos"
  fi
  read_app_command_sync_status
  if (( LOCAL_CANDIDATE_MODE == 1 || ROLLBACK_CONTROL_MODE == 1 || REMOTE_CANDIDATE_MODE == 1 )); then
    zip_progress_done "$APP_COMMAND_SYNC_SUMMARY"
  fi

  publish_rollback_request_after_validation
  if (( ROLLBACK_CONTROL_MODE == 1 )); then
    mark_deployment_committed
    finalize_rollback_request_success
  fi

  publish_local_candidate_after_validation
  mark_deployment_committed
fi
