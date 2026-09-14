rollback_request_roots() {
  printf '%s\n' "$ROLLBACK_REQUEST_DEFAULT_ROOT"
  if [[ -n "${ROLLBACK_REQUEST_DATA_ROOT//[[:space:]]/}" && "$ROLLBACK_REQUEST_DATA_ROOT" != "$ROLLBACK_REQUEST_DEFAULT_ROOT" ]]; then
    printf '%s\n' "$ROLLBACK_REQUEST_DATA_ROOT"
  fi
  if [[ -n "${ROLLBACK_REQUEST_TMP_ROOT//[[:space:]]/}" && "$ROLLBACK_REQUEST_TMP_ROOT" != "$ROLLBACK_REQUEST_DEFAULT_ROOT" && "$ROLLBACK_REQUEST_TMP_ROOT" != "$ROLLBACK_REQUEST_DATA_ROOT" ]]; then
    printf '%s\n' "$ROLLBACK_REQUEST_TMP_ROOT"
  fi
}

ensure_rollback_request_dirs() {
  local root
  while IFS= read -r root; do
    [[ -n "${root//[[:space:]]/}" ]] || continue
    mkdir -p "$root" "$root/done" "$root/failed" 2>/dev/null || true
    chown ubuntu:ubuntu "$root" "$root/done" "$root/failed" 2>/dev/null || true
    chmod 0775 "$root" "$root/done" "$root/failed" 2>/dev/null || true
  done < <(rollback_request_roots)
}

select_rollback_request_root() {
  local root pending active
  ensure_rollback_request_dirs
  while IFS= read -r root; do
    [[ -n "${root//[[:space:]]/}" ]] || continue
    pending="$root/pending.json"
    active="$root/active.json"
    if [[ -f "$pending" ]]; then
      mv "$pending" "$active" 2>/dev/null || true
    fi
    if [[ -f "$active" ]]; then
      ROLLBACK_REQUEST_ROOT="$root"
      ROLLBACK_REQUEST_PENDING_FILE="$pending"
      ROLLBACK_REQUEST_ACTIVE_FILE="$active"
      return 0
    fi
  done < <(rollback_request_roots)
  return 1
}

