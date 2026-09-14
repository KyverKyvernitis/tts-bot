#!/usr/bin/env bash
# Módulo carregado por atualizar.sh; não executar diretamente.
# Fila local de candidatos e dispatch entre execuções.

load_pending_local_candidate() {
  local manifest active_file pending_file legacy_active legacy_pending active_payload resuming_active=0 started_at_iso
  LOCAL_CANDIDATE_PENDING_FILE=""
  LOCAL_CANDIDATE_DIR=""

  # A fila é criada com ownership correto na origem. Não percorra todo o histórico
  # com chown -R a cada tick: conforme done/failed crescem, isso transforma o
  # simples claim de um candidato em I/O proporcional ao histórico inteiro.
  install -d -o ubuntu -g ubuntu -m 0775 \
    "$CANDIDATE_QUEUE_ROOT" \
    "$CANDIDATE_QUEUE_PENDING_DIR" \
    "$CANDIDATE_QUEUE_ACTIVE_DIR" \
    "$CANDIDATE_QUEUE_DONE_DIR" \
    "$CANDIDATE_QUEUE_FAILED_DIR" \
    "$CANDIDATE_QUEUE_CANCELLED_DIR" 2>/dev/null || true

  # Recuperação primeiro: se uma execução caiu com item ativo, retome esse item
  # antes de pegar outro pending. Isso evita aplicar fora de ordem.
  active_file="$(find "$CANDIDATE_QUEUE_ACTIVE_DIR" -maxdepth 1 -type f -name '*.json' 2>/dev/null | sort | head -n 1 || true)"
  if [[ -n "${active_file//[[:space:]]/}" ]]; then
    resuming_active=1
    LOCAL_CANDIDATE_PENDING_FILE="$active_file"
    LOCAL_CANDIDATE_DIR="$(json_field_from_file "$active_file" candidate_dir 2>/dev/null || true)"
    if [[ -z "${LOCAL_CANDIDATE_DIR//[[:space:]]/}" || ! -d "$LOCAL_CANDIDATE_DIR" ]]; then
      mv "$active_file" "$CANDIDATE_QUEUE_FAILED_DIR/$(basename "$active_file").missing.$(date +%Y%m%d%H%M%S)" 2>/dev/null || rm -f "$active_file" 2>/dev/null || true
      LOCAL_CANDIDATE_PENDING_FILE=""
      LOCAL_CANDIDATE_DIR=""
      return 1
    fi
    logger -t "$LOG_TAG" "Retomando item ativo da fila: $(basename "$active_file")"
  else
    # Compatibilidade com o formato antigo candidates/*/active.json.
    legacy_active="$(find "$CANDIDATE_ROOT" -mindepth 2 -maxdepth 2 -type f -name active.json 2>/dev/null | grep -v '/queue/' | sort | head -n 1 || true)"
    if [[ -n "${legacy_active//[[:space:]]/}" ]]; then
      resuming_active=1
      LOCAL_CANDIDATE_PENDING_FILE="$legacy_active"
      LOCAL_CANDIDATE_DIR="$(dirname "$legacy_active")"
      logger -t "$LOG_TAG" "Retomando candidato local ativo legado: $(basename "$LOCAL_CANDIDATE_DIR")"
    else
      pending_file="$(find "$CANDIDATE_QUEUE_PENDING_DIR" -maxdepth 1 -type f -name '*.json' 2>/dev/null | sort | head -n 1 || true)"

      # Migração/compatibilidade: se o bot antigo ainda criou candidates/pending.json,
      # trate como um item de fila sem sobrescrever os novos pendentes.
      if [[ -z "${pending_file//[[:space:]]/}" && -f "$CANDIDATE_PENDING_FILE" ]]; then
        legacy_pending="$CANDIDATE_QUEUE_PENDING_DIR/legacy-$(date +%Y%m%d%H%M%S)-$(basename "$CANDIDATE_PENDING_FILE")"
        mv "$CANDIDATE_PENDING_FILE" "$legacy_pending" 2>/dev/null || true
        pending_file="$legacy_pending"
      fi

      if [[ -z "${pending_file//[[:space:]]/}" ]]; then
        return 1
      fi

      active_payload="$CANDIDATE_QUEUE_ACTIVE_DIR/$(basename "$pending_file")"
      if ! mv "$pending_file" "$active_payload" 2>/dev/null; then
        # Outro processo pode ter pego no mesmo instante. O flock torna isso raro,
        # mas falhar limpo evita duplicar aplicação.
        return 1
      fi
      LOCAL_CANDIDATE_PENDING_FILE="$active_payload"
      LOCAL_CANDIDATE_DIR="$(json_field_from_file "$active_payload" candidate_dir 2>/dev/null || true)"
      if [[ -z "${LOCAL_CANDIDATE_DIR//[[:space:]]/}" || ! -d "$LOCAL_CANDIDATE_DIR" ]]; then
        mv "$active_payload" "$CANDIDATE_QUEUE_FAILED_DIR/$(basename "$active_payload").missing.$(date +%Y%m%d%H%M%S)" 2>/dev/null || rm -f "$active_payload" 2>/dev/null || true
        LOCAL_CANDIDATE_PENDING_FILE=""
        LOCAL_CANDIDATE_DIR=""
        return 1
      fi
      logger -t "$LOG_TAG" "Candidato local recebido da fila: $(basename "$LOCAL_CANDIDATE_DIR")"
    fi
  fi

  if (( resuming_active == 0 )) && [[ -f "${LOCAL_CANDIDATE_PENDING_FILE:-}" ]]; then
    local queue_created_at queue_wait_ms queue_wait_text
    queue_created_at="$(json_field_from_file "$LOCAL_CANDIDATE_PENDING_FILE" created_at 2>/dev/null || true)"
    queue_wait_ms="$(python3 - "$queue_created_at" <<'PYQUEUEWAIT' 2>/dev/null || echo 0
import datetime, sys, time
raw = (sys.argv[1] or '').strip()
try:
    dt = datetime.datetime.fromisoformat(raw.replace('Z', '+00:00'))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    print(max(0, int(time.time() * 1000) - int(dt.timestamp() * 1000)))
except Exception:
    print(0)
PYQUEUEWAIT
)"
    [[ "$queue_wait_ms" =~ ^[0-9]+$ ]] || queue_wait_ms=0
    queue_wait_text="$(format_update_duration_ms "$queue_wait_ms")"
    printf '[timing] dispatch.queue_wait=%s (%sms)\n' "$queue_wait_text" "$queue_wait_ms"
    logger -t "$LOG_TAG" "timing dispatch.queue_wait=${queue_wait_ms}ms" 2>/dev/null || true
  fi

  manifest="$LOCAL_CANDIDATE_DIR/manifest.json"
  if [[ ! -f "$manifest" ]] || ! python3 - "$manifest" <<'PYVALIDMANIFEST' >/dev/null 2>&1
