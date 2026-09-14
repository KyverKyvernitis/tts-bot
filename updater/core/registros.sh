#!/usr/bin/env bash
# Módulo carregado por atualizar.sh; não executar diretamente.
# Registro, evidências, incidentes e entrega de alertas.

clear_update_runtime_state() {
  [[ -f "$UPDATE_RUNTIME_STATE_FILE" ]] || return 0
  UPDATE_RUNTIME_RUN_ID_VALUE="$UPDATE_RUNTIME_RUN_ID" \
  UPDATE_RUNTIME_STATE_FILE_VALUE="$UPDATE_RUNTIME_STATE_FILE" \
  python3 - <<'PYCLEARUPDATESTATE' 2>/dev/null || true
import json, os, pathlib
path = pathlib.Path(os.environ['UPDATE_RUNTIME_STATE_FILE_VALUE'])
try:
    payload = json.loads(path.read_text(encoding='utf-8'))
except Exception:
    payload = {}
if not isinstance(payload, dict) or payload.get('run_id') == os.environ.get('UPDATE_RUNTIME_RUN_ID_VALUE'):
    try:
        path.unlink()
    except FileNotFoundError:
        pass
PYCLEARUPDATESTATE
}

cleanup_runtime_artifacts() {
  clear_update_runtime_state || true
  rm -f "$RUN_LOG_FILE"
  if [[ -n "${REMOTE_WORKTREE_DIR:-}" && -d "$REMOTE_WORKTREE_DIR" ]]; then
    repo_git worktree remove --force "$REMOTE_WORKTREE_DIR" >/dev/null 2>&1 || rm -rf "$REMOTE_WORKTREE_DIR" 2>/dev/null || true
  fi
  if [[ -n "${REMOTE_CANDIDATE_ARTIFACT_ROOT:-}" && -d "$REMOTE_CANDIDATE_ARTIFACT_ROOT" && ! -L "$REMOTE_CANDIDATE_ARTIFACT_ROOT" ]]; then
    # READY é cache de build por commit e pode ser reutilizado numa retomada.
    # Artefato parcial nunca é confiável e deve desaparecer no EXIT.
    if [[ ! -s "$REMOTE_CANDIDATE_ARTIFACT_ROOT/ready.json" ]]; then
      rm -rf -- "$REMOTE_CANDIDATE_ARTIFACT_ROOT" 2>/dev/null || true
    fi
  fi
  if [[ -n "${LOCAL_CANDIDATE_WORKTREE_DIR:-}" && -d "$LOCAL_CANDIDATE_WORKTREE_DIR" ]]; then
    repo_git worktree remove --force "$LOCAL_CANDIDATE_WORKTREE_DIR" >/dev/null 2>&1 || rm -rf "$LOCAL_CANDIDATE_WORKTREE_DIR" 2>/dev/null || true
  fi
  if [[ -n "${UPDATER_RUNTIME_COPY:-}" && -f "$UPDATER_RUNTIME_COPY" ]]; then
    rm -f "$UPDATER_RUNTIME_COPY" 2>/dev/null || true
  fi
}

trim_alert_text() {
  local limit="${1:-4000}"
  # Importante: a versão anterior passava o script Python por heredoc em
  # `python3 -`, então o Python consumia o heredoc como stdin e NÃO lia o pipe.
  # Com `set -o pipefail`, o produtor do pipe recebia SIGPIPE e o updater caía
  # com código 141 durante `git status | trim_alert_text`. Usar `-c` preserva
  # stdin para o texto real e torna o helper seguro para pipelines.
  python3 -c 'import sys
limit = int(sys.argv[1])
text = sys.stdin.read().replace("\r\n", "\n").replace("\r", "\n").strip()
if text and len(text) > limit:
    text = text[: max(0, limit - 1)].rstrip() + "…"
if text:
    sys.stdout.write(text + "\n")
' "$limit" || true
}

sanitize_update_component() {
  local raw="${1:-update}"
  printf '%s' "$raw" | tr -cs '[:alnum:]._-' '_' | sed -E 's/^_+//; s/_+$//' | cut -c1-96
}