load_pending_rollback_request() {
  ROLLBACK_CONTROL_MODE=0
  ROLLBACK_REQUEST_FILE=""
  select_rollback_request_root || return 1
  ROLLBACK_CONTROL_MODE=1
  ROLLBACK_REQUEST_FILE="$ROLLBACK_REQUEST_ACTIVE_FILE"
  ROLLBACK_REQUEST_ID="$(json_field_from_file "$ROLLBACK_REQUEST_FILE" id 2>/dev/null || true)"
  ROLLBACK_REQUEST_ACTION="$(json_field_from_file "$ROLLBACK_REQUEST_FILE" mode 2>/dev/null || true)"
  ROLLBACK_REQUEST_BRANCH="$(json_field_from_file "$ROLLBACK_REQUEST_FILE" branch 2>/dev/null || true)"
  ROLLBACK_EXPECTED_HEAD="$(sanitize_commit_ref "$(json_field_from_file "$ROLLBACK_REQUEST_FILE" expected_head 2>/dev/null || true)")"
  ROLLBACK_REVERT_COMMIT="$(sanitize_commit_ref "$(json_field_from_file "$ROLLBACK_REQUEST_FILE" revert_commit 2>/dev/null || true)")"
  ROLLBACK_UPDATE_FROM="$(sanitize_commit_ref "$(json_field_from_file "$ROLLBACK_REQUEST_FILE" update_from 2>/dev/null || true)")"
  ROLLBACK_UPDATE_TO="$(sanitize_commit_ref "$(json_field_from_file "$ROLLBACK_REQUEST_FILE" update_to 2>/dev/null || true)")"
  ROLLBACK_ROLLBACK_COMMIT="$(sanitize_commit_ref "$(json_field_from_file "$ROLLBACK_REQUEST_FILE" rollback_commit 2>/dev/null || true)")"
  ROLLBACK_REDO_COMMIT="$(sanitize_commit_ref "$(json_field_from_file "$ROLLBACK_REQUEST_FILE" redo_commit 2>/dev/null || true)")"
  ROLLBACK_MESSAGE_CHANNEL_ID="$(json_field_from_file "$ROLLBACK_REQUEST_FILE" message.channel_id 2>/dev/null || true)"
  ROLLBACK_MESSAGE_ID="$(json_field_from_file "$ROLLBACK_REQUEST_FILE" message.message_id 2>/dev/null || true)"
  ROLLBACK_SOURCE_AUTHOR_ID="$(json_field_from_file "$ROLLBACK_REQUEST_FILE" source_author_id 2>/dev/null || true)"
  ROLLBACK_REQUESTED_BY="$(json_field_from_file "$ROLLBACK_REQUEST_FILE" requested_by 2>/dev/null || true)"
  ROLLBACK_PREVIOUS_RECORD_JSON="$(json_field_from_file "$ROLLBACK_REQUEST_FILE" previous_record 2>/dev/null || echo '{}')"
  [[ -n "${ROLLBACK_REQUEST_ID//[[:space:]]/}" ]] || ROLLBACK_REQUEST_ID="rollback-$(date +%Y%m%d%H%M%S)"
  [[ -n "${ROLLBACK_REQUEST_BRANCH//[[:space:]]/}" ]] || ROLLBACK_REQUEST_BRANCH="main"
  BRANCH="$ROLLBACK_REQUEST_BRANCH"
  if [[ "$ROLLBACK_REQUEST_ACTION" != "rollback" && "$ROLLBACK_REQUEST_ACTION" != "redo" ]]; then
    ROLLBACK_REQUEST_ACTION="rollback"
  fi

  if [[ "$ROLLBACK_REQUEST_ACTION" == "rollback" ]]; then
    # O alvo técnico é o commit atual salvo no controle. `ROLLBACK_UPDATE_TO`
    # é metadado do update original e pode não ser mais o HEAD depois de uma
    # reaplicação; use apenas como fallback para estados antigos.
    ROLLBACK_EXPECTED_HEAD="${ROLLBACK_EXPECTED_HEAD:-${ROLLBACK_REVERT_COMMIT:-${ROLLBACK_UPDATE_TO:-}}}"
    ROLLBACK_REVERT_COMMIT="${ROLLBACK_REVERT_COMMIT:-${ROLLBACK_EXPECTED_HEAD:-${ROLLBACK_UPDATE_TO:-}}}"
  else
    # Para refazer, revertemos o commit de rollback.
    ROLLBACK_EXPECTED_HEAD="${ROLLBACK_ROLLBACK_COMMIT:-${ROLLBACK_EXPECTED_HEAD:-${ROLLBACK_REVERT_COMMIT:-}}}"
    ROLLBACK_REVERT_COMMIT="${ROLLBACK_ROLLBACK_COMMIT:-${ROLLBACK_REVERT_COMMIT:-${ROLLBACK_REDO_COMMIT:-$ROLLBACK_EXPECTED_HEAD}}}"
  fi
  ROLLBACK_EXPECTED_HEAD="$(sanitize_commit_ref "$ROLLBACK_EXPECTED_HEAD")"
  ROLLBACK_REVERT_COMMIT="$(sanitize_commit_ref "$ROLLBACK_REVERT_COMMIT")"

  # Retorna sucesso sempre que existe um request ativo. Campos inválidos são
  # tratados por prepare_rollback_request_update, que arquiva o request e edita
  # a mensagem em vez de deixar o timer ignorar o estado e repetir forever.
  return 0
}

archive_rollback_request() {
  local status="${1:-done}"
  [[ -n "${ROLLBACK_REQUEST_FILE:-}" ]] || return 0
  mkdir -p "$ROLLBACK_REQUEST_ROOT/$status" 2>/dev/null || true
  chown ubuntu:ubuntu "$ROLLBACK_REQUEST_ROOT" "$ROLLBACK_REQUEST_ROOT/$status" 2>/dev/null || true
  chmod 0775 "$ROLLBACK_REQUEST_ROOT" "$ROLLBACK_REQUEST_ROOT/$status" 2>/dev/null || true
  if [[ -f "$ROLLBACK_REQUEST_FILE" ]]; then
    mv "$ROLLBACK_REQUEST_FILE" "$ROLLBACK_REQUEST_ROOT/$status/${ROLLBACK_REQUEST_ID:-rollback}.$(date +%Y%m%d%H%M%S).json" 2>/dev/null || rm -f "$ROLLBACK_REQUEST_FILE" 2>/dev/null || true
  fi
}

commit_exists() {
  local commit="$(sanitize_commit_ref "${1:-}")"
  [[ -n "$commit" ]] || return 1
  repo_git rev-parse --verify "${commit}^{commit}" >/dev/null 2>&1
}