import json, pathlib, sys
path = pathlib.Path(sys.argv[1])
data = json.loads(path.read_text(encoding='utf-8'))
if not isinstance(data, dict) or not str(data.get('id') or '').strip():
    raise SystemExit(1)
PYVALIDMANIFEST
  then
    logger -t "$LOG_TAG" "Candidato ativo inválido: manifest ausente ou corrompido em $LOCAL_CANDIDATE_DIR" 2>/dev/null || true
    LOCAL_CANDIDATE_MODE=1
    LOCAL_CANDIDATE_VERIFY_ERROR="manifest ausente ou corrompido"
    archive_local_candidate "failed"
    send_error "Candidato de update corrompido" "Resumo: Um item da fila tinha manifest ausente ou inválido e foi arquivado sem alterar a VPS.
Candidato: $(basename "$LOCAL_CANDIDATE_DIR")
Hora: $(date '+%d/%m/%Y %H:%M:%S')" "candidate-corrupt-$(basename "$LOCAL_CANDIDATE_DIR")" || true
    LOCAL_CANDIDATE_MODE=0
    LOCAL_CANDIDATE_DIR=""
    LOCAL_CANDIDATE_PENDING_FILE=""
    return 1
  fi
  LOCAL_CANDIDATE_MODE=1
  LOCAL_CANDIDATE_ID="$(json_field_from_file "$manifest" id 2>/dev/null || true)"
  LOCAL_CANDIDATE_DISPLAY_ID="$(json_field_from_file "$manifest" display_id 2>/dev/null || true)"
  LOCAL_CANDIDATE_BASE_COMMIT="$(json_field_from_file "$manifest" base_commit 2>/dev/null || true)"
  LOCAL_CANDIDATE_COMMIT_MESSAGE="$(json_field_from_file "$manifest" commit_message 2>/dev/null || true)"
  LOCAL_CANDIDATE_ZIP_NAME="$(json_field_from_file "$manifest" zip_name 2>/dev/null || true)"
  LOCAL_CANDIDATE_ZIP_SHA256="$(json_field_from_file "$manifest" zip_sha256 2>/dev/null || true)"
  LOCAL_CANDIDATE_SOURCE_AUTHOR_ID="$(json_field_from_file "$manifest" discord_status.source_author_id 2>/dev/null || true)"
  LOCAL_CANDIDATE_SCHEMA_VERSION="$(json_field_from_file "$manifest" schema_version 2>/dev/null || true)"
  [[ "$LOCAL_CANDIDATE_SCHEMA_VERSION" =~ ^[0-9]+$ ]] || LOCAL_CANDIDATE_SCHEMA_VERSION=2
  LOCAL_CANDIDATE_FILES_DIR="$LOCAL_CANDIDATE_DIR/files"
  LOCAL_CANDIDATE_PATCH_FILE="$LOCAL_CANDIDATE_DIR/patch.diff"
  LOCAL_CANDIDATE_USE_PATCH=0
  BRANCH="$(json_field_from_file "$manifest" branch 2>/dev/null || true)"
  [[ -n "${BRANCH//[[:space:]]/}" ]] || BRANCH="main"
  [[ -n "${LOCAL_CANDIDATE_ID//[[:space:]]/}" ]] || LOCAL_CANDIDATE_ID="$(basename "$LOCAL_CANDIDATE_DIR")"
  [[ -n "${LOCAL_CANDIDATE_DISPLAY_ID//[[:space:]]/}" ]] || LOCAL_CANDIDATE_DISPLAY_ID="$LOCAL_CANDIDATE_ID"
  [[ -n "${LOCAL_CANDIDATE_COMMIT_MESSAGE//[[:space:]]/}" ]] || LOCAL_CANDIDATE_COMMIT_MESSAGE="update: aplicar $LOCAL_CANDIDATE_DISPLAY_ID"
  if [[ -f "$LOCAL_CANDIDATE_PENDING_FILE" ]]; then
    LOCAL_CANDIDATE_ATTEMPT="$(RESUMING_ACTIVE="$resuming_active" python3 - "$LOCAL_CANDIDATE_PENDING_FILE" <<'PYATTEMPT' 2>/dev/null || echo 1
