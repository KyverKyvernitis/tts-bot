#!/usr/bin/env bash
set -Eeuo pipefail

REPO_DIR="/home/ubuntu/bot"
BRANCH="main"
SERVICE="tts-bot"
LAVALINK_SERVICE="lavalink"
LOG_TAG="tts-bot-updater"
DIRTY_MARKER_FILE="$REPO_DIR/.fatal-update-dirty"
LOCAL_CHANGES_MARKER_FILE="$REPO_DIR/.fatal-update-local-changes"
CANDIDATE_ROOT="${DISCORD_AUTO_UPDATE_STAGING_DIR:-$(dirname "$REPO_DIR")/bot-update-staging}/candidates"
# Formato antigo: um único pending.json. Mantemos leitura por compatibilidade,
# mas novos ZIPs entram em queue/pending/*.json para não sobrescrever candidatos.
CANDIDATE_PENDING_FILE="$CANDIDATE_ROOT/pending.json"
CANDIDATE_QUEUE_ROOT="${DISCORD_AUTO_UPDATE_QUEUE_DIR:-$CANDIDATE_ROOT/queue}"
CANDIDATE_QUEUE_PENDING_DIR="$CANDIDATE_QUEUE_ROOT/pending"
CANDIDATE_QUEUE_ACTIVE_DIR="$CANDIDATE_QUEUE_ROOT/active"
CANDIDATE_QUEUE_DONE_DIR="$CANDIDATE_QUEUE_ROOT/done"
CANDIDATE_QUEUE_FAILED_DIR="$CANDIDATE_QUEUE_ROOT/failed"
CANDIDATE_QUEUE_CANCELLED_DIR="$CANDIDATE_QUEUE_ROOT/cancelled"
UPDATE_RUNTIME_STATE_FILE="${DISCORD_AUTO_UPDATE_RUNTIME_STATE_FILE:-$CANDIDATE_ROOT/runtime-state.json}"
UPDATER_LOCK_FILE="${DISCORD_AUTO_UPDATE_LOCK_FILE:-/run/lock/tts-bot-updater.lock}"
REMOTE_REJECTED_FILE="${DISCORD_AUTO_UPDATE_REJECTED_REMOTE_FILE:-$REPO_DIR/data/updater/rejected_remote_commits.json}"
REMOTE_FETCH_STATE_FILE="${TTS_BOT_REMOTE_FETCH_STATE_FILE:-$CANDIDATE_ROOT/remote-fetch-state.tsv}"
# Reuso curto apenas para ZIP local. O polling remoto continua fazendo fetch real
# para não atrasar a descoberta de commits novos no GitHub.
LOCAL_FETCH_REUSE_SECONDS="${TTS_BOT_LOCAL_FETCH_REUSE_SECONDS:-15}"
REMOTE_FETCH_REUSED=0
ROLLBACK_REQUEST_DEFAULT_ROOT="$CANDIDATE_ROOT/rollback"
ROLLBACK_REQUEST_DATA_ROOT="${DISCORD_AUTO_UPDATE_ROLLBACK_REQUEST_DIR:-$REPO_DIR/data/runtime/update-rollback}"
ROLLBACK_REQUEST_TMP_ROOT="${TMPDIR:-/tmp}/tts-bot-update-rollback"
ROLLBACK_REQUEST_ROOT="$ROLLBACK_REQUEST_DEFAULT_ROOT"
ROLLBACK_REQUEST_PENDING_FILE="$ROLLBACK_REQUEST_ROOT/pending.json"
ROLLBACK_REQUEST_ACTIVE_FILE="$ROLLBACK_REQUEST_ROOT/active.json"

# Diretório canônico dos módulos. A cópia runtime temporária herda este caminho
# para carregar módulos da mesma revisão antes de qualquer promoção do checkout.
UPDATER_SOURCE_DIR="${TTS_BOT_UPDATER_SOURCE_DIR:-}"
if [[ -z "$UPDATER_SOURCE_DIR" ]]; then
  UPDATER_SOURCE_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
fi
export TTS_BOT_UPDATER_SOURCE_DIR="$UPDATER_SOURCE_DIR"

