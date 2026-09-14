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

send_update_status_payload() {
  # `${1:-{}}` acrescenta uma chave `}` ao argumento em Bash, produzindo JSON
  # inválido e fazendo toda edição via /internal/update/zip-status ser descartada.
  local payload_json="${1:-}"
  [[ -n "${payload_json//[[:space:]]/}" ]] || payload_json='{}'
  local final_delivery="${2:-0}"
  prepare_update_delivery_dirs || true
  local rc=0
  if UPDATE_PAYLOAD_JSON="$payload_json" FINAL_DELIVERY="$final_delivery" \
  BOT_HEALTH_URL="$BOT_HEALTH_URL" REPO_DIR="$REPO_DIR" UPDATE_STATUS_OUTBOX_DIR="$UPDATE_STATUS_OUTBOX_DIR" python3 - <<'PYSENDSTATUS'
import datetime, hashlib, json, os, pathlib, time, urllib.error, urllib.request, uuid

try:
    payload = json.loads(os.environ.get('UPDATE_PAYLOAD_JSON') or '{}')
except Exception:
    raise SystemExit(2)
if not isinstance(payload, dict) or not payload.get('channel_id') or not payload.get('message_id'):
    raise SystemExit(2)

payload.setdefault('event_at', datetime.datetime.now(datetime.timezone.utc).isoformat())
delivery_seed = '\0'.join(
    str(payload.get(key) or '')
    for key in ('channel_id', 'message_id', 'candidate_id', 'status', 'title', 'event_at')
)
delivery_id = str(payload.get('delivery_id') or '').strip() or hashlib.sha256(delivery_seed.encode('utf-8')).hexdigest()[:24]
payload['delivery_id'] = delivery_id

url = (os.environ.get('BOT_HEALTH_URL') or 'http://127.0.0.1:10000/health').replace('/health', '/internal/update/zip-status')
headers = {'Content-Type': 'application/json'}
try:
    env_path = pathlib.Path(os.environ.get('REPO_DIR', '/home/ubuntu/bot')) / '.env'
    if env_path.exists():
        for line in env_path.read_text(encoding='utf-8', errors='ignore').splitlines():
            if line.startswith('BOT_INTERNAL_UPDATE_TOKEN='):
                token = line.split('=', 1)[1].strip().strip('"').strip("'")
                if token:
                    headers['X-Update-Token'] = token
                break
except Exception:
    pass

body = json.dumps(payload, ensure_ascii=False).encode('utf-8')
final_delivery = str(os.environ.get('FINAL_DELIVERY') or '0').lower() in {'1', 'true', 'yes'}
try:
    attempts = max(1, min(10, int(os.environ.get('DISCORD_AUTO_UPDATE_DELIVERY_ATTEMPTS') or (5 if final_delivery else 1))))
except (TypeError, ValueError):
    attempts = 5 if final_delivery else 1
try:
    base_delay = max(0.0, min(10.0, float(os.environ.get('DISCORD_AUTO_UPDATE_DELIVERY_RETRY_DELAY_SECONDS') or 1.5)))
except (TypeError, ValueError):
    base_delay = 1.5
try:
    request_timeout = max(1.0, min(30.0, float(os.environ.get('DISCORD_AUTO_UPDATE_DELIVERY_TIMEOUT_SECONDS') or 10)))
except (TypeError, ValueError):
    request_timeout = 10.0
last_error = ''
for attempt in range(attempts):
    try:
        req = urllib.request.Request(url, data=body, headers=headers, method='POST')
        with urllib.request.urlopen(req, timeout=request_timeout) as response:
            parsed = json.loads(response.read().decode('utf-8', errors='ignore') or '{}')
        if parsed.get('ok') and (parsed.get('delivered') or parsed.get('ignored')):
            raise SystemExit(0)
        last_error = str(parsed.get('error') or parsed or 'resposta sem confirmação')[:500]
    except urllib.error.HTTPError as exc:
        try:
            detail = exc.read().decode('utf-8', errors='ignore')
        except Exception:
            detail = ''
        last_error = f'HTTP {exc.code}: {detail[:400]}'
    except Exception as exc:
        last_error = f'{type(exc).__name__}: {exc}'[:500]
    if attempt + 1 < attempts:
        time.sleep(min(8.0, base_delay * (2 ** attempt)))

if not final_delivery:
    raise SystemExit(0)

outbox = pathlib.Path(os.environ.get('UPDATE_STATUS_OUTBOX_DIR') or '/tmp/update-status-outbox')
try:
    outbox.mkdir(parents=True, exist_ok=True)
    safe_id = ''.join(ch if ch.isalnum() or ch in '._-' else '_' for ch in delivery_id)[:120]
    path = outbox / f'{safe_id}.json'
    existing = {}
    if path.is_file():
        try:
            existing = json.loads(path.read_text(encoding='utf-8'))
        except Exception:
            existing = {}
    if not isinstance(existing, dict):
        existing = {}
    job = {
        'schema_version': 2,
        'delivery_id': delivery_id,
        'payload': payload,
        'created_at': existing.get('created_at') or datetime.datetime.now(datetime.timezone.utc).isoformat(),
        'updated_at': datetime.datetime.now(datetime.timezone.utc).isoformat(),
        'attempts': int(existing.get('attempts') or 0),
        'last_error': last_error or 'entrega indisponível',
        'next_attempt_at': 0,
    }
    tmp = path.with_name('.' + path.name + f'.{os.getpid()}.{uuid.uuid4().hex}.tmp')
    tmp.write_text(json.dumps(job, ensure_ascii=False, indent=2, sort_keys=True), encoding='utf-8')
    os.replace(tmp, path)
    os.chmod(path, 0o664)
except Exception as exc:
    print(f'falha ao persistir status final: {type(exc).__name__}: {exc}', file=os.sys.stderr)
    raise SystemExit(2)
raise SystemExit(0)
PYSENDSTATUS
  then
    rc=0
  else
    rc=$?
  fi
  chown ubuntu:ubuntu "$UPDATE_STATUS_OUTBOX_DIR" 2>/dev/null || true
  if (( rc != 0 )); then
    logger -t "$LOG_TAG" "falha ao entregar ou persistir status final do update (rc=$rc)" 2>/dev/null || true
  fi
  return "$rc"
}

flush_update_status_outbox() {
  prepare_update_delivery_dirs || true
  BOT_HEALTH_URL="$BOT_HEALTH_URL" REPO_DIR="$REPO_DIR" UPDATE_STATUS_OUTBOX_DIR="$UPDATE_STATUS_OUTBOX_DIR" python3 - <<'PYFLUSHSTATUS'
import datetime, json, os, pathlib, time, urllib.error, urllib.request

root = pathlib.Path(os.environ.get('UPDATE_STATUS_OUTBOX_DIR') or '/tmp/update-status-outbox')
if not root.is_dir():
    raise SystemExit(0)
url = (os.environ.get('BOT_HEALTH_URL') or 'http://127.0.0.1:10000/health').replace('/health', '/internal/update/zip-status')
headers = {'Content-Type': 'application/json'}
try:
    env_path = pathlib.Path(os.environ.get('REPO_DIR', '/home/ubuntu/bot')) / '.env'
    if env_path.exists():
        for line in env_path.read_text(encoding='utf-8', errors='ignore').splitlines():
            if line.startswith('BOT_INTERNAL_UPDATE_TOKEN='):
                token = line.split('=', 1)[1].strip().strip('"').strip("'")
                if token:
                    headers['X-Update-Token'] = token
                break
except Exception:
    pass

now = time.time()

# Um processo pode morrer depois de renomear o job para .sending.*. Nesse caso,
# o glob normal não o encontra mais e a entrega ficava presa para sempre.
# Recoloque claims antigos na fila antes de buscar novos trabalhos.
for stale_claim in root.glob('.sending.*.json'):
    try:
        if now - stale_claim.stat().st_mtime < 120:
            continue
        parts = stale_claim.name.split('.', 3)
        original_name = parts[3] if len(parts) == 4 and parts[3] else f'recovered-{int(now)}.json'
        target = root / original_name
        if target.exists():
            target = root / f'recovered-{int(now)}-{os.getpid()}-{original_name}'
        os.replace(stale_claim, target)
    except OSError:
        pass

for path in sorted(root.glob('*.json'), key=lambda item: item.stat().st_mtime)[:100]:
    claim = path.with_name(f'.sending.{os.getpid()}.{path.name}')
    try:
        os.replace(path, claim)
    except OSError:
        continue
    data = {}
    try:
        data = json.loads(claim.read_text(encoding='utf-8'))
        payload = data.get('payload') if isinstance(data, dict) else None
        if not isinstance(payload, dict):
            raise ValueError('job de status sem payload válido')
        attempts = int(data.get('attempts') or 0)
        next_attempt_at = float(data.get('next_attempt_at') or 0)
        if next_attempt_at > now:
            os.replace(claim, path)
            continue
        req = urllib.request.Request(url, data=json.dumps(payload, ensure_ascii=False).encode('utf-8'), headers=headers, method='POST')
        with urllib.request.urlopen(req, timeout=10) as response:
            result = json.loads(response.read().decode('utf-8', errors='ignore') or '{}')
        if result.get('ok') and (result.get('delivered') or result.get('ignored')):
            claim.unlink(missing_ok=True)
            continue
        raise RuntimeError(str(result.get('error') or result or 'sem confirmação'))
    except Exception as exc:
        try:
            permanent = isinstance(exc, (json.JSONDecodeError, ValueError, TypeError, AttributeError))
            attempts = 20 if permanent else (int(data.get('attempts') or 0) + 1 if isinstance(data, dict) else 1)
            if attempts >= 20:
                dead = root / 'failed'
                dead.mkdir(parents=True, exist_ok=True)
                failed_path = dead / path.name
                if isinstance(data, dict):
                    data['attempts'] = attempts
                    data['last_error'] = f'{type(exc).__name__}: {exc}'[:600]
                    data['failed_at'] = datetime.datetime.now(datetime.timezone.utc).isoformat()
                    claim.write_text(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True), encoding='utf-8')
                os.replace(claim, failed_path)
                continue
            if not isinstance(data, dict):
                data = {}
            data['attempts'] = attempts
            data['last_error'] = f'{type(exc).__name__}: {exc}'[:600]
            data['updated_at'] = datetime.datetime.now(datetime.timezone.utc).isoformat()
            data['next_attempt_at'] = now + min(300, 5 * (2 ** min(attempts, 6)))
            claim.write_text(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True), encoding='utf-8')
            os.replace(claim, path)
        except Exception:
            try:
                os.replace(claim, path)
            except Exception:
                pass
PYFLUSHSTATUS
  local rc=$?
  chown -R ubuntu:ubuntu "$UPDATE_STATUS_OUTBOX_DIR" 2>/dev/null || true
  return "$rc"
}

flush_update_alert_outbox() {
  # A fila de logs pertence ao bot. Quando ele está online, pedimos um flush
  # imediato; quando está reiniciando/offline, os jobs permanecem no disco e o
  # loop de reconciliação do bot os entrega assim que voltar.
  prepare_update_delivery_dirs || true
  BOT_HEALTH_URL="$BOT_HEALTH_URL" REPO_DIR="$REPO_DIR" python3 - <<'PYFLUSHLOG' 2>/dev/null || true
import json, os, pathlib, urllib.request

url = (os.environ.get('BOT_HEALTH_URL') or 'http://127.0.0.1:10000/health').replace('/health', '/internal/update/flush-logs')
headers = {'Content-Type': 'application/json'}
try:
    env_path = pathlib.Path(os.environ.get('REPO_DIR', '/home/ubuntu/bot')) / '.env'
    if env_path.exists():
        for line in env_path.read_text(encoding='utf-8', errors='ignore').splitlines():
            if line.startswith('BOT_INTERNAL_UPDATE_TOKEN='):
                token = line.split('=', 1)[1].strip().strip('"').strip("'")
                if token:
                    headers['X-Update-Token'] = token
                break
except Exception:
    pass
try:
    request = urllib.request.Request(url, data=b'{}', headers=headers, method='POST')
    urllib.request.urlopen(request, timeout=3).read()
except Exception:
    pass
PYFLUSHLOG
  return 0
}

notify_zip_status_message() {
  local status="${1:-info}"
  local title="${2:-Atualização}"
  local description="${3:-}"
  local final_delivery=0 generated_fallback_ui=0
  [[ "$status" =~ ^(success|ok|warn|error|done|failed)$ ]] && final_delivery=1

  # Todo estado final usa o mesmo renderer compacto do bot, inclusive falhas,
  # rejeições e recuperações que terminam antes do bloco final principal. Isso
  # evita voltar ao card técnico antigo justamente nos caminhos de erro.
  if (( final_delivery == 1 )) && [[ -z "${ZIP_STATUS_UI_JSON:-}" ]]; then
    local fallback_display_id fallback_from fallback_to fallback_count fallback_duration fallback_github
    fallback_display_id="${LOCAL_CANDIDATE_DISPLAY_ID:-${UPDATE_DISPLAY_ID:-${ROLLBACK_REQUEST_ID:-}}}"
    fallback_from="$(short_commit "${PREVIOUS_COMMIT:-${CURRENT_COMMIT:-}}")"
    fallback_to="$(short_commit "${REMOTE_COMMIT:-${CURRENT_COMMIT:-}}")"
    fallback_count="$(format_update_file_count "${CHANGED_FILES_COUNT:-0}")"
    fallback_duration="$(human_duration "${SECONDS:-0}")"
    fallback_github="false"
    [[ "$status" =~ ^(success|ok|done)$ ]] && fallback_github="true"
    ZIP_STATUS_UI_JSON="$(
      UI_STATUS="$status" UI_HEADLINE="$title" UI_SUMMARY="$description" \
      UI_DISPLAY_ID="$fallback_display_id" UI_BRANCH="${BRANCH:-main}" \
      UI_FROM="$fallback_from" UI_TO="$fallback_to" UI_FILE_COUNT="$fallback_count" \
      UI_DIFF="${DIFF_TOTAL_SUMMARY:-}" UI_IMPACT="${APPLY_MODE:-}" UI_DURATION="$fallback_duration" \
      UI_TOTAL_DURATION="${TOTAL_FROM_RECEIVE_DURATION:-}" UI_HEALTH="${BOT_HEALTHCHECK_STATUS:-}" UI_GITHUB="$fallback_github" \
      UI_CHECKS="${CHECKS_TEXT:-}" UI_TIMINGS="${TIMINGS_TEXT:-}" UI_CACHE="${CACHE_TEXT:-}" \
      UI_TESTS="${TEST_PLAN_TEXT:-}" UI_FILES="${CHANGED_FILES:-}" UI_PROCESSES="${CHANGED_PROCESSES:-}" \
      python3 - <<'PYFALLBACKUI'
import json, os, re
headline = os.environ.get("UI_HEADLINE") or ""
headline = re.sub(r"^[^\wÀ-ÿ]+\s*", "", headline, count=1).strip()
print(json.dumps({
    "kind": "final",
    "status": os.environ.get("UI_STATUS") or "error",
    "headline": headline,
    "summary": os.environ.get("UI_SUMMARY") or "",
    "display_id": os.environ.get("UI_DISPLAY_ID") or "",
    "branch": os.environ.get("UI_BRANCH") or "main",
    "from": os.environ.get("UI_FROM") or "",
    "to": os.environ.get("UI_TO") or "",
    "file_count_text": os.environ.get("UI_FILE_COUNT") or "0 arquivos",
    "diff_summary": os.environ.get("UI_DIFF") or "",
    "impact": os.environ.get("UI_IMPACT") or "",
    "duration": os.environ.get("UI_DURATION") or "",
    "total_duration": os.environ.get("UI_TOTAL_DURATION") or "",
    "bot_health": os.environ.get("UI_HEALTH") or "",
    "github_synced": (os.environ.get("UI_GITHUB") or "false").lower() == "true",
    "checks_text": os.environ.get("UI_CHECKS") or "",
    "timings_text": os.environ.get("UI_TIMINGS") or "",
    "cache_text": os.environ.get("UI_CACHE") or "",
    "tests_text": os.environ.get("UI_TESTS") or "",
    "files_text": os.environ.get("UI_FILES") or "",
    "processes": os.environ.get("UI_PROCESSES") or "",
}, ensure_ascii=False))
PYFALLBACKUI
    )"
    generated_fallback_ui=1
  fi

  if (( REMOTE_CANDIDATE_MODE == 1 )); then
    post_direct_update_message "$REMOTE_STATUS_CHANNEL_ID" "$REMOTE_STATUS_MESSAGE_ID" "$status" "$title" "$description" "${ZIP_STATUS_CONTROL_JSON:-}"
    (( generated_fallback_ui == 1 )) && ZIP_STATUS_UI_JSON=""
    return 0
  fi
  (( LOCAL_CANDIDATE_MODE == 1 )) || return 0
  [[ -n "${LOCAL_CANDIDATE_DIR:-}" && -f "$LOCAL_CANDIDATE_DIR/manifest.json" ]] || return 0
  local channel_id message_id payload
  channel_id="$(json_field_from_file "$LOCAL_CANDIDATE_DIR/manifest.json" discord_status.channel_id 2>/dev/null || true)"
  message_id="$(json_field_from_file "$LOCAL_CANDIDATE_DIR/manifest.json" discord_status.message_id 2>/dev/null || true)"
  [[ -n "$channel_id" && -n "$message_id" ]] || return 0
  payload="$(CHANNEL_ID_VALUE="$channel_id" MESSAGE_ID_VALUE="$message_id" STATUS_VALUE="$status" TITLE_VALUE="$title" DESCRIPTION_VALUE="$description" CONTROL_VALUE="${ZIP_STATUS_CONTROL_JSON:-}" UI_VALUE="${ZIP_STATUS_UI_JSON:-}" CANDIDATE_ID_VALUE="${LOCAL_CANDIDATE_ID:-}" DISPLAY_ID_VALUE="${LOCAL_CANDIDATE_DISPLAY_ID:-}" python3 - <<'PYBUILDPAYLOAD'
import datetime, json, os
payload = {
    'channel_id': os.environ.get('CHANNEL_ID_VALUE') or '',
    'message_id': os.environ.get('MESSAGE_ID_VALUE') or '',
    'status': os.environ.get('STATUS_VALUE') or 'info',
    'title': os.environ.get('TITLE_VALUE') or 'Atualização',
    'description': os.environ.get('DESCRIPTION_VALUE') or '',
    'candidate_id': os.environ.get('CANDIDATE_ID_VALUE') or '',
    'display_id': os.environ.get('DISPLAY_ID_VALUE') or '',
    'event_at': datetime.datetime.now(datetime.timezone.utc).isoformat(),
}
try:
    control = json.loads(os.environ.get('CONTROL_VALUE') or '')
    if isinstance(control, dict):
        payload['control'] = control
except Exception:
    pass
try:
    ui = json.loads(os.environ.get('UI_VALUE') or '')
    if isinstance(ui, dict):
        payload['ui'] = ui
except Exception:
    pass
print(json.dumps(payload, ensure_ascii=False))
PYBUILDPAYLOAD
)"
  send_update_status_payload "$payload" "$final_delivery"
  local delivery_rc=$?
  (( generated_fallback_ui == 1 )) && ZIP_STATUS_UI_JSON=""
  return "$delivery_rc"
}

post_direct_update_message() {
  local channel_id="${1:-}"
  local message_id="${2:-}"
  local status="${3:-info}"
  local title="${4:-Atualização}"
  local description="${5:-}"
  local control_json="${6:-}"
  local final_delivery=0 payload
  [[ -n "$channel_id" && -n "$message_id" ]] || return 0
  [[ "$status" =~ ^(success|ok|warn|error|done|failed)$ ]] && final_delivery=1
  payload="$(CHANNEL_ID_VALUE="$channel_id" MESSAGE_ID_VALUE="$message_id" STATUS_VALUE="$status" TITLE_VALUE="$title" DESCRIPTION_VALUE="$description" CONTROL_VALUE="$control_json" UI_VALUE="${ZIP_STATUS_UI_JSON:-}" python3 - <<'PYDIRECTPAYLOAD'
import datetime, json, os
payload = {
    'channel_id': os.environ.get('CHANNEL_ID_VALUE') or '',
    'message_id': os.environ.get('MESSAGE_ID_VALUE') or '',
    'status': os.environ.get('STATUS_VALUE') or 'info',
    'title': os.environ.get('TITLE_VALUE') or 'Atualização',
    'description': os.environ.get('DESCRIPTION_VALUE') or '',
    'event_at': datetime.datetime.now(datetime.timezone.utc).isoformat(),
}
try:
    control = json.loads(os.environ.get('CONTROL_VALUE') or '')
    if isinstance(control, dict):
        payload['control'] = control
except Exception:
    pass
try:
    ui = json.loads(os.environ.get('UI_VALUE') or '')
    if isinstance(ui, dict):
        payload['ui'] = ui
except Exception:
    pass
print(json.dumps(payload, ensure_ascii=False))
PYDIRECTPAYLOAD
)"
  send_update_status_payload "$payload" "$final_delivery"
}