import datetime, json, os, pathlib, sys
path = pathlib.Path(sys.argv[1])
try:
    data = json.loads(path.read_text(encoding='utf-8'))
except Exception:
    data = {}
resuming = os.environ.get('RESUMING_ACTIVE') == '1'
previous_attempt = int(data.get('attempt') or 0)
attempt = max(1, previous_attempt) if resuming else previous_attempt + 1
resume_count = int(data.get('resume_count') or 0) + (1 if resuming else 0)
data.update({
    'state': 'active',
    'attempt': attempt,
    'resume_count': resume_count,
    'started_at': data.get('started_at') or datetime.datetime.now(datetime.timezone.utc).isoformat(),
    'heartbeat_at': datetime.datetime.now(datetime.timezone.utc).isoformat(),
    'last_error': None,
})
if resuming:
    data['resumed_at'] = datetime.datetime.now(datetime.timezone.utc).isoformat()
tmp = path.with_name('.' + path.name + '.tmp')
tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True), encoding='utf-8')
os.replace(tmp, path)
print(attempt)
PYATTEMPT
)"
    LOCAL_CANDIDATE_RESUME_COUNT="$(json_field_from_file "$LOCAL_CANDIDATE_PENDING_FILE" resume_count 2>/dev/null || true)"
    [[ "$LOCAL_CANDIDATE_RESUME_COUNT" =~ ^[0-9]+$ ]] || LOCAL_CANDIDATE_RESUME_COUNT=0
    started_at_iso="$(json_field_from_file "$LOCAL_CANDIDATE_PENDING_FILE" started_at 2>/dev/null || true)"
    LOCAL_CANDIDATE_UPDATER_STARTED_MS="$(python3 - "$started_at_iso" <<'PYSTARTMS' 2>/dev/null || echo 0
