#!/usr/bin/env bash
set -Eeuo pipefail

REPO_DIR="/home/ubuntu/bot"
BRANCH="main"
SERVICE="tts-bot"
LAVALINK_SERVICE="lavalink"
LOG_TAG="bot-updater"
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
  # A fotografia dos módulos também pertence à transação: capturá-la com o
  # lock evita copiar duas revisões enquanto outra execução promove o Git.
  mkdir -p "$(dirname "$UPDATER_LOCK_FILE")" 2>/dev/null || true
  exec 9>"$UPDATER_LOCK_FILE"
  if [[ "${TTS_BOT_UPDATER_WAIT_FOR_LOCK:-0}" == 1 ]]; then
    # O serviço novo espera a família antiga encerrar, sem loop do .path.
    flock 9
  elif ! flock -n 9; then
    logger -t "$LOG_TAG" "updater já está em execução; mantendo fila para o próximo ciclo" 2>/dev/null || true
    exit 0
  fi
  UPDATER_RUNTIME_BASE="${TTS_BOT_UPDATER_RUNTIME_DIR:-${TMPDIR:-/tmp}}"
  if [[ "$UPDATER_RUNTIME_BASE" != /* || ! -d "$UPDATER_RUNTIME_BASE" || -L "$UPDATER_RUNTIME_BASE" || ! -w "$UPDATER_RUNTIME_BASE" ]]; then
    UPDATER_RUNTIME_BASE="/tmp"
  fi
  # mktemp evita colisão/symlink previsível em /tmp. No serviço systemd, a
  # cópia fica em RuntimeDirectory e é removida pelo próprio systemd mesmo em
  # SIGKILL, reboot ou queda antes do trap EXIT.
  UPDATER_RUNTIME_BUNDLE="$(mktemp -d "$UPDATER_RUNTIME_BASE/bot-updater.XXXXXX.core")"
  trap 'rm -rf -- "$UPDATER_RUNTIME_BUNDLE"' EXIT
  cp -a -- "$UPDATER_SOURCE_DIR/." "$UPDATER_RUNTIME_BUNDLE/"
  UPDATER_SOURCE_DIR="$UPDATER_RUNTIME_BUNDLE"
  UPDATER_RUNTIME_COPY="$UPDATER_SOURCE_DIR/atualizar.sh"
  chmod 0700 "$UPDATER_RUNTIME_COPY"
  export TTS_BOT_UPDATER_RUNNING_COPY=1
  export TTS_BOT_UPDATER_SOURCE_DIR="$UPDATER_SOURCE_DIR"
  export TTS_BOT_UPDATER_RUNTIME_COPY="$UPDATER_RUNTIME_COPY"
  export TTS_BOT_UPDATER_RUNTIME_BUNDLE="$UPDATER_RUNTIME_BUNDLE"
  exec /usr/bin/env bash "$UPDATER_RUNTIME_COPY" "$@"
fi
UPDATER_RUNTIME_COPY="${TTS_BOT_UPDATER_RUNTIME_COPY:-}"
UPDATER_RUNTIME_BUNDLE="${TTS_BOT_UPDATER_RUNTIME_BUNDLE:-}"
# Cobre também saídas anteriores ao carregamento dos traps completos.
trap 'if [[ -n "$UPDATER_RUNTIME_BUNDLE" && "$UPDATER_SOURCE_DIR" == "$UPDATER_RUNTIME_BUNDLE" ]]; then rm -rf -- "$UPDATER_RUNTIME_BUNDLE"; fi' EXIT

# Perfil conservador padrão: protege heartbeat/voz do bot na VPS pequena.
# Trechos curtos de Git/worktree usam um perfil moderado temporário; antes de
# testes, builds e snapshots pesados voltamos sempre ao perfil conservador.
# módulo: configuracao.sh
. "$UPDATER_SOURCE_DIR/configuracao.sh"

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
UPDATER_UNIT="bot-updater.service"
UPDATER_EPHEMERAL_DIR="${TTS_BOT_UPDATER_RUNTIME_DIR:-${TMPDIR:-/tmp}}"
if [[ "$UPDATER_EPHEMERAL_DIR" != /* || ! -d "$UPDATER_EPHEMERAL_DIR" || -L "$UPDATER_EPHEMERAL_DIR" || ! -w "$UPDATER_EPHEMERAL_DIR" ]]; then
  UPDATER_EPHEMERAL_DIR="/tmp"
fi
RUN_LOG_FILE="$(mktemp "$UPDATER_EPHEMERAL_DIR/bot-updater.XXXXXX.log")"
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
# módulo: manutencao.sh
. "$UPDATER_SOURCE_DIR/manutencao.sh"
# módulo: persistencia.sh
. "$UPDATER_SOURCE_DIR/persistencia.sh"

# módulo: progresso.sh
. "$UPDATER_SOURCE_DIR/progresso.sh"

# módulo: reversao.sh
. "$UPDATER_SOURCE_DIR/reversao.sh"

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

# módulo: orquestracao.sh
. "$UPDATER_SOURCE_DIR/orquestracao.sh"

# módulo: finalizacao.sh
. "$UPDATER_SOURCE_DIR/finalizacao.sh"