commits_have_same_tree() {
  local left="$(sanitize_commit_ref "${1:-}")"
  local right="$(sanitize_commit_ref "${2:-}")"
  [[ -n "$left" && -n "$right" ]] || return 1
  commit_exists "$left" || return 1
  commit_exists "$right" || return 1
  repo_git diff --quiet "$left" "$right" --
}

rollback_desired_tree_commit() {
  if [[ "$ROLLBACK_REQUEST_ACTION" == "redo" ]]; then
    sanitize_commit_ref "${ROLLBACK_UPDATE_TO:-}"
  else
    sanitize_commit_ref "${ROLLBACK_UPDATE_FROM:-}"
  fi
}

rollback_control_json() {
  local mode="${1:-rollback}"
  local head_commit="$(sanitize_commit_ref "${2:-}")"
  local revert_commit="$(sanitize_commit_ref "${3:-$head_commit}")"
  local update_from="$(sanitize_commit_ref "${4:-${ROLLBACK_UPDATE_FROM:-}}")"
  local update_to="$(sanitize_commit_ref "${5:-${ROLLBACK_UPDATE_TO:-}}")"
  local rollback_commit="$(sanitize_commit_ref "${6:-${ROLLBACK_ROLLBACK_COMMIT:-}}")"
  local redo_commit="$(sanitize_commit_ref "${7:-${ROLLBACK_REDO_COMMIT:-}}")"
  python3 - "$mode" "$head_commit" "$revert_commit" "$BRANCH" "$ROLLBACK_SOURCE_AUTHOR_ID" "$update_from" "$update_to" "$rollback_commit" "$redo_commit" <<'PYCTRL'
import json, sys
mode, head, revert, branch, author, update_from, update_to, rollback_commit, redo_commit = sys.argv[1:10]
payload = {
    "enabled": True,
    "mode": mode,
    "branch": branch or "main",
    "expected_head": head,
    "revert_commit": revert or head,
    "head_commit": head,
    "source_author_id": author,
}
for key, value in {
    "update_from": update_from,
    "update_to": update_to,
    "rollback_commit": rollback_commit,
    "redo_commit": redo_commit,
}.items():
    if value:
        payload[key] = value
print(json.dumps(payload, ensure_ascii=False))
PYCTRL
}