import datetime, sys
raw = (sys.argv[1] or '').strip()
try:
    dt = datetime.datetime.fromisoformat(raw.replace('Z', '+00:00'))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    print(max(0, int(dt.timestamp() * 1000)))
except Exception:
    print(0)
PYSTARTMS
)"
    [[ "$LOCAL_CANDIDATE_UPDATER_STARTED_MS" =~ ^[0-9]+$ ]] || LOCAL_CANDIDATE_UPDATER_STARTED_MS=0
  fi

  CHANGED_FILES_RAW="$(python3 - "$manifest" <<'PYFILES'
import json, sys
try:
    data = json.load(open(sys.argv[1], encoding='utf-8'))
except Exception:
    raise SystemExit(1)
for item in data.get('changed_files') or []:
    item = str(item).strip()
    if item:
        print(item)
PYFILES
)"
  CHANGED_DIFF_NUMSTAT_RAW="$(python3 - "$manifest" <<'PYDIFF'
import json, sys
try:
    data = json.load(open(sys.argv[1], encoding='utf-8'))
except Exception:
    raise SystemExit(1)
for item in ((data.get('diff_stats') or {}).get('entries') or []):
    path = str(item.get('path') or '').strip()
    if not path:
        continue
    if item.get('binary'):
        print(f'-\t-\t{path}')
    else:
        print(f"{int(item.get('added') or 0)}\t{int(item.get('removed') or 0)}\t{path}")
PYDIFF
)"
  zip_progress_hydrate_from_candidate || true
  return 0
}
verify_local_candidate_integrity() {
  (( LOCAL_CANDIDATE_MODE == 1 )) || return 1
  local py output rc max_age
  py="$(current_bot_python_bin)"
  [[ -n "$py" ]] || { LOCAL_CANDIDATE_VERIFY_ERROR="Python indisponível para validar o candidato"; return 1; }
  max_age="${DISCORD_AUTO_UPDATE_CANDIDATE_MAX_AGE_SECONDS:-86400}"
  [[ "$max_age" =~ ^[0-9]+$ ]] || max_age=86400
  set +e
  output="$(cd "$REPO_DIR" && sudo -u ubuntu -H "$py" -m updater.utilitarios.seguranca verify-candidate "$LOCAL_CANDIDATE_DIR" --max-age-seconds "$max_age" 2>&1)"
  rc=$?
  set -e
  if (( rc == 0 )); then
    LOCAL_CANDIDATE_VERIFY_ERROR=""
    logger -t "$LOG_TAG" "Integridade confirmada para $LOCAL_CANDIDATE_DISPLAY_ID: $output"
    return 0
  fi
  LOCAL_CANDIDATE_VERIFY_ERROR="$(VERIFY_OUTPUT="$output" python3 - <<'PYVERIFY'
import json, os
raw = os.environ.get('VERIFY_OUTPUT') or ''
for line in reversed(raw.splitlines()):
    try:
        data = json.loads(line)
    except Exception:
        continue
    if isinstance(data, dict) and data.get('error'):
        print(str(data['error'])[:900])
        break
else:
    print(raw.strip()[-900:] or 'falha de integridade sem detalhes')
PYVERIFY
)"
  return 1
}