update_incident_identifier() {
  local value="${LOCAL_CANDIDATE_DISPLAY_ID:-${ROLLBACK_REQUEST_ID:-}}"
  if [[ -z "${value//[[:space:]]/}" ]]; then
    if [[ -n "${REMOTE_COMMIT:-}" ]]; then
      value="UPD-$(short_commit "$REMOTE_COMMIT" | tr '[:lower:]' '[:upper:]')"
    else
      value="RUN-${UPDATE_RUNTIME_RUN_ID:-$$}"
    fi
  fi
  sanitize_update_component "$value"
}

ensure_update_incident_dir() {
  if [[ -n "${UPDATE_INCIDENT_DIR:-}" && -d "$UPDATE_INCIDENT_DIR" ]]; then
    return 0
  fi
  local update_id run_id
  update_id="$(update_incident_identifier)"
  run_id="$(sanitize_update_component "${UPDATE_RUNTIME_RUN_ID:-run-$$}")"
  [[ -n "$update_id" ]] || update_id="update"
  [[ -n "$run_id" ]] || run_id="run-$$"
  UPDATE_INCIDENT_DIR="$UPDATE_INCIDENT_ROOT/$update_id/$run_id"
  UPDATE_FAILURE_FILE="$UPDATE_INCIDENT_DIR/failure.json"
  UPDATE_ROLLBACK_FAILURE_FILE="$UPDATE_INCIDENT_DIR/rollback_failure.json"
  mkdir -p "$UPDATE_INCIDENT_DIR" 2>/dev/null || return 1
  chmod 0755 "$UPDATE_INCIDENT_ROOT" "$(dirname "$UPDATE_INCIDENT_DIR")" "$UPDATE_INCIDENT_DIR" 2>/dev/null || true
  chown ubuntu:ubuntu "$UPDATE_INCIDENT_ROOT" "$(dirname "$UPDATE_INCIDENT_DIR")" "$UPDATE_INCIDENT_DIR" 2>/dev/null || true
  return 0
}

stage_log_file_for() {
  local stage_name="${1:-${STAGE:-stage}}" slug
  ensure_update_incident_dir || return 1
  slug="$(sanitize_update_component "$stage_name")"
  [[ -n "$slug" ]] || slug="stage"
  printf '%s/%s.log' "$UPDATE_INCIDENT_DIR" "$slug"
}

collect_smart_error_excerpt() {
  local source_file="${1:-$RUN_LOG_FILE}"
  [[ -f "$source_file" ]] || return 0
  python3 - "$source_file" <<'PYERRORSNIP' 2>/dev/null || true
import pathlib, re, sys
path = pathlib.Path(sys.argv[1])
try:
    text = path.read_text(encoding='utf-8', errors='replace')
except Exception:
    raise SystemExit
text = re.sub(r'\x1b\[[0-9;]*[A-Za-z]', '', text).replace('\r\n', '\n').replace('\r', '\n')
lines = text.splitlines()
patterns = [
    re.compile(r'error TS\d+:', re.I),
    re.compile(r'^npm ERR!', re.I),
    re.compile(r'Traceback \(most recent call last\):'),
    re.compile(r'\bfatal:', re.I),
    re.compile(r'\b(?:SyntaxError|IndentationError|TabError|ImportError|ModuleNotFoundError|CalledProcessError)\b'),
    re.compile(r'\bpermission denied\b', re.I),
    re.compile(r'\bEUSAGE\b', re.I),
    re.compile(r'(^|[\s:])(?:FAILED|ERROR)(?:[\s:]|$)', re.I),
]
match_index = None
for pattern in patterns:
    for index, line in enumerate(lines):
        if pattern.search(line):
            match_index = index
            break
    if match_index is not None:
        break
if match_index is None:
    selected = lines[-40:]
else:
    start = max(0, match_index - 5)
    end = min(len(lines), match_index + 16)
    selected = lines[start:end]
    tail = lines[-8:]
    if end < len(lines) - 8:
        selected += ['…', '--- final do log ---', *tail]
result = '\n'.join(selected).strip()
if len(result) > 3000:
    result = result[:2999].rstrip() + '…'
if result:
    print(result)
PYERRORSNIP
}