prepare_rollback_request_update() {
  ROLLBACK_CONTROL_MODE=1
  LOCAL_CANDIDATE_MODE=0
  if [[ -z "${ROLLBACK_EXPECTED_HEAD//[[:space:]]/}" || -z "${ROLLBACK_REVERT_COMMIT//[[:space:]]/}" ]]; then
    local fail_title="Falha ao reverter"
    [[ "$ROLLBACK_REQUEST_ACTION" == "redo" ]] && fail_title="Falha ao reaplicar"
    post_direct_update_message "$ROLLBACK_MESSAGE_CHANNEL_ID" "$ROLLBACK_MESSAGE_ID" "error" "$fail_title" "Não encontrei o commit de destino. Nenhuma alteração foi aplicada." || true
    logger -t "$LOG_TAG" "rollback/redo inválido: action=$ROLLBACK_REQUEST_ACTION expected=${ROLLBACK_EXPECTED_HEAD:-vazio} revert=${ROLLBACK_REVERT_COMMIT:-vazio} update_from=${ROLLBACK_UPDATE_FROM:-vazio} update_to=${ROLLBACK_UPDATE_TO:-vazio}"
    archive_rollback_request "failed"
    exit 0
  fi
  zip_progress_publish "Validando estado atual"
  STAGE="fetch remoto"
  repo_git fetch origin "$BRANCH"
  if ! load_repo_ref_snapshot "$BRANCH"; then return 1; fi
  CURRENT_COMMIT="$GIT_REFS_CURRENT"
  REMOTE_COMMIT="$GIT_REFS_REMOTE"
  PREVIOUS_COMMIT="$CURRENT_COMMIT"
  SHORT_FROM="$(short_commit "$CURRENT_COMMIT")"
  record_remote_fetch_state "$REMOTE_COMMIT" || true
  mark_update_timing "fetch"

  if [[ "$CURRENT_COMMIT" != "$REMOTE_COMMIT" || "$CURRENT_COMMIT" != "$ROLLBACK_EXPECTED_HEAD" ]]; then
    local unavailable_title="Reversão indisponível"
    local desired_commit noop_title
    [[ "$ROLLBACK_REQUEST_ACTION" == "redo" ]] && unavailable_title="Reaplicação indisponível"
    desired_commit="$(rollback_desired_tree_commit)"
    if [[ -n "$desired_commit" ]] && commits_have_same_tree "$CURRENT_COMMIT" "$desired_commit"; then
      noop_title="Nenhuma alteração necessária"
      post_direct_update_message "$ROLLBACK_MESSAGE_CHANNEL_ID" "$ROLLBACK_MESSAGE_ID" "warn" "$noop_title" "O estado atual já corresponde ao resultado esperado. Nada foi alterado." || true
      archive_rollback_request "done"
      exit 0
    fi
    post_direct_update_message "$ROLLBACK_MESSAGE_CHANNEL_ID" "$ROLLBACK_MESSAGE_ID" "error" "$unavailable_title" "O estado atual mudou. Nenhuma alteração foi aplicada." || true
    archive_rollback_request "failed"
    exit 0
  fi

  STAGE="verificação de alterações locais"
  clear_local_changes_marker_if_clean
  fail_local_changes_before_pull

  zip_progress_done_and_publish "Estado validado" "Aplicando reversão"

  STAGE="reversão local"
  if ! repo_git rev-parse --verify "${ROLLBACK_REVERT_COMMIT}^{commit}" >/dev/null 2>&1; then
    local fail_title="Falha ao reverter"
    [[ "$ROLLBACK_REQUEST_ACTION" == "redo" ]] && fail_title="Falha ao reaplicar"
    local retry_control
    retry_control="$(rollback_control_json "$ROLLBACK_REQUEST_ACTION" "$ROLLBACK_EXPECTED_HEAD" "$ROLLBACK_REVERT_COMMIT" 2>/dev/null || true)"
    logger -t "$LOG_TAG" "commit de rollback/redo não encontrado: action=$ROLLBACK_REQUEST_ACTION expected=$ROLLBACK_EXPECTED_HEAD revert=$ROLLBACK_REVERT_COMMIT current=$CURRENT_COMMIT remote=$REMOTE_COMMIT"
    post_direct_update_message "$ROLLBACK_MESSAGE_CHANNEL_ID" "$ROLLBACK_MESSAGE_ID" "error" "$fail_title" "Não encontrei o commit de destino. Nenhuma alteração foi aplicada." "$retry_control" || true
    archive_rollback_request "failed"
    exit 0
  fi
  if ! repo_git revert --no-commit "$ROLLBACK_REVERT_COMMIT"; then
    repo_git revert --abort >/dev/null 2>&1 || true
    repo_git reset --hard "$PREVIOUS_COMMIT" >/dev/null 2>&1 || true
    local fail_title="Falha ao reverter"
    [[ "$ROLLBACK_REQUEST_ACTION" == "redo" ]] && fail_title="Falha ao reaplicar"
    local retry_control
    retry_control="$(rollback_control_json "$ROLLBACK_REQUEST_ACTION" "$ROLLBACK_EXPECTED_HEAD" "$ROLLBACK_REVERT_COMMIT" 2>/dev/null || true)"
    post_direct_update_message "$ROLLBACK_MESSAGE_CHANNEL_ID" "$ROLLBACK_MESSAGE_ID" "error" "$fail_title" "O estado local foi restaurado. Nada foi publicado no GitHub." "$retry_control" || true
    archive_rollback_request "failed"
    exit 0
  fi
  UPDATE_APPLIED=1
  if ! refresh_changed_files_from_staged_diff; then
    LAST_ERROR_STDERR="${LAST_ERROR_STDERR:-falha ao calcular o diff staged da reversão}"
    return 1
  fi
  if [[ -z "${CHANGED_FILES_RAW//[[:space:]]/}" ]]; then
    repo_git reset --hard "$PREVIOUS_COMMIT" >/dev/null 2>&1 || true
    local retry_control
    retry_control="$(rollback_control_json "$ROLLBACK_REQUEST_ACTION" "$ROLLBACK_EXPECTED_HEAD" "$ROLLBACK_REVERT_COMMIT" 2>/dev/null || true)"
    post_direct_update_message "$ROLLBACK_MESSAGE_CHANNEL_ID" "$ROLLBACK_MESSAGE_ID" "warn" "Nenhuma alteração" "O estado já estava equivalente." "$retry_control" || true
    archive_rollback_request "done"
    exit 0
  fi
  classify_changed_files
  COMMIT_SUBJECT="${ROLLBACK_REQUEST_ACTION} discord zip update"
  mark_update_timing "rollback_apply"
  zip_progress_done "Reversão preparada"
}