update_local_candidate_heartbeat() {
  (( LOCAL_CANDIDATE_MODE == 1 )) || return 0
  [[ -f "${LOCAL_CANDIDATE_PENDING_FILE:-}" ]] || return 0
  local state_name="${1:-active}"
  local error_text="${2:-}"
  local stage_name="${3:-}"
  STATE_NAME="$state_name" ERROR_TEXT="$error_text" STAGE_NAME="$stage_name" python3 - "$LOCAL_CANDIDATE_PENDING_FILE" <<'PYHEART' 2>/dev/null || true
import datetime, json, os, pathlib, sys
path = pathlib.Path(sys.argv[1])
try:
    data = json.loads(path.read_text(encoding='utf-8'))
except Exception:
    data = {}
data['state'] = os.environ.get('STATE_NAME') or 'active'
data['heartbeat_at'] = datetime.datetime.now(datetime.timezone.utc).isoformat()
if os.environ.get('STAGE_NAME'):
    data['stage'] = os.environ['STAGE_NAME'][:300]
if os.environ.get('ERROR_TEXT'):
    data['last_error'] = os.environ['ERROR_TEXT'][:1200]
tmp = path.with_name('.' + path.name + '.tmp')
tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True), encoding='utf-8')
os.replace(tmp, path)
PYHEART
}

archive_local_candidate() {
  local status="${1:-done}" queue_archive_dir archived_candidate_dir stamp
  case "$status" in
    done) queue_archive_dir="$CANDIDATE_QUEUE_DONE_DIR" ;;
    cancelled) queue_archive_dir="$CANDIDATE_QUEUE_CANCELLED_DIR" ;;
    *) status="failed"; queue_archive_dir="$CANDIDATE_QUEUE_FAILED_DIR" ;;
  esac
  if [[ -z "${LOCAL_CANDIDATE_DIR:-}" ]]; then
    return 0
  fi
  stamp="$(date +%Y%m%d%H%M%S)"
  archived_candidate_dir="$CANDIDATE_ROOT/$status/$(basename "$LOCAL_CANDIDATE_DIR").$stamp"
  mkdir -p "$CANDIDATE_ROOT/$status" "$queue_archive_dir" 2>/dev/null || true
  chown ubuntu:ubuntu "$CANDIDATE_ROOT" "$CANDIDATE_ROOT/$status" "$queue_archive_dir" 2>/dev/null || true
  chmod 0775 "$CANDIDATE_ROOT" "$CANDIDATE_ROOT/$status" "$queue_archive_dir" 2>/dev/null || true

  update_local_candidate_heartbeat "$status" "${LOCAL_CANDIDATE_VERIFY_ERROR:-}" || true
  # Artefatos READY são cache de build por commit e não fazem parte do histórico
  # do candidato. Remova-os antes de arquivar; dependency layers ficam no cache
  # global e são preservados somente enquanto houver referência ou retenção.
  if [[ -d "$LOCAL_CANDIDATE_DIR/runtime-artifacts" && ! -L "$LOCAL_CANDIDATE_DIR/runtime-artifacts" ]]; then
    rm -rf -- "$LOCAL_CANDIDATE_DIR/runtime-artifacts" 2>/dev/null || true
  fi
  if [[ -f "${LOCAL_CANDIDATE_PENDING_FILE:-}" ]]; then
    ARCHIVE_STATUS="$status" ARCHIVED_CANDIDATE_DIR="$archived_candidate_dir" python3 - "$LOCAL_CANDIDATE_PENDING_FILE" <<'PYARCHIVEQUEUE' 2>/dev/null || true
import datetime, json, os, pathlib, sys
path = pathlib.Path(sys.argv[1])
try:
    data = json.loads(path.read_text(encoding='utf-8'))
except Exception:
    data = {}
data.update({
    'state': os.environ.get('ARCHIVE_STATUS') or 'failed',
    'archived_at': datetime.datetime.now(datetime.timezone.utc).isoformat(),
    'archived_candidate_dir': os.environ.get('ARCHIVED_CANDIDATE_DIR') or '',
})
tmp = path.with_name('.' + path.name + '.tmp')
tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True), encoding='utf-8')
os.replace(tmp, path)
PYARCHIVEQUEUE
    mv "$LOCAL_CANDIDATE_PENDING_FILE" "$queue_archive_dir/$(basename "$LOCAL_CANDIDATE_PENDING_FILE").$stamp" 2>/dev/null || rm -f "$LOCAL_CANDIDATE_PENDING_FILE" 2>/dev/null || true
  fi
  if [[ -d "$LOCAL_CANDIDATE_DIR" ]]; then
    mv "$LOCAL_CANDIDATE_DIR" "$archived_candidate_dir" 2>/dev/null || rm -rf "$LOCAL_CANDIDATE_DIR" 2>/dev/null || true
    [[ -d "$archived_candidate_dir" ]] && touch "$archived_candidate_dir" 2>/dev/null || true
  fi
  refresh_pending_queue_messages || true
}