collect_run_log_excerpt() {
  local preferred="${CURRENT_STAGE_LOG_FILE:-}"
  if [[ -n "$preferred" && "${CURRENT_STAGE_LOG_STAGE:-}" == "$STAGE" && -s "$preferred" ]]; then
    collect_smart_error_excerpt "$preferred"
  elif [[ -f "$RUN_LOG_FILE" ]]; then
    collect_smart_error_excerpt "$RUN_LOG_FILE"
  fi
}

classify_failure_code() {
  local stage_lc="${1,,}" command_lc="${2,,}" excerpt_lc="${3,,}"
  if [[ "$stage_lc" == *"dependências do frontend"* ]]; then
    printf 'FRONTEND_NPM_CI_FAILED'
  elif [[ "$stage_lc" == *"testes do frontend"* ]]; then
    printf 'FRONTEND_TEST_FAILED'
  elif [[ "$stage_lc" == *"typecheck do frontend"* ]]; then
    printf 'FRONTEND_TSC_FAILED'
  elif [[ "$stage_lc" == *"build do frontend"* ]]; then
    if [[ "$excerpt_lc" == *"error ts"* ]]; then
      printf 'FRONTEND_TSC_FAILED'
    else
      printf 'FRONTEND_BUILD_FAILED'
    fi
  elif [[ "$stage_lc" == *"publicação do frontend"* ]]; then
    printf 'FRONTEND_PUBLISH_FAILED'
  elif [[ "$stage_lc" == *"dependências do backend"* ]]; then
    printf 'BACKEND_NPM_CI_FAILED'
  elif [[ "$stage_lc" == *"testes do backend"* ]]; then
    printf 'BACKEND_TEST_FAILED'
  elif [[ "$stage_lc" == *"build do backend"* ]]; then
    printf 'BACKEND_BUILD_FAILED'
  elif [[ "$stage_lc" == *"validação de integridade do candidato"* ]]; then
    printf 'CANDIDATE_INTEGRITY_FAILED'
  elif [[ "$stage_lc" == *"validação de segurança do zip"* ]]; then
    printf 'CANDIDATE_SECURITY_REJECTED'
  elif [[ "$stage_lc" == *"aplicação local do candidato"* || "$stage_lc" == *"aplicação do candidato"* ]]; then
    if [[ "$excerpt_lc" == *"permission denied"* ]]; then
      printf 'CANDIDATE_PERMISSION_DENIED'
    else
      printf 'CANDIDATE_APPLY_FAILED'
    fi
  elif [[ "$stage_lc" == *"verificação pós-build do repositório"* ]]; then
    printf 'DIRTY_WORKTREE_AFTER_STAGE'
  elif [[ "$command_lc" == *"git add"* && "$excerpt_lc" == *"permission denied"* ]]; then
    printf 'CANDIDATE_PERMISSION_DENIED'
  elif [[ "$excerpt_lc" == *"error ts"* ]]; then
    printf 'FRONTEND_TSC_FAILED'
  elif [[ "$excerpt_lc" == *"permission denied"* ]]; then
    printf 'PERMISSION_DENIED'
  else
    printf 'UPDATE_STAGE_FAILED'
  fi
}