publish_rollback_request_after_validation() {
  (( ROLLBACK_CONTROL_MODE == 1 )) || return 0
  STAGE="commit da reversão"
  local msg
  if [[ "$ROLLBACK_REQUEST_ACTION" == "redo" ]]; then
    msg="redo discord zip update $(short_commit "$ROLLBACK_REVERT_COMMIT")"
  else
    msg="rollback discord zip update $(short_commit "$ROLLBACK_REVERT_COMMIT")"
  fi
  zip_progress_publish "Fazendo commit..."
  git_add_changed_files
  repo_git commit -m "$msg"
  REMOTE_COMMIT="$(repo_git rev-parse HEAD)"
  ROLLBACK_NEW_COMMIT="$REMOTE_COMMIT"
  if [[ "$ROLLBACK_REQUEST_ACTION" == "redo" ]]; then
    ROLLBACK_REDO_COMMIT="$REMOTE_COMMIT"
  else
    ROLLBACK_ROLLBACK_COMMIT="$REMOTE_COMMIT"
  fi
  SHORT_TO="$(short_commit "$REMOTE_COMMIT")"
  mark_update_timing "commit"
  zip_progress_done "Commit criado"
  STAGE="push GitHub"
  zip_progress_publish "Publicando no GitHub..."
  repo_git push origin "HEAD:$BRANCH"
  record_remote_fetch_state "$REMOTE_COMMIT" || true
  # O commit de reversão/reaplicação já está remoto; a notificação posterior
  # não pode transformar isso em rollback automático do rollback.
  mark_deployment_committed
  mark_update_timing "push"
  zip_progress_done "GitHub atualizado"
}

finalize_rollback_request_success() {
  (( ROLLBACK_CONTROL_MODE == 1 )) || return 1
  local duration changed_files diff_summary apply_mode control_json title summary next_mode status_title file_count file_label altered_label
  duration="$(human_duration "$SECONDS")"
  changed_files="$(format_changed_files)"
  diff_summary="$(format_diff_total_summary)"
  file_count="$(printf '%s\n' "$CHANGED_FILES_RAW" | awk 'NF {c++} END {print c+0}')"
  file_label="$(format_update_file_count "$file_count")"
  if (( file_count == 1 )); then altered_label="alterado"; else altered_label="alterados"; fi
  if [[ "$FAST_RELOAD_STATUS" == OK* ]]; then
    apply_mode="recarga controlada de cog"
  elif (( ${BOT_CHANGED:-0} == 0 )); then
    apply_mode="sem reinício do bot"
  else
    apply_mode="reinício completo"
  fi
  if [[ "$ROLLBACK_REQUEST_ACTION" == "redo" ]]; then
    title="↪️ Atualização reaplicada"
    summary="Atualização reaplicada e estabilidade confirmada."
    next_mode="rollback"
  else
    title="↩️ Atualização revertida"
    summary="Reversão aplicada e estabilidade confirmada."
    next_mode="redo"
  fi
  control_json="$(rollback_control_json "$next_mode" "$REMOTE_COMMIT" "$REMOTE_COMMIT" "$ROLLBACK_UPDATE_FROM" "$ROLLBACK_UPDATE_TO" "$ROLLBACK_ROLLBACK_COMMIT" "$ROLLBACK_REDO_COMMIT")"
  local desc
  desc="$summary

${SHORT_FROM} → ${SHORT_TO}
$file_label $altered_label · $diff_summary
Aplicação: $apply_mode
Tempo total: $duration"
  post_direct_update_message "$ROLLBACK_MESSAGE_CHANNEL_ID" "$ROLLBACK_MESSAGE_ID" "success" "$title" "$desc" "$control_json" || true
  local body
  body="Resumo: $summary
Branch: $BRANCH
Commit: ${SHORT_FROM} → ${SHORT_TO}
Update: $file_label · $diff_summary
Aplicação: $apply_mode
Processos alterados: $(format_changed_processes)
Arquivos:
$changed_files
Duração: $duration
Hora: $(date '+%d/%m/%Y %H:%M:%S')"
  send_alert_reliably success "$title" "$body" "" "" "rollback-${ROLLBACK_REQUEST_ID:-unknown}-${SHORT_TO:-unknown}" || true
  archive_rollback_request "done"
  logger -t "$LOG_TAG" "$title"
  exit 0
}
