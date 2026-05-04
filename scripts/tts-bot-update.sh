#!/usr/bin/env bash
set -Eeuo pipefail

REPO_DIR="/home/ubuntu/bot"
BRANCH="main"
SERVICE="tts-bot"
CALLKEEPER_SERVICE="callkeeper"
LOG_TAG="tts-bot-updater"
DIRTY_MARKER_FILE="$REPO_DIR/.fatal-update-dirty"

FRONT_DIR="$REPO_DIR/activity /sinuca"
BACK_DIR="$REPO_DIR/activity /sinuca-server"
FRONT_PUBLISH_DIR="/var/www/sinuca"
BACK_PORT="8787"
BACK_HEALTH_URL="http://127.0.0.1:${BACK_PORT}/health"
BOT_HEALTH_URL="http://127.0.0.1:10000/health"

STAGE="inicialização"
FAILED_STAGE=""
CURRENT_COMMIT=""
REMOTE_COMMIT=""
PREVIOUS_COMMIT=""
COMMIT_SUBJECT=""
UPDATE_APPLIED=0
ROLLBACK_DONE=0

FRONT_CHANGED=0
BACK_CHANGED=0
BOT_CHANGED=0
CALLKEEPER_CHANGED=0

BOT_HEALTHCHECK_STATUS="não verificado"
CALLKEEPER_STATUS="não alterado"
FRONT_STATUS="não alterado"
BACK_STATUS="não alterado"
ACTIVITY_HEALTHCHECK_STATUS="não verificado"
ROLLBACK_STATUS="não foi necessário"
CHANGED_FILES_RAW=""
UPDATER_UNIT="tts-bot-updater.service"
RUN_LOG_FILE="${TMPDIR:-/tmp}/tts-bot-updater.$$.log"
: > "$RUN_LOG_FILE"

# DevAI integration: log persistente que a DevAI lê via DEVAI_LOG_PATHS.
# Sem isso a DevAI nunca vê falhas do updater systemd (que loga só pra
# journalctl + tmpfile com PID no nome).
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

cleanup_runtime_artifacts() {
  rm -f "$RUN_LOG_FILE"
}

trim_alert_text() {
  local limit="${1:-4000}"
  python3 - "$limit" <<'PY'
import sys
limit = int(sys.argv[1])
text = sys.stdin.read().replace("\r\n", "\n").replace("\r", "\n").strip()
if not text:
    print("")
    raise SystemExit
if len(text) > limit:
    text = text[: limit - 1].rstrip() + "…"
print(text)
PY
}

collect_run_log_excerpt() {
  if [[ -f "$RUN_LOG_FILE" ]]; then
    tail -n 40 "$RUN_LOG_FILE" 2>/dev/null | sed 's/\[[0-9;]*[A-Za-z]//g' | trim_alert_text 1500
  fi
}