local_candidate_suspicion_reason() {
  (( LOCAL_CANDIDATE_MODE == 1 )) || return 1
  [[ "${DISCORD_AUTO_UPDATE_ALLOW_FULL_REPO_ZIP:-0}" == "1" ]] && return 1
  [[ -f "${LOCAL_CANDIDATE_DIR:-}/manifest.json" ]] || return 1
  python3 - "$LOCAL_CANDIDATE_DIR/manifest.json" <<'PYSUSPECT'
import json, pathlib, sys
try:
    data = json.loads(pathlib.Path(sys.argv[1]).read_text(encoding='utf-8'))
except Exception:
    raise SystemExit(1)
zip_name = str(data.get('zip_name') or '').strip().lower()
changed = [str(x).strip() for x in (data.get('changed_files') or []) if str(x).strip()]
protected_prefixes = (
    '.git/', '.github/workflows/', 'data/', 'logs/', 'node_modules/',
    'secrets/', 'google-credentials', 'youtube-cookies',
)
safe_env_templates = {'.env.example', '.env.sample', '.env.template'}
reasons = []
if zip_name.startswith('repo-') or zip_name.startswith('tts-bot-main') or zip_name.startswith('tts-bot-base'):
    reasons.append('o arquivo parece uma base completa, não um patch')
if len(changed) > 120:
    reasons.append(f'muitos arquivos alterados para um patch normal ({len(changed)})')
for path in changed:
    low = path.lower()
    parts = pathlib.PurePosixPath(path.replace('\\', '/')).parts
    if any(part != part.strip() for part in parts):
        reasons.append(f'caminho suspeito ou inválido: {path}')
        break
    basename = parts[-1].lower() if parts else ''
    protected_env = basename == '.env' or (basename.startswith('.env.') and basename not in safe_env_templates)
    if protected_env or low.startswith(protected_prefixes) or '/node_modules/' in low or '/.git/' in low:
        reasons.append(f'caminho protegido/suspeito no ZIP: {path}')
        break
lockfiles = [p for p in changed if p.endswith('package-lock.json')]
if len(lockfiles) >= 2 and len(changed) > 20:
    reasons.append('parece conter árvore de projeto/frontend completa')
if reasons:
    print('; '.join(dict.fromkeys(reasons)))
PYSUSPECT
}