create_direct_update_message() {
  local status="${1:-applying}"
  local title="${2:-$UPDATE_TITLE_EMOJI Aplicando atualização...}"
  local description="${3:-}"
  STATUS_VALUE="$status" TITLE_VALUE="$title" DESCRIPTION_VALUE="$description" \
  BOT_HEALTH_URL="$BOT_HEALTH_URL" REPO_DIR="$REPO_DIR" python3 - <<'PYCREATE' 2>/dev/null || true
import json, os, shlex, urllib.request
from pathlib import Path
payload = {
    "status": os.environ.get("STATUS_VALUE") or "applying",
    "title": os.environ.get("TITLE_VALUE") or "Atualização",
    "description": os.environ.get("DESCRIPTION_VALUE") or "",
}
url = (os.environ.get("BOT_HEALTH_URL") or "http://127.0.0.1:10000/health").replace("/health", "/internal/update/create-zip-status")
headers = {"Content-Type": "application/json"}
try:
    env_path = Path(os.environ.get("REPO_DIR", "/home/ubuntu/bot")) / ".env"
    if env_path.exists():
        for line in env_path.read_text(encoding="utf-8", errors="ignore").splitlines():
            if line.startswith("BOT_INTERNAL_UPDATE_TOKEN="):
                token = line.split("=", 1)[1].strip().strip('"').strip("'")
                if token:
                    headers["X-Update-Token"] = token
                break
except Exception:
    pass
try:
    req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"), headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=4) as resp:
        data = json.loads(resp.read().decode("utf-8", errors="ignore") or "{}")
    if data.get("ok"):
        print("REMOTE_STATUS_CHANNEL_ID=" + shlex.quote(str(data.get("channel_id") or "")))
        print("REMOTE_STATUS_MESSAGE_ID=" + shlex.quote(str(data.get("message_id") or "")))
except Exception:
    pass
PYCREATE
}

zip_progress_title() {
  local stage_label="${1:-Processando atualização}"
  local lowered="${stage_label,,}"
  if (( ROLLBACK_CONTROL_MODE == 1 )); then
    if [[ "${ROLLBACK_REQUEST_ACTION:-rollback}" == "redo" ]]; then
      printf '↪️ Reaplicando atualização'
    else
      printf '↩️ Revertendo atualização'
    fi
    return 0
  fi

  # O título acompanha a fase sem expor o subsistema alterado. A etapa detalhada
  # continua logo abaixo, enquanto o cabeçalho deixa claro que o painel avançou.
  case "$lowered" in
    *fila*|*aguard*)
      printf '📦 Atualização na fila'
      ;;
    *finalizando*)
      printf '✅ Finalizando atualização'
      ;;
    *fazendo\ commit*|*commit\ criado*|*registrando*)
      printf '📝 Registrando atualização'
      ;;
    *publicando\ no\ github*|*github*)
      printf '☁️ Publicando atualização'
      ;;
    *publicando*|*enviando*)
      printf '🚀 Publicando atualização'
      ;;
    *compilando*|*build*)
      printf '🛠️ Compilando atualização'
      ;;
    *instalando*|*preparando\ servidor*|*dependências*)
      printf '📦 Preparando atualização'
      ;;
    *reiniciando*)
      printf '🔄 Reiniciando serviços'
      ;;
    *recarregando*|*reload*)
      printf '🔄 Aplicando atualização'
      ;;
    *conferindo*|*validando*|*verificando*|*analisando*|*recuperando*)
      printf '🔎 Verificando atualização'
      ;;
    *aplicando*|*preparando\ arquivos*)
      printf '⚙️ Aplicando atualização'
      ;;
    *)
      printf '⚙️ Atualizando'
      ;;
  esac
}

zip_progress_macro_index() {
  local stage_label="${1:-Processando atualização}"
  local lowered="${stage_label,,}"
  case "$lowered" in
    *github*|*push*|*sincronizando*) printf '9' ;;
    *health*|*saúde*|*saude*|*comandos*|*estabilidade*|*finalizando*|*estado\ publicado*|*recuperando\ confirmação*|"validando"|*validando\ arquivos*) printf '8' ;;
    *publicando\ interface*|*publicando\ servidor*|*reiniciando*|*recarregando*|*reload*|*ativando*|*validando\ aplicação*|*validando\ aplicacao*) printf '7' ;;
    *promov*|*aplicando\ na\ vps*) printf '6' ;;
    *ready*|*release*|*preservando\ runtime*|*fazendo\ commit*|*commit\ criado*|*registrando*) printf '5' ;;
    *validando\ runtime*|*verificando\ comandos*|*test*|*typescript*|*compilando*|*build*|*dependências*|*dependencias*) printf '4' ;;
    *aplicando\ em\ área*|*worktree*|*staging*|*isolamento*) printf '3' ;;
    *validando\ estado*|*preparando\ arquivos*) printf '2' ;;
    *integridade*|*segurança*|*seguranca*|*validando\ permissões*|*analisando*) printf '1' ;;
    *conferindo\ zip*|*pacote*|*conferindo\ commit*) printf '0' ;;
    *validando*) printf '4' ;;
    *) printf '2' ;;
  esac
}

zip_progress_advance_macro_index() {
  local candidate="${1:-0}" current="${ZIP_PROGRESS_CURRENT_MACRO_INDEX:--1}"
  local max_index="${ZIP_PROGRESS_MACRO_MAX_INDEX:-9}"
  [[ "$candidate" =~ ^[0-9]+$ ]] || candidate=0
  [[ "$current" =~ ^-?[0-9]+$ ]] || current=-1
  (( candidate > max_index )) && candidate="$max_index"
  (( current > max_index )) && current="$max_index"
  if (( candidate > current )); then
    ZIP_PROGRESS_CURRENT_MACRO_INDEX="$candidate"
  else
    ZIP_PROGRESS_CURRENT_MACRO_INDEX="$current"
  fi
}

zip_progress_add_macro_duration() {
  local macro_index="${1:-}" elapsed_ms="${2:-0}" var current
  [[ "$macro_index" =~ ^[0-9]+$ ]] || return 0
  (( macro_index <= ${ZIP_PROGRESS_MACRO_MAX_INDEX:-9} )) || return 0
  [[ "$elapsed_ms" =~ ^[0-9]+$ ]] || elapsed_ms=0
  var="ZIP_PROGRESS_MACRO_DURATION_MS_${macro_index}"
  current="${!var:-0}"
  [[ "$current" =~ ^[0-9]+$ ]] || current=0
  printf -v "$var" '%d' "$((current + elapsed_ms))"
}

zip_progress_macro_duration_pairs() {
  local index var elapsed_ms duration output=""
  for ((index=0; index<=${ZIP_PROGRESS_MACRO_MAX_INDEX:-9}; index++)); do
    var="ZIP_PROGRESS_MACRO_DURATION_MS_${index}"
    elapsed_ms="${!var:-0}"
    [[ "$elapsed_ms" =~ ^[0-9]+$ ]] || elapsed_ms=0
    (( elapsed_ms > 0 )) || continue
    duration="$(format_update_duration_ms "$elapsed_ms")"
    [[ -n "$output" ]] && output+="|"
    output+="${index}=${duration}"
  done
  printf '%s' "$output"
}

zip_progress_status() {
  if (( ROLLBACK_CONTROL_MODE == 1 )); then
    printf 'progress'
  else
    printf 'applying'
  fi
}

zip_progress_identifier() {
  if (( LOCAL_CANDIDATE_MODE == 1 )); then
    printf '%s' "${LOCAL_CANDIDATE_DISPLAY_ID:-$LOCAL_CANDIDATE_ID}"
  elif (( ROLLBACK_CONTROL_MODE == 1 )); then
    printf '%s' "${ROLLBACK_REQUEST_ID:-controle}"
  elif [[ -n "${SHORT_TO:-}" ]]; then
    printf 'commit %s' "$SHORT_TO"
  else
    printf 'atualização'
  fi
}