persist_primary_failure() {
  local failed_line="${1:-?}" failed_function="${2:-main}"
  (( UPDATE_PRIMARY_FAILURE_WRITTEN == 0 )) || return 0
  ensure_update_incident_dir || return 0
  local run_snapshot="$UPDATE_INCIDENT_DIR/run.log" stage_snapshot="${CURRENT_STAGE_LOG_FILE:-}"
  if [[ -f "$RUN_LOG_FILE" ]]; then
    cp -f -- "$RUN_LOG_FILE" "$run_snapshot" 2>/dev/null || true
    chmod 0644 "$run_snapshot" 2>/dev/null || true
  fi
  if [[ -z "$stage_snapshot" || ! -f "$stage_snapshot" || "${CURRENT_STAGE_LOG_STAGE:-}" != "${FAILED_STAGE:-$STAGE}" ]]; then
    stage_snapshot="$run_snapshot"
  fi
  FAILURE_FILE_VALUE="$UPDATE_FAILURE_FILE" \
  FAILURE_CODE_VALUE="${LAST_ERROR_CODE:-UPDATE_STAGE_FAILED}" \
  FAILURE_STAGE_VALUE="${FAILED_STAGE:-$STAGE}" \
  FAILURE_COMMAND_VALUE="${LAST_ERROR_COMMAND:-desconhecido}" \
  FAILURE_EXIT_CODE_VALUE="${LAST_ERROR_EXIT_CODE:-1}" \
  FAILURE_FUNCTION_VALUE="$failed_function" \
  FAILURE_LINE_VALUE="$failed_line" \
  FAILURE_SERVICE_VALUE="${LAST_ERROR_SERVICE_UNIT:-$UPDATER_UNIT}" \
  FAILURE_STDERR_VALUE="${LAST_ERROR_STDERR:-}" \
  FAILURE_JOURNAL_VALUE="${LAST_ERROR_LOGS:-}" \
  FAILURE_RUN_LOG_VALUE="$run_snapshot" \
  FAILURE_STAGE_LOG_VALUE="$stage_snapshot" \
  FAILURE_RUN_ID_VALUE="${UPDATE_RUNTIME_RUN_ID:-}" \
  FAILURE_UPDATE_ID_VALUE="$(update_incident_identifier)" \
  FAILURE_BRANCH_VALUE="$BRANCH" \
  FAILURE_CURRENT_COMMIT_VALUE="${CURRENT_COMMIT:-}" \
  FAILURE_TARGET_COMMIT_VALUE="${REMOTE_COMMIT:-}" \
  python3 - <<'PYFAILURE' 2>/dev/null || true
import datetime, json, os, pathlib
path = pathlib.Path(os.environ['FAILURE_FILE_VALUE'])
payload = {
    'schema_version': 1,
    'kind': 'primary_failure',
    'failure_code': os.environ.get('FAILURE_CODE_VALUE') or 'UPDATE_STAGE_FAILED',
    'stage': os.environ.get('FAILURE_STAGE_VALUE') or '',
    'command': os.environ.get('FAILURE_COMMAND_VALUE') or '',
    'exit_code': int(os.environ.get('FAILURE_EXIT_CODE_VALUE') or 1),
    'function': os.environ.get('FAILURE_FUNCTION_VALUE') or '',
    'line': os.environ.get('FAILURE_LINE_VALUE') or '',
    'service_unit': os.environ.get('FAILURE_SERVICE_VALUE') or '',
    'stderr_excerpt': os.environ.get('FAILURE_STDERR_VALUE') or '',
    'journal_excerpt': os.environ.get('FAILURE_JOURNAL_VALUE') or '',
    'stdout_path': os.environ.get('FAILURE_STAGE_LOG_VALUE') or os.environ.get('FAILURE_RUN_LOG_VALUE') or '',
    'stderr_path': os.environ.get('FAILURE_STAGE_LOG_VALUE') or os.environ.get('FAILURE_RUN_LOG_VALUE') or '',
    'stream_mode': 'merged',
    'run_log_path': os.environ.get('FAILURE_RUN_LOG_VALUE') or '',
    'run_id': os.environ.get('FAILURE_RUN_ID_VALUE') or '',
    'update_id': os.environ.get('FAILURE_UPDATE_ID_VALUE') or '',
    'branch': os.environ.get('FAILURE_BRANCH_VALUE') or '',
    'current_commit': os.environ.get('FAILURE_CURRENT_COMMIT_VALUE') or '',
    'target_commit': os.environ.get('FAILURE_TARGET_COMMIT_VALUE') or '',
    'timestamp': datetime.datetime.now(datetime.timezone.utc).isoformat(),
}
try:
    with path.open('x', encoding='utf-8') as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write('\n')
except FileExistsError:
    pass
PYFAILURE
  if [[ -f "$UPDATE_FAILURE_FILE" ]]; then
    UPDATE_PRIMARY_FAILURE_WRITTEN=1
    chown ubuntu:ubuntu "$UPDATE_FAILURE_FILE" "$run_snapshot" 2>/dev/null || true
    chmod 0444 "$UPDATE_FAILURE_FILE" 2>/dev/null || true
    logger -t "$LOG_TAG" "falha primária preservada em $UPDATE_FAILURE_FILE" 2>/dev/null || true
  fi
}