service_unit_for_stage() {
  local stage_lc="${1,,}"
  if [[ "$stage_lc" == *"callkeeper"* ]]; then
    printf '%s.service' "$CALLKEEPER_SERVICE"
  elif [[ "$stage_lc" == *"bot"* ]]; then
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

register_error_context() {
  LAST_ERROR_EXIT_CODE="${1:-1}"
  LAST_ERROR_COMMAND="${2:-desconhecido}"
  LAST_ERROR_SERVICE_UNIT="$(service_unit_for_stage "$STAGE")"
  LAST_ERROR_STDERR="$(collect_run_log_excerpt)"
  LAST_ERROR_LOGS="$(collect_journal_excerpt "$LAST_ERROR_SERVICE_UNIT")"
  if [[ -z "${LAST_ERROR_STDERR//[[:space:]]/}" ]]; then
    LAST_ERROR_STDERR="nenhuma saída adicional capturada"
  fi
}

LAST_ERROR_EXIT_CODE=""
LAST_ERROR_COMMAND=""
LAST_ERROR_SERVICE_UNIT=""
LAST_ERROR_STDERR=""
LAST_ERROR_LOGS=""

send_info() {
  sudo -u ubuntu /home/ubuntu/bot/alert.sh info "$1" "$2" || true
}

send_warn() {
  sudo -u ubuntu /home/ubuntu/bot/alert.sh warn "$1" "$2" || true
}

send_error() {
  sudo -u ubuntu /home/ubuntu/bot/alert.sh error "$1" "$2" || true
}

human_duration() {
  local total="${1:-0}"
  local m=$((total / 60))
  local s=$((total % 60))
  if (( m > 0 )); then
    printf "%dm %02ds" "$m" "$s"
  else
    printf "%ds" "$s"
  fi
}

run_as_ubuntu() {
  sudo -u ubuntu -H bash -lc "$1"
}

wait_for_health() {
  local url="${1:?}"
  local attempts="${2:-12}"
  local delay="${3:-5}"
  local i

  for ((i=1; i<=attempts; i++)); do
    if curl -fsS --max-time 5 "$url" >/dev/null 2>&1; then
      return 0
    fi
    sleep "$delay"
  done

  return 1
}

short_commit() {
  local value="${1:-}"
  if [[ -z "$value" ]]; then
    printf 'desconhecido'
  else
    printf '%s' "${value:0:7}"
  fi
}

marker_value() {
  local key="${1:?}"
  if [[ ! -f "$DIRTY_MARKER_FILE" ]]; then
    return 0
  fi
  awk -F= -v wanted="$key" '$1 == wanted { sub($1 "=", ""); print; exit }' "$DIRTY_MARKER_FILE" 2>/dev/null || true
}

write_dirty_marker() {
  local failed_commit="${1:-}"
  local rollback_commit="${2:-}"
  local failed_stage="${3:-desconhecido}"
  local failed_command="${4:-desconhecido}"

  cat > "$DIRTY_MARKER_FILE" <<EOM
FAILED_REMOTE_COMMIT=$failed_commit
ROLLED_BACK_TO=$rollback_commit
FAILED_STAGE=$failed_stage
FAILED_COMMAND=$failed_command
FAILED_AT=$(date -u '+%Y-%m-%dT%H:%M:%SZ')
EOM
  chown ubuntu:ubuntu "$DIRTY_MARKER_FILE" 2>/dev/null || true
}

clear_dirty_marker() {
  rm -f "$DIRTY_MARKER_FILE"
}

format_changed_files() {
  if [[ -n "$CHANGED_FILES_RAW" ]]; then
    printf '%s\n' "$CHANGED_FILES_RAW" | head -n 20 | sed 's/^/• /'
  else
    printf '• nenhum arquivo listado'
  fi
}

deploy_bot() {
  STAGE="dependências do bot"
  if [[ -x "$REPO_DIR/.venv/bin/pip" && -f "$REPO_DIR/requirements.txt" ]]; then
    sudo -u ubuntu -H "$REPO_DIR/.venv/bin/pip" install -r "$REPO_DIR/requirements.txt"
  fi

  if (( BOT_CHANGED == 1 )); then
    STAGE="reinício do bot"
    systemctl restart "$SERVICE"
    sleep 3
    STAGE="healthcheck do bot"
    if systemctl is-active --quiet "$SERVICE" && wait_for_health "$BOT_HEALTH_URL" 12 5; then
      BOT_HEALTHCHECK_STATUS="OK"
      return 0
    fi
    BOT_HEALTHCHECK_STATUS="falhou"
    return 1
  fi

  STAGE="healthcheck do bot"
  if wait_for_health "$BOT_HEALTH_URL" 2 2; then
    BOT_HEALTHCHECK_STATUS="OK"
    return 0
  fi

  BOT_HEALTHCHECK_STATUS="falhou"
  return 1
}

deploy_callkeeper() {
  if (( CALLKEEPER_CHANGED == 0 )); then
    CALLKEEPER_STATUS="não alterado"
    return 0
  fi

  STAGE="configuração do CallKeeper"
  if [[ -f "$REPO_DIR/deploy/systemd/callkeeper.service" ]]; then
    cp "$REPO_DIR/deploy/systemd/callkeeper.service" /etc/systemd/system/callkeeper.service
    systemctl daemon-reload
    systemctl enable "$CALLKEEPER_SERVICE" >/dev/null 2>&1 || true
  fi

  STAGE="reinício do CallKeeper"
  systemctl restart "$CALLKEEPER_SERVICE"
  sleep 2

  STAGE="healthcheck do CallKeeper"
  if systemctl is-active --quiet "$CALLKEEPER_SERVICE"; then
    CALLKEEPER_STATUS="OK"
    return 0
  fi

  CALLKEEPER_STATUS="falhou"
  return 1
}

deploy_frontend() {
  if (( FRONT_CHANGED == 0 )); then
    FRONT_STATUS="não alterado"
    return 0
  fi

  if [[ ! -d "$FRONT_DIR" ]]; then
    FRONT_STATUS="frontend não encontrado em $FRONT_DIR"
    return 1
  fi

  STAGE="build do frontend"
  run_as_ubuntu "cd \"$FRONT_DIR\" && [ -d node_modules ] || npm install"
  run_as_ubuntu "cd \"$FRONT_DIR\" && npm run build"

  STAGE="publicação do frontend"
  mkdir -p "$FRONT_PUBLISH_DIR"
  rm -rf "${FRONT_PUBLISH_DIR:?}/"*
  cp -r "$FRONT_DIR/dist/." "$FRONT_PUBLISH_DIR/"
  FRONT_STATUS="frontend publicado em $FRONT_PUBLISH_DIR"
  return 0
}

deploy_backend() {
  if (( BACK_CHANGED == 0 )); then
    BACK_STATUS="não alterado"
  else
    if [[ ! -d "$BACK_DIR" ]]; then
      BACK_STATUS="backend não encontrado em $BACK_DIR"
      return 1
    fi

    STAGE="build do backend"
    run_as_ubuntu "cd \"$BACK_DIR\" && [ -d node_modules ] || npm install"
    run_as_ubuntu "cd \"$BACK_DIR\" && npm run build"

    STAGE="reinício do backend"
    fuser -k "${BACK_PORT}/tcp" >/dev/null 2>&1 || true
    run_as_ubuntu "cd \"$BACK_DIR\"; set -a; [ -f \"$REPO_DIR/.env\" ] && . \"$REPO_DIR/.env\" || true; [ -f .env ] && . ./.env || true; set +a; nohup node dist/index.js >> sinuca-server.log 2>&1 &"
    sleep 3
    BACK_STATUS="backend reiniciado na porta $BACK_PORT"
  fi

  STAGE="healthcheck da activity"
  if wait_for_health "$BACK_HEALTH_URL" 18 5; then
    ACTIVITY_HEALTHCHECK_STATUS="OK"
    if (( BACK_CHANGED == 1 )); then
      BACK_STATUS="backend publicado e validado em $BACK_HEALTH_URL"
    fi
    return 0
  fi

  ACTIVITY_HEALTHCHECK_STATUS="falhou"
  if (( BACK_CHANGED == 1 )); then
    BACK_STATUS="backend reiniciado, mas healthcheck falhou em $BACK_HEALTH_URL"
  elif (( FRONT_CHANGED == 0 )); then
    BACK_STATUS="backend sem mudanças, mas healthcheck falhou em $BACK_HEALTH_URL"
  fi
  return 1
}

rollback_after_failure() {
  local exit_code="${1:-1}"
  local failed_command="${2:-desconhecido}"
  register_error_context "$exit_code" "$failed_command"
  local rollback_bot_status="não executado"
  local rollback_front_status="não executado"
  local rollback_back_status="não executado"
  local rollback_activity_status="não executado"
  local reset_status=1

  trap - ERR
  set +e

  if (( ROLLBACK_DONE == 1 )); then
    exit "$exit_code"
  fi
  ROLLBACK_DONE=1

  logger -t "$LOG_TAG" "Erro fatal após update. Voltando de $(short_commit "$REMOTE_COMMIT") para $(short_commit "$PREVIOUS_COMMIT")"

  STAGE="rollback git"
  sudo -u ubuntu -H git reset --hard "$PREVIOUS_COMMIT" >/dev/null 2>&1
  reset_status=$?

  write_dirty_marker "$REMOTE_COMMIT" "$PREVIOUS_COMMIT" "$FAILED_STAGE" "$failed_command"
  ROLLBACK_STATUS="aplicado para $(short_commit "$PREVIOUS_COMMIT") e commit remoto marcado como sujo"

  if (( FRONT_CHANGED == 1 )); then
    deploy_frontend
    rollback_front_status="$FRONT_STATUS"
  else
    rollback_front_status="não precisou republicar"
  fi

  if (( BACK_CHANGED == 1 )); then
    deploy_backend
    rollback_back_status="$BACK_STATUS"
    rollback_activity_status="$ACTIVITY_HEALTHCHECK_STATUS"
  else
    if wait_for_health "$BACK_HEALTH_URL" 2 2; then
      rollback_activity_status="OK"
    else
      rollback_activity_status="falhou"
    fi
    rollback_back_status="não precisou reiniciar"
  fi

  deploy_bot
  rollback_bot_status="$BOT_HEALTHCHECK_STATUS"

  local rollback_callkeeper_status="não precisou reiniciar"
  if (( CALLKEEPER_CHANGED == 1 )); then
    deploy_callkeeper
    rollback_callkeeper_status="$CALLKEEPER_STATUS"
  fi

  local duration
  duration="$(human_duration "$SECONDS")"

  local body
  body="Resumo: O update falhou de forma fatal. O repositório voltou para o commit anterior e o commit remoto foi marcado como sujo nesta VPS.
Host: $HOSTNAME
Branch: $BRANCH
Serviço: tts-bot-updater
Serviço afetado: ${LAST_ERROR_SERVICE_UNIT:-$UPDATER_UNIT}
Commit anterior: $(short_commit "$PREVIOUS_COMMIT")
Commit alvo: $(short_commit "$REMOTE_COMMIT")
Commit: $(short_commit "$PREVIOUS_COMMIT") ← $(short_commit "$REMOTE_COMMIT")
Mudança: ${COMMIT_SUBJECT:-sem mensagem}
Update: ${COMMIT_SUBJECT:-sem mensagem}
Etapa: ${FAILED_STAGE:-$STAGE}
Comando: $failed_command
Código: $exit_code
Rollback: $ROLLBACK_STATUS
Commit sujo: sim
Bot: $rollback_bot_status
CallKeeper: $rollback_callkeeper_status
Frontend: $rollback_front_status
Backend: $rollback_back_status
Activity: $rollback_activity_status
Motivo: reset git=$reset_status
Stderr:
${LAST_ERROR_STDERR:-nenhuma saída adicional capturada}
Últimas linhas:
${LAST_ERROR_LOGS:-nenhum log adicional encontrado}
Duração: $duration
Hora: $(date '+%d/%m/%Y %H:%M:%S')"

  send_error "Rollback aplicado após erro fatal" "$body"
  exit "$exit_code"
}

on_error() {
  local exit_code="$?"
  local failed_command="${BASH_COMMAND:-desconhecido}"
  FAILED_STAGE="$STAGE"
  register_error_context "$exit_code" "$failed_command"

  if (( UPDATE_APPLIED == 1 )) && [[ -n "$PREVIOUS_COMMIT" ]]; then
    rollback_after_failure "$exit_code" "$failed_command"
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
Comando: $failed_command
Código: $exit_code
Rollback: $ROLLBACK_STATUS
Commit sujo: não
Stderr:
${LAST_ERROR_STDERR:-nenhuma saída adicional capturada}
Últimas linhas:
${LAST_ERROR_LOGS:-nenhum log adicional encontrado}
Hora: $(date '+%d/%m/%Y %H:%M:%S')"
  send_error "Falha no auto update" "$body"
  exit "$exit_code"
}

trap 'cleanup_runtime_artifacts' EXIT
trap 'on_error' ERR

SECONDS=0
cd "$REPO_DIR"

STAGE="commit atual"
CURRENT_COMMIT="$(sudo -u ubuntu -H git rev-parse HEAD)"
PREVIOUS_COMMIT="$CURRENT_COMMIT"

STAGE="fetch remoto"
sudo -u ubuntu -H git fetch origin "$BRANCH"
REMOTE_COMMIT="$(sudo -u ubuntu -H git rev-parse "origin/$BRANCH")"
COMMIT_SUBJECT="$(sudo -u ubuntu -H git log -1 --pretty=%s "$REMOTE_COMMIT")"

if [[ -f "$DIRTY_MARKER_FILE" ]]; then
  MARKED_FAILED_COMMIT="$(marker_value FAILED_REMOTE_COMMIT)"
  if [[ -n "$MARKED_FAILED_COMMIT" && "$REMOTE_COMMIT" == "$MARKED_FAILED_COMMIT" ]]; then
    logger -t "$LOG_TAG" "Commit remoto $(short_commit "$REMOTE_COMMIT") continua marcado como sujo após rollback fatal; aguardando um novo commit no GitHub."
    exit 0
  fi
  clear_dirty_marker
fi

if [[ "$CURRENT_COMMIT" == "$REMOTE_COMMIT" ]]; then
  logger -t "$LOG_TAG" "Sem mudanças em $BRANCH"
  exit 0
fi

SHORT_FROM="$(short_commit "$CURRENT_COMMIT")"
SHORT_TO="$(short_commit "$REMOTE_COMMIT")"

CHANGED_FILES_RAW="$(sudo -u ubuntu -H git diff --name-only "$CURRENT_COMMIT" "$REMOTE_COMMIT" || true)"

if printf '%s\n' "$CHANGED_FILES_RAW" | grep -q '^activity /sinuca/'; then
  FRONT_CHANGED=1
fi
if printf '%s\n' "$CHANGED_FILES_RAW" | grep -q '^activity /sinuca-server/'; then
  BACK_CHANGED=1
fi
if printf '%s\n' "$CHANGED_FILES_RAW" | grep -vq '^activity /sinuca'; then
  BOT_CHANGED=1
fi
if printf '%s\n' "$CHANGED_FILES_RAW" | grep -Eq '^(callkeeper_service\.py|callkeeper_runtime/|deploy/systemd/callkeeper\.service|config\.py|db\.py|requirements\.txt)$'; then
  CALLKEEPER_CHANGED=1
fi

logger -t "$LOG_TAG" "Atualizando de $CURRENT_COMMIT para $REMOTE_COMMIT"

STAGE="git pull"
sudo -u ubuntu -H git pull --ff-only origin "$BRANCH"
UPDATE_APPLIED=1

FAILED_STAGE=""

deploy_bot
deploy_callkeeper
deploy_frontend
deploy_backend

DURATION="$(human_duration "$SECONDS")"
ROLLBACK_STATUS="não foi necessário"
CHANGED_FILES="$(format_changed_files)"

OVERALL_OK=1
[[ "$BOT_HEALTHCHECK_STATUS" == "OK" ]] || OVERALL_OK=0
if (( CALLKEEPER_CHANGED == 1 )) && [[ "$CALLKEEPER_STATUS" != "OK" ]]; then
  OVERALL_OK=0
fi
[[ "$ACTIVITY_HEALTHCHECK_STATUS" == "OK" ]] || OVERALL_OK=0

if (( OVERALL_OK == 1 )); then
  ALERT_TYPE="success"
  ALERT_TITLE="Update aplicado com sucesso"
  ALERT_SUMMARY="O update foi aplicado e os healthchecks passaram."
else
  ALERT_TYPE="warn"
  ALERT_TITLE="Update aplicado com alerta"
  ALERT_SUMMARY="O update foi aplicado, mas pelo menos um healthcheck falhou após as tentativas."
fi

BODY="Resumo: $ALERT_SUMMARY
Host: $HOSTNAME
Branch: $BRANCH
Commit: ${SHORT_FROM} → ${SHORT_TO}
Mudança: $COMMIT_SUBJECT
Arquivos:
$CHANGED_FILES
Bot: $BOT_HEALTHCHECK_STATUS
CallKeeper: $CALLKEEPER_STATUS
Frontend: $FRONT_STATUS
Backend: $BACK_STATUS
Activity: $ACTIVITY_HEALTHCHECK_STATUS
Rollback: $ROLLBACK_STATUS
Duração: $DURATION
Hora: $(date '+%d/%m/%Y %H:%M:%S')"

/home/ubuntu/bot/alert.sh "$ALERT_TYPE" "$ALERT_TITLE" "$BODY"
logger -t "$LOG_TAG" "$ALERT_TITLE"