zip_progress_hydrate_from_candidate() {
  (( LOCAL_CANDIDATE_MODE == 1 )) || return 0
  (( ZIP_PROGRESS_HANDOFF_LOADED == 0 )) || return 0
  [[ -f "${LOCAL_CANDIDATE_DIR:-}/manifest.json" ]] || return 0

  local record_kind elapsed_ms encoded_label label total_ms=0 summed_ms=0 started_at_ms=0 now_ms line index
  local -a handoff_labels=()
  local -a handoff_elapsed=()

  while IFS=$'\t' read -r record_kind elapsed_ms encoded_label; do
    case "$record_kind" in
      META)
        [[ "$elapsed_ms" =~ ^[0-9]+$ ]] && total_ms="$elapsed_ms"
        ;;
      START)
        [[ "$elapsed_ms" =~ ^[0-9]+$ ]] && started_at_ms="$elapsed_ms"
        ;;
      STEP)
        [[ "$elapsed_ms" =~ ^[0-9]+$ ]] || elapsed_ms=0
        label="$(printf '%s' "$encoded_label" | base64 --decode 2>/dev/null || true)"
        label="$(printf '%s' "$label" | tr '\r\n\t' '   ' | tr -s ' ' | sed 's/^ //;s/ $//' | cut -c1-120)"
        [[ -n "${label//[[:space:]]/}" ]] || continue
        if [[ -n "${ZIP_PROGRESS_DONE_LABELS:-}" ]] \
          && printf '%s\n' "$ZIP_PROGRESS_DONE_LABELS" | grep -Fxq -- "$label"; then
          continue
        fi
        handoff_labels+=("$label")
        handoff_elapsed+=("$elapsed_ms")
        summed_ms=$((summed_ms + elapsed_ms))
        ;;
    esac
  done < <(python3 - "${LOCAL_CANDIDATE_DIR}/manifest.json" <<'PYHANDOFF' 2>/dev/null || true
import base64
import json
import pathlib
import sys

try:
    data = json.loads(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"))
except Exception:
    raise SystemExit(0)

handoff = data.get("progress_handoff")
if not isinstance(handoff, dict) or int(handoff.get("schema_version") or 0) != 1:
    raise SystemExit(0)

try:
    total_ms = max(0, min(int(handoff.get("preparation_total_ms") or 0), 15 * 60 * 1000))
except (TypeError, ValueError):
    total_ms = 0
print(f"META\t{total_ms}\t")
try:
    started_at_ms = max(0, int(handoff.get("started_at_epoch_ms") or 0))
except (TypeError, ValueError):
    started_at_ms = 0
print(f"START\t{started_at_ms}\t")

seen: set[str] = set()
steps = handoff.get("completed_steps")
if not isinstance(steps, list):
    steps = []
for raw in steps[:20]:
    if not isinstance(raw, dict):
        continue
    label = " ".join(str(raw.get("label") or "").split())[:120]
    if not label or label in seen:
        continue
    try:
        elapsed_ms = max(0, min(int(raw.get("elapsed_ms") or 0), 10 * 60 * 1000))
    except (TypeError, ValueError):
        elapsed_ms = 0
    seen.add(label)
    encoded = base64.b64encode(label.encode("utf-8")).decode("ascii")
    print(f"STEP\t{elapsed_ms}\t{encoded}")
PYHANDOFF
  )

  ((${#handoff_labels[@]} > 0)) || return 0
  (( total_ms < summed_ms )) && total_ms="$summed_ms"
  (( total_ms > 900000 )) && total_ms=900000

  for ((index=0; index<${#handoff_labels[@]}; index++)); do
    label="${handoff_labels[$index]}"
    elapsed_ms="${handoff_elapsed[$index]}"
    if [[ -n "${ZIP_PROGRESS_DONE_LABELS:-}" ]]; then
      ZIP_PROGRESS_DONE_LABELS+=$'\n'
    fi
    ZIP_PROGRESS_DONE_LABELS="${ZIP_PROGRESS_DONE_LABELS:-}${label}"
    ZIP_PROGRESS_LAST_DONE_LABEL="$label"
    ZIP_PROGRESS_LAST_DONE_DURATION="$(format_update_duration_ms "$elapsed_ms")"
    ZIP_PROGRESS_LAST_DONE_MACRO_INDEX="$(zip_progress_macro_index "$label")"
    zip_progress_add_macro_duration "$ZIP_PROGRESS_LAST_DONE_MACRO_INDEX" "$elapsed_ms"
    line="-# $label"
    if [[ -n "${ZIP_PROGRESS_HISTORY//[[:space:]]/}" ]]; then
      ZIP_PROGRESS_HISTORY+=$'\n'
    fi
    ZIP_PROGRESS_HISTORY+="$line"
    ZIP_PROGRESS_COMPLETED_COUNT=$((ZIP_PROGRESS_COMPLETED_COUNT + 1))
  done
  zip_progress_trim_history

  now_ms="$(update_now_ms)"
  if [[ "$now_ms" =~ ^[0-9]+$ ]] && (( ZIP_PROGRESS_STARTED_MS <= 0 )); then
    if (( started_at_ms > 0 && started_at_ms <= now_ms )); then
      ZIP_PROGRESS_STARTED_MS="$started_at_ms"
      ZIP_PROGRESS_RECEIVED_AT_MS="$started_at_ms"
      local updater_started_ms="$LOCAL_CANDIDATE_UPDATER_STARTED_MS"
      [[ "$updater_started_ms" =~ ^[0-9]+$ ]] || updater_started_ms="$UPDATER_PROCESS_STARTED_MS"
      if [[ "$updater_started_ms" =~ ^[0-9]+$ ]] && (( updater_started_ms >= started_at_ms )); then
        ZIP_PROGRESS_UPDATER_DELAY_MS=$((updater_started_ms - started_at_ms))
      fi
    else
      ZIP_PROGRESS_STARTED_MS=$((now_ms - total_ms))
      (( ZIP_PROGRESS_STARTED_MS < 0 )) && ZIP_PROGRESS_STARTED_MS=0
    fi
  fi
  ZIP_PROGRESS_STAGE_LABEL=""
  ZIP_PROGRESS_STAGE_STARTED_MS=0
  ZIP_PROGRESS_HANDOFF_LOADED=1
}

zip_progress_trim_history() {
  local line_count
  line_count="$(printf '%s\n' "$ZIP_PROGRESS_HISTORY" | awk 'NF {c++} END {print c+0}')"
  while (( line_count > ZIP_PROGRESS_MAX_VISIBLE_STEPS )); do
    ZIP_PROGRESS_HISTORY="$(printf '%s\n' "$ZIP_PROGRESS_HISTORY" | awk 'BEGIN{removed=0} {if (!removed && NF) {removed=1; next} print}')"
    ZIP_PROGRESS_HIDDEN_COUNT=$((ZIP_PROGRESS_HIDDEN_COUNT + 1))
    line_count=$((line_count - 1))
  done
}

zip_progress_close_active_macro_on_advance() {
  local next_label="${1:-}" now_ms="${2:-0}"
  local active_label="${ZIP_PROGRESS_STAGE_LABEL:-}" active_started="${ZIP_PROGRESS_STAGE_STARTED_MS:-0}"
  local active_macro next_macro elapsed_ms
  [[ -n "${active_label//[[:space:]]/}" ]] || return 0
  [[ "$active_started" =~ ^[0-9]+$ ]] || return 0
  (( active_started > 0 )) || return 0
  [[ "$now_ms" =~ ^[0-9]+$ ]] || return 0

  active_macro="$(zip_progress_macro_index "$active_label")"
  next_macro="$(zip_progress_macro_index "$next_label")"
  [[ "$active_macro" =~ ^[0-9]+$ && "$next_macro" =~ ^[0-9]+$ ]] || return 0
  # Evento atrasado/regressivo não pode fechar nem reatribuir a etapa atual.
  (( next_macro >= active_macro )) || return 0

  elapsed_ms=$((now_ms - active_started))
  (( elapsed_ms < 0 )) && elapsed_ms=0
  zip_progress_add_macro_duration "$active_macro" "$elapsed_ms"
  if (( next_macro > active_macro )); then
    ZIP_PROGRESS_LAST_DONE_LABEL="$active_label"
    ZIP_PROGRESS_LAST_DONE_DURATION="$(format_update_duration_ms "$elapsed_ms")"
    ZIP_PROGRESS_LAST_DONE_MACRO_INDEX="$active_macro"
  fi
}

zip_progress_publish() {
  local stage_label="${1:-Processando atualização}"
  local detail="${2:-}"
  local title status description identifier now_ms elapsed_ms elapsed_text footer stage_changed=0
  now_ms="$(update_now_ms)"
  if (( ZIP_PROGRESS_STARTED_MS <= 0 )); then
    ZIP_PROGRESS_STARTED_MS="$now_ms"
  fi
  if [[ "$ZIP_PROGRESS_STAGE_LABEL" != "$stage_label" && "$ZIP_PROGRESS_STAGE_STARTED_MS" -gt 0 ]]; then
    zip_progress_close_active_macro_on_advance "$stage_label" "$now_ms"
  fi
  if [[ "$ZIP_PROGRESS_STAGE_LABEL" != "$stage_label" || "$ZIP_PROGRESS_STAGE_STARTED_MS" -le 0 ]]; then
    ZIP_PROGRESS_STAGE_LABEL="$stage_label"
    ZIP_PROGRESS_STAGE_STARTED_MS="$now_ms"
    stage_changed=1
  fi
  if declare -F write_update_runtime_state >/dev/null 2>&1; then
    write_update_runtime_state "$stage_label"
  fi
  title="$(zip_progress_title "$stage_label")"
  status="$(zip_progress_status)"
  description=""
  if (( ZIP_PROGRESS_HIDDEN_COUNT > 0 )); then
    if (( ZIP_PROGRESS_HIDDEN_COUNT == 1 )); then
      description+="-# … 1 etapa anterior concluída"$'\n'
    else
      description+="-# … $ZIP_PROGRESS_HIDDEN_COUNT etapas anteriores concluídas"$'\n'
    fi
  fi
  if [[ -n "${ZIP_PROGRESS_HISTORY//[[:space:]]/}" ]]; then
    description+="$ZIP_PROGRESS_HISTORY"$'\n'
  fi
  description+="$UPDATE_STAGE_EMOJI **$stage_label**"
  if [[ -n "${detail//[[:space:]]/}" ]]; then
    description+=$'\n'"-# $detail"
  fi
  identifier="$(zip_progress_identifier)"
  elapsed_ms=$((now_ms - ZIP_PROGRESS_STARTED_MS))
  (( elapsed_ms < 0 )) && elapsed_ms=0
  elapsed_text="$(format_update_duration_ms "$elapsed_ms")"
  if (( ZIP_PROGRESS_COMPLETED_COUNT == 1 )); then
    footer="$identifier · 1 etapa concluída · $elapsed_text"
  else
    footer="$identifier · $ZIP_PROGRESS_COMPLETED_COUNT etapas concluídas · $elapsed_text"
  fi
  description+=$'\n'"-# $footer"
  local macro_index action_name
  macro_index="$(zip_progress_macro_index "$stage_label")"
  zip_progress_advance_macro_index "$macro_index"
  macro_index="$ZIP_PROGRESS_CURRENT_MACRO_INDEX"
  action_name="update"
  if (( ROLLBACK_CONTROL_MODE == 1 )); then
    if [[ "${ROLLBACK_REQUEST_ACTION:-rollback}" == "redo" ]]; then
      action_name="redo"
    else
      action_name="rollback"
    fi
  fi
  local macro_duration_pairs
  macro_duration_pairs="$(zip_progress_macro_duration_pairs)"
  ZIP_STATUS_UI_JSON="$(UI_KIND=progress UI_STAGE="$stage_label" UI_DETAIL="$detail" UI_IDENTIFIER="$identifier" UI_ELAPSED="$elapsed_text" UI_COMPLETED_STAGE="${ZIP_PROGRESS_LAST_DONE_LABEL:-}" UI_COMPLETED_DURATION="${ZIP_PROGRESS_LAST_DONE_DURATION:-}" UI_COMPLETED_MACRO_INDEX="${ZIP_PROGRESS_LAST_DONE_MACRO_INDEX:--1}" UI_MACRO_INDEX="$macro_index" UI_MACRO_DURATIONS="$macro_duration_pairs" UI_ACTION="$action_name" python3 - <<'PYPROGRESSUI'
import json, os
try:
    completed_macro_index = int(os.environ.get("UI_COMPLETED_MACRO_INDEX") or -1)
except (TypeError, ValueError):
    completed_macro_index = -1
macro_durations = {}
for item in (os.environ.get("UI_MACRO_DURATIONS") or "").split("|"):
    if "=" not in item:
        continue
    key, value = item.split("=", 1)
    key, value = key.strip(), value.strip()
    if key.isdigit() and value:
        macro_durations[key] = value
print(json.dumps({
    "kind": "progress",
    "stage": os.environ.get("UI_STAGE") or "Processando atualização",
    "detail": os.environ.get("UI_DETAIL") or "",
    "identifier": os.environ.get("UI_IDENTIFIER") or "",
    "elapsed": os.environ.get("UI_ELAPSED") or "",
    "completed_stage": os.environ.get("UI_COMPLETED_STAGE") or "",
    "completed_duration": os.environ.get("UI_COMPLETED_DURATION") or "",
    "completed_macro_index": completed_macro_index,
    "macro_durations": macro_durations,
    "macro_index": int(os.environ.get("UI_MACRO_INDEX") or 0),
    "action": os.environ.get("UI_ACTION") or "update",
}, ensure_ascii=False))
PYPROGRESSUI
)"
  if (( ROLLBACK_CONTROL_MODE == 1 )); then
    post_direct_update_message "$ROLLBACK_MESSAGE_CHANNEL_ID" "$ROLLBACK_MESSAGE_ID" "$status" "$title" "$description" || true
  else
    notify_zip_status_message "$status" "$title" "$description" || true
  fi
  ZIP_STATUS_UI_JSON=""
  update_local_candidate_heartbeat "active" "" "$stage_label"
}

zip_recovery_publish() {
  local stage_label="${1:-Restaurando versão anterior}"
  local detail="${2:-}"
  local recovery_step="${3:-0}"
  local now_ms elapsed_ms elapsed_text identifier failure_code
  now_ms="$(update_now_ms)"
  if (( ZIP_RECOVERY_STARTED_MS <= 0 )); then
    ZIP_RECOVERY_STARTED_MS="$now_ms"
  fi
  elapsed_ms=$((now_ms - ZIP_RECOVERY_STARTED_MS))
  (( elapsed_ms < 0 )) && elapsed_ms=0
  elapsed_text="$(format_update_duration_ms "$elapsed_ms")"
  identifier="$(zip_progress_identifier)"
  failure_code="${LAST_ERROR_CODE:-UPDATE_STAGE_FAILED}"
  ZIP_STATUS_UI_JSON="$(UI_STAGE="$stage_label" UI_DETAIL="$detail" UI_IDENTIFIER="$identifier" UI_ELAPSED="$elapsed_text" UI_RECOVERY_STEP="$recovery_step" UI_FAILURE_CODE="$failure_code" python3 - <<'PYRECOVERYUI'
import json, os
print(json.dumps({
    "kind": "recovery",
    "stage": os.environ.get("UI_STAGE") or "Restaurando versão anterior",
    "detail": os.environ.get("UI_DETAIL") or "",
    "identifier": os.environ.get("UI_IDENTIFIER") or "",
    "elapsed": os.environ.get("UI_ELAPSED") or "",
    "recovery_step": int(os.environ.get("UI_RECOVERY_STEP") or 0),
    "failure_code": os.environ.get("UI_FAILURE_CODE") or "UPDATE_STAGE_FAILED",
}, ensure_ascii=False))
PYRECOVERYUI
)"
  notify_zip_status_message "recovering" "Atualização falhou" "$detail" || true
  ZIP_STATUS_UI_JSON=""
}

zip_progress_done() {
  local done_label="${1:-}"
  [[ -n "${done_label//[[:space:]]/}" ]] || return 0
  # Uma retomada ou uma transição repetida não pode recolocar a mesma microetapa
  # no histórico. O painel sempre avança de forma monotônica.
  if [[ -n "${ZIP_PROGRESS_DONE_LABELS:-}" ]] \
    && printf '%s\n' "${ZIP_PROGRESS_DONE_LABELS:-}" | grep -Fxq -- "$done_label"; then
    return 0
  fi
  if [[ -n "${ZIP_PROGRESS_DONE_LABELS:-}" ]]; then
    ZIP_PROGRESS_DONE_LABELS+=$'\n'
  fi
  ZIP_PROGRESS_DONE_LABELS="${ZIP_PROGRESS_DONE_LABELS:-}${done_label}"
  local now_ms elapsed_ms elapsed_text line
  now_ms="$(update_now_ms)"
  if (( ZIP_PROGRESS_STAGE_STARTED_MS > 0 )); then
    elapsed_ms=$((now_ms - ZIP_PROGRESS_STAGE_STARTED_MS))
  else
    elapsed_ms=0
  fi
  (( elapsed_ms < 0 )) && elapsed_ms=0
  elapsed_text="$(format_update_duration_ms "$elapsed_ms")"
  local completed_macro_source completed_macro_index
  completed_macro_source="${ZIP_PROGRESS_STAGE_LABEL:-$done_label}"
  completed_macro_index="$(zip_progress_macro_index "$completed_macro_source")"
  ZIP_PROGRESS_LAST_DONE_LABEL="$done_label"
  ZIP_PROGRESS_LAST_DONE_DURATION="$elapsed_text"
  ZIP_PROGRESS_LAST_DONE_MACRO_INDEX="$completed_macro_index"
  zip_progress_add_macro_duration "$completed_macro_index" "$elapsed_ms"
  ZIP_PROGRESS_COMPLETED_COUNT=$((ZIP_PROGRESS_COMPLETED_COUNT + 1))
  line="-# $done_label"
  if [[ -n "${ZIP_PROGRESS_HISTORY//[[:space:]]/}" ]]; then
    ZIP_PROGRESS_HISTORY+=$'\n'
  fi
  ZIP_PROGRESS_HISTORY+="$line"
  zip_progress_trim_history
  ZIP_PROGRESS_STAGE_LABEL=""
  ZIP_PROGRESS_STAGE_STARTED_MS=0
}

zip_progress_done_and_publish() {
  local done_label="${1:-}"
  local next_label="${2:-Processando atualização}"
  local detail="${3:-}"
  zip_progress_done "$done_label"
  zip_progress_publish "$next_label" "$detail"
}

zip_progress_heartbeat_seconds() {
  local interval="${DISCORD_AUTO_UPDATE_PROGRESS_HEARTBEAT_SECONDS:-12}"
  [[ "$interval" =~ ^[0-9]+$ ]] || interval=12
  (( interval < 5 )) && interval=5
  (( interval > 60 )) && interval=60
  printf '%s' "$interval"
}

zip_progress_run_as_ubuntu() {
  local stage_label="${1:?}"
  local detail="${2:-Em andamento}"
  local command="${3:?}"
  local pid rc started_ms now_ms elapsed_ms interval next_publish_ms stage_log

  zip_progress_publish "$stage_label" "$detail"
  interval="$(zip_progress_heartbeat_seconds)"
  started_ms="$(update_now_ms)"
  next_publish_ms=$((started_ms + interval * 1000))
  stage_log="$(stage_log_file_for "$STAGE" 2>/dev/null || true)"
  CURRENT_STAGE_LOG_FILE="$stage_log"
  CURRENT_STAGE_LOG_STAGE="$STAGE"
  CURRENT_STAGE_COMMAND="$command"
  if [[ -n "$stage_log" ]]; then
    : > "$stage_log" 2>/dev/null || true
    chmod 0644 "$stage_log" 2>/dev/null || true
  fi

  # O comando roda em um shell filho sem herdar o trap ERR transacional. Assim,
  # o pai pode acompanhar o PID, publicar heartbeats e tratar o status uma vez.
  # Quando possível, a saída também é preservada num log persistente exclusivo
  # da etapa; o tee global continua recebendo a mesma saída normalmente.
  if [[ -n "$stage_log" ]]; then
    ( set -o pipefail; sudo -u ubuntu -H bash -lc "$command" 2>&1 | tee -a "$stage_log" ) &
  else
    sudo -u ubuntu -H bash -lc "$command" &
  fi
  pid=$!

  while kill -0 "$pid" 2>/dev/null; do
    sleep 1
    kill -0 "$pid" 2>/dev/null || break
    now_ms="$(update_now_ms)"
    if (( now_ms >= next_publish_ms )); then
      elapsed_ms=$((now_ms - started_ms))
      (( elapsed_ms < 0 )) && elapsed_ms=0
      zip_progress_publish "$stage_label" "$detail"
      next_publish_ms=$((now_ms + interval * 1000))
    fi
  done

  local restore_errexit=0
  [[ $- == *e* ]] && restore_errexit=1
  set +e
  wait "$pid"
  rc=$?
  if (( restore_errexit == 1 )); then
    set -e
  else
    set +e
  fi
  return "$rc"
}

zip_progress_only_site_changed() {
  (( FRONT_CHANGED == 1 || BACK_CHANGED == 1 )) || return 1
  (( BOT_CHANGED == 0 )) || return 1
  (( REQUIREMENTS_CHANGED == 0 )) || return 1
  (( AUDIO_SYSTEMD_CHANGED == 0 )) || return 1
  (( CLEANUP_CHANGED == 0 )) || return 1
  (( PHONE_LAVALINK_WATCH_CHANGED == 0 )) || return 1
  (( PHONE_WORKER_WATCH_CHANGED == 0 )) || return 1
  (( VPS_SYSTEMD_UNITS_CHANGED == 0 )) || return 1
  (( ALERT_CHANGED == 0 )) || return 1
  (( PHONE_WORKER_SYNC_REQUIRED == 0 )) || return 1
  (( CORE_WORKER_APK_CHANGED == 0 )) || return 1
  (( CORE_WORKER_AUTOMATION_REQUIRED == 0 )) || return 1
  return 0
}

zip_progress_process_detail() {
  local processes
  processes="$(format_changed_processes 2>/dev/null || true)"
  if [[ -n "${processes//[[:space:]]/}" && "$processes" != "nenhum processo alterado" ]]; then
    printf '%s' "$processes"
  fi
}

format_cog_module_names() {
  local modules_text="${1:-}"
  MODULES_TEXT="$modules_text" python3 - <<'PYCOGS'
import os
mods = []
for raw in (os.environ.get("MODULES_TEXT") or "").splitlines():
    raw = raw.strip()
    if not raw:
        continue
    name = raw
    if name.startswith("cogs."):
        name = name[5:]
    mods.append(name.replace("_", "-"))
print(", ".join(dict.fromkeys(mods)))
PYCOGS
}

fast_reload_stage_label() {
  local modules_text="${1:-}"
  [[ -n "${modules_text//[[:space:]]/}" ]] || return 1
  local names count
  names="$(format_cog_module_names "$modules_text")"
  count="$(printf '%s\n' "$modules_text" | awk 'NF {c++} END {print c+0}')"
  if [[ "$count" =~ ^[0-9]+$ && "$count" -gt 1 ]]; then
    printf 'Recarregando cogs: %s' "$names"
  else
    printf 'Recarregando cog: %s' "$names"
  fi
}

zip_progress_next_apply_stage() {
  local fast_modules process_detail
  fast_modules="$(fast_reload_modules_for_changed_files 2>/dev/null || true)"
  FAST_RELOAD_MODULES="$fast_modules"
  if [[ -n "${fast_modules//[[:space:]]/}" ]]; then
    fast_reload_stage_label "$fast_modules"
    return 0
  fi
  # Mostre a primeira operação real, não um reinício genérico. Em patches do
  # site, npm ci/build é normalmente a parte mais longa e precisa ficar visível.
  if (( BOT_CHANGED == 0 && FRONT_CHANGED == 1 )); then
    printf 'Instalando dependências'
    return 0
  fi
  if (( BOT_CHANGED == 0 && FRONT_CHANGED == 0 && BACK_CHANGED == 1 )); then
    printf 'Preparando servidor'
    return 0
  fi
  process_detail="$(zip_progress_process_detail)"
  if [[ -n "${process_detail//[[:space:]]/}" ]]; then
    if [[ "$process_detail" == *,* ]]; then
      printf 'Reiniciando processos: %s' "$process_detail"
    else
      printf 'Reiniciando processo: %s' "$process_detail"
    fi
    return 0
  fi
  printf 'Validando aplicação'
}

zip_progress_done_apply_stage() {
  # Frontend/backend já publicam suas próprias fases e health check. Não acrescente
  # depois uma etapa genérica de “processo reiniciado”, que fazia o painel parecer
  # voltar ao início justamente quando o build terminava.
  if zip_progress_only_site_changed; then
    return 0
  fi
  local process_detail names count
  if [[ "${FAST_RELOAD_STATUS:-}" == "OK"* && -n "${FAST_RELOAD_MODULES//[[:space:]]/}" ]]; then
    names="$(format_cog_module_names "$FAST_RELOAD_MODULES")"
    count="$(printf '%s\n' "$FAST_RELOAD_MODULES" | awk 'NF {c++} END {print c+0}')"
    if [[ "$count" =~ ^[0-9]+$ && "$count" -gt 1 ]]; then
      zip_progress_done "Cogs recarregadas: **$names**"
    else
      zip_progress_done "Cog recarregada: **$names**"
    fi
    return 0
  fi
  process_detail="$(zip_progress_process_detail)"
  if [[ -n "${process_detail//[[:space:]]/}" ]]; then
    zip_progress_done "Processos reiniciados: **$process_detail**"
  else
    zip_progress_done "Aplicação validada"
  fi
}

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

format_changed_files() {
  if [[ -n "$CHANGED_DIFF_NUMSTAT_RAW" ]]; then
    CHANGED_DIFF_NUMSTAT_INPUT="$CHANGED_DIFF_NUMSTAT_RAW" python3 - <<'PYDIFF'
import os
raw = os.environ.get("CHANGED_DIFF_NUMSTAT_INPUT") or ""
lines = []
for item in raw.splitlines():
    parts = item.split("\t")
    if len(parts) < 3:
        continue
    add, rem, path = parts[0], parts[1], parts[-1]
    if add == "-" or rem == "-":
        lines.append(f"• {path}  binário")
    else:
        lines.append(f"• {path}  +{int(add or 0)} -{int(rem or 0)}")
limit = 20
for line in lines[:limit]:
    print(line)
if len(lines) > limit:
    remaining = len(lines) - limit
    label = 'arquivo restante' if remaining == 1 else 'arquivos restantes'
    print(f"+{remaining} {label}")
if not lines:
    print("• nenhum arquivo listado")
PYDIFF
  elif [[ -n "$CHANGED_FILES_RAW" ]]; then
    printf '%s\n' "$CHANGED_FILES_RAW" | head -n 20 | sed 's/^/• /'
    local total
    total="$(printf '%s\n' "$CHANGED_FILES_RAW" | awk 'NF {c++} END {print c+0}')"
    if [[ "$total" =~ ^[0-9]+$ && "$total" -gt 20 ]]; then
      local remaining=$((total - 20))
      if (( remaining == 1 )); then
        printf '+1 arquivo restante\n'
      else
        printf '+%s arquivos restantes\n' "$remaining"
      fi
    fi
  else
    printf '• nenhum arquivo listado'
  fi
}

format_diff_total_summary() {
  if [[ -z "$CHANGED_DIFF_NUMSTAT_RAW" ]]; then
    printf 'diff indisponível'
    return 0
  fi
  CHANGED_DIFF_NUMSTAT_INPUT="$CHANGED_DIFF_NUMSTAT_RAW" python3 - <<'PYDIFF'
import os
raw = os.environ.get("CHANGED_DIFF_NUMSTAT_INPUT") or ""
added = removed = binaries = 0
for item in raw.splitlines():
    parts = item.split("\t")
    if len(parts) < 3:
        continue
    add, rem = parts[0], parts[1]
    if add == "-" or rem == "-":
        binaries += 1
        continue
    added += int(add or 0)
    removed += int(rem or 0)
out = f"+{added} -{removed}"
if binaries:
    out += f" · {binaries} " + ('binário' if binaries == 1 else 'binários')
print(out)
PYDIFF
}

classify_changed_files() {
  FRONT_CHANGED=0
  BACK_CHANGED=0
  FRONT_TESTS_CHANGED=0
  BACK_TESTS_CHANGED=0
  FRONT_TESTS_REQUIRED=0
  FRONT_TYPECHECK_REQUIRED=0
  BACK_TESTS_REQUIRED=0
  FRONT_TEST_PLAN_STATUS="não executado"
  BACK_TEST_PLAN_STATUS="não executado"
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
  APP_COMMANDS_MAY_HAVE_CHANGED=0

  # Classificação é feita em uma única passagem Bash. A versão anterior
  # disparava dezenas de grep/pipes a cada refresh do diff, custo perceptível
  # em updates pequenos e repetido no staging/retomada.
  local file
  while IFS= read -r file; do
    [[ -n "$file" ]] || continue

    case "$file" in
      bot.py|cogs/*.py|utility/commands/*.py)
        APP_COMMANDS_MAY_HAVE_CHANGED=1
        ;;
    esac

    case "$file" in
      dashboard/frontend/tests/*)
        FRONT_TESTS_CHANGED=1
        FRONT_TESTS_REQUIRED=1
        ;;
      dashboard/frontend/*|activity/sinuca/*)
        FRONT_CHANGED=1
        case "$file" in
          dashboard/frontend/src/*.css|dashboard/frontend/index.html)
            ;;
          *)
            FRONT_TESTS_REQUIRED=1
            FRONT_TYPECHECK_REQUIRED=1
            ;;
        esac
        ;;
    esac

    case "$file" in
      dashboard/frontend/src/*.ts|dashboard/frontend/src/*.tsx|dashboard/frontend/tsconfig.json|dashboard/frontend/package.json|dashboard/frontend/package-lock.json)
        FRONT_TYPECHECK_REQUIRED=1
        ;;
    esac

    case "$file" in
      dashboard/backend/tests/*)
        BACK_TESTS_CHANGED=1
        BACK_TESTS_REQUIRED=1
        ;;
      dashboard/backend/*|activity/sinuca-server/*)
        BACK_CHANGED=1
        BACK_TESTS_REQUIRED=1
        ;;
      shared/help_catalog.json)
        BACK_CHANGED=1
        ;;
    esac

    case "$file" in
      bot.py|webserver.py|config.py|db.py|start.sh|requirements.txt|requirements.lock|cogs/*|music_system/*|utility/*)
        BOT_CHANGED=1
        ;;
      deploy/systemd/tts-bot.service|deploy/systemd/vps/tts-bot.service)
        BOT_CHANGED=1
        ;;
    esac

    [[ "$file" == "requirements.txt" || "$file" == "requirements.lock" ]] && REQUIREMENTS_CHANGED=1

    case "$file" in
      deploy/systemd/lavalink.service|deploy/systemd/tts-bot.service|deploy/systemd/tts-bot-alert@.service)
        AUDIO_SYSTEMD_CHANGED=1
        ;;
    esac
    case "$file" in
      alert.sh|deploy/systemd/tts-bot-alert@.service|deploy/systemd/vps/tts-bot-alert@.service)
        ALERT_CHANGED=1
        ;;
    esac
    case "$file" in
      deploy/systemd/tts-bot.service|deploy/systemd/tts-bot-updater.service|deploy/systemd/tts-bot-updater.timer|deploy/systemd/tts-bot-updater.path|deploy/systemd/tts-bot-alert@.service|deploy/systemd/cleanup-audio-temp.service|deploy/systemd/cleanup-audio-temp.timer|deploy/systemd/sinuca-activity-server.service|deploy/systemd/phone-worker-watch.service|deploy/systemd/phone-worker-watch.timer|deploy/systemd/tts-bot.service.d/*|deploy/systemd/vps/tts-bot.service|deploy/systemd/vps/tts-bot-updater.service|deploy/systemd/vps/tts-bot-updater.timer|deploy/systemd/vps/tts-bot-updater.path|deploy/systemd/vps/tts-bot-alert@.service|deploy/systemd/vps/cleanup-audio-temp.service|deploy/systemd/vps/cleanup-audio-temp.timer|deploy/systemd/vps/sinuca-activity-server.service|deploy/systemd/vps/phone-worker-watch.service|deploy/systemd/vps/phone-worker-watch.timer|deploy/systemd/vps/tts-bot.service.d/*|deploy/sudoers.d/*|deploy/journald/*|deploy/tmpfiles.d/*|scripts/install-vps-systemd-units.sh)
        VPS_SYSTEMD_UNITS_CHANGED=1
        ;;
    esac
    case "$file" in
      cleanup-audio-temp.sh|deploy/systemd/cleanup-audio-temp.service|deploy/systemd/cleanup-audio-temp.timer)
        CLEANUP_CHANGED=1
        ;;
    esac
    case "$file" in
      scripts/phone-lavalink-watch.sh|deploy/systemd/phone-lavalink-watch.service|deploy/systemd/phone-lavalink-watch.timer|deploy/termux/phone-lavalink/*)
        PHONE_LAVALINK_WATCH_CHANGED=1
        ;;
    esac
    case "$file" in
      scripts/phone-worker-watch.sh|scripts/phone-worker-client.py|deploy/systemd/phone-worker-watch.service|deploy/systemd/phone-worker-watch.timer|deploy/termux/phone-worker/*)
        PHONE_WORKER_WATCH_CHANGED=1
        ;;
    esac
    case "$file" in
      deploy/termux/phone-worker/*)
        PHONE_WORKER_SYNC_REQUIRED=1
        CORE_WORKER_AUTOMATION_REQUIRED=1
        ;;
      android/core-worker-app/*)
        CORE_WORKER_APK_CHANGED=1
        CORE_WORKER_AUTOMATION_REQUIRED=1
        ;;
      scripts/core-worker-automation.py|utility/commands/workers_registry.py|webserver.py)
        CORE_WORKER_AUTOMATION_REQUIRED=1
        ;;
    esac
  done <<< "$CHANGED_FILES_RAW"
}

fast_reload_modules_for_changed_files() {
  CHANGED_FILES_RAW_INPUT="$CHANGED_FILES_RAW" \
  HOT_RELOAD_ALLOW="${DISCORD_AUTO_UPDATE_HOT_RELOAD_ALLOW:-}" \
  HOT_RELOAD_DENY="${DISCORD_AUTO_UPDATE_HOT_RELOAD_DENY:-music,dashboard_sync,terminal_cmd}" \
  python3 - <<'PYFAST'
import os, pathlib, re
raw = [line.strip() for line in (os.environ.get("CHANGED_FILES_RAW_INPUT") or "").splitlines() if line.strip()]
if not raw:
    raise SystemExit(1)

def names(value):
    return {part.strip().removeprefix("cogs.").removesuffix(".py") for part in re.split(r"[,;\s]+", value or "") if part.strip()}

allow = names(os.environ.get("HOT_RELOAD_ALLOW") or "")
deny = names(os.environ.get("HOT_RELOAD_DENY") or "") | {"__init__"}
modules = []
for path in raw:
    parts = pathlib.PurePosixPath(path).parts
    if len(parts) != 2 or parts[0] != "cogs" or not parts[1].endswith(".py"):
        raise SystemExit(1)
    name = parts[1][:-3]
    if name in deny or (allow and name not in allow):
        raise SystemExit(1)
    modules.append("cogs." + name)
print("\n".join(dict.fromkeys(modules)))
PYFAST
}

try_fast_cog_reload() {
  local modules_text="${1:-}"
  local check_app_commands="${2:-0}"
  [[ -n "${modules_text//[[:space:]]/}" ]] || return 1
  local payload token header_args=() response http_code verification_epoch restarts_before
  verification_epoch="$(date +%s)"
  restarts_before="$(service_restart_count "$SERVICE")"
  payload="$(MODULES_TEXT="$modules_text" CHECK_APP_COMMANDS="$check_app_commands" python3 - <<'PYPAYLOAD'
import json, os
mods = [line.strip() for line in (os.environ.get("MODULES_TEXT") or "").splitlines() if line.strip()]
check = str(os.environ.get("CHECK_APP_COMMANDS") or "").strip().lower() in {"1", "true", "yes", "sim", "on"}
print(json.dumps({"modules": mods, "check_app_commands": check}, ensure_ascii=False))
PYPAYLOAD
)"
  token=""
  if [[ -f "$REPO_DIR/.env" ]]; then
    token="$(grep -E '^BOT_INTERNAL_UPDATE_TOKEN=' "$REPO_DIR/.env" 2>/dev/null | tail -n 1 | cut -d= -f2- | tr -d ' "' || true)"
  fi
  if [[ -n "$token" ]]; then
    header_args=(-H "X-Update-Token: $token")
  fi
  response="$(mktemp)"
  http_code="$(curl -sS -o "$response" -w '%{http_code}' --max-time 90 -H 'Content-Type: application/json' "${header_args[@]}" -d "$payload" http://127.0.0.1:10000/internal/update/reload-cogs 2>/dev/null || true)"
  if [[ "$http_code" != "200" ]]; then
    FAST_RELOAD_STATUS="falhou; fallback restart (${http_code:-sem HTTP})"
    logger -t "$LOG_TAG" "Fast reload falhou HTTP=${http_code:-sem HTTP}: $(cat "$response" 2>/dev/null | tail -c 300)"
    rm -f "$response"
    return 1
  fi
  if ! python3 - "$response" <<'PYOK' >/dev/null 2>&1; then
import json, sys
p = sys.argv[1]
data = json.load(open(p, encoding='utf-8'))
raise SystemExit(0 if data.get('ok') is True else 1)
PYOK
    FAST_RELOAD_STATUS="falhou; fallback restart"
    logger -t "$LOG_TAG" "Fast reload retornou falha: $(cat "$response" 2>/dev/null | tail -c 500)"
    rm -f "$response"
    return 1
  fi
  rm -f "$response"
  STAGE="estabilidade após reload rápido"
  if verify_bot_after_restart "$verification_epoch" "$restarts_before" 0 reload; then
    FAST_RELOAD_STATUS="OK; estabilidade confirmada"
    return 0
  fi
  FAST_RELOAD_STATUS="reload executado; estabilidade falhou; fallback restart"
  return 1
}

cleanup_known_generated_update_artifacts() {
  # Estes arquivos/pastas são gerados por build/publicação do Core Worker e
  # não devem bloquear o auto updater. Não remove código fonte nem registry.
  sudo -u ubuntu -H rm -rf "$REPO_DIR/android/core-worker-app/app/build" 2>/dev/null || rm -rf "$REPO_DIR/android/core-worker-app/app/build" 2>/dev/null || true
  sudo -u ubuntu -H rm -rf "$REPO_DIR/android/core-worker-app/.gradle" 2>/dev/null || rm -rf "$REPO_DIR/android/core-worker-app/.gradle" 2>/dev/null || true
  sudo -u ubuntu -H rm -f "$REPO_DIR/android/core-worker-app/app/build.gradle.bak"* 2>/dev/null || rm -f "$REPO_DIR/android/core-worker-app/app/build.gradle.bak"* 2>/dev/null || true
  # Não removemos android/core-worker-app/releases aqui: é onde latest.json/APKs
  # privados ficam publicados para os celulares. O auto updater já ignora essa
  # pasta ao criar commits e alterações não rastreadas não bloqueiam git pull.
}

local_changes_fingerprint() {
  if declare -F load_repo_status_snapshot >/dev/null 2>&1 && load_repo_status_snapshot; then
    printf '%s\n' "${GIT_STATUS_FINGERPRINT:-}"
    return 0
  fi
  repo_git status --porcelain=v1 --untracked-files=no 2>/dev/null | sha256sum | awk '{print $1}'
}

clear_local_changes_marker_if_clean() {
  local reuse_snapshot="${1:-0}" status_text
  if [[ "$reuse_snapshot" == "1" && "${GIT_STATUS_SNAPSHOT_READY:-0}" == "1" ]]; then
    status_text="$(printf '%s' "${GIT_STATUS_RAW:-}" | trim_alert_text 1800)"
  else
    status_text="$(collect_local_tracked_changes)"
  fi
  if [[ -z "${status_text//[[:space:]]/}" ]]; then
    rm -f "$LOCAL_CHANGES_MARKER_FILE" 2>/dev/null || true
  fi
}

collect_local_tracked_changes() {
  # Um único status snapshot substitui status + diff + diff --cached.
  if declare -F load_repo_status_snapshot >/dev/null 2>&1 && load_repo_status_snapshot; then
    printf '%s' "${GIT_STATUS_RAW:-}" | trim_alert_text 1800
    return 0
  fi
  repo_git status --short --untracked-files=no 2>/dev/null | trim_alert_text 1800 || true
}

format_tracked_files_for_alert() {
  local raw="${1:-}" item count=0 text=""
  while IFS= read -r item; do
    [[ -n "$item" ]] || continue
    (( count < 40 )) || break
    [[ -z "$text" ]] || text+=$'\n'
    text+="• $item"
    count=$((count + 1))
  done <<< "$raw"
  if (( ${#text} > 1500 )); then
    text="${text:0:1499}…"
  fi
  printf '%s\n' "$text"
}

collect_local_tracked_files() {
  if declare -F load_repo_status_snapshot >/dev/null 2>&1 && load_repo_status_snapshot; then
    format_tracked_files_for_alert "${GIT_STATUS_FILES_RAW:-}"
    return 0
  fi
  {
    repo_git diff --name-only 2>/dev/null || true
    repo_git diff --name-only --cached 2>/dev/null || true
  } | awk '!seen[$0]++ && NF {print "• " $0; if (++n >= 40) exit}' | trim_alert_text 1500
}

candidate_local_changes_are_expected() {
  local reuse_snapshot="${1:-0}"
  (( LOCAL_CANDIDATE_MODE == 1 )) || return 1
  [[ -n "${CHANGED_FILES_RAW//[[:space:]]/}" ]] || return 1
  if ! declare -F load_repo_status_snapshot >/dev/null 2>&1; then
    CHANGED_FILES_RAW="$CHANGED_FILES_RAW" REPO_DIR="$REPO_DIR" python3 - <<'PYCANDIDATE_DIRTY_FALLBACK'
import os
import subprocess
import sys
expected = {line.strip() for line in os.environ.get("CHANGED_FILES_RAW", "").splitlines() if line.strip()}
repo = os.environ.get("REPO_DIR", "/home/ubuntu/bot")
def git_lines(*args: str) -> set[str]:
    cp = subprocess.run(
        ["sudo", "-u", "ubuntu", "-H", "git", *args],
        cwd=repo, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    if cp.returncode != 0:
        print((cp.stderr or "git falhou sem stderr").strip(), file=sys.stderr)
        raise SystemExit(2)
    return {line.strip() for line in cp.stdout.splitlines() if line.strip()}
dirty = git_lines("diff", "--name-only") | git_lines("diff", "--name-only", "--cached")
extra = dirty - expected
if extra:
    print("\n".join(sorted(extra)))
    raise SystemExit(1)
raise SystemExit(0)
PYCANDIDATE_DIRTY_FALLBACK
    return $?
  fi
  if [[ "$reuse_snapshot" != "1" || "${GIT_STATUS_SNAPSHOT_READY:-0}" != "1" ]]; then
    load_repo_status_snapshot || return 2
  fi
  [[ -n "${GIT_STATUS_FILES_RAW//[[:space:]]/}" ]] || return 0

  local dirty expected found
  while IFS= read -r dirty; do
    [[ -n "$dirty" ]] || continue
    found=0
    while IFS= read -r expected; do
      [[ "$dirty" == "$expected" ]] && { found=1; break; }
    done <<< "$CHANGED_FILES_RAW"
    (( found == 1 )) || {
      printf '%s\n' "$dirty"
      return 1
    }
  done <<< "$GIT_STATUS_FILES_RAW"
  return 0
}

ensure_no_unstaged_tracked_changes() {
  if declare -F load_repo_status_snapshot >/dev/null 2>&1; then
    if ! load_repo_status_snapshot; then
      LAST_ERROR_STDERR="não foi possível verificar alterações rastreadas após o build"
      return 1
    fi
    local dirty="${GIT_STATUS_UNSTAGED_FILES_RAW:-}"
    if [[ -z "${dirty//[[:space:]]/}" ]]; then
      return 0
    fi
    LAST_ERROR_STDERR="arquivos rastreados foram alterados durante build/deploy:
$(printf '%s\n' "$dirty" | trim_alert_text 1500)"
    logger -t "$LOG_TAG" "$LAST_ERROR_STDERR" 2>/dev/null || true
    return 1
  fi

  local dirty
  if ! dirty="$(repo_git diff --name-only 2>/dev/null)"; then
    LAST_ERROR_STDERR="não foi possível verificar alterações rastreadas após o build"
    return 1
  fi
  [[ -z "${dirty//[[:space:]]/}" ]] && return 0
  LAST_ERROR_STDERR="arquivos rastreados foram alterados durante build/deploy:
$(printf '%s\n' "$dirty" | trim_alert_text 1500)"
  logger -t "$LOG_TAG" "$LAST_ERROR_STDERR" 2>/dev/null || true
  return 1
}


fail_local_changes_before_pull() {
  local status_text files_text duration body fingerprint previous_fingerprint
  if declare -F load_repo_status_snapshot >/dev/null 2>&1 && load_repo_status_snapshot; then
    status_text="$(printf '%s' "${GIT_STATUS_RAW:-}" | trim_alert_text 1800)"
    fingerprint="${GIT_STATUS_FINGERPRINT:-}"
    files_text="$(format_tracked_files_for_alert "${GIT_STATUS_FILES_RAW:-}")"
  else
    status_text="$(collect_local_tracked_changes)"
    fingerprint="$(local_changes_fingerprint)"
    files_text="$(collect_local_tracked_files)"
  fi
  if [[ -z "${status_text//[[:space:]]/}" ]]; then
    return 0
  fi

  previous_fingerprint=""
  if [[ -f "$LOCAL_CHANGES_MARKER_FILE" ]]; then
    previous_fingerprint="$(awk -F= '$1 == "FINGERPRINT" { sub($1 "=", ""); print; exit }' "$LOCAL_CHANGES_MARKER_FILE" 2>/dev/null || true)"
  fi
  if [[ -n "$fingerprint" && "$fingerprint" == "$previous_fingerprint" ]]; then
    logger -t "$LOG_TAG" "Alterações locais ainda bloqueiam o update; alerta já enviado para este mesmo estado."
    exit 1
  fi
  MANUAL_FAILURE_ALERT_SENT=1
  duration="$(human_duration "$SECONDS")"
  cat > "$LOCAL_CHANGES_MARKER_FILE" <<EOM
FINGERPRINT=$fingerprint
REMOTE_COMMIT=$REMOTE_COMMIT
CURRENT_COMMIT=$CURRENT_COMMIT
AT=$(date -u '+%Y-%m-%dT%H:%M:%SZ')
EOM
  chown ubuntu:ubuntu "$LOCAL_CHANGES_MARKER_FILE" 2>/dev/null || true

  body="Resumo: O updater parou antes do git pull porque existem alterações locais em arquivos rastreados. O Git bloqueou o merge para não sobrescrever seus testes na VPS.
Host: $HOSTNAME
Branch: $BRANCH
Serviço: tts-bot-updater
Serviço afetado: $UPDATER_UNIT
Commit anterior: $(short_commit "$CURRENT_COMMIT")
Commit alvo: $(short_commit "$REMOTE_COMMIT")
Commit: $(short_commit "$CURRENT_COMMIT") → $(short_commit "$REMOTE_COMMIT")
Update: ${COMMIT_SUBJECT:-sem mensagem}
Etapa: verificação de alterações locais
Código: 1
Rollback: não foi necessário
Commit sujo: sim
Diagnóstico: existem mudanças locais que seriam sobrescritas pelo merge.
Arquivos locais:
${files_text:-nenhum arquivo listado}
Status git:
$status_text
Ação sugerida: deixe o repo limpo antes do updater. Normalmente: git restore <arquivo> e rm -rf android/core-worker-app/app/build android/core-worker-app/releases. Se a alteração for intencional, envie como patch oficial em ZIP.
Duração: $duration
Hora: $(date '+%d/%m/%Y %H:%M:%S')"

  send_error "Falha na atualização automática: alterações locais" "$body"
  exit 1
}


sanitize_vps_lavalink_units() {
  # Remove dependências locais do Lavalink. O node de áudio válido fica no phone worker/Music Agent.
  local file changed_any=0
  for file in /etc/systemd/system/tts-bot.service /etc/systemd/system/tts-bot.service.d/*.conf; do
    [[ -f "$file" ]] || continue
    if grep -q 'lavalink.service' "$file" 2>/dev/null; then
      cp -a "$file" "$file.backup.$(date +%Y%m%d-%H%M%S)" 2>/dev/null || true
      python3 - "$file" <<'PY_SANITIZE_LAVALINK'
import sys
from pathlib import Path
p=Path(sys.argv[1])
text=p.read_text(encoding='utf-8', errors='replace')
out=[]
for line in text.splitlines():
    stripped=line.strip()
    if stripped.startswith(('Wants=', 'Requires=', 'After=', 'Before=')) and 'lavalink.service' in stripped:
        key, value = line.split('=', 1)
        parts=[part for part in value.split() if part != 'lavalink.service']
        if parts:
            out.append(key + '=' + ' '.join(parts))
        else:
            out.append('# ' + key + '=lavalink.service removido: Lavalink roda no phone worker/Music Agent')
        continue
    if stripped.startswith('ExecStartPre=') and 'wait-audio-node-ready.py' in stripped:
        out.append('# ExecStartPre wait-audio-node-ready.py removido: Lavalink local da VPS não é usado')
        continue
    out.append(line)
p.write_text('\n'.join(out).rstrip() + '\n', encoding='utf-8')
PY_SANITIZE_LAVALINK
      changed_any=1
    fi
  done
  if ! env_truthy VPS_LAVALINK_ENABLED; then
    systemctl stop lavalink.service >/dev/null 2>&1 || true
    systemctl disable lavalink.service >/dev/null 2>&1 || true
    systemctl reset-failed lavalink.service >/dev/null 2>&1 || true
    # systemctl mask falha quando /etc/systemd/system/lavalink.service é um arquivo
    # real. Fazemos a máscara idempotente manualmente para impedir restart-loop local.
    if [[ -e /etc/systemd/system/lavalink.service && ! -L /etc/systemd/system/lavalink.service ]]; then
      cp -a /etc/systemd/system/lavalink.service "/etc/systemd/system/lavalink.service.backup.$(date +%Y%m%d-%H%M%S)" 2>/dev/null || true
      mv /etc/systemd/system/lavalink.service "/etc/systemd/system/lavalink.service.disabled.$(date +%Y%m%d-%H%M%S)" 2>/dev/null || true
    fi
    ln -sfn /dev/null /etc/systemd/system/lavalink.service 2>/dev/null || true
    changed_any=1
  fi
  if (( changed_any == 1 )); then
    systemctl daemon-reload || true
  fi
}


normalize_healthcheck_crontab() {
  # Corrige linhas temporárias quebradas criadas durante diagnóstico manual.
  # Não reativa healthcheck/resource-check automaticamente: eles continuam
  # pausados até a correção passar pelo patch e pelo operador.
  STAGE="normalização do crontab de emergência"
  local tmp current
  tmp="${TMPDIR:-/tmp}/tts-bot-cron.$$"
  current="${TMPDIR:-/tmp}/tts-bot-cron-current.$$"
  if ! sudo -u ubuntu -H crontab -l > "$current" 2>/dev/null; then
    CRONTAB_HEALTH_STATUS="sem crontab do usuário ubuntu"
    rm -f "$tmp" "$current" 2>/dev/null || true
    return 0
  fi
  python3 - "$current" "$tmp" <<'PY_CRON'
import sys
from pathlib import Path

src = Path(sys.argv[1])
dst = Path(sys.argv[2])
text = src.read_text(encoding='utf-8', errors='replace')

HEALTH_DISABLED = '# TEMP_DISABLED_HEALTHCHECK_UNTIL_PATCH_20260524 * * * * * /home/ubuntu/bot/healthcheck.sh >/dev/null 2>&1'
RESOURCE_DISABLED = '# TEMP_DISABLED_EMERGENCY_20260524 */5 * * * * /home/ubuntu/bot/resource-check.sh >/dev/null 2>&1'
HEALTH_ACTIVE = '* * * * * /home/ubuntu/bot/healthcheck.sh >/dev/null 2>&1'
RESOURCE_ACTIVE = '*/5 * * * * /home/ubuntu/bot/resource-check.sh >/dev/null 2>&1'

out = []
has_disabled_health = False
has_active_health = False
has_disabled_resource = False
has_active_resource = False

def is_redirect_only(line):
    return line.strip() in {'>/dev/null 2>&1', '>>/dev/null 2>&1', '2>&1', '&>/dev/null'}

def kind_for(line):
    if 'healthcheck.sh' in line:
        return 'health'
    if 'resource-check.sh' in line:
        return 'resource'
    return None

def disabled(line):
    return line.lstrip().startswith('#') or 'TEMP_DISABLED' in line

for raw in text.splitlines():
    line = raw.rstrip('\r')
    if is_redirect_only(line):
        continue
    kind = kind_for(line)
    if kind == 'health':
        if disabled(line):
            if not has_disabled_health:
                out.append(HEALTH_DISABLED)
                has_disabled_health = True
        else:
            if not has_active_health:
                out.append(HEALTH_ACTIVE)
                has_active_health = True
        continue
    if kind == 'resource':
        if disabled(line):
            if not has_disabled_resource:
                out.append(RESOURCE_DISABLED)
                has_disabled_resource = True
        else:
            if not has_active_resource:
                out.append(RESOURCE_ACTIVE)
                has_active_resource = True
        continue
    out.append(line)

dst.write_text('\n'.join(out).rstrip() + '\n', encoding='utf-8')
PY_CRON
  if ! cmp -s "$current" "$tmp"; then
    cp -a /var/spool/cron/crontabs/ubuntu "$REPO_DIR/crontab.backup.auto-clean.$(date +%Y%m%d-%H%M%S)" 2>/dev/null || true
    sudo -u ubuntu -H crontab "$tmp" || true
    CRONTAB_HEALTH_STATUS="normalizado; healthcheck/resource-check seguem pausados se estavam pausados"
  else
    CRONTAB_HEALTH_STATUS="limpo"
  fi
  rm -f "$tmp" "$current" 2>/dev/null || true
}

managed_systemd_template_source() {
  local rel="${1:-}"
  local root_src="$REPO_DIR/deploy/systemd/$rel"
  local vps_src="$REPO_DIR/deploy/systemd/vps/$rel"

  # Quando o mesmo template existe nos dois diretórios, respeite o caminho que
  # realmente mudou. A preferência fixa por vps/ fazia patches na raiz serem
  # aceitos e commitados, mas a unit antiga continuava instalada na VPS.
  if printf '%s\n' "$CHANGED_FILES_RAW" | grep -Fxq "deploy/systemd/vps/$rel"; then
    [[ -f "$vps_src" ]] && printf '%s' "$vps_src"
    return 0
  fi
  if printf '%s\n' "$CHANGED_FILES_RAW" | grep -Fxq "deploy/systemd/$rel"; then
    [[ -f "$root_src" ]] && printf '%s' "$root_src"
    return 0
  fi

  if [[ -f "$vps_src" ]]; then
    printf '%s' "$vps_src"
  elif [[ -f "$root_src" ]]; then
    printf '%s' "$root_src"
  fi
}

build_vps_systemd_template_overlay() {
  local overlay="${1:?overlay obrigatório}"
  local rel src changed_path
  local -a managed_units=(
    tts-bot.service
    tts-bot-updater.service
    tts-bot-updater.timer
    tts-bot-updater.path
    tts-bot-alert@.service
    cleanup-audio-temp.service
    cleanup-audio-temp.timer
    sinuca-activity-server.service
    phone-worker-watch.service
    phone-worker-watch.timer
  )

  mkdir -p "$overlay"
  for rel in "${managed_units[@]}"; do
    src="$(managed_systemd_template_source "$rel")"
    [[ -n "$src" && -f "$src" ]] || continue
    mkdir -p "$overlay/$(dirname "$rel")"
    cp -a "$src" "$overlay/$rel"
  done

  # Os drop-ins vivem normalmente em vps/. Mantenha a árvore completa e
  # sobreponha apenas arquivos da raiz explicitamente alterados no patch.
  if [[ -d "$REPO_DIR/deploy/systemd/vps/tts-bot.service.d" ]]; then
    mkdir -p "$overlay/tts-bot.service.d"
    cp -a "$REPO_DIR/deploy/systemd/vps/tts-bot.service.d/." "$overlay/tts-bot.service.d/"
  fi
  while IFS= read -r changed_path; do
    [[ "$changed_path" == deploy/systemd/tts-bot.service.d/* ]] || continue
    rel="${changed_path#deploy/systemd/}"
    src="$REPO_DIR/$changed_path"
    [[ -f "$src" ]] || continue
    mkdir -p "$overlay/$(dirname "$rel")"
    cp -a "$src" "$overlay/$rel"
  done <<< "$CHANGED_FILES_RAW"
}


deploy_vps_systemd_units() {
  if (( VPS_SYSTEMD_UNITS_CHANGED == 0 )); then
    VPS_SYSTEMD_UNITS_STATUS="não alterado"
    return 0
  fi

  STAGE="sincronização dos units systemd da VPS"
  if [[ ! -x "$REPO_DIR/scripts/install-vps-systemd-units.sh" && ! -f "$REPO_DIR/scripts/install-vps-systemd-units.sh" ]]; then
    VPS_SYSTEMD_UNITS_STATUS="script ausente"
    LAST_ERROR_STDERR="scripts/install-vps-systemd-units.sh não foi encontrado"
    return 1
  fi

  local rc=0 template_overlay
  template_overlay="$(mktemp -d "${TMPDIR:-/tmp}/tts-bot-systemd-overlay.XXXXXX")"
  if ! build_vps_systemd_template_overlay "$template_overlay"; then
    rm -rf "$template_overlay"
    VPS_SYSTEMD_UNITS_STATUS="falha ao preparar templates"
    LAST_ERROR_STDERR="não foi possível preparar os templates systemd do patch"
    return 1
  fi

  if REPO_DIR="$REPO_DIR" TEMPLATE_DIR="$template_overlay" \
      bash "$REPO_DIR/scripts/install-vps-systemd-units.sh" --from-updater; then
    VPS_SYSTEMD_UNITS_STATUS="sincronizados"
    rm -rf "$template_overlay"
    return 0
  else
    rc=$?
  fi
  rm -rf "$template_overlay"

  VPS_SYSTEMD_UNITS_STATUS="falha ao sincronizar"
  LAST_ERROR_STDERR="o instalador das units systemd falhou; a VPS não foi deixada com templates parcialmente atualizados"
  return "$rc"
}

deploy_alert_unit() {
  STAGE="configuração do alerta systemd"
  local src=""
  src="$(managed_systemd_template_source "tts-bot-alert@.service")"
  if [[ -n "$src" ]]; then
    cp "$src" /etc/systemd/system/tts-bot-alert@.service
    systemctl daemon-reload || true
    ALERT_UNIT_STATUS="unit instalada"
  else
    ALERT_UNIT_STATUS="unit ausente no deploy"
  fi
}

deploy_audio_services() {
  sanitize_vps_lavalink_units
  if (( AUDIO_SYSTEMD_CHANGED == 0 )); then
    AUDIO_SERVICES_STATUS="não alterado; Lavalink VPS sanitizado"
    return 0
  fi

  STAGE="configuração dos serviços de áudio"
  local installed=0 lavalink_unit_changed=0

  if printf '%s\n' "$CHANGED_FILES_RAW" | grep -q '^deploy/systemd/lavalink\.service$'; then
    lavalink_unit_changed=1
    if env_truthy VPS_LAVALINK_ENABLED && [[ -f "$REPO_DIR/deploy/systemd/lavalink.service" ]]; then
      cp "$REPO_DIR/deploy/systemd/lavalink.service" /etc/systemd/system/lavalink.service
      installed=1
    fi
  fi

  if printf '%s\n' "$CHANGED_FILES_RAW" | grep -q '^deploy/systemd/tts-bot\.service$'; then
    if [[ -f "$REPO_DIR/deploy/systemd/tts-bot.service" ]]; then
      cp "$REPO_DIR/deploy/systemd/tts-bot.service" /etc/systemd/system/tts-bot.service
      installed=1
    fi
  fi

  if (( installed == 1 )); then
    systemctl daemon-reload
  fi

  if (( lavalink_unit_changed == 1 )); then
    if env_truthy VPS_LAVALINK_ENABLED; then
      systemctl enable "$LAVALINK_SERVICE" >/dev/null 2>&1 || true
      systemctl restart "$LAVALINK_SERVICE" || true
      if systemctl is-active --quiet "$LAVALINK_SERVICE"; then
        AUDIO_SERVICES_STATUS="Lavalink ativo"
      else
        AUDIO_SERVICES_STATUS="Lavalink configurado, mas não ficou ativo"
      fi
    else
      AUDIO_SERVICES_STATUS="Lavalink VPS não iniciado; node de áudio roda no phone worker/Music Agent"
    fi
  else
    AUDIO_SERVICES_STATUS="units atualizadas; Lavalink não alterado"
  fi
}


deploy_cleanup_timer() {
  if (( CLEANUP_CHANGED == 0 )); then
    CLEANUP_STATUS="não alterada"
    return 0
  fi

  STAGE="configuração da limpeza de temporários"
  local installed=0

  local cleanup_service_src="" cleanup_timer_src=""
  cleanup_service_src="$(managed_systemd_template_source "cleanup-audio-temp.service")"
  cleanup_timer_src="$(managed_systemd_template_source "cleanup-audio-temp.timer")"

  if [[ -f "$cleanup_service_src" ]]; then
    cp "$cleanup_service_src" /etc/systemd/system/cleanup-audio-temp.service
    installed=1
  fi
  if [[ -f "$cleanup_timer_src" ]]; then
    cp "$cleanup_timer_src" /etc/systemd/system/cleanup-audio-temp.timer
    installed=1
  fi

  if (( installed == 0 )); then
    CLEANUP_STATUS="units de limpeza não encontrados"
    return 0
  fi

  systemctl daemon-reload
  systemctl enable --now cleanup-audio-temp.timer >/dev/null 2>&1 || true
  systemctl start cleanup-audio-temp.service >/dev/null 2>&1 || true

  if systemctl is-active --quiet cleanup-audio-temp.timer; then
    CLEANUP_STATUS="timer ativo"
  else
    CLEANUP_STATUS="timer instalado, mas não ativo"
  fi
}


deploy_phone_lavalink_watch() {
  # Lavalink/NodeLink foi removido do worker. Esta etapa não instala mais unit
  # nova; ela só desativa qualquer timer/service antigo que ainda exista na VPS.
  if (( PHONE_LAVALINK_WATCH_CHANGED == 0 )); then
    PHONE_LAVALINK_WATCH_STATUS="removido do fluxo"
    return 0
  fi

  STAGE="desativando watcher legado do Lavalink"
  systemctl disable --now phone-lavalink-watch.timer phone-lavalink-watch.service >/dev/null 2>&1 || true
  systemctl reset-failed phone-lavalink-watch.timer phone-lavalink-watch.service >/dev/null 2>&1 || true
  systemctl daemon-reload >/dev/null 2>&1 || true
  PHONE_LAVALINK_WATCH_STATUS="desativado/removido do fluxo"
}


deploy_phone_worker_watch() {
  if (( PHONE_WORKER_WATCH_CHANGED == 0 )); then
    PHONE_WORKER_WATCH_STATUS="não alterado"
    return 0
  fi

  STAGE="configuração do watcher do phone worker"
  local installed=0

  # Não chmod em script rastreado: systemd usa /usr/bin/env bash e isso evita repo sujo.
  local phone_worker_service_src="" phone_worker_timer_src=""
  phone_worker_service_src="$(managed_systemd_template_source "phone-worker-watch.service")"
  phone_worker_timer_src="$(managed_systemd_template_source "phone-worker-watch.timer")"

  if [[ -f "$phone_worker_service_src" ]]; then
    cp "$phone_worker_service_src" /etc/systemd/system/phone-worker-watch.service
    installed=1
  fi
  if [[ -f "$phone_worker_timer_src" ]]; then
    cp "$phone_worker_timer_src" /etc/systemd/system/phone-worker-watch.timer
    installed=1
  fi

  if (( installed == 0 )); then
    PHONE_WORKER_WATCH_STATUS="units não encontradas"
    return 0
  fi

  systemctl daemon-reload

  local worker_value=""
  if [[ -f "$REPO_DIR/.env" ]]; then
    worker_value="$(grep -E '^PHONE_WORKER_ENABLED=' "$REPO_DIR/.env" 2>/dev/null | tail -n 1 | cut -d= -f2- | tr -d ' "' || true)"
    worker_value="${worker_value,,}"
  fi

  local watch_value=""
  if [[ -f "$REPO_DIR/.env" ]]; then
    watch_value="$(grep -E '^PHONE_WORKER_WATCH_ENABLED=' "$REPO_DIR/.env" 2>/dev/null | tail -n 1 | cut -d= -f2- | tr -d ' "' || true)"
    watch_value="${watch_value,,}"
  fi

  if [[ "$watch_value" == "1" || "$watch_value" == "true" || "$watch_value" == "yes" || "$watch_value" == "on" || "$watch_value" == "sim" ]]; then
    systemctl enable --now phone-worker-watch.timer >/dev/null 2>&1 || true
    systemctl start phone-worker-watch.service >/dev/null 2>&1 || true
    if systemctl is-active --quiet phone-worker-watch.timer; then
      PHONE_WORKER_WATCH_STATUS="timer ativo"
    else
      PHONE_WORKER_WATCH_STATUS="timer instalado, mas não ativo"
    fi
  else
    systemctl disable --now phone-worker-watch.timer phone-worker-watch.service >/dev/null 2>&1 || true
    PHONE_WORKER_WATCH_STATUS="instalado; inativo até PHONE_WORKER_WATCH_ENABLED=true"
  fi
}


deploy_phone_worker_sync() {
  if (( PHONE_WORKER_SYNC_REQUIRED == 0 )); then
    PHONE_WORKER_SYNC_STATUS="sem mudanças"
    return 0
  fi

  if ! env_truthy PHONE_WORKER_LEGACY_SSH_SYNC_ENABLED; then
    PHONE_WORKER_SYNC_STATUS="agendado para automação por jobs após restart"
    return 0
  fi

  STAGE="sincronização legada do phone-worker por SSH"

  if [[ ! -x "$REPO_DIR/scripts/sync-phone-worker.sh" ]]; then
    PHONE_WORKER_SYNC_STATUS="não executado: scripts/sync-phone-worker.sh ausente"
    return 0
  fi

  local output status_line
  output="$(sudo -u ubuntu -H bash "$REPO_DIR/scripts/sync-phone-worker.sh" 2>&1 || true)"
  status_line="$(printf '%s\n' "$output" | grep -E '\[phone-worker-sync\]' | tail -n 1 | sed -E 's/^\[phone-worker-sync\][[:space:]]*//' || true)"

  if [[ -n "${status_line//[[:space:]]/}" ]]; then
    PHONE_WORKER_SYNC_STATUS="$status_line"
  else
    PHONE_WORKER_SYNC_STATUS="executado; sem status legível"
  fi

  logger -t "$LOG_TAG" "Phone-worker sync legado: $PHONE_WORKER_SYNC_STATUS"
  return 0
}

run_core_worker_post_update_automation() {
  if (( CORE_WORKER_AUTOMATION_REQUIRED == 0 )); then
    CORE_WORKER_AGENT_UPDATE_STATUS="sem mudanças"
    CORE_WORKER_APK_BUILD_STATUS="sem mudanças"
    CORE_WORKER_NOTIFY_STATUS="sem mudanças"
    return 0
  fi

  STAGE="automação pós-update dos Core Workers"
  local py
  py="$(current_bot_python_bin)"
  if [[ -z "$py" || ! -f "$REPO_DIR/scripts/core-worker-automation.py" ]]; then
    CORE_WORKER_AGENT_UPDATE_STATUS="não executado: core-worker-automation ausente"
    CORE_WORKER_APK_BUILD_STATUS="não executado"
    CORE_WORKER_NOTIFY_STATUS="não executado"
    return 0
  fi

  local output automation_rc=0
  output="$(sudo -u ubuntu -H env CORE_WORKER_CHANGED_FILES="$CHANGED_FILES_RAW" "$py" "$REPO_DIR/scripts/core-worker-automation.py" after-update 2>&1)" || automation_rc=$?
  logger -t "$LOG_TAG" "Core Worker automation: $output"

  local parsed
  parsed="$(CORE_WORKER_AUTOMATION_RAW="$output" python3 - <<'PYJSON' 2>/dev/null || true
import json, os
raw = os.environ.get('CORE_WORKER_AUTOMATION_RAW') or '{}'
line = next((ln for ln in reversed(raw.splitlines()) if ln.strip().startswith('{')), '{}')
try:
    data = json.loads(line)
except Exception:
    data = {}
agent = data.get('agent_update') or {}
apk = data.get('apk_build') or {}
def brief_agent(obj):
    if not obj:
        return 'sem mudanças'
    queued = len(obj.get('queued') or [])
    skipped = len(obj.get('skipped') or [])
    errors = len(obj.get('errors') or [])
    version = obj.get('target_version') or '?'
    return f'agent {version}: {queued} job(s), {skipped} skip, {errors} erro(s)'
def brief_apk(obj):
    if not obj:
        return 'sem mudanças'
    if obj.get('ok'):
        job = obj.get('job') or {}
        if job.get('job_id'):
            return f"APK {obj.get('versionName') or '?'}: build job {job.get('job_id')}"
        if obj.get('already_published'):
            return f"APK {obj.get('versionName') or '?'}: já publicado para esta fonte"
        return f"APK {obj.get('versionName') or '?'}: {obj.get('message') or obj.get('phase') or 'pendente'}"
    return f"APK {obj.get('versionName') or '?'}: pendente ({obj.get('message') or obj.get('error') or 'sem builder'})"
print(brief_agent(agent))
print(brief_apk(apk))
print('apps verão banner/notify quando latest.json novo for publicado' if apk else 'sem notificação nova')
PYJSON
)"
  CORE_WORKER_AGENT_UPDATE_STATUS="$(printf '%s\n' "$parsed" | sed -n '1p')"
  CORE_WORKER_APK_BUILD_STATUS="$(printf '%s\n' "$parsed" | sed -n '2p')"
  CORE_WORKER_NOTIFY_STATUS="$(printf '%s\n' "$parsed" | sed -n '3p')"
  [[ -n "${CORE_WORKER_AGENT_UPDATE_STATUS//[[:space:]]/}" ]] || CORE_WORKER_AGENT_UPDATE_STATUS="executado; sem resumo"
  [[ -n "${CORE_WORKER_APK_BUILD_STATUS//[[:space:]]/}" ]] || CORE_WORKER_APK_BUILD_STATUS="executado; sem resumo"
  [[ -n "${CORE_WORKER_NOTIFY_STATUS//[[:space:]]/}" ]] || CORE_WORKER_NOTIFY_STATUS="executado"
  if (( automation_rc != 0 )); then
    # Telefone offline/pending é um estado normal e o orquestrador retorna rc=0.
    # rc != 0 significa falha interna real: preserve o deploy do bot, mas não
    # pinte a automação como sucesso/degradação genérica.
    CORE_WORKER_AGENT_UPDATE_STATUS="FALHA INTERNA na automação (rc=$automation_rc) · ${CORE_WORKER_AGENT_UPDATE_STATUS}"
    CORE_WORKER_APK_BUILD_STATUS="FALHA INTERNA na automação (rc=$automation_rc) · ${CORE_WORKER_APK_BUILD_STATUS}"
    CORE_WORKER_NOTIFY_STATUS="falha interna persistida; deploy principal concluído sem rollback por worker offline"
    logger -t "$LOG_TAG" "Core Worker automation FALHOU internamente (rc=$automation_rc); deploy principal preservado"
  fi
  return 0
}


restart_bot_service_once() {
  local phase="deploy"
  local count=0
  if (( ROLLBACK_IN_PROGRESS == 1 )); then
    phase="rollback"
    count="$BOT_RESTARTS_ROLLBACK"
  else
    count="$BOT_RESTARTS_DEPLOY"
  fi

  if (( count >= 1 )); then
    LAST_ERROR_STDERR="restart do bot bloqueado: limite de 1 reinício na fase $phase já foi consumido"
    logger -t "$LOG_TAG" "$LAST_ERROR_STDERR" 2>/dev/null || true
    return 75
  fi

  # Consuma o orçamento antes da chamada: mesmo uma tentativa que pare o
  # processo e falhe ao subir não pode ser repetida indefinidamente.
  if [[ "$phase" == "rollback" ]]; then
    BOT_RESTARTS_ROLLBACK=$((BOT_RESTARTS_ROLLBACK + 1))
  else
    BOT_RESTARTS_DEPLOY=$((BOT_RESTARTS_DEPLOY + 1))
  fi

  # Um start-limit-hit anterior não deve impedir um único reinício legítimo.
  # O limite por execução acima evita transformar reset-failed em loop.
  systemctl reset-failed "$SERVICE" >/dev/null 2>&1 || true
  systemctl restart "$SERVICE" || return $?
  logger -t "$LOG_TAG" "restart do bot executado: fase=$phase deploy=$BOT_RESTARTS_DEPLOY rollback=$BOT_RESTARTS_ROLLBACK" 2>/dev/null || true
  return 0
}

mark_deployment_committed() {
  DEPLOYMENT_COMMITTED=1
  STAGE="finalização pós-deploy"
  if (( LOCAL_CANDIDATE_MODE == 1 )); then
    write_local_candidate_state "deployment_completed" "${REMOTE_COMMIT:-}"
  fi
  logger -t "$LOG_TAG" "limite transacional concluído em $(short_commit "${REMOTE_COMMIT:-${CURRENT_COMMIT:-}}")" 2>/dev/null || true
}

build_final_status_description() {
  local summary="${1:-}"
  local display_id="${2:-}"
  local short_from="${3:-}"
  local short_to="${4:-}"
  local changed_count="${5:-0}"
  local diff_summary="${6:-diff indisponível}"
  local apply_mode="${7:-aplicação concluída}"
  local duration="${8:-tempo indisponível}"
  local health_status="${9:-health não informado}"
  local file_count_text=""

  if [[ "$changed_count" =~ ^[0-9]+$ ]] && (( changed_count == 1 )); then
    file_count_text="1 arquivo alterado"
  elif [[ "$changed_count" =~ ^[0-9]+$ ]]; then
    file_count_text="$changed_count arquivos alterados"
  else
    file_count_text="arquivos alterados"
  fi

  # A string de formato é literal; IDs e commits são somente argumentos. Isso
  # impede que crases de Markdown virem substituição de comando do Bash.
  printf -v ZIP_STATUS_DESCRIPTION '%s\n\nAtualização `%s`\n`%s` → `%s`\n%s · %s\n%s · duração total: %s' \
    "$summary" "$display_id" "$short_from" "$short_to" "$file_count_text" \
    "$diff_summary" "$apply_mode" "$duration"
  # “OK” isolado não comunica nada e aparecia como uma linha solta no cartão.
  # Só acrescente saúde quando houver informação diferente do sucesso padrão.
  if [[ -n "${health_status//[[:space:]]/}" && "$health_status" != "OK" ]]; then
    ZIP_STATUS_DESCRIPTION+=$'\n\n'"Saúde: $health_status"
  fi
}

deploy_bot() {
  # Caminho rápido: não reinstale systemd/watchers/áudio em todo update.
  # Cada rotina só roda quando os arquivos dela mudaram; isso reduz bastante
  # o custo dos patches comuns.
  if (( VPS_SYSTEMD_UNITS_CHANGED == 1 )); then
    normalize_healthcheck_crontab
    deploy_vps_systemd_units
  fi
  if (( ALERT_CHANGED == 1 || VPS_SYSTEMD_UNITS_CHANGED == 1 )); then
    deploy_alert_unit
  fi
  if (( AUDIO_SYSTEMD_CHANGED == 1 )); then
    deploy_audio_services
  fi
  if (( CLEANUP_CHANGED == 1 )); then
    deploy_cleanup_timer
  fi
  if (( PHONE_LAVALINK_WATCH_CHANGED == 1 )); then
    deploy_phone_lavalink_watch
  fi
  if (( PHONE_WORKER_WATCH_CHANGED == 1 )); then
    deploy_phone_worker_watch
  fi
  if (( PHONE_WORKER_SYNC_REQUIRED == 1 )); then
    deploy_phone_worker_sync
  fi

  if (( REQUIREMENTS_CHANGED == 1 && ROLLBACK_IN_PROGRESS == 0 )); then
    STAGE="ativação do runtime Python"
    if (( LOCAL_CANDIDATE_MODE == 1 || REMOTE_CANDIDATE_MODE == 1 )); then
      local runtime_commit="${LOCAL_CANDIDATE_PREPARED_COMMIT:-${REMOTE_COMMIT:-}}"
      if (( LOCAL_CANDIDATE_RUNTIME_READY == 0 )); then
        hydrate_local_candidate_runtime_artifacts "$runtime_commit" || {
          LAST_ERROR_STDERR="runtime Python candidato READY ausente após promoção"
          LAST_ERROR_CODE="PYTHON_RUNTIME_READY_MISSING"
          return 1
        }
      fi
      if (( LOCAL_CANDIDATE_PYTHON_READY == 0 )) || ! verify_local_candidate_artifact_integrity python; then
        LAST_ERROR_STDERR="runtime Python candidato ausente ou divergiu da validação READY"
        LAST_ERROR_CODE="PYTHON_RUNTIME_READY_INVALID"
        return 1
      fi
      if ! activate_python_runtime_release "$runtime_commit"; then
        return 1
      fi
    else
      # Compatibilidade de operações administrativas legadas fora dos caminhos
      # transacionais local/remoto. Updates normais nunca instalam na .venv live.
      local legacy_py
      legacy_py="$(current_bot_python_bin)"
      local legacy_requirements
      legacy_requirements="$(python_runtime_install_requirements_file "$REPO_DIR" || true)"
      if [[ -x "$legacy_py" && -n "$legacy_requirements" && -f "$legacy_requirements" ]]; then
        sudo -u ubuntu -H "$legacy_py" -m pip install --disable-pip-version-check --prefer-binary --no-input --progress-bar off -r "$legacy_requirements"
      fi
    fi
  fi

  if (( BOT_CHANGED == 1 )); then
    local fast_modules restart_epoch restarts_before
    fast_modules="$(fast_reload_modules_for_changed_files || true)"
    FAST_RELOAD_MODULES="$fast_modules"
    if [[ -n "${fast_modules//[[:space:]]/}" ]]; then
      STAGE="reload rápido de cogs"
      if try_fast_cog_reload "$fast_modules" "$APP_COMMANDS_MAY_HAVE_CHANGED"; then
        return 0
      fi
      logger -t "$LOG_TAG" "Fast reload indisponível; usando restart completo seguro do bot principal. Status: $FAST_RELOAD_STATUS"
      if (( LOCAL_CANDIDATE_MODE == 1 || ROLLBACK_CONTROL_MODE == 1 || REMOTE_CANDIDATE_MODE == 1 )); then
        zip_progress_done "Reload da cog falhou"
        zip_progress_publish "Reiniciando processo: bot"
      fi
      FAST_RELOAD_MODULES=""
    fi

    restarts_before="$(service_restart_count "$SERVICE")"

    STAGE="reinício do bot"
    restart_epoch="$(date +%s)"
    local bot_phase_started_ms bot_phase_finished_ms
    bot_phase_started_ms="$(update_now_ms)"
    restart_bot_service_once
    bot_phase_finished_ms="$(update_now_ms)"
    append_update_timing_ms "bot.restart_command" "$((bot_phase_finished_ms - bot_phase_started_ms))"

    if env_truthy LAVALINK_ENABLED; then
      STAGE="espera curta do Lavalink"
      bot_phase_started_ms="$(update_now_ms)"
      wait_for_lavalink_ready || true
      bot_phase_finished_ms="$(update_now_ms)"
      append_update_timing_ms "bot.lavalink_wait" "$((bot_phase_finished_ms - bot_phase_started_ms))"
    fi

    STAGE="validação fatal do bot"
    local health_profile
    health_profile="$(bot_health_profile_for_changed_files)"
    verify_bot_after_restart "$restart_epoch" "$restarts_before" 1 "$health_profile"
    return $?
  fi

  STAGE="healthcheck do bot"
  if refresh_bot_health_status; then
    if has_real_warning_text "$BOT_WARNINGS_STATUS" || cogs_have_failures "$BOT_COGS_STATUS"; then
      BOT_HEALTHCHECK_STATUS="OK com avisos"
      UPDATE_HAS_WARNINGS=1
    else
      BOT_HEALTHCHECK_STATUS="OK"
    fi
  else
    if [[ "$BOT_HEALTH_DETAIL_STATUS" == "HTTP sem resposta" ]]; then
      BOT_HEALTHCHECK_STATUS="não alterado; health HTTP sem resposta"
      UPDATE_HAS_WARNINGS=1
    else
      BOT_HEALTHCHECK_STATUS="falhou: health não saudável ($BOT_HEALTH_DETAIL_STATUS)"
      return 1
    fi
  fi
  return 0
}


frontend_release_root_for_key() {
  local key
  key="$(sanitize_commit_ref "${1:-}")"
  [[ -n "$key" ]] || return 1
  local root="${FRONT_RELEASE_ROOT:-$(dirname "$FRONT_PUBLISH_DIR")/sinuca-releases}"
  printf '%s/%s\n' "$root" "$key"
}

write_frontend_release_manifest() {
  local root="${1:?}" key="${2:?}"
  FRONT_RELEASE_DIR="$root" FRONT_RELEASE_KEY="$key" python3 - <<'PYFRONTRELEASEWRITE'
import datetime, hashlib, json, os, pathlib
root = pathlib.Path(os.environ['FRONT_RELEASE_DIR'])

def tree_hash(base: pathlib.Path) -> str:
    digest = hashlib.sha256()
    resolved_base = base.resolve()
    for item in sorted(base.rglob('*'), key=lambda p: p.as_posix()):
        if item.name == '.tts-release.json':
            continue
        rel = item.relative_to(base).as_posix()
        if item.is_symlink():
            target = os.readlink(item)
            if os.path.isabs(target):
                raise SystemExit(1)
            resolved = (item.parent / target).resolve(strict=False)
            try:
                resolved.relative_to(resolved_base)
            except ValueError:
                raise SystemExit(1)
            digest.update(b'L\0' + rel.encode() + b'\0' + target.encode() + b'\0')
            continue
        if not item.is_file():
            continue
        digest.update(b'F\0' + rel.encode() + b'\0')
        with item.open('rb') as fh:
            for chunk in iter(lambda: fh.read(1024 * 1024), b''):
                digest.update(chunk)
    return digest.hexdigest()

if not (root / 'index.html').is_file():
    raise SystemExit(1)
payload = {
    'state': 'ready',
    'release_key': os.environ['FRONT_RELEASE_KEY'],
    'created_at': datetime.datetime.now(datetime.timezone.utc).isoformat(),
    'sha256': tree_hash(root),
}
tmp = root / '.tts-release.json.tmp'
final = root / '.tts-release.json'
tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding='utf-8')
os.replace(tmp, final)
PYFRONTRELEASEWRITE
}

verify_frontend_release() {
  local root="${1:?}" expected_key="${2:-}" manifest
  manifest="$root/.tts-release.json"
  [[ -d "$root" && ! -L "$root" && -s "$root/index.html" && -s "$manifest" && ! -L "$manifest" ]] || return 1
  FRONT_RELEASE_DIR="$root" FRONT_RELEASE_MANIFEST="$manifest" FRONT_RELEASE_EXPECTED_KEY="$expected_key" python3 - <<'PYFRONTRELEASEVERIFY' >/dev/null 2>&1
import hashlib, json, os, pathlib
root = pathlib.Path(os.environ['FRONT_RELEASE_DIR'])
manifest = pathlib.Path(os.environ['FRONT_RELEASE_MANIFEST'])
data = json.loads(manifest.read_text(encoding='utf-8'))
if data.get('state') != 'ready':
    raise SystemExit(1)
expected = os.environ.get('FRONT_RELEASE_EXPECTED_KEY') or ''
if expected and str(data.get('release_key') or '') != expected:
    raise SystemExit(1)

def tree_hash(base: pathlib.Path) -> str:
    digest = hashlib.sha256()
    resolved_base = base.resolve()
    for item in sorted(base.rglob('*'), key=lambda p: p.as_posix()):
        if item.name == '.tts-release.json':
            continue
        rel = item.relative_to(base).as_posix()
        if item.is_symlink():
            target = os.readlink(item)
            if os.path.isabs(target):
                raise SystemExit(1)
            resolved = (item.parent / target).resolve(strict=False)
            try:
                resolved.relative_to(resolved_base)
            except ValueError:
                raise SystemExit(1)
            digest.update(b'L\0' + rel.encode() + b'\0' + target.encode() + b'\0')
            continue
        if not item.is_file():
            continue
        digest.update(b'F\0' + rel.encode() + b'\0')
        with item.open('rb') as fh:
            for chunk in iter(lambda: fh.read(1024 * 1024), b''):
                digest.update(chunk)
    return digest.hexdigest()

if tree_hash(root) != str(data.get('sha256') or ''):
    raise SystemExit(1)
PYFRONTRELEASEVERIFY
}

frontend_active_release_key() {
  [[ -L "$FRONT_PUBLISH_DIR" ]] || return 1
  local target root resolved
  target="$(readlink -f -- "$FRONT_PUBLISH_DIR" 2>/dev/null || true)"
  root="$(readlink -f -- "${FRONT_RELEASE_ROOT:-$(dirname "$FRONT_PUBLISH_DIR")/sinuca-releases}" 2>/dev/null || true)"
  [[ -n "$target" && -n "$root" && "$target" == "$root"/* ]] || return 1
  resolved="$(basename "$target")"
  [[ -n "$resolved" ]] || return 1
  verify_frontend_release "$target" "$resolved" || return 1
  printf '%s\n' "$resolved"
}

frontend_publication_is_healthy() {
  [[ -e "$FRONT_PUBLISH_DIR" || -L "$FRONT_PUBLISH_DIR" ]] || return 1
  [[ -s "$FRONT_PUBLISH_DIR/index.html" ]] || return 1
  [[ -r "$FRONT_PUBLISH_DIR/index.html" ]] || return 1
  if [[ -L "$FRONT_PUBLISH_DIR" ]]; then
    frontend_active_release_key >/dev/null 2>&1 || return 1
  fi
  return 0
}

adopt_frontend_publication_as_release() {
  local baseline_key="${1:-}" release_root release_dir parent tmp_link
  if frontend_active_release_key >/dev/null 2>&1; then
    frontend_active_release_key
    return 0
  fi
  [[ -d "$FRONT_PUBLISH_DIR" && ! -L "$FRONT_PUBLISH_DIR" && -s "$FRONT_PUBLISH_DIR/index.html" ]] || return 1
  baseline_key="$(sanitize_commit_ref "$baseline_key")"
  [[ -n "$baseline_key" ]] || return 1
  release_root="${FRONT_RELEASE_ROOT:-$(dirname "$FRONT_PUBLISH_DIR")/sinuca-releases}"
  release_dir="$(frontend_release_root_for_key "$baseline_key")" || return 1
  parent="$(dirname "$FRONT_PUBLISH_DIR")"
  install -d -m 0755 "$release_root" || return 1
  if [[ -e "$release_dir" || -L "$release_dir" ]]; then
    if ! verify_frontend_release "$release_dir" "$baseline_key"; then
      return 1
    fi
    # Só descarta a árvore física se ela for byte-a-byte equivalente ao release já existente.
    local live_hash release_hash
    live_hash="$(FRONT_RELEASE_DIR="$FRONT_PUBLISH_DIR" python3 - <<'PYFRONTLIVEHASH'
import hashlib, os, pathlib
base = pathlib.Path(os.environ['FRONT_RELEASE_DIR'])
resolved_base = base.resolve()
d = hashlib.sha256()
for item in sorted(base.rglob('*'), key=lambda p: p.as_posix()):
    if item.name == '.tts-release.json':
        continue
    rel = item.relative_to(base).as_posix()
    if item.is_symlink():
        target = os.readlink(item)
        if os.path.isabs(target):
            raise SystemExit(1)
        resolved = (item.parent / target).resolve(strict=False)
        try:
            resolved.relative_to(resolved_base)
        except ValueError:
            raise SystemExit(1)
        d.update(b'L\0' + rel.encode() + b'\0' + target.encode() + b'\0')
        continue
    if not item.is_file():
        continue
    d.update(b'F\0' + rel.encode() + b'\0')
    with item.open('rb') as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b''):
            d.update(chunk)
print(d.hexdigest())
PYFRONTLIVEHASH
)"
    release_hash="$(FRONT_RELEASE_MANIFEST="$release_dir/.tts-release.json" python3 - <<'PYFRONTHASHREAD'
import json, os
print(json.load(open(os.environ['FRONT_RELEASE_MANIFEST'], encoding='utf-8')).get('sha256',''))
PYFRONTHASHREAD
)"
    [[ -n "$live_hash" && "$live_hash" == "$release_hash" ]] || return 1
    rm -rf -- "$FRONT_PUBLISH_DIR" || return 1
  else
    # Registra o hash ainda no path live; o mv seguinte é rename no mesmo filesystem.
    write_frontend_release_manifest "$FRONT_PUBLISH_DIR" "$baseline_key" || return 1
    if ! mv -- "$FRONT_PUBLISH_DIR" "$release_dir"; then
      rm -f -- "$FRONT_PUBLISH_DIR/.tts-release.json" 2>/dev/null || true
      return 1
    fi
  fi
  tmp_link="$parent/.sinuca-current.$$.${RANDOM}"
  ln -s -- "$release_dir" "$tmp_link" || return 1
  if ! mv -Tf -- "$tmp_link" "$FRONT_PUBLISH_DIR"; then
    rm -f -- "$tmp_link" 2>/dev/null || true
    if [[ ! -e "$FRONT_PUBLISH_DIR" && -d "$release_dir" ]]; then
      mv -- "$release_dir" "$FRONT_PUBLISH_DIR" 2>/dev/null || true
    fi
    return 1
  fi
  verify_frontend_release "$release_dir" "$baseline_key" || return 1
  printf '%s\n' "$baseline_key"
}

prepare_frontend_release() {
  local source_dir="${1:?}" release_key="${2:?}" release_root release_dir tmp copy_mode="copy"
  release_key="$(sanitize_commit_ref "$release_key")"
  [[ -n "$release_key" && -s "$source_dir/index.html" ]] || return 1
  release_root="${FRONT_RELEASE_ROOT:-$(dirname "$FRONT_PUBLISH_DIR")/sinuca-releases}"
  release_dir="$(frontend_release_root_for_key "$release_key")" || return 1
  if verify_frontend_release "$release_dir" "$release_key"; then
    printf '%s\n' "$release_dir"
    return 0
  fi
  install -d -m 0755 "$release_root" || return 1
  tmp="$(mktemp -d "$release_root/.${release_key}.XXXXXX")" || return 1
  if cp -al -- "$source_dir/." "$tmp/" 2>/dev/null; then
    copy_mode="hardlink"
  else
    rm -rf -- "$tmp"/* "$tmp"/.[!.]* "$tmp"/..?* 2>/dev/null || true
    if command -v rsync >/dev/null 2>&1; then
      rsync -a --delete "$source_dir/" "$tmp/" || { rm -rf -- "$tmp"; return 1; }
    else
      cp -a -- "$source_dir/." "$tmp/" || { rm -rf -- "$tmp"; return 1; }
    fi
  fi
  [[ -s "$tmp/index.html" ]] || { rm -rf -- "$tmp"; return 1; }
  find "$tmp" -type d -exec chmod 0755 {} + 2>/dev/null || true
  find "$tmp" -type f -exec chmod 0644 {} + 2>/dev/null || true
  write_frontend_release_manifest "$tmp" "$release_key" || { rm -rf -- "$tmp"; return 1; }
  verify_frontend_release "$tmp" "$release_key" || { rm -rf -- "$tmp"; return 1; }
  rm -rf -- "$release_dir" 2>/dev/null || true
  mv -- "$tmp" "$release_dir" || { rm -rf -- "$tmp"; return 1; }
  logger -t "$LOG_TAG" "frontend release $(short_commit "$release_key") preparada via $copy_mode" 2>/dev/null || true
  printf '%s\n' "$release_dir"
}

activate_frontend_release() {
  local release_key="${1:?}" release_dir parent tmp_link backup="" had_physical=0
  release_key="$(sanitize_commit_ref "$release_key")"
  release_dir="$(frontend_release_root_for_key "$release_key")" || return 1
  verify_frontend_release "$release_dir" "$release_key" || return 1
  parent="$(dirname "$FRONT_PUBLISH_DIR")"
  install -d -m 0755 "$parent" || return 1
  tmp_link="$parent/.sinuca-current.$$.${RANDOM}"
  ln -s -- "$release_dir" "$tmp_link" || return 1
  if [[ -d "$FRONT_PUBLISH_DIR" && ! -L "$FRONT_PUBLISH_DIR" ]]; then
    backup="$parent/.sinuca-legacy.$$.${RANDOM}"
    mv -- "$FRONT_PUBLISH_DIR" "$backup" || { rm -f -- "$tmp_link"; return 1; }
    had_physical=1
  fi
  if ! mv -Tf -- "$tmp_link" "$FRONT_PUBLISH_DIR"; then
    rm -f -- "$tmp_link" 2>/dev/null || true
    if (( had_physical == 1 )); then
      mv -- "$backup" "$FRONT_PUBLISH_DIR" 2>/dev/null || true
    fi
    return 1
  fi
  if ! frontend_publication_is_healthy; then
    rm -f -- "$FRONT_PUBLISH_DIR" 2>/dev/null || true
    if (( had_physical == 1 )); then
      mv -- "$backup" "$FRONT_PUBLISH_DIR" 2>/dev/null || true
    fi
    return 1
  fi
  (( had_physical == 0 )) || rm -rf -- "$backup" 2>/dev/null || true
  return 0
}

publish_frontend_atomically() {
  local source_dir="${1:?}" release_key="${2:-}"
  if [[ -z "$release_key" ]]; then
    release_key="${LOCAL_CANDIDATE_ARTIFACT_COMMIT:-${LOCAL_CANDIDATE_PREPARED_COMMIT:-${REMOTE_COMMIT:-}}}"
  fi
  if [[ -z "$release_key" ]]; then
    release_key="$(repo_git rev-parse HEAD 2>/dev/null || true)"
  fi
  release_key="$(sanitize_commit_ref "$release_key")"
  [[ -n "$release_key" ]] || {
    FRONT_STATUS="não foi possível determinar a chave da release frontend"
    LAST_ERROR_STDERR="$FRONT_STATUS"
    return 1
  }
  prepare_frontend_release "$source_dir" "$release_key" >/dev/null || {
    FRONT_STATUS="falha ao preparar release imutável do frontend"
    LAST_ERROR_STDERR="$FRONT_STATUS"
    return 1
  }
  if ! activate_frontend_release "$release_key"; then
    FRONT_STATUS="não foi possível ativar release frontend $(short_commit "$release_key")"
    LAST_ERROR_STDERR="$FRONT_STATUS"
    return 1
  fi
  return 0
}

collect_protected_frontend_release_keys() {
  local active=""
  active="$(frontend_active_release_key 2>/dev/null || true)"
  [[ -n "$active" ]] && printf '%s\n' "$active"
  [[ -d "${RUNTIME_RELEASE_ROOT:-}" ]] || return 0
  find "$RUNTIME_RELEASE_ROOT" -mindepth 2 -maxdepth 2 -type f -name release.json -print0 2>/dev/null | while IFS= read -r -d '' manifest; do
    FRONT_RUNTIME_MANIFEST="$manifest" python3 - <<'PYFRONTREFS' 2>/dev/null || true
import json, os
try:
    data = json.load(open(os.environ['FRONT_RUNTIME_MANIFEST'], encoding='utf-8'))
except Exception:
    raise SystemExit(0)
record = data.get('frontend') if isinstance(data, dict) else None
if isinstance(record, dict):
    key = str(record.get('release_key') or '')
    if key:
        print(key)
PYFRONTREFS
  done
}

prune_frontend_releases() {
  local root="${FRONT_RELEASE_ROOT:-$(dirname "$FRONT_PUBLISH_DIR")/sinuca-releases}" keep="${FRONT_RELEASE_RETENTION:-4}" count=0 entry key
  [[ "$keep" =~ ^[0-9]+$ ]] || keep=4
  (( keep >= 1 )) || keep=1
  [[ -d "$root" ]] || return 0
  declare -A protected=()
  while IFS= read -r key; do
    [[ -n "$key" ]] && protected["$key"]=1
  done < <(collect_protected_frontend_release_keys | sort -u)
  while IFS= read -r entry; do
    [[ -d "$entry" ]] || continue
    key="$(basename "$entry")"
    if [[ -n "${protected[$key]:-}" ]]; then
      continue
    fi
    count=$((count + 1))
    if (( count > keep )); then
      rm -rf -- "$entry" 2>/dev/null || true
    fi
  done < <(find "$root" -mindepth 1 -maxdepth 1 -type d ! -name '.*' -printf '%T@ %p\n' 2>/dev/null | sort -nr | cut -d' ' -f2-)
}

deploy_frontend() {
  local repair_mode=0

  if (( FRONT_CHANGED == 0 )); then
    if frontend_publication_is_healthy; then
      if (( ${FRONT_TESTS_CHANGED:-0} == 1 )); then
        FRONT_STATUS="testes do frontend aprovados no worktree; publicação não necessária"
      else
        FRONT_STATUS="não alterado"
      fi
      return 0
    fi
    # Mesmo sem arquivos do frontend no patch, restaura automaticamente uma
    # publicação ausente/corrompida. Candidatos transacionais normalmente já
    # terão preparado esse reparo no worktree antes da promoção.
    FRONT_CHANGED=1
    repair_mode=1
    logger -t "$LOG_TAG" "publicação do frontend inválida; reconstrução automática solicitada"
  fi

  if (( (LOCAL_CANDIDATE_MODE == 1 || REMOTE_CANDIDATE_MODE == 1) && FRONT_CHANGED == 1 && ROLLBACK_IN_PROGRESS == 0 )); then
    if (( LOCAL_CANDIDATE_RUNTIME_READY == 0 )); then
      hydrate_local_candidate_runtime_artifacts "${LOCAL_CANDIDATE_PREPARED_COMMIT:-${REMOTE_COMMIT:-$(repo_git rev-parse HEAD)}}" || true
    fi
    if [[ -z "${LOCAL_CANDIDATE_FRONTEND_ARTIFACT:-}" || ! -s "$LOCAL_CANDIDATE_FRONTEND_ARTIFACT/index.html" ]]; then
      FRONT_STATUS="artefato frontend READY ausente; recusando rebuild no checkout live"
      LAST_ERROR_STDERR="$FRONT_STATUS"
      LAST_ERROR_CODE="FRONTEND_PREBUILT_ARTIFACT_MISSING"
      return 1
    fi
    if ! verify_local_candidate_artifact_integrity frontend; then
      FRONT_STATUS="artefato frontend READY divergiu do hash validado"
      LAST_ERROR_STDERR="$FRONT_STATUS"
      LAST_ERROR_CODE="FRONTEND_PREBUILT_ARTIFACT_INVALID"
      return 1
    fi
    STAGE="publicação do frontend"
    zip_progress_publish "Publicando interface" "Ativando artefato já validado"
    if ! publish_frontend_atomically "$LOCAL_CANDIDATE_FRONTEND_ARTIFACT"; then
      return 1
    fi
    FRONT_RUNTIME_MUTATED=1
    FRONT_STATUS="frontend publicado a partir do artefato READY do worktree"
    zip_progress_done "Interface publicada"
    return 0
  fi

  if [[ ! -d "$FRONT_DIR" ]]; then
    FRONT_STATUS="frontend não encontrado em $FRONT_DIR"
    return 1
  fi

  STAGE="dependências do frontend"
  zip_progress_run_as_ubuntu \
    "Instalando dependências" \
    "Verificando pacotes" \
    "cd \"$FRONT_DIR\" && if [ -f package-lock.json ]; then npm ci $NPM_INSTALL_FLAGS; else npm install $NPM_INSTALL_FLAGS; fi"
  zip_progress_done_and_publish \
    "Dependências instaladas" \
    "Compilando interface" \
    "Gerando os arquivos de produção"

  STAGE="testes do frontend"
  zip_progress_run_as_ubuntu \
    "Validando interface" \
    "Executando testes do site" \
    "cd \"$FRONT_DIR\" && npm test"
  zip_progress_done_and_publish \
    "Testes da interface aprovados" \
    "Compilando interface" \
    "Gerando os arquivos de produção"

  STAGE="build do frontend"
  zip_progress_run_as_ubuntu \
    "Compilando interface" \
    "Gerando os arquivos de produção" \
    "cd \"$FRONT_DIR\" && npm run build"
  zip_progress_done_and_publish \
    "Interface compilada" \
    "Publicando interface" \
    "Atualizando arquivos"

  STAGE="publicação do frontend"
  if [[ ! -s "$FRONT_DIR/dist/index.html" ]]; then
    FRONT_STATUS="build do frontend não produziu dist/index.html"
    LAST_ERROR_STDERR="$FRONT_STATUS"
    return 1
  fi
  if ! publish_frontend_atomically "$FRONT_DIR/dist"; then
    return 1
  fi
  FRONT_RUNTIME_MUTATED=1

  STAGE="limpeza do frontend"
  zip_progress_run_as_ubuntu \
    "Publicando interface" \
    "Limpando temporários" \
    "cd \"$FRONT_DIR\" && rm -rf node_modules"
  zip_progress_done "Interface publicada"

  if (( repair_mode == 1 )); then
    FRONT_STATUS="frontend reconstruído e republicado em $FRONT_PUBLISH_DIR; node_modules removido após build"
  else
    FRONT_STATUS="frontend publicado em $FRONT_PUBLISH_DIR; node_modules removido após build"
  fi
  return 0
}

install_backend_prebuilt_artifact() {
  local source_dir="${1:?}" token temp_dist temp_modules backup_dist backup_modules had_dist=0 had_modules=0
  local deps_key deps_layer
  [[ -s "$source_dir/dist/index.js" && -s "$source_dir/deps.json" ]] || {
    LAST_ERROR_STDERR="artefato backend incompleto: $source_dir"
    return 1
  }
  [[ -d "$BACK_DIR" ]] || {
    LAST_ERROR_STDERR="backend live não encontrado em $BACK_DIR"
    return 1
  }

  deps_key="$(BACK_DEPS_FILE="$source_dir/deps.json" python3 - <<'PYBACKINSTALLDEPS' 2>/dev/null
import json, os
with open(os.environ['BACK_DEPS_FILE'], encoding='utf-8') as fh:
    data = json.load(fh)
key = str(data.get('deps_key') or '')
if len(key) != 64:
    raise SystemExit(1)
print(key)
PYBACKINSTALLDEPS
)" || {
    LAST_ERROR_STDERR="artefato backend possui deps_key inválido"
    return 1
  }
  deps_layer="$(node_dependency_layer_root backend prod "$deps_key")" || return 1
  if ! verify_node_dependency_layer "$deps_layer" "$deps_key" prod; then
    LAST_ERROR_STDERR="dependency layer backend ausente ou inválido: ${deps_key:0:12}"
    return 1
  fi

  token="${UPDATE_RUNTIME_RUN_ID//[^[:alnum:]._-]/_}"
  temp_dist="$BACK_DIR/.update-dist-$token"
  temp_modules="$BACK_DIR/.update-node_modules-$token"
  backup_dist="$BACK_DIR/.previous-dist-$token"
  backup_modules="$BACK_DIR/.previous-node_modules-$token"

  sudo -u ubuntu -H rm -rf -- "$temp_dist" "$temp_modules" "$backup_dist" "$backup_modules"
  sudo -u ubuntu -H cp -a -- "$source_dir/dist" "$temp_dist" || return 1
  sudo -u ubuntu -H ln -s -- "$deps_layer/node_modules" "$temp_modules" || {
    sudo -u ubuntu -H rm -rf -- "$temp_dist" "$temp_modules"
    return 1
  }

  if [[ -e "$BACK_DIR/dist" || -L "$BACK_DIR/dist" ]]; then
    sudo -u ubuntu -H mv -- "$BACK_DIR/dist" "$backup_dist" || return 1
    had_dist=1
  fi
  if [[ -e "$BACK_DIR/node_modules" || -L "$BACK_DIR/node_modules" ]]; then
    if ! sudo -u ubuntu -H mv -- "$BACK_DIR/node_modules" "$backup_modules"; then
      (( had_dist == 1 )) && sudo -u ubuntu -H mv -- "$backup_dist" "$BACK_DIR/dist" 2>/dev/null || true
      return 1
    fi
    had_modules=1
  fi

  if ! sudo -u ubuntu -H mv -- "$temp_dist" "$BACK_DIR/dist"; then
    (( had_modules == 1 )) && sudo -u ubuntu -H mv -- "$backup_modules" "$BACK_DIR/node_modules" 2>/dev/null || true
    (( had_dist == 1 )) && sudo -u ubuntu -H mv -- "$backup_dist" "$BACK_DIR/dist" 2>/dev/null || true
    sudo -u ubuntu -H rm -rf -- "$temp_dist" "$temp_modules"
    return 1
  fi
  if ! sudo -u ubuntu -H mv -- "$temp_modules" "$BACK_DIR/node_modules"; then
    sudo -u ubuntu -H rm -rf -- "$BACK_DIR/dist" "$temp_modules" 2>/dev/null || true
    (( had_modules == 1 )) && sudo -u ubuntu -H mv -- "$backup_modules" "$BACK_DIR/node_modules" 2>/dev/null || true
    (( had_dist == 1 )) && sudo -u ubuntu -H mv -- "$backup_dist" "$BACK_DIR/dist" 2>/dev/null || true
    return 1
  fi

  sudo -u ubuntu -H rm -rf -- "$backup_dist" "$backup_modules" 2>/dev/null || true
  return 0
}

deploy_backend() {
  if (( BACK_CHANGED == 0 && FRONT_CHANGED == 0 )); then
    if (( ${BACK_TESTS_CHANGED:-0} == 1 )); then
      BACK_STATUS="testes do backend aprovados no worktree; publicação não necessária"
    else
      BACK_STATUS="não alterado"
    fi
    ACTIVITY_HEALTHCHECK_STATUS="não alterada"
    return 0
  fi

  if (( BACK_CHANGED == 0 )); then
    BACK_STATUS="não alterado"
    STAGE="healthcheck informativo do painel web"
    zip_progress_publish "Validando" "Confirmando disponibilidade"
    if { if declare -F wait_for_health_adaptive >/dev/null 2>&1; then wait_for_health_adaptive "$BACK_HEALTH_URL" 8 1; else wait_for_health "$BACK_HEALTH_URL" 3 2; fi; }; then
      ACTIVITY_HEALTHCHECK_STATUS="OK"
      zip_progress_done "Validação concluída"
    else
      ACTIVITY_HEALTHCHECK_STATUS="indisponível; backend não foi alterado"
      zip_progress_done "Publicação concluída; validação indisponível"
    fi
    return 0
  fi

  if (( (LOCAL_CANDIDATE_MODE == 1 || REMOTE_CANDIDATE_MODE == 1) && BACK_CHANGED == 1 && ROLLBACK_IN_PROGRESS == 0 )); then
    if (( LOCAL_CANDIDATE_RUNTIME_READY == 0 )); then
      hydrate_local_candidate_runtime_artifacts "${LOCAL_CANDIDATE_PREPARED_COMMIT:-${REMOTE_COMMIT:-$(repo_git rev-parse HEAD)}}" || true
    fi
    if [[ -z "${LOCAL_CANDIDATE_BACKEND_ARTIFACT:-}" || ! -s "$LOCAL_CANDIDATE_BACKEND_ARTIFACT/dist/index.js" || ! -s "$LOCAL_CANDIDATE_BACKEND_ARTIFACT/deps.json" ]]; then
      BACK_STATUS="artefato backend READY ausente; recusando rebuild no checkout live"
      LAST_ERROR_STDERR="$BACK_STATUS"
      LAST_ERROR_CODE="BACKEND_PREBUILT_ARTIFACT_MISSING"
      return 1
    fi
    if ! verify_local_candidate_artifact_integrity backend; then
      BACK_STATUS="artefato backend READY divergiu do hash validado"
      LAST_ERROR_STDERR="$BACK_STATUS"
      LAST_ERROR_CODE="BACKEND_PREBUILT_ARTIFACT_INVALID"
      return 1
    fi
    STAGE="publicação do backend"
    zip_progress_publish "Publicando servidor" "Ativando artefato já validado"
    if ! install_backend_prebuilt_artifact "$LOCAL_CANDIDATE_BACKEND_ARTIFACT"; then
      BACK_STATUS="falha ao ativar artefato backend pré-compilado"
      LAST_ERROR_STDERR="${LAST_ERROR_STDERR:-$BACK_STATUS}"
      LAST_ERROR_CODE="BACKEND_PUBLISH_FAILED"
      return 1
    fi
    BACK_RUNTIME_MUTATED=1

    STAGE="reinício do backend"
    if ! systemctl cat "$BACK_SERVICE" >/dev/null 2>&1; then
      BACK_STATUS="serviço systemd ausente: $BACK_SERVICE"
      LAST_ERROR_STDERR="$BACK_STATUS"
      return 1
    fi
    systemctl reset-failed "$BACK_SERVICE" >/dev/null 2>&1 || true
    systemctl restart "$BACK_SERVICE"
    if ! wait_for_service_active "$BACK_SERVICE" 20 0.25; then
      BACK_STATUS="serviço não ficou ativo: $BACK_SERVICE"
      LAST_ERROR_STDERR="$(journalctl -u "$BACK_SERVICE" -n 30 --no-pager 2>/dev/null | trim_alert_text 1800)"
      return 1
    fi
    zip_progress_done_and_publish "Servidor reiniciado" "Validando" "Aguardando resposta"

    STAGE="healthcheck do painel web"
    if { if declare -F wait_for_health_adaptive >/dev/null 2>&1; then wait_for_health_adaptive "$BACK_HEALTH_URL" 14 2; else wait_for_health "$BACK_HEALTH_URL" 8 3; fi; }; then
      ACTIVITY_HEALTHCHECK_STATUS="OK"
      BACK_STATUS="backend publicado a partir do artefato READY e validado em $BACK_HEALTH_URL"
      zip_progress_done "Validação concluída"
      return 0
    fi
    ACTIVITY_HEALTHCHECK_STATUS="falhou"
    BACK_STATUS="backend pré-compilado ativado, mas healthcheck falhou em $BACK_HEALTH_URL"
    return 1
  fi

  if [[ ! -d "$BACK_DIR" ]]; then
    BACK_STATUS="backend não encontrado em $BACK_DIR"
    return 1
  fi

  # npm ci altera node_modules do runtime live antes mesmo do restart. Se
  # qualquer etapa falhar a partir daqui, o rollback precisa restaurar a
  # release preservada em vez de recompilar a versão anterior.
  BACK_RUNTIME_MUTATED=1
  STAGE="dependências do backend"
  zip_progress_run_as_ubuntu \
    "Preparando servidor" \
    "Verificando pacotes" \
    "cd \"$BACK_DIR\" && if [ -f package-lock.json ]; then npm ci $NPM_INSTALL_FLAGS; else npm install $NPM_INSTALL_FLAGS; fi"
  zip_progress_done_and_publish \
    "Servidor preparado" \
    "Compilando servidor" \
    "Gerando arquivos"

  STAGE="testes do backend"
  zip_progress_run_as_ubuntu \
    "Validando servidor" \
    "Executando testes do dashboard" \
    "cd \"$BACK_DIR\" && npm test"
  zip_progress_done_and_publish \
    "Testes do servidor aprovados" \
    "Compilando servidor" \
    "Gerando arquivos"

  STAGE="build do backend"
  zip_progress_run_as_ubuntu \
    "Compilando servidor" \
    "Gerando arquivos" \
    "cd \"$BACK_DIR\" && npm run build && npm prune --omit=dev --no-audit --no-fund --progress=false"
  zip_progress_done_and_publish \
    "Servidor compilado" \
    "Reiniciando servidor" \
    "Aplicando nova versão"

  STAGE="reinício do backend"
  if ! systemctl cat "$BACK_SERVICE" >/dev/null 2>&1; then
    BACK_STATUS="serviço systemd ausente: $BACK_SERVICE"
    LAST_ERROR_STDERR="$BACK_STATUS"
    return 1
  fi
  systemctl reset-failed "$BACK_SERVICE" >/dev/null 2>&1 || true
  systemctl restart "$BACK_SERVICE"
  if ! wait_for_service_active "$BACK_SERVICE" 20 0.25; then
    BACK_STATUS="serviço não ficou ativo: $BACK_SERVICE"
    LAST_ERROR_STDERR="$(journalctl -u "$BACK_SERVICE" -n 30 --no-pager 2>/dev/null | trim_alert_text 1800)"
    return 1
  fi
  BACK_STATUS="backend reiniciado pelo systemd na porta $BACK_PORT"
  zip_progress_done_and_publish \
    "Servidor reiniciado" \
    "Validando" \
    "Aguardando resposta"

  STAGE="healthcheck do painel web"
  if { if declare -F wait_for_health_adaptive >/dev/null 2>&1; then wait_for_health_adaptive "$BACK_HEALTH_URL" 14 2; else wait_for_health "$BACK_HEALTH_URL" 8 3; fi; }; then
    ACTIVITY_HEALTHCHECK_STATUS="OK"
    BACK_STATUS="backend publicado e validado em $BACK_HEALTH_URL"
    zip_progress_done "Validação concluída"
    return 0
  fi

  ACTIVITY_HEALTHCHECK_STATUS="falhou"
  BACK_STATUS="backend reiniciado, mas healthcheck falhou em $BACK_HEALTH_URL"
  return 1
}


runtime_release_root_for_commit() {
  local commit="$(sanitize_commit_ref "${1:-}")"
  [[ -n "$commit" ]] || return 1
  printf '%s/%s\n' "$RUNTIME_RELEASE_ROOT" "$commit"
}

write_runtime_release_manifest() {
  local root="${1:?}" commit="${2:?}" front_ready="${3:-0}" back_ready="${4:-0}"
  RELEASE_ROOT="$root" RELEASE_COMMIT="$commit" RELEASE_FRONT_READY="$front_ready" RELEASE_BACK_READY="$back_ready" \
    python3 - <<'PYRUNTIMERELEASE'
import datetime, hashlib, json, os, pathlib
root = pathlib.Path(os.environ['RELEASE_ROOT'])

def tree_hash(path: pathlib.Path) -> str:
    if not path.exists():
        return ''
    digest = hashlib.sha256()
    base = path.resolve()
    for item in sorted(path.rglob('*'), key=lambda p: p.as_posix()):
        rel = item.relative_to(path).as_posix()
        if item.is_symlink():
            target = os.readlink(item)
            if os.path.isabs(target):
                raise SystemExit(f'release contains absolute symlink: {rel}')
            resolved = (item.parent / target).resolve(strict=False)
            try:
                resolved.relative_to(base)
            except ValueError:
                raise SystemExit(f'release symlink escapes root: {rel}')
            digest.update(b'L\0' + rel.encode() + b'\0' + target.encode() + b'\0')
            continue
        if not item.is_file():
            continue
        digest.update(b'F\0' + rel.encode() + b'\0')
        with item.open('rb') as fh:
            for chunk in iter(lambda: fh.read(1024 * 1024), b''):
                digest.update(chunk)
    return digest.hexdigest()

front_ready = os.environ.get('RELEASE_FRONT_READY') == '1'
front_release_key = ''
if front_ready:
    ref = root / 'frontend.json'
    if ref.is_file() and not ref.is_symlink():
        data = json.loads(ref.read_text(encoding='utf-8'))
        front_release_key = str(data.get('release_key') or '')
        if not front_release_key:
            raise SystemExit('frontend runtime release missing release_key')
    elif (root / 'frontend').is_dir():
        # Compatibilidade para releases criados antes do frontend por referência.
        front_release_key = ''
    else:
        raise SystemExit('frontend runtime release missing reference')

back_ready = os.environ.get('RELEASE_BACK_READY') == '1'
back_deps_key = ''
if back_ready:
    deps_file = root / 'backend' / 'deps.json'
    if not deps_file.is_file() or deps_file.is_symlink():
        raise SystemExit('backend release missing deps.json')
    deps = json.loads(deps_file.read_text(encoding='utf-8'))
    back_deps_key = str(deps.get('deps_key') or '')
    if len(back_deps_key) != 64:
        raise SystemExit('backend release has invalid deps_key')

payload = {
    'state': 'ready',
    'commit': os.environ['RELEASE_COMMIT'],
    'created_at': datetime.datetime.now(datetime.timezone.utc).isoformat(),
    'frontend': {
        'ready': front_ready,
        'release_key': front_release_key,
        'sha256': tree_hash(root / 'frontend') if front_ready and not front_release_key else '',
    },
    'backend': {
        'ready': back_ready,
        'sha256': tree_hash(root / 'backend' / 'dist') if back_ready else '',
        'deps_key': back_deps_key,
    },
}
path = root / 'release.json'
tmp = root / '.release.json.tmp'
tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding='utf-8')
os.replace(tmp, path)
PYRUNTIMERELEASE
}

verify_runtime_release_component() {
  local root="${1:?}" kind="${2:?}" ready
  ready="$root/release.json"
  [[ -s "$ready" && ! -L "$ready" ]] || return 1

  if [[ "$kind" == "frontend" ]]; then
    local release_key release_dir
    release_key="$(RELEASE_READY_FILE="$ready" python3 - <<'PYFRONTRELEASEKEY' 2>/dev/null
import json, os
with open(os.environ['RELEASE_READY_FILE'], encoding='utf-8') as fh:
    data = json.load(fh)
record = data.get('frontend') if isinstance(data, dict) else None
if data.get('state') != 'ready' or not isinstance(record, dict) or not record.get('ready'):
    raise SystemExit(1)
print(str(record.get('release_key') or ''))
PYFRONTRELEASEKEY
)" || return 1
    if [[ -n "$release_key" ]]; then
      release_dir="$(frontend_release_root_for_key "$release_key")" || return 1
      verify_frontend_release "$release_dir" "$release_key"
      return $?
    fi
    # Compatibilidade com release Wave 6–11 que continha snapshot físico.
    RELEASE_READY_FILE="$ready" RELEASE_ROOT="$root" python3 - <<'PYLEGACYFRONTVERIFY' >/dev/null 2>&1
import hashlib, json, os, pathlib
ready = pathlib.Path(os.environ['RELEASE_READY_FILE'])
root = pathlib.Path(os.environ['RELEASE_ROOT'])
data = json.loads(ready.read_text(encoding='utf-8'))
record = data.get('frontend') if isinstance(data, dict) else None
if not isinstance(record, dict) or not record.get('ready'):
    raise SystemExit(1)
base = root / 'frontend'
if not base.is_dir():
    raise SystemExit(1)
digest = hashlib.sha256()
resolved_base = base.resolve()
for item in sorted(base.rglob('*'), key=lambda p: p.as_posix()):
    rel = item.relative_to(base).as_posix()
    if item.is_symlink():
        target = os.readlink(item)
        if os.path.isabs(target):
            raise SystemExit(1)
        resolved = (item.parent / target).resolve(strict=False)
        try:
            resolved.relative_to(resolved_base)
        except ValueError:
            raise SystemExit(1)
        digest.update(b'L\0' + rel.encode() + b'\0' + target.encode() + b'\0')
        continue
    if not item.is_file():
        continue
    digest.update(b'F\0' + rel.encode() + b'\0')
    with item.open('rb') as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b''):
            digest.update(chunk)
if digest.hexdigest() != str(record.get('sha256') or ''):
    raise SystemExit(1)
PYLEGACYFRONTVERIFY
    return $?
  fi

  if [[ "$kind" == "backend" ]]; then
    local deps_key deps_layer
    deps_key="$(RELEASE_READY_FILE="$ready" python3 - <<'PYBACKRELEASEDEPS' 2>/dev/null
import json, os
with open(os.environ['RELEASE_READY_FILE'], encoding='utf-8') as fh:
    data = json.load(fh)
record = data.get('backend') if isinstance(data, dict) else None
if data.get('state') != 'ready' or not isinstance(record, dict) or not record.get('ready'):
    raise SystemExit(1)
key = str(record.get('deps_key') or '')
if len(key) != 64:
    raise SystemExit(1)
print(key)
PYBACKRELEASEDEPS
)" || return 1
    deps_layer="$(node_dependency_layer_root backend prod "$deps_key")" || return 1
    verify_node_dependency_layer "$deps_layer" "$deps_key" prod || return 1
  fi

  RELEASE_READY_FILE="$ready" RELEASE_KIND="$kind" RELEASE_ROOT="$root" python3 - <<'PYVERIFYRUNTIME' >/dev/null 2>&1
import hashlib, json, os, pathlib
ready = pathlib.Path(os.environ['RELEASE_READY_FILE'])
kind = os.environ['RELEASE_KIND']
root = pathlib.Path(os.environ['RELEASE_ROOT'])
data = json.loads(ready.read_text(encoding='utf-8'))
record = data.get(kind) if isinstance(data, dict) else None
if data.get('state') != 'ready' or not isinstance(record, dict) or not record.get('ready'):
    raise SystemExit(1)
path = root / 'backend/dist'

def tree_hash(base: pathlib.Path) -> str:
    digest = hashlib.sha256()
    resolved_base = base.resolve()
    for item in sorted(base.rglob('*'), key=lambda p: p.as_posix()):
        rel = item.relative_to(base).as_posix()
        if item.is_symlink():
            target = os.readlink(item)
            if os.path.isabs(target):
                raise SystemExit(1)
            resolved = (item.parent / target).resolve(strict=False)
            try:
                resolved.relative_to(resolved_base)
            except ValueError:
                raise SystemExit(1)
            digest.update(b'L\0' + rel.encode() + b'\0' + target.encode() + b'\0')
            continue
        if not item.is_file():
            continue
        digest.update(b'F\0' + rel.encode() + b'\0')
        with item.open('rb') as fh:
            for chunk in iter(lambda: fh.read(1024 * 1024), b''):
                digest.update(chunk)
    return digest.hexdigest()

if not path.exists() or tree_hash(path) != str(record.get('sha256') or ''):
    raise SystemExit(1)
PYVERIFYRUNTIME
}

capture_runtime_release_snapshot() {
  local commit="$(sanitize_commit_ref "${1:-$PREVIOUS_COMMIT}")"
  [[ -n "$commit" ]] || return 1
  if (( ${REQUIREMENTS_CHANGED:-0} == 1 )); then
    if ! capture_python_runtime_release_snapshot "$commit"; then
      return 1
    fi
  fi
  if (( FRONT_CHANGED == 0 && BACK_CHANGED == 0 )); then
    RUNTIME_RELEASE_SNAPSHOT_READY=1
    RUNTIME_RELEASE_SNAPSHOT_COMMIT="$commit"
    return 0
  fi

  local root tmp front_ready=0 back_ready=0
  root="$(runtime_release_root_for_commit "$commit")" || return 1
  if [[ -s "$root/release.json" ]]; then
    local reusable=1
    (( FRONT_CHANGED == 0 )) || verify_runtime_release_component "$root" frontend || reusable=0
    (( BACK_CHANGED == 0 )) || verify_runtime_release_component "$root" backend || reusable=0
    if (( reusable == 1 )); then
      RUNTIME_RELEASE_SNAPSHOT_ROOT="$root"
      RUNTIME_RELEASE_SNAPSHOT_COMMIT="$commit"
      RUNTIME_RELEASE_SNAPSHOT_READY=1
      logger -t "$LOG_TAG" "release runtime anterior reutilizado para rollback: $(short_commit "$commit")" 2>/dev/null || true
      return 0
    fi
  fi

  install -d -o ubuntu -g ubuntu -m 0775 "$RUNTIME_RELEASE_ROOT" || return 1
  tmp="$(mktemp -d "$RUNTIME_RELEASE_ROOT/.${commit}.XXXXXX")" || return 1

  if (( FRONT_CHANGED == 1 )); then
    if ! frontend_publication_is_healthy; then
      LAST_ERROR_STDERR="não há publicação frontend saudável para preservar antes da promoção"
      LAST_ERROR_CODE="RUNTIME_RELEASE_FRONTEND_BASELINE_MISSING"
      rm -rf -- "$tmp" 2>/dev/null || true
      return 1
    fi
    local baseline_front_release_key=""
    baseline_front_release_key="$(frontend_active_release_key 2>/dev/null || true)"
    if [[ -z "$baseline_front_release_key" ]]; then
      baseline_front_release_key="$(adopt_frontend_publication_as_release "$commit" 2>/dev/null || true)"
    fi
    if [[ -z "$baseline_front_release_key" ]]; then
      LAST_ERROR_STDERR="não foi possível adotar/reutilizar release frontend anterior"
      LAST_ERROR_CODE="RUNTIME_RELEASE_FRONTEND_ADOPTION_FAILED"
      rm -rf -- "$tmp" 2>/dev/null || true
      return 1
    fi
    RELEASE_FRONT_REF_FILE="$tmp/frontend.json" RELEASE_FRONT_REF_KEY="$baseline_front_release_key" python3 - <<'PYRELEASEFRONTREF'
import json, os, pathlib
path = pathlib.Path(os.environ['RELEASE_FRONT_REF_FILE'])
payload = {'release_key': os.environ['RELEASE_FRONT_REF_KEY']}
path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding='utf-8')
PYRELEASEFRONTREF
    front_ready=1
  fi

  if (( BACK_CHANGED == 1 )); then
    if [[ ! -s "$BACK_DIR/dist/index.js" || ! -e "$BACK_DIR/node_modules" && ! -L "$BACK_DIR/node_modules" ]]; then
      LAST_ERROR_STDERR="backend live não possui dist/index.js e node_modules preserváveis antes da promoção"
      LAST_ERROR_CODE="RUNTIME_RELEASE_BACKEND_BASELINE_MISSING"
      rm -rf -- "$tmp" 2>/dev/null || true
      return 1
    fi
    if ! backend_live_dependency_layer "$BACK_DIR"; then
      LAST_ERROR_STDERR="não foi possível adotar/reutilizar dependency layer do backend live antes da promoção"
      LAST_ERROR_CODE="RUNTIME_RELEASE_BACKEND_DEP_LAYER_FAILED"
      rm -rf -- "$tmp" 2>/dev/null || true
      return 1
    fi
    local baseline_dep_key="$LAST_NODE_DEP_LAYER_KEY"
    [[ "$baseline_dep_key" =~ ^[a-f0-9]{64}$ ]] || {
      rm -rf -- "$tmp" 2>/dev/null || true
      return 1
    }
    install -d -m 0755 "$tmp/backend"
    cp -a -- "$BACK_DIR/dist" "$tmp/backend/dist" || { rm -rf -- "$tmp" 2>/dev/null || true; return 1; }
    RELEASE_BACK_DEPS_FILE="$tmp/backend/deps.json" RELEASE_BACK_DEPS_KEY="$baseline_dep_key" python3 - <<'PYRELEASEBACKDEPS'
import json, os, pathlib
path = pathlib.Path(os.environ['RELEASE_BACK_DEPS_FILE'])
payload = {'mode': 'prod', 'deps_key': os.environ['RELEASE_BACK_DEPS_KEY']}
path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding='utf-8')
PYRELEASEBACKDEPS
    back_ready=1
  fi

  if ! write_runtime_release_manifest "$tmp" "$commit" "$front_ready" "$back_ready"; then
    LAST_ERROR_STDERR="não foi possível registrar release runtime anterior"
    LAST_ERROR_CODE="RUNTIME_RELEASE_MANIFEST_FAILED"
    rm -rf -- "$tmp" 2>/dev/null || true
    return 1
  fi
  if (( front_ready == 1 )) && ! verify_runtime_release_component "$tmp" frontend; then
    rm -rf -- "$tmp" 2>/dev/null || true
    return 1
  fi
  if (( back_ready == 1 )) && ! verify_runtime_release_component "$tmp" backend; then
    rm -rf -- "$tmp" 2>/dev/null || true
    return 1
  fi

  rm -rf -- "$root" 2>/dev/null || true
  if ! mv -- "$tmp" "$root"; then
    rm -rf -- "$tmp" 2>/dev/null || true
    return 1
  fi
  chmod 0644 "$root/release.json" 2>/dev/null || true
  RUNTIME_RELEASE_SNAPSHOT_ROOT="$root"
  RUNTIME_RELEASE_SNAPSHOT_COMMIT="$commit"
  RUNTIME_RELEASE_SNAPSHOT_READY=1
  logger -t "$LOG_TAG" "release runtime anterior preservado para rollback: $(short_commit "$commit") em $root" 2>/dev/null || true
  return 0
}

restore_frontend_runtime_release() {
  local commit="$(sanitize_commit_ref "${1:-$PREVIOUS_COMMIT}")" root release_key=""
  root="$(runtime_release_root_for_commit "$commit")" || return 1
  if ! verify_runtime_release_component "$root" frontend; then
    FRONT_STATUS="release frontend anterior ausente ou corrompido para $(short_commit "$commit")"
    LAST_ERROR_STDERR="$FRONT_STATUS"
    LAST_ERROR_CODE="ROLLBACK_FRONTEND_RELEASE_INVALID"
    return 1
  fi
  release_key="$(RELEASE_READY_FILE="$root/release.json" python3 - <<'PYROLLBACKFRONTKEY' 2>/dev/null
import json, os
with open(os.environ['RELEASE_READY_FILE'], encoding='utf-8') as fh:
    data = json.load(fh)
record = data.get('frontend') if isinstance(data, dict) else None
print(str(record.get('release_key') or '') if isinstance(record, dict) else '')
PYROLLBACKFRONTKEY
)" || true
  if [[ -z "$release_key" ]]; then
    # Migração de releases Wave 6–11: transforma o snapshot físico antigo em
    # release dedicada uma única vez; hardlink é usado quando o filesystem permite.
    [[ -s "$root/frontend/index.html" ]] || return 1
    prepare_frontend_release "$root/frontend" "$commit" >/dev/null || {
      LAST_ERROR_CODE="ROLLBACK_FRONTEND_RELEASE_MIGRATION_FAILED"
      return 1
    }
    release_key="$commit"
  fi
  if ! activate_frontend_release "$release_key"; then
    LAST_ERROR_CODE="ROLLBACK_FRONTEND_RELEASE_PUBLISH_FAILED"
    return 1
  fi
  FRONT_STATUS="frontend restaurado sem rebuild por release $(short_commit "$release_key")"
  return 0
}

restore_backend_runtime_release() {
  local commit="$(sanitize_commit_ref "${1:-$PREVIOUS_COMMIT}")" root
  root="$(runtime_release_root_for_commit "$commit")" || return 1
  if ! verify_runtime_release_component "$root" backend; then
    BACK_STATUS="release backend anterior ausente ou corrompido para $(short_commit "$commit")"
    LAST_ERROR_STDERR="$BACK_STATUS"
    LAST_ERROR_CODE="ROLLBACK_BACKEND_RELEASE_INVALID"
    return 1
  fi
  if ! install_backend_prebuilt_artifact "$root/backend"; then
    BACK_STATUS="falha ao restaurar release backend anterior"
    LAST_ERROR_STDERR="${LAST_ERROR_STDERR:-$BACK_STATUS}"
    LAST_ERROR_CODE="ROLLBACK_BACKEND_RELEASE_INSTALL_FAILED"
    return 1
  fi
  if ! systemctl cat "$BACK_SERVICE" >/dev/null 2>&1; then
    BACK_STATUS="serviço systemd ausente durante rollback: $BACK_SERVICE"
    LAST_ERROR_STDERR="$BACK_STATUS"
    LAST_ERROR_CODE="ROLLBACK_BACKEND_SERVICE_MISSING"
    return 1
  fi
  systemctl reset-failed "$BACK_SERVICE" >/dev/null 2>&1 || true
  systemctl restart "$BACK_SERVICE" || return 1
  if ! wait_for_service_active "$BACK_SERVICE" 20 0.25; then
    BACK_STATUS="backend anterior restaurado, mas serviço não ficou ativo"
    LAST_ERROR_STDERR="$(journalctl -u "$BACK_SERVICE" -n 30 --no-pager 2>/dev/null | trim_alert_text 1800)"
    LAST_ERROR_CODE="ROLLBACK_BACKEND_SERVICE_FAILED"
    return 1
  fi
  if ! { if declare -F wait_for_health_adaptive >/dev/null 2>&1; then wait_for_health_adaptive "$BACK_HEALTH_URL" 14 2; else wait_for_health "$BACK_HEALTH_URL" 8 3; fi; }; then
    ACTIVITY_HEALTHCHECK_STATUS="falhou"
    BACK_STATUS="backend anterior restaurado sem rebuild, mas healthcheck falhou"
    LAST_ERROR_CODE="ROLLBACK_BACKEND_HEALTH_FAILED"
    return 1
  fi
  ACTIVITY_HEALTHCHECK_STATUS="OK"
  BACK_STATUS="backend restaurado sem rebuild a partir do release $(short_commit "$commit")"
  return 0
}

prune_runtime_releases() {
  prune_python_runtime_releases || true
  prune_frontend_releases || true
  local keep="${RUNTIME_RELEASE_RETENTION:-3}" current count=0 entry
  [[ "$keep" =~ ^[0-9]+$ ]] || keep=3
  (( keep >= 1 )) || keep=1
  [[ -d "$RUNTIME_RELEASE_ROOT" ]] || return 0
  current="$(repo_git rev-parse HEAD 2>/dev/null || true)"
  while IFS= read -r entry; do
    [[ -n "$entry" ]] || continue
    if [[ "$(basename "$entry")" == "$current" ]]; then
      continue
    fi
    count=$((count + 1))
    if (( count > keep )); then
      rm -rf -- "$entry" 2>/dev/null || true
    fi
  done < <(find "$RUNTIME_RELEASE_ROOT" -mindepth 1 -maxdepth 1 -type d ! -name '.*' -printf '%T@ %p\n' 2>/dev/null | sort -nr | cut -d' ' -f2-)
}


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