persist_rollback_failure() {
  local git_status="${1:-desconhecido}" front_status="${2:-não executado}" back_status="${3:-não executado}" bot_status="${4:-não executado}" activity_status="${5:-não executado}" python_status="${6:-não executado}" rollback_log="${7:-}"
  ensure_update_incident_dir || return 0
  ROLLBACK_FAILURE_FILE_VALUE="$UPDATE_ROLLBACK_FAILURE_FILE" \
  ROLLBACK_FAILURE_PRIMARY_FILE_VALUE="$UPDATE_FAILURE_FILE" \
  ROLLBACK_FAILURE_GIT_VALUE="$git_status" \
  ROLLBACK_FAILURE_FRONT_VALUE="$front_status" \
  ROLLBACK_FAILURE_BACK_VALUE="$back_status" \
  ROLLBACK_FAILURE_BOT_VALUE="$bot_status" \
  ROLLBACK_FAILURE_ACTIVITY_VALUE="$activity_status" \
  ROLLBACK_FAILURE_PYTHON_VALUE="$python_status" \
  ROLLBACK_FAILURE_LOG_VALUE="$rollback_log" \
  ROLLBACK_FAILURE_RUN_ID_VALUE="${UPDATE_RUNTIME_RUN_ID:-}" \
  ROLLBACK_FAILURE_UPDATE_ID_VALUE="$(update_incident_identifier)" \
  python3 - <<'PYROLLBACKFAIL' 2>/dev/null || true
import datetime, json, os, pathlib
path = pathlib.Path(os.environ['ROLLBACK_FAILURE_FILE_VALUE'])
payload = {
    'schema_version': 1,
    'kind': 'rollback_failure',
    'primary_failure_path': os.environ.get('ROLLBACK_FAILURE_PRIMARY_FILE_VALUE') or '',
    'git': os.environ.get('ROLLBACK_FAILURE_GIT_VALUE') or '',
    'frontend': os.environ.get('ROLLBACK_FAILURE_FRONT_VALUE') or '',
    'backend': os.environ.get('ROLLBACK_FAILURE_BACK_VALUE') or '',
    'bot': os.environ.get('ROLLBACK_FAILURE_BOT_VALUE') or '',
    'activity_health': os.environ.get('ROLLBACK_FAILURE_ACTIVITY_VALUE') or '',
    'python_runtime': os.environ.get('ROLLBACK_FAILURE_PYTHON_VALUE') or '',
    'rollback_log_path': os.environ.get('ROLLBACK_FAILURE_LOG_VALUE') or '',
    'run_id': os.environ.get('ROLLBACK_FAILURE_RUN_ID_VALUE') or '',
    'update_id': os.environ.get('ROLLBACK_FAILURE_UPDATE_ID_VALUE') or '',
    'timestamp': datetime.datetime.now(datetime.timezone.utc).isoformat(),
}
tmp = path.with_name('.' + path.name + '.tmp')
tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + '\n', encoding='utf-8')
os.replace(tmp, path)
PYROLLBACKFAIL
  chown ubuntu:ubuntu "$UPDATE_ROLLBACK_FAILURE_FILE" "$rollback_log" 2>/dev/null || true
  chmod 0644 "$UPDATE_ROLLBACK_FAILURE_FILE" 2>/dev/null || true
}