reject_local_candidate_safely() {
  local title="${1:-Atualização bloqueada}"
  local summary="${2:-O candidato foi arquivado sem alterar a VPS.}"
  local reason="${3:-candidato rejeitado}"
  MANUAL_FAILURE_ALERT_SENT=1
  trap - ERR
  set +e
  local rejection_stage="${STAGE:-candidato rejeitado}"
  FAILED_STAGE="$rejection_stage"
  LAST_ERROR_EXIT_CODE="${LAST_ERROR_EXIT_CODE:-1}"
  [[ "$LAST_ERROR_EXIT_CODE" =~ ^[0-9]+$ ]] || LAST_ERROR_EXIT_CODE=1
  (( LAST_ERROR_EXIT_CODE == 0 )) && LAST_ERROR_EXIT_CODE=1
  LAST_ERROR_COMMAND="${LAST_ERROR_COMMAND:-reject_local_candidate_safely}"
  LAST_ERROR_SERVICE_UNIT="${LAST_ERROR_SERVICE_UNIT:-$UPDATER_UNIT}"
  LAST_ERROR_STDERR="${LAST_ERROR_STDERR:-$reason}"
  LAST_ERROR_LOGS="${LAST_ERROR_LOGS:-$(collect_journal_excerpt "$LAST_ERROR_SERVICE_UNIT")}"
  LAST_ERROR_CODE="${LAST_ERROR_CODE:-$(classify_failure_code "$rejection_stage" "$LAST_ERROR_COMMAND" "$LAST_ERROR_STDERR")}"
  [[ "$LAST_ERROR_CODE" != "UPDATE_STAGE_FAILED" ]] || LAST_ERROR_CODE="CANDIDATE_REJECTED"
  persist_primary_failure "manual" "reject_local_candidate_safely" || true
  STAGE="candidato rejeitado"
  normalize_changed_file_permissions "antes de restaurar candidato rejeitado" || true
  if (( UPDATE_APPLIED == 1 )); then
    cleanup_local_candidate_new_files_after_reset
    repo_git reset --hard "${PREVIOUS_COMMIT:-HEAD}" >/dev/null 2>&1 || true
    cleanup_local_candidate_new_files_after_reset
  else
    discard_local_candidate_worktree || true
  fi
  update_local_candidate_heartbeat "failed" "$reason" || true
  notify_zip_status_message "error" "$title" "$summary" || true
  archive_local_candidate "failed"
  send_error "$title" "Resumo: $summary
Motivo: $reason
Candidato: ${LOCAL_CANDIDATE_ID:-desconhecido}
ZIP: ${LOCAL_CANDIDATE_ZIP_NAME:-desconhecido}
Commit preservado: $(short_commit "${PREVIOUS_COMMIT:-$CURRENT_COMMIT}")
Código de falha: ${LAST_ERROR_CODE:-CANDIDATE_REJECTED}
Evidência primária: ${UPDATE_FAILURE_FILE:-não persistida}
Arquivos:
$(format_changed_files)
Hora: $(date '+%d/%m/%Y %H:%M:%S')"
  logger -t "$LOG_TAG" "Candidato local ${LOCAL_CANDIDATE_ID:-desconhecido} rejeitado: $reason"
  exit 0
}

local_candidate_base_conflict_reason() {
  (( LOCAL_CANDIDATE_MODE == 1 )) || return 1
  [[ -n "${LOCAL_CANDIDATE_BASE_COMMIT//[[:space:]]/}" ]] || return 1
  [[ -n "${REMOTE_COMMIT//[[:space:]]/}" ]] || return 1
  [[ "$LOCAL_CANDIDATE_BASE_COMMIT" != "$REMOTE_COMMIT" ]] || return 1

  if ! repo_git cat-file -e "$LOCAL_CANDIDATE_BASE_COMMIT^{commit}" 2>/dev/null; then
    printf 'base original do ZIP não existe mais no repositório local'
    return 0
  fi
  if ! repo_git merge-base --is-ancestor "$LOCAL_CANDIDATE_BASE_COMMIT" "$REMOTE_COMMIT" 2>/dev/null; then
    printf 'base original do ZIP não é ancestral do GitHub atual'
    return 0
  fi

  local remote_changed
  if ! remote_changed="$(repo_git diff --name-only "$LOCAL_CANDIDATE_BASE_COMMIT" "$REMOTE_COMMIT" -- 2>/dev/null)"; then
    printf 'não foi possível comparar a base original do ZIP com o GitHub atual'
    return 0
  fi
  CANDIDATE_CHANGED="$CHANGED_FILES_RAW" REMOTE_CHANGED="$remote_changed" python3 - <<'PYBASE'
import os
candidate = {line.strip() for line in os.environ.get('CANDIDATE_CHANGED', '').splitlines() if line.strip()}
remote = {line.strip() for line in os.environ.get('REMOTE_CHANGED', '').splitlines() if line.strip()}
conflicts = sorted(candidate & remote)
if conflicts:
    shown = ', '.join(conflicts[:8])
    if len(conflicts) > 8:
        extra = len(conflicts) - 8
        shown += f', +{extra} ' + ('arquivo' if extra == 1 else 'arquivos')
    print(f'conflito com update anterior em: {shown}')
PYBASE
}

local_candidate_queue_has_pending() {
  find "$CANDIDATE_QUEUE_PENDING_DIR" -maxdepth 1 -type f -name '*.json' 2>/dev/null | grep -q .
}