# O updater pode substituir updater/core/atualizar.sh enquanto ele mesmo está
# rodando. Bash lê partes do arquivo sob demanda; se o arquivo for alterado no
# meio da execução, o processo pode continuar lendo uma versão diferente e
# quebrar com variáveis antigas/novas fora de sincronia. Por isso o processo
# real sempre roda a partir de uma cópia temporária estável.
if [[ "${TTS_BOT_UPDATER_RUNNING_COPY:-0}" != "1" ]]; then
  UPDATER_RUNTIME_BASE="${TTS_BOT_UPDATER_RUNTIME_DIR:-${TMPDIR:-/tmp}}"
  if [[ "$UPDATER_RUNTIME_BASE" != /* || ! -d "$UPDATER_RUNTIME_BASE" || -L "$UPDATER_RUNTIME_BASE" || ! -w "$UPDATER_RUNTIME_BASE" ]]; then
    UPDATER_RUNTIME_BASE="/tmp"
  fi
  # mktemp evita colisão/symlink previsível em /tmp. No serviço systemd, a
  # cópia fica em RuntimeDirectory e é removida pelo próprio systemd mesmo em
  # SIGKILL, reboot ou queda antes do trap EXIT.
  UPDATER_RUNTIME_COPY="$(mktemp "$UPDATER_RUNTIME_BASE/tts-bot-update.XXXXXX.run")"
  cp -- "$0" "$UPDATER_RUNTIME_COPY"
  chmod 0700 "$UPDATER_RUNTIME_COPY" 2>/dev/null || true
  export TTS_BOT_UPDATER_RUNNING_COPY=1
  export TTS_BOT_UPDATER_RUNTIME_COPY
  exec /usr/bin/env bash "$UPDATER_RUNTIME_COPY" "$@"
fi
UPDATER_RUNTIME_COPY="${TTS_BOT_UPDATER_RUNTIME_COPY:-}"

# Perfil conservador padrão: protege heartbeat/voz do bot na VPS pequena.
# Trechos curtos de Git/worktree usam um perfil moderado temporário; antes de
# testes, builds e snapshots pesados voltamos sempre ao perfil conservador.
# módulo: configuracao.sh
. "$UPDATER_SOURCE_DIR/configuracao.sh"

mkdir -p "$(dirname "$UPDATER_LOCK_FILE")" 2>/dev/null || true
exec 9>"$UPDATER_LOCK_FILE"
if ! flock -n 9; then
  logger -t "$LOG_TAG" "updater já está em execução; mantendo fila para o próximo ciclo" 2>/dev/null || true
  exit 0
fi
UPDATE_RUNTIME_RUN_ID="$(date +%Y%m%d%H%M%S)-$$-${RANDOM:-0}"

FRONT_DIR="$REPO_DIR/dashboard/frontend"
BACK_DIR="$REPO_DIR/dashboard/backend"
FRONT_PUBLISH_DIR="/var/www/sinuca"
FRONT_RELEASE_ROOT="${TTS_BOT_FRONTEND_RELEASE_ROOT:-$(dirname "$FRONT_PUBLISH_DIR")/sinuca-releases}"
FRONT_RELEASE_RETENTION="${TTS_BOT_FRONTEND_RELEASE_RETENTION:-4}"
BACK_PORT="8787"
BACK_SERVICE="${DASHBOARD_SYSTEMD_SERVICE:-sinuca-activity-server.service}"
BACK_HEALTH_URL="http://127.0.0.1:${BACK_PORT}/health"
BOT_HEALTH_URL="http://127.0.0.1:10000/health"
RUNTIME_RELEASE_ROOT="${TTS_BOT_RUNTIME_RELEASE_ROOT:-$CANDIDATE_ROOT/runtime-releases}"
RUNTIME_RELEASE_RETENTION="${TTS_BOT_RUNTIME_RELEASE_RETENTION:-3}"
REMOTE_RUNTIME_ARTIFACT_ROOT="${TTS_BOT_REMOTE_RUNTIME_ARTIFACT_ROOT:-$CANDIDATE_ROOT/remote-runtime-artifacts}"
PYTHON_RUNTIME_ROOT="${TTS_BOT_PYTHON_RUNTIME_ROOT:-$CANDIDATE_ROOT/python-runtimes}"
PYTHON_RUNTIME_CURRENT_LINK="${TTS_BOT_PYTHON_RUNTIME_CURRENT_LINK:-$PYTHON_RUNTIME_ROOT/current}"
PYTHON_RUNTIME_RETENTION="${TTS_BOT_PYTHON_RUNTIME_RETENTION:-3}"
PYTHON_TOOL_ROOT="${TTS_BOT_PYTHON_TOOL_ROOT:-$CANDIDATE_ROOT/python-tools}"
PYTHON_UV_VERSION="${TTS_BOT_UV_VERSION:-0.12.13}"
PYTHON_UV_CACHE_ROOT="${TTS_BOT_UV_CACHE_ROOT:-$CANDIDATE_ROOT/uv-cache}"
PYTHON_INSTALLER_MODE="${TTS_BOT_PYTHON_INSTALLER:-auto}"
PYTHON_AUTO_BOOTSTRAP_UV="${TTS_BOT_AUTO_BOOTSTRAP_UV:-1}"
NODE_DEPENDENCY_CACHE_ROOT="${TTS_BOT_NODE_DEPENDENCY_CACHE_ROOT:-$CANDIDATE_ROOT/node-dependency-cache}"
NODE_DEPENDENCY_CACHE_RETENTION="${TTS_BOT_NODE_DEPENDENCY_CACHE_RETENTION:-4}"
TYPESCRIPT_CACHE_ROOT="${TTS_BOT_TYPESCRIPT_CACHE_ROOT:-$CANDIDATE_ROOT/typescript-cache}"
TYPESCRIPT_CACHE_RETENTION="${TTS_BOT_TYPESCRIPT_CACHE_RETENTION:-4}"
NPM_INSTALL_FLAGS="--prefer-offline --no-audit --no-fund --progress=false"
APP_COMMAND_SYNC_STATUS_FILE="$REPO_DIR/data/app_commands_sync_status.json"

STAGE="inicialização"
FAILED_STAGE=""
CURRENT_COMMIT=""
REMOTE_COMMIT=""
PREVIOUS_COMMIT=""
COMMIT_SUBJECT=""
UPDATE_APPLIED=0
# Limite transacional: depois que o deploy foi validado e, quando aplicável,
# publicado no GitHub, falhas de formatação/Discord/log não podem mais acionar
# rollback do código nem reiniciar o bot.
DEPLOYMENT_COMMITTED=0
DELIVERY_PHASE=0
ROLLBACK_DONE=0
ROLLBACK_IN_PROGRESS=0
BOT_RESTARTS_DEPLOY=0
BOT_RESTARTS_ROLLBACK=0
MANUAL_FAILURE_ALERT_SENT=0
LOCAL_CANDIDATE_MODE=0
LOCAL_CANDIDATE_RESUME_DELIVERY_ONLY=0
LOCAL_CANDIDATE_ID=""
LOCAL_CANDIDATE_DISPLAY_ID=""
LOCAL_CANDIDATE_DIR=""
LOCAL_CANDIDATE_BASE_COMMIT=""
LOCAL_CANDIDATE_COMMIT_MESSAGE=""
LOCAL_CANDIDATE_ZIP_NAME=""
LOCAL_CANDIDATE_ZIP_SHA256=""
LOCAL_CANDIDATE_SOURCE_AUTHOR_ID=""
LOCAL_CANDIDATE_SCHEMA_VERSION=2
LOCAL_CANDIDATE_ATTEMPT=0
LOCAL_CANDIDATE_RESUME_COUNT=0
LOCAL_CANDIDATE_VERIFY_ERROR=""
LOCAL_CANDIDATE_PENDING_FILE=""
LOCAL_CANDIDATE_FILES_DIR=""
LOCAL_CANDIDATE_PATCH_FILE=""
LOCAL_CANDIDATE_USE_PATCH=0
LOCAL_CANDIDATE_PUBLISHED=0
LOCAL_CANDIDATE_WORKTREE_DIR=""
LOCAL_CANDIDATE_PREPARED_COMMIT=""
LOCAL_CANDIDATE_ALREADY_PROMOTED=0
LOCAL_CANDIDATE_ARTIFACT_ROOT=""
LOCAL_CANDIDATE_FRONTEND_ARTIFACT=""
LOCAL_CANDIDATE_BACKEND_ARTIFACT=""
LOCAL_CANDIDATE_BACKEND_DEP_KEY=""
LOCAL_CANDIDATE_BACKEND_DEP_LAYER=""
LOCAL_CANDIDATE_PYTHON_ARTIFACT=""
LOCAL_CANDIDATE_PYTHON_READY=0
LOCAL_CANDIDATE_ARTIFACT_COMMIT=""
LOCAL_CANDIDATE_RUNTIME_READY=0
READY_FAST_PATH_USED=0
RUNTIME_RELEASE_SNAPSHOT_COMMIT=""
RUNTIME_RELEASE_SNAPSHOT_ROOT=""
RUNTIME_RELEASE_SNAPSHOT_READY=0
FRONT_RUNTIME_MUTATED=0
BACK_RUNTIME_MUTATED=0
PYTHON_RUNTIME_MUTATED=0
REMOTE_CANDIDATE_MODE=0
REMOTE_STATUS_CHANNEL_ID=""
REMOTE_STATUS_MESSAGE_ID=""
REMOTE_WORKTREE_DIR=""
REMOTE_REJECT_REASON=""
REMOTE_CANDIDATE_ARTIFACT_ROOT=""
NODE_DEP_CACHE_HITS=0
NODE_DEP_CACHE_MISSES=0
TYPESCRIPT_CACHE_HITS=0
TYPESCRIPT_CACHE_MISSES=0
PYTHON_INSTALLER_STATUS="não usado"
PYTHON_UV_BIN_RESOLVED=""
LAST_TYPESCRIPT_CACHE_HIT=0
FRONT_TEST_PLAN_STATUS="não executado"
BACK_TEST_PLAN_STATUS="não executado"
SELECTED_NODE_TEST_COMMAND=""
SELECTED_NODE_TEST_STATUS=""
LAST_NODE_DEP_LAYER_PATH=""
LAST_NODE_DEP_LAYER_KEY=""
LAST_NODE_DEP_CACHE_HIT=0
ROLLBACK_CONTROL_MODE=0
ROLLBACK_REQUEST_ID=""
ROLLBACK_REQUEST_FILE=""
ROLLBACK_REQUEST_ACTION=""
ROLLBACK_REQUEST_BRANCH=""
ROLLBACK_EXPECTED_HEAD=""
ROLLBACK_REVERT_COMMIT=""
ROLLBACK_MESSAGE_CHANNEL_ID=""
ROLLBACK_MESSAGE_ID=""
ROLLBACK_SOURCE_AUTHOR_ID=""
ROLLBACK_REQUESTED_BY=""
ROLLBACK_PREVIOUS_RECORD_JSON="{}"
ROLLBACK_NEW_COMMIT=""
ROLLBACK_UPDATE_FROM=""
ROLLBACK_UPDATE_TO=""
ROLLBACK_ROLLBACK_COMMIT=""
ROLLBACK_REDO_COMMIT=""

FRONT_CHANGED=0
BACK_CHANGED=0
FRONT_TESTS_CHANGED=0
BACK_TESTS_CHANGED=0
FRONT_TESTS_REQUIRED=0
FRONT_TYPECHECK_REQUIRED=0
BOT_CHANGED=0
REQUIREMENTS_CHANGED=0
AUDIO_SYSTEMD_CHANGED=0
CLEANUP_CHANGED=0
PHONE_LAVALINK_WATCH_CHANGED=0
PHONE_WORKER_WATCH_CHANGED=0
VPS_SYSTEMD_UNITS_CHANGED=0
ALERT_CHANGED=0
PHONE_WORKER_SYNC_REQUIRED=0
CORE_WORKER_APK_CHANGED=0
CORE_WORKER_AUTOMATION_REQUIRED=0

BOT_HEALTHCHECK_STATUS="não verificado"
BOT_HEALTH_JSON=""
BOT_HEALTH_DETAIL_STATUS="não verificado"
BOT_COGS_STATUS="não verificado"
BOT_WARNINGS_STATUS="sem avisos"
PREFLIGHT_PY_STATUS="não verificado"
PREFLIGHT_BASH_STATUS="não verificado"
PREFLIGHT_COG_IMPORT_STATUS="não verificado"
PREFLIGHT_RUNTIME_STATUS="não verificado"
UPDATE_HAS_WARNINGS=0
AUDIO_SERVICES_STATUS="não alterado"
CLEANUP_STATUS="não alterada"
PHONE_LAVALINK_WATCH_STATUS="não alterado"
PHONE_WORKER_WATCH_STATUS="não alterado"
VPS_SYSTEMD_UNITS_STATUS="não alterado"
PHONE_WORKER_SYNC_STATUS="sem mudanças"
CORE_WORKER_AGENT_UPDATE_STATUS="sem mudanças"
CORE_WORKER_APK_BUILD_STATUS="sem mudanças"
CORE_WORKER_NOTIFY_STATUS="sem mudanças"
FRONT_STATUS="não alterado"
BACK_STATUS="não alterado"
ACTIVITY_HEALTHCHECK_STATUS="não verificado"
ROLLBACK_STATUS="não foi necessário"
# Variáveis opcionais usadas apenas quando certos scripts/instaladores mudam.
# Com `set -u`, elas precisam existir desde o topo para a etapa final de
# falha de entrega ao Discord nunca derrubar o updater após o commit/push já ter passado.
ALERT_UNIT_STATUS="não alterado"
CRONTAB_HEALTH_STATUS="não alterado"
APP_COMMAND_SYNC_SUMMARY="Comandos sem mudanças"
APP_COMMAND_SYNC_WEBHOOK_BLOCK=""
APP_COMMAND_SYNC_ADDED_COUNT=0
APP_COMMAND_SYNC_REMOVED_COUNT=0
APP_COMMAND_SYNC_CHANGED=0
APP_COMMAND_SYNC_PERFORMED=0
APP_COMMANDS_MAY_HAVE_CHANGED=0
CHANGED_FILES_RAW=""
CHANGED_DIFF_NUMSTAT_RAW=""
CHANGED_STATUS_RAW=""
DIFF_TOTAL_SUMMARY=""
GIT_DIFF_SNAPSHOT_HASH=""
NODE_TOOLCHAIN_NODE_VERSION=""
NODE_TOOLCHAIN_NPM_VERSION=""
NODE_TOOLCHAIN_PLATFORM=""
FAST_RELOAD_STATUS="não usado"
FAST_RELOAD_MODULES=""
UPDATER_UNIT="tts-bot-updater.service"
UPDATER_EPHEMERAL_DIR="${TTS_BOT_UPDATER_RUNTIME_DIR:-${TMPDIR:-/tmp}}"
if [[ "$UPDATER_EPHEMERAL_DIR" != /* || ! -d "$UPDATER_EPHEMERAL_DIR" || -L "$UPDATER_EPHEMERAL_DIR" || ! -w "$UPDATER_EPHEMERAL_DIR" ]]; then
  UPDATER_EPHEMERAL_DIR="/tmp"
fi
RUN_LOG_FILE="$(mktemp "$UPDATER_EPHEMERAL_DIR/tts-bot-updater.XXXXXX.log")"
ZIP_STATUS_CONTROL_JSON=""
ZIP_STATUS_UI_JSON=""
UPDATE_TITLE_EMOJI="<a:areia:1496606578395189473>"
UPDATE_STAGE_EMOJI="<a:loading:1510065277868445796>"
UPDATE_CHECK_EMOJI="<:checkmark:1548838297806311445>"
UPDATE_ERROR_EMOJI="<:x_mark:1548838423169605654>"
UPDATE_DATABASE_EMOJI="<:Database:1548838603059105872>"
UPDATE_CLOUD_EMOJI="<:Cloud:1548838660915462254>"
UPDATE_FILES_EMOJI="<:Files:1548838468665475193>"
UPDATE_GITHUB_EMOJI="<:Github:1548838545500807239>"
ZIP_PROGRESS_HISTORY=""
ZIP_PROGRESS_COMPLETED_COUNT=0
ZIP_PROGRESS_HIDDEN_COUNT=0
ZIP_PROGRESS_MAX_VISIBLE_STEPS=10
ZIP_PROGRESS_STAGE_LABEL=""
ZIP_PROGRESS_STAGE_STARTED_MS=0
ZIP_PROGRESS_STARTED_MS=0
ZIP_PROGRESS_RECEIVED_AT_MS=0
ZIP_PROGRESS_UPDATER_DELAY_MS=0
ZIP_PROGRESS_CURRENT_MACRO_INDEX=-1
ZIP_PROGRESS_MACRO_MAX_INDEX=9
ZIP_PROGRESS_LAST_DONE_LABEL=""
ZIP_PROGRESS_LAST_DONE_DURATION=""
ZIP_PROGRESS_LAST_DONE_MACRO_INDEX=-1
UPDATER_PROCESS_STARTED_MS=0
LOCAL_CANDIDATE_UPDATER_STARTED_MS=0
ZIP_PROGRESS_DONE_LABELS=""
ZIP_PROGRESS_HANDOFF_LOADED=0
ZIP_RECOVERY_STARTED_MS=0
UPDATER_STEP_LAST=0
UPDATER_TIMINGS=""
BOT_HEALTH_IS_HEALTHY=0
BOT_HEALTH_READY_HEALTHY=0
UPDATE_STATUS_OUTBOX_DIR="$REPO_DIR/data/runtime/update-status-outbox"
UPDATE_ALERT_OUTBOX_DIR="$REPO_DIR/data/runtime/update-alert-outbox"
UPDATE_DELIVERY_RECEIPTS_DIR="$REPO_DIR/data/runtime/update-delivery-receipts"
UPDATE_INCIDENT_ROOT="${DISCORD_AUTO_UPDATE_INCIDENT_DIR:-$REPO_DIR/logs/updates}"
UPDATE_INCIDENT_DIR=""
UPDATE_FAILURE_FILE=""
UPDATE_ROLLBACK_FAILURE_FILE=""
UPDATE_PRIMARY_FAILURE_WRITTEN=0
CURRENT_STAGE_LOG_FILE=""
CURRENT_STAGE_LOG_STAGE=""
CURRENT_STAGE_COMMAND=""
LAST_ERROR_CODE=""
chmod 0644 "$RUN_LOG_FILE" 2>/dev/null || true

# Log persistente do updater para diagnóstico pelo /vps.
# O systemd guarda a saída no journalctl, mas este arquivo facilita anexar
# falhas recentes sem depender só do journal.
PERSISTENT_LOG_DIR="$REPO_DIR/logs"
PERSISTENT_LOG_FILE="$PERSISTENT_LOG_DIR/updater.log"
mkdir -p "$PERSISTENT_LOG_DIR" 2>/dev/null || true
# Rotação simples: se passar de 2MB, renomeia .log -> .log.1 e começa do zero.
if [[ -f "$PERSISTENT_LOG_FILE" ]]; then
  log_size=$(stat -c '%s' "$PERSISTENT_LOG_FILE" 2>/dev/null || echo 0)
  if [[ "$log_size" -gt 2097152 ]]; then
    mv -f "$PERSISTENT_LOG_FILE" "$PERSISTENT_LOG_FILE.1" 2>/dev/null || true
  fi
fi
{
  echo ""
  echo "===== $(date -Iseconds) updater started (pid=$$) ====="
} >> "$PERSISTENT_LOG_FILE" 2>/dev/null || true

# Tee pra ambos os arquivos. O persistente vira logs/updater.log;
# o tmpfile continua existindo pra `collect_run_log_excerpt`.
exec > >(tee -a "$RUN_LOG_FILE" "$PERSISTENT_LOG_FILE") 2>&1

# módulo: estado.sh
. "$UPDATER_SOURCE_DIR/estado.sh"
# módulo: git.sh
. "$UPDATER_SOURCE_DIR/git.sh"
# módulo: registros.sh
. "$UPDATER_SOURCE_DIR/registros.sh"
# módulo: tempos.sh
. "$UPDATER_SOURCE_DIR/tempos.sh"
format_changed_processes() {
  local items=()
  # Quando o patch é somente de cog recarregável, não trate como reinício de bot.
  # O estágio visual mostra "Recarregando cog/cogs" em vez de "Reiniciando processo".
  local fast_modules_for_process=""
  fast_modules_for_process="$(fast_reload_modules_for_changed_files 2>/dev/null || true)"
  if (( BOT_CHANGED == 1 || REQUIREMENTS_CHANGED == 1 )); then
    if [[ -n "${fast_modules_for_process//[[:space:]]/}" && "${FAST_RELOAD_STATUS:-não usado}" != *"fallback"* ]]; then
      :
    else
      items+=("bot")
    fi
  fi
  if (( FRONT_CHANGED == 1 || BACK_CHANGED == 1 )); then
    items+=("site")
  fi
  if (( PHONE_WORKER_SYNC_REQUIRED == 1 || PHONE_WORKER_WATCH_CHANGED == 1 || PHONE_LAVALINK_WATCH_CHANGED == 1 || CORE_WORKER_APK_CHANGED == 1 || CORE_WORKER_AUTOMATION_REQUIRED == 1 )); then
    items+=("worker")
  fi
  if (( AUDIO_SYSTEMD_CHANGED == 1 || CLEANUP_CHANGED == 1 )); then
    items+=("áudio")
  fi
  if (( VPS_SYSTEMD_UNITS_CHANGED == 1 )); then
    items+=("sistema VPS")
  fi
  if ((${#items[@]} == 0)); then
    printf 'nenhum processo alterado'
    return 0
  fi
  local joined="" item
  for item in "${items[@]}"; do
    [[ -z "$joined" ]] || joined+=", "
    joined+="$item"
  done
  printf '%s' "$joined"
}

# módulo: validacao.sh
. "$UPDATER_SOURCE_DIR/validacao.sh"

# módulo: fila.sh
. "$UPDATER_SOURCE_DIR/fila.sh"
prune_archive_root() {
  local root="${1:?}" retention_days="${2:?}" keep_count="${3:?}"
  local -a entries=()
  local index path
  [[ -d "$root" && ! -L "$root" ]] || return 0
  [[ "$retention_days" =~ ^[0-9]+$ ]] || return 0
  [[ "$keep_count" =~ ^[0-9]+$ ]] || return 0
  (( keep_count < 1 )) && keep_count=1
  (( keep_count > 100 )) && keep_count=100

  find "$root" -mindepth 1 -maxdepth 1 -type d -mtime "+$retention_days" -exec rm -rf -- {} + 2>/dev/null || true
  mapfile -t entries < <(
    find "$root" -mindepth 1 -maxdepth 1 -type d -printf '%T@\t%p\n' 2>/dev/null \
      | sort -t $'\t' -k1,1nr \
      | cut -f2-
  )
  for ((index=keep_count; index<${#entries[@]}; index++)); do
    path="${entries[$index]}"
    [[ -n "$path" && ! -L "$path" && "$(dirname -- "$path")" == "$root" ]] || continue
    rm -rf -- "$path" 2>/dev/null || true
  done
}

prune_updater_runtime_orphans() {
  local legacy_tmp="${TMPDIR:-/tmp}"
  [[ "$legacy_tmp" == /* && -d "$legacy_tmp" && ! -L "$legacy_tmp" ]] || return 0

  # Apenas prefixos privados do updater, sem recursão fora das raízes exatas.
  find "$legacy_tmp" -maxdepth 1 -type f -name 'tts-bot-update.*.run' -mmin +60 -delete 2>/dev/null || true
  find "$legacy_tmp" -maxdepth 1 -type f -name 'tts-bot-updater.*.log' -mmin +1440 -delete 2>/dev/null || true
  find "$legacy_tmp" -maxdepth 1 -type f \( -name 'tts-bot-git-add.*' -o -name 'tts-bot-git-add-retry.*' \) -mmin +360 -delete 2>/dev/null || true
  find "$legacy_tmp" -mindepth 1 -maxdepth 1 -type d \( -name 'tts-bot-remote-candidate.*' -o -name 'tts-bot-systemd-overlay.*' \) -mmin +360 -exec rm -rf -- {} + 2>/dev/null || true
  repo_git worktree prune --expire=now >/dev/null 2>&1 || true
}

prune_rejected_remote_commits() {
  local retention_days="${DISCORD_AUTO_UPDATE_REJECTED_RETENTION_DAYS:-30}"
  local keep_count="${DISCORD_AUTO_UPDATE_REJECTED_KEEP_COUNT:-100}"
  [[ -f "$REMOTE_REJECTED_FILE" && ! -L "$REMOTE_REJECTED_FILE" ]] || return 0
  [[ "$retention_days" =~ ^[0-9]+$ ]] || retention_days=30
  [[ "$keep_count" =~ ^[0-9]+$ ]] || keep_count=100
  REJECTED_FILE_VALUE="$REMOTE_REJECTED_FILE" REJECTED_RETENTION_DAYS="$retention_days" REJECTED_KEEP_COUNT="$keep_count" \
    python3 - <<'PYPRUNEREJECTED' 2>/dev/null || true
import datetime, json, os, pathlib, tempfile

path = pathlib.Path(os.environ['REJECTED_FILE_VALUE'])
try:
    data = json.loads(path.read_text(encoding='utf-8'))
except Exception:
    raise SystemExit
items = data.get('commits') if isinstance(data, dict) else None
if not isinstance(items, dict):
    raise SystemExit

now = datetime.datetime.now(datetime.timezone.utc)
cutoff = now - datetime.timedelta(days=max(0, int(os.environ['REJECTED_RETENTION_DAYS'])))
keep_count = max(1, min(1000, int(os.environ['REJECTED_KEEP_COUNT'])))
retained = []
for commit, record in items.items():
    if not isinstance(record, dict):
        retained.append((now, commit, record))
        continue
    raw = str(record.get('rejected_at') or '')
    try:
        stamp = datetime.datetime.fromisoformat(raw.replace('Z', '+00:00'))
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=datetime.timezone.utc)
    except Exception:
        stamp = now
    if stamp >= cutoff:
        retained.append((stamp, commit, record))
retained.sort(key=lambda item: (item[0], item[1]), reverse=True)
new_items = {commit: record for _stamp, commit, record in retained[:keep_count]}
if new_items == items:
    raise SystemExit
data['commits'] = new_items
fd, tmp_name = tempfile.mkstemp(prefix='.' + path.name + '.', suffix='.tmp', dir=path.parent)
tmp = pathlib.Path(tmp_name)
try:
    with os.fdopen(fd, 'w', encoding='utf-8') as handle:
        json.dump(data, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)
finally:
    try:
        tmp.unlink()
    except FileNotFoundError:
        pass
PYPRUNEREJECTED
  chown ubuntu:ubuntu "$REMOTE_REJECTED_FILE" 2>/dev/null || true
}

human_storage_bytes() {
  local value="${1:-0}"
  if command -v numfmt >/dev/null 2>&1; then
    numfmt --to=iec-i --suffix=B "$value" 2>/dev/null || printf '%s bytes' "$value"
  else
    printf '%s bytes' "$value"
  fi
}

guard_updater_disk_space() {
  local metrics total available hard_min hard_percent soft_percent hard_floor percent_floor event_id
  metrics="$(df -P -B1 "$REPO_DIR" 2>/dev/null | awk 'NR == 2 {print $2, $4}')"
  read -r total available <<< "$metrics"
  [[ "$total" =~ ^[0-9]+$ && "$available" =~ ^[0-9]+$ ]] || {
    logger -t "$LOG_TAG" "proteção de espaço: não foi possível ler o filesystem" 2>/dev/null || true
    return 0
  }

  hard_min="${TTS_BOT_UPDATER_DISK_HARD_MIN_BYTES:-2147483648}"
  hard_percent="${TTS_BOT_UPDATER_DISK_HARD_MIN_PERCENT:-5}"
  soft_percent="${TTS_BOT_UPDATER_DISK_SOFT_MIN_PERCENT:-15}"
  [[ "$hard_min" =~ ^[0-9]+$ ]] || hard_min=2147483648
  [[ "$hard_percent" =~ ^[0-9]+$ && "$hard_percent" -le 50 ]] || hard_percent=5
  [[ "$soft_percent" =~ ^[0-9]+$ && "$soft_percent" -le 80 ]] || soft_percent=15
  percent_floor=$((total * hard_percent / 100))
  hard_floor="$hard_min"
  (( percent_floor > hard_floor )) && hard_floor="$percent_floor"

  if (( available < hard_floor )); then
    event_id="updater-disk-critical-$(date +%Y%m%d)"
    logger -t "$LOG_TAG" "update pausado: espaço crítico, livre=$(human_storage_bytes "$available") mínimo=$(human_storage_bytes "$hard_floor")" 2>/dev/null || true
    send_error "Update pausado por pouco espaço" "Resumo: A atualização não foi iniciada para evitar uma aplicação parcial.
Espaço livre: $(human_storage_bytes "$available")
Mínimo seguro: $(human_storage_bytes "$hard_floor")
Bot: preservado e sem reinício
Ação: revise os artefatos de armazenamento antes de tentar novamente.
Hora: $(date '+%d/%m/%Y %H:%M:%S')" "$event_id"
    return 1
  fi

  if (( available * 100 < total * soft_percent )); then
    event_id="updater-disk-warning-$(date +%Y%m%d)"
    logger -t "$LOG_TAG" "proteção de espaço: nível preventivo, livre=$(human_storage_bytes "$available")" 2>/dev/null || true
    send_warn "Espaço da VPS abaixo do nível preventivo" "Resumo: O updater ainda pode funcionar, mas o espaço livre caiu abaixo de ${soft_percent}%.
Espaço livre: $(human_storage_bytes "$available")
Limite crítico atual: $(human_storage_bytes "$hard_floor")
Bot: preservado
Ação: revise os artefatos de armazenamento antes de atingir o bloqueio de segurança.
Hora: $(date '+%d/%m/%Y %H:%M:%S')" "$event_id"
  fi
  return 0
}

updater_heavy_maintenance_due() {
  local interval="${UPDATE_HEAVY_MAINTENANCE_INTERVAL_SECONDS:-3600}"
  local stamp="${UPDATE_HEAVY_MAINTENANCE_STAMP:-$CANDIDATE_ROOT/.heavy-maintenance.stamp}"
  local now last=0
  [[ "$interval" =~ ^[0-9]+$ ]] || interval=3600
  (( interval >= 60 )) || interval=60
  now="$(date +%s)"
  if [[ -f "$stamp" && ! -L "$stamp" ]]; then
    last="$(stat -c %Y "$stamp" 2>/dev/null || printf '0')"
  fi
  [[ "$last" =~ ^[0-9]+$ ]] || last=0
  (( now - last >= interval ))
}

mark_updater_heavy_maintenance() {
  local stamp="${UPDATE_HEAVY_MAINTENANCE_STAMP:-$CANDIDATE_ROOT/.heavy-maintenance.stamp}"
  local tmp="${stamp}.tmp.$$"
  install -d -o ubuntu -g ubuntu -m 0775 "$(dirname "$stamp")" 2>/dev/null || true
  printf '%s\n' "$(date +%s)" > "$tmp" 2>/dev/null || return 0
  chown ubuntu:ubuntu "$tmp" 2>/dev/null || true
  chmod 0644 "$tmp" 2>/dev/null || true
  mv -f -- "$tmp" "$stamp" 2>/dev/null || rm -f -- "$tmp" 2>/dev/null || true
}

prune_update_artifacts() {
  # O timer do updater pode rodar a cada minuto. As podas abaixo fazem vários
  # find/sort e percorrem metadados de caches grandes; executá-las em todo tick
  # desperdiça I/O mesmo quando não há update. Faça manutenção pesada no máximo
  # uma vez por hora por padrão. A proteção de espaço continua rodando em todo
  # ciclo e pode bloquear updates independentemente desta poda.
  if ! updater_heavy_maintenance_due; then
    return 0
  fi

  local done_days="${DISCORD_AUTO_UPDATE_DONE_RETENTION_DAYS:-7}"
  local failed_days="${DISCORD_AUTO_UPDATE_FAILED_RETENTION_DAYS:-30}"
  local cancelled_days="${DISCORD_AUTO_UPDATE_CANCELLED_RETENTION_DAYS:-7}"
  local done_keep="${DISCORD_AUTO_UPDATE_DONE_KEEP_COUNT:-5}"
  local failed_keep="${DISCORD_AUTO_UPDATE_FAILED_KEEP_COUNT:-5}"
  local cancelled_keep="${DISCORD_AUTO_UPDATE_CANCELLED_KEEP_COUNT:-3}"
  local outbox_days="${DISCORD_AUTO_UPDATE_OUTBOX_RETENTION_DAYS:-7}"
  local receipt_days="${DISCORD_AUTO_UPDATE_DELIVERY_RECEIPT_RETENTION_DAYS:-30}"
  local incident_days="${DISCORD_AUTO_UPDATE_INCIDENT_RETENTION_DAYS:-30}"
  [[ "$done_days" =~ ^[0-9]+$ ]] || done_days=7
  [[ "$failed_days" =~ ^[0-9]+$ ]] || failed_days=30
  [[ "$cancelled_days" =~ ^[0-9]+$ ]] || cancelled_days=7
  [[ "$done_keep" =~ ^[0-9]+$ ]] || done_keep=5
  [[ "$failed_keep" =~ ^[0-9]+$ ]] || failed_keep=5
  [[ "$cancelled_keep" =~ ^[0-9]+$ ]] || cancelled_keep=3
  [[ "$outbox_days" =~ ^[0-9]+$ ]] || outbox_days=7
  [[ "$receipt_days" =~ ^[0-9]+$ ]] || receipt_days=30
  [[ "$incident_days" =~ ^[0-9]+$ ]] || incident_days=30
  prune_archive_root "$CANDIDATE_ROOT/done" "$done_days" "$done_keep"
  prune_archive_root "$CANDIDATE_ROOT/failed" "$failed_days" "$failed_keep"
  prune_archive_root "$CANDIDATE_ROOT/cancelled" "$cancelled_days" "$cancelled_keep"
  prune_archive_root "$REMOTE_RUNTIME_ARTIFACT_ROOT" 1 2
  prune_node_dependency_layers || true
  prune_typescript_cache || true
  find "$CANDIDATE_QUEUE_DONE_DIR" -type f -mtime "+$done_days" -delete 2>/dev/null || true
  find "$CANDIDATE_QUEUE_FAILED_DIR" -type f -mtime "+$failed_days" -delete 2>/dev/null || true
  find "$CANDIDATE_QUEUE_CANCELLED_DIR" -type f -mtime "+$cancelled_days" -delete 2>/dev/null || true
  find "$UPDATE_STATUS_OUTBOX_DIR" -type f -name '*.json' -mtime "+$outbox_days" -delete 2>/dev/null || true
  find "$UPDATE_ALERT_OUTBOX_DIR" -type f -name '*.json' -mtime "+$outbox_days" -delete 2>/dev/null || true
  find "$UPDATE_ALERT_OUTBOX_DIR" -type f -name '*.attachment' -mtime "+$outbox_days" -delete 2>/dev/null || true
  find "$UPDATE_DELIVERY_RECEIPTS_DIR" -type f -mtime "+$receipt_days" -delete 2>/dev/null || true
  find "$UPDATE_INCIDENT_ROOT" -mindepth 2 -maxdepth 2 -type d -mtime "+$incident_days" -exec rm -rf -- {} + 2>/dev/null || true
  find "$UPDATE_INCIDENT_ROOT" -mindepth 1 -maxdepth 1 -type d -empty -delete 2>/dev/null || true
  prune_rejected_remote_commits
  prune_updater_runtime_orphans
  mark_updater_heavy_maintenance
}

git_add_changed_files_or_reject() {
  local context="${1:-git add}"
  [[ -n "${CHANGED_FILES_RAW//[[:space:]]/}" ]] || return 0
  local errfile
  errfile="$(mktemp "${TMPDIR:-/tmp}/tts-bot-git-add.XXXXXX")"
  if git_add_changed_files 2>"$errfile"; then
    rm -f "$errfile" 2>/dev/null || true
    return 0
  fi

  LAST_ERROR_STDERR="$(cat "$errfile" 2>/dev/null || true)"
  rm -f "$errfile" 2>/dev/null || true

  # Arquivos/pastas novos podem ser criados pelo updater root antes do commit.
  # O git add roda como ubuntu; se o path ficou root-owned, normalize e tente uma vez.
  if printf '%s' "$LAST_ERROR_STDERR" | grep -qiE 'Permission denied|unable to index file|adding files failed'; then
    normalize_changed_file_permissions "$context" || true
    errfile="$(mktemp "${TMPDIR:-/tmp}/tts-bot-git-add-retry.XXXXXX")"
    if git_add_changed_files 2>"$errfile"; then
      rm -f "$errfile" 2>/dev/null || true
      LAST_ERROR_STDERR=""
      return 0
    fi
    LAST_ERROR_STDERR="$(cat "$errfile" 2>/dev/null || true)"
    rm -f "$errfile" 2>/dev/null || true
  fi

  reject_local_candidate_safely \
    "Falha ao aplicar atualização" \
    "Não consegui preparar os arquivos do ZIP. A VPS foi restaurada e o candidato foi arquivado." \
    "$context: ${LAST_ERROR_STDERR:-git add falhou}"
}

write_local_candidate_state() {
  (( LOCAL_CANDIDATE_MODE == 1 )) || return 0
  [[ -n "${LOCAL_CANDIDATE_DIR:-}" && -d "$LOCAL_CANDIDATE_DIR" ]] || return 0
  local state_name="${1:-state}"
  local extra_commit="${2:-}"
  python3 - "$LOCAL_CANDIDATE_DIR" "$state_name" "$extra_commit" <<'PYSTATE' 2>/dev/null || true
import datetime, json, os, pathlib, sys
root = pathlib.Path(sys.argv[1])
state = sys.argv[2]
commit = sys.argv[3]
path = root / "state.json"
data = {}
if path.exists():
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        data = {}
data.update({
    "state": state,
    "commit": commit,
    "updated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
})
tmp = path.with_name('.' + path.name + f'.{os.getpid()}.tmp')
tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
os.replace(tmp, path)
PYSTATE
}

write_local_candidate_recovery_state() {
  (( LOCAL_CANDIDATE_MODE == 1 )) || return 0
  [[ -n "${LOCAL_CANDIDATE_DIR:-}" && -d "$LOCAL_CANDIDATE_DIR" ]] || return 0
  local rollback_ok="${1:-false}"
  local restored_commit="${2:-}"
  local target_commit="${3:-}"
  local failure_code="${4:-UPDATE_STAGE_FAILED}"
  local failed_stage="${5:-}"
  local recovery_duration="${6:-}"
  local bot_health="${7:-}"
  ROLLBACK_OK_VALUE="$rollback_ok" RESTORED_COMMIT_VALUE="$restored_commit" \
  TARGET_COMMIT_VALUE="$target_commit" FAILURE_CODE_VALUE="$failure_code" \
  FAILED_STAGE_VALUE="$failed_stage" RECOVERY_DURATION_VALUE="$recovery_duration" \
  BOT_HEALTH_VALUE="$bot_health" python3 - "$LOCAL_CANDIDATE_DIR" <<'PYRECOVERYSTATE' 2>/dev/null || true
import datetime, json, os, pathlib, sys
root = pathlib.Path(sys.argv[1])
path = root / "state.json"
data = {}
if path.exists():
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        data = {}
rollback_ok = (os.environ.get("ROLLBACK_OK_VALUE") or "false").lower() == "true"
data.update({
    "state": "failed",
    "commit": os.environ.get("RESTORED_COMMIT_VALUE") or "",
    "target_commit": os.environ.get("TARGET_COMMIT_VALUE") or "",
    "rollback_ok": rollback_ok,
    "recovery_state": "restored" if rollback_ok else "incomplete",
    "failure_code": os.environ.get("FAILURE_CODE_VALUE") or "UPDATE_STAGE_FAILED",
    "failed_stage": os.environ.get("FAILED_STAGE_VALUE") or "",
    "recovery_duration": os.environ.get("RECOVERY_DURATION_VALUE") or "",
    "bot_health": os.environ.get("BOT_HEALTH_VALUE") or "",
    "updated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
})
tmp = path.with_name('.' + path.name + f'.{os.getpid()}.tmp')
tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
os.replace(tmp, path)
PYRECOVERYSTATE
}

# módulo: progresso.sh
. "$UPDATER_SOURCE_DIR/progresso.sh"

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

# módulo: candidato.sh
. "$UPDATER_SOURCE_DIR/candidato.sh"

# módulo: mudancas.sh
. "$UPDATER_SOURCE_DIR/mudancas.sh"

# módulo: aplicacao.sh
. "$UPDATER_SOURCE_DIR/aplicacao.sh"

# módulo: recuperacao.sh
. "$UPDATER_SOURCE_DIR/recuperacao.sh"

trap 'cleanup_runtime_artifacts' EXIT
trap 'on_error "$LINENO" "${FUNCNAME[0]:-main}"' ERR

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

  local remote_ready_fast_path=0
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
send_alert_reliably "$ALERT_TYPE" "$ALERT_TITLE" "$BODY" "$FINAL_RAW_LOG" "tts-bot-updater.log" "$FINAL_ALERT_EVENT_ID" || FINAL_ALERT_DELIVERY_RC=$?
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