service_unit_for_stage() {
  local stage_lc="${1,,}"
  if [[ "$stage_lc" == *"bot"* ]]; then
    printf '%s.service' "$SERVICE"
  else
    printf '%s' "$UPDATER_UNIT"
  fi
}

collect_journal_excerpt() {
  local unit="${1:-$UPDATER_UNIT}"
  local logs
  logs="$(journalctl -u "$unit" -n 40 --no-pager 2>/dev/null | tail -n 25 || true)"
  if [[ -z "${logs//[[:space:]]/}" ]]; then
    logs="nenhum log adicional encontrado"
  fi
  printf '%s' "$logs" | trim_alert_text 1500
}

phone_worker_log_summary_text() {
  if [[ ! -x "$REPO_DIR/scripts/phone-worker-client.py" && ! -f "$REPO_DIR/scripts/phone-worker-client.py" ]]; then
    printf 'indisponível: cliente ausente'
    return 0
  fi
  if ! env_truthy PHONE_WORKER_UPDATE_LOG_SUMMARY_ENABLED && ! env_truthy PHONE_WORKER_ENABLED; then
    printf 'desativado'
    return 0
  fi
  local source_file="${1:-$RUN_LOG_FILE}"
  if [[ ! -s "$source_file" ]]; then
    printf 'sem logs para analisar'
    return 0
  fi
  local timeout_value="4"
  if [[ -f "$REPO_DIR/.env" ]]; then
    timeout_value="$(grep -E '^PHONE_WORKER_UPDATE_LOG_SUMMARY_TIMEOUT_SECONDS=' "$REPO_DIR/.env" 2>/dev/null | tail -n 1 | cut -d= -f2- | tr -d ' "' || true)"
    [[ -n "$timeout_value" ]] || timeout_value="4"
  fi
  local raw summary
  raw="$(sudo -u ubuntu -H python3 "$REPO_DIR/scripts/phone-worker-client.py" log-summary "$source_file" --timeout "$timeout_value" 2>/dev/null || true)"
  if [[ -z "${raw//[[:space:]]/}" ]]; then
    printf 'indisponível: sem resposta'
    return 0
  fi
  summary="$(PHONE_WORKER_RAW="$raw" python3 - <<'PYJSON' 2>/dev/null || true
import json, os
try:
    data = json.loads(os.environ.get('PHONE_WORKER_RAW') or '{}')
except Exception:
    raise SystemExit
counts = data.get('counts') or {}
top = data.get('top_messages') or []
parts = []
for key in ('critical','error','warning','timeout','traceback','exception','failed','lavalink','yt_dlp','rate_limit'):
    value = int(counts.get(key) or 0)
    if value:
        parts.append(f'{key}={value}')
if not parts:
    parts.append('sem padrões críticos')
if top:
    msg = str((top[0] or {}).get('message') or '')[:140]
    cnt = (top[0] or {}).get('count') or 1
    if msg:
        parts.append(f'top({cnt}x): {msg}')
print(' | '.join(parts)[:900])
PYJSON
)"
  if [[ -n "${summary//[[:space:]]/}" ]]; then
    printf '%s' "$summary"
  else
    printf 'indisponível: resposta inválida'
  fi
}

register_error_context() {
  LAST_ERROR_EXIT_CODE="${1:-1}"
  LAST_ERROR_COMMAND="${2:-desconhecido}"
  if [[ "${CURRENT_STAGE_LOG_STAGE:-}" == "$STAGE" && -n "${CURRENT_STAGE_COMMAND//[[:space:]]/}" ]]; then
    LAST_ERROR_COMMAND="$CURRENT_STAGE_COMMAND"
  fi
  LAST_ERROR_SERVICE_UNIT="$(service_unit_for_stage "$STAGE")"
  LAST_ERROR_STDERR="$(collect_run_log_excerpt)"
  LAST_ERROR_LOGS="$(collect_journal_excerpt "$LAST_ERROR_SERVICE_UNIT")"
  if [[ -z "${LAST_ERROR_STDERR//[[:space:]]/}" ]]; then
    LAST_ERROR_STDERR="nenhuma saída adicional capturada"
  fi
  LAST_ERROR_CODE="$(classify_failure_code "$STAGE" "$LAST_ERROR_COMMAND" "$LAST_ERROR_STDERR")"
}