trigger_updater_if_queue_pending() {
  if local_candidate_queue_has_pending; then
    (sleep 2; systemctl start --no-block "$UPDATER_UNIT" >/dev/null 2>&1 || true) &
  fi
}

refresh_pending_queue_messages() {
  [[ -d "$CANDIDATE_QUEUE_PENDING_DIR" ]] || return 0
  local payload
  while IFS= read -r payload; do
    [[ -n "${payload//[[:space:]]/}" ]] || continue
    send_update_status_payload "$payload" 0
  done < <(CANDIDATE_QUEUE_PENDING_DIR="$CANDIDATE_QUEUE_PENDING_DIR" CANDIDATE_QUEUE_ACTIVE_DIR="$CANDIDATE_QUEUE_ACTIVE_DIR" python3 - <<'PYQUEUESTATUS' 2>/dev/null || true
import datetime, json, os, pathlib
pending_root = pathlib.Path(os.environ['CANDIDATE_QUEUE_PENDING_DIR'])
active_root = pathlib.Path(os.environ['CANDIDATE_QUEUE_ACTIVE_DIR'])
active_count = sum(1 for p in active_root.glob('*.json') if p.is_file()) if active_root.is_dir() else 0
for index, queue_path in enumerate(sorted(pending_root.glob('*.json')), start=1):
    try:
        queue = json.loads(queue_path.read_text(encoding='utf-8'))
        candidate_dir = pathlib.Path(str(queue.get('candidate_dir') or ''))
        manifest = json.loads((candidate_dir / 'manifest.json').read_text(encoding='utf-8'))
        status = manifest.get('discord_status') or {}
        channel_id = str(status.get('channel_id') or '')
        message_id = str(status.get('message_id') or '')
        candidate_id = str(manifest.get('id') or queue.get('id') or '')
        display_id = str(manifest.get('display_id') or queue.get('display_id') or candidate_id)
        if not channel_id or not message_id or not candidate_id:
            continue
        position = active_count + index
        diff = manifest.get('diff_stats') or {}
        count = len(manifest.get('changed_files') or [])
        summary = str(diff.get('summary') or '').strip()
        position_at_enqueue = int(queue.get('queue_position_at_enqueue') or position)
        queue.update({
            'state': 'queued',
            'current_position': position,
            'position_updated_at': datetime.datetime.now(datetime.timezone.utc).isoformat(),
        })
        tmp = queue_path.with_name('.' + queue_path.name + '.tmp')
        tmp.write_text(json.dumps(queue, ensure_ascii=False, indent=2, sort_keys=True), encoding='utf-8')
        os.replace(tmp, queue_path)

        # O primeiro candidato recém-preparado já está com a animação ativa no
        # Discord. Não substitua esse painel por uma fila que não existe.
        if position == 1 and active_count == 0 and position_at_enqueue <= 1:
            continue

        file_line = (f'1 arquivo preparado' if count == 1 else f'{count} arquivos preparados') if count else ''
        if file_line and summary:
            file_line += f' · {summary}'

        if position == 1 and active_count == 0:
            lines = [f'Atualização `{display_id}`', 'Iniciando agora.']
            if file_line:
                lines.append(f'-# {file_line}')
            payload_status = 'applying'
            payload_title = '⚙️ Iniciando atualização'
        else:
            detail = f'1 atualização antes desta.' if position == 2 else f'{position - 1} atualizações antes desta.'
            lines = [f'Atualização `{display_id}`', f'Posição na fila: **{position}**', detail]
            if file_line:
                lines.append(f'-# {file_line}')
            payload_status = 'queued'
            payload_title = '📦 Atualização na fila'

        payload = {
            'channel_id': channel_id,
            'message_id': message_id,
            'status': payload_status,
            'title': payload_title,
            'description': '\n'.join(lines),
            'candidate_id': candidate_id,
            'display_id': display_id,
            'event_at': datetime.datetime.now(datetime.timezone.utc).isoformat(),
            'control': {'enabled': True, 'mode': 'cancel', 'candidate_id': candidate_id},
        }
        print(json.dumps(payload, ensure_ascii=False, separators=(',', ':')))
    except Exception:
        continue
PYQUEUESTATUS
  )
}