LAST_ERROR_EXIT_CODE=""
LAST_ERROR_COMMAND=""
LAST_ERROR_SERVICE_UNIT=""
LAST_ERROR_STDERR=""
LAST_ERROR_LOGS=""
LAST_ERROR_LINE=""
LAST_ERROR_FUNCTION=""

prepare_update_delivery_dirs() {
  mkdir -p "$UPDATE_STATUS_OUTBOX_DIR" "$UPDATE_ALERT_OUTBOX_DIR" "$UPDATE_DELIVERY_RECEIPTS_DIR" 2>/dev/null || return 1
  chown ubuntu:ubuntu "$UPDATE_STATUS_OUTBOX_DIR" "$UPDATE_ALERT_OUTBOX_DIR" "$UPDATE_DELIVERY_RECEIPTS_DIR" 2>/dev/null || true
  chmod 0775 "$UPDATE_STATUS_OUTBOX_DIR" "$UPDATE_ALERT_OUTBOX_DIR" "$UPDATE_DELIVERY_RECEIPTS_DIR" 2>/dev/null || true
}

send_alert_reliably() {
  local alert_type="${1:-info}"
  local title="${2:-Auto update}"
  local body="${3:-}"
  local attach="${4:-}"
  local attach_name="${5:-}"
  local event_id="${6:-}"
  local receipt="" receipt_safe=""

  prepare_update_delivery_dirs || true
  if [[ -n "${event_id//[[:space:]]/}" ]]; then
    receipt_safe="$(printf '%s' "$event_id" | tr -c 'A-Za-z0-9._-' '_' | cut -c1-120)"
    receipt="$UPDATE_DELIVERY_RECEIPTS_DIR/${receipt_safe}.alert.done"
    [[ -f "$receipt" ]] && return 0
  fi

  # alert.sh não envia mais webhook. Ele é somente o produtor durável da fila
  # que o próprio bot consome no canal técnico de logs. O sexto argumento torna
  # eventos finais idempotentes entre retry/restart do updater.
  if sudo -u ubuntu /usr/bin/env \
      REPO_DIR="$REPO_DIR" \
      UPDATE_ALERT_OUTBOX_DIR="$UPDATE_ALERT_OUTBOX_DIR" \
      bash "$REPO_DIR/alert.sh" "$alert_type" "$title" "$body" "$attach" "$attach_name" "$event_id"; then
    logger -t "$LOG_TAG" "log técnico enfileirado para entrega pelo bot: ${event_id:-$title}" 2>/dev/null || true
    return 0
  fi

  logger -t "$LOG_TAG" "falha ao persistir log técnico: ${event_id:-$title}" 2>/dev/null || true
  return 1
}

send_info() {
  local title="${1:-Auto update}"
  local body="${2:-}"
  send_alert_reliably info "$title" "$body" "" "" "${3:-}" || true
}

send_warn() {
  local title="${1:-Auto update}"
  local body="${2:-}"
  send_alert_reliably warn "$title" "$body" "" "" "${3:-}" || true
}

send_error() {
  local title="${1:-Falha no auto update}"
  local body="${2:-}"
  local attach=""
  if [[ -f "$RUN_LOG_FILE" && -s "$RUN_LOG_FILE" ]]; then
    attach="$RUN_LOG_FILE"
  fi
  local incident_id="${LOCAL_CANDIDATE_ID:-${ROLLBACK_REQUEST_ID:-${REMOTE_COMMIT:-${CURRENT_COMMIT:-}}}}"
  [[ -n "${incident_id//[[:space:]]/}" ]] || incident_id="$(date +%Y%m%d%H%M%S)-$$"
  local event_id="${3:-error-${incident_id}-${FAILED_STAGE:-$STAGE}}"
  send_alert_reliably error "$title" "$body" "$attach" "tts-bot-updater.log" "$event_id" || true
}

