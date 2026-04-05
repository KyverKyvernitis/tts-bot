#!/usr/bin/env bash
set -Eeuo pipefail

REPO_DIR="/home/ubuntu/bot"
BRANCH="main"
SERVICE="tts-bot"
ACTIVITY_SERVICE="sinuca-activity-server"
LOG_TAG="tts-bot-updater"
STATE_MARKER_FILE="$REPO_DIR/.update_last_failed_remote_hash"

FRONT_DIR="$REPO_DIR/activity /sinuca"
BACK_DIR="$REPO_DIR/activity /sinuca-server"
FRONT_PUBLISH_DIR="/var/www/sinuca"
BACK_PORT="8787"
BACK_HEALTH_URL="http://127.0.0.1:${BACK_PORT}/health"
BOT_HEALTH_URL="http://127.0.0.1:10000/health"
HOSTNAME="$(hostname)"
FORCE_UPDATE="${FORCE_UPDATE:-0}"

CURRENT_COMMIT=""
REMOTE_COMMIT=""
SHORT_FROM="—"
SHORT_TO="—"
COMMIT_SUBJECT="—"
CHANGED_FILES_RAW=""
CHANGED_FILES_BLOCK="—"
DIRTY_DETAILS="—"
FAIL_REASON="Falha não especificada."

FRONT_CHANGED=0
BACK_CHANGED=0
BOT_CHANGED=0
UPDATE_STARTED=0
ROLLBACK_DONE=0

PRE_BOT_HEALTHCHECK_STATUS="não verificado"
PRE_ACTIVITY_HEALTHCHECK_STATUS="não verificado"
BOT_HEALTHCHECK_STATUS="não verificado"
ACTIVITY_HEALTHCHECK_STATUS="não verificado"
FRONT_STATUS="não alterado"
BACK_STATUS="não alterado"
ROLLBACK_STATUS="não executado"

send_info() {
  sudo -u ubuntu "$REPO_DIR/alert.sh" info "$1" "$2" || true
}

send_warn() {
  sudo -u ubuntu "$REPO_DIR/alert.sh" warn "$1" "$2" || true
}

send_error() {
  sudo -u ubuntu "$REPO_DIR/alert.sh" error "$1" "$2" || true
}

human_duration() {
  local total="${1:-0}"
  local h=$((total / 3600))
  local m=$(((total % 3600) / 60))
  local s=$((total % 60))

  if (( h > 0 )); then
    printf "%dh %02dm %02ds" "$h" "$m" "$s"
  elif (( m > 0 )); then
    printf "%dm %02ds" "$m" "$s"
  else
    printf "%ds" "$s"
  fi
}

run_as_ubuntu() {
  sudo -u ubuntu -H bash -lc "$1"
}

git_as_ubuntu() {
  sudo -u ubuntu -H git -C "$REPO_DIR" "$@"
}

systemd_service_exists() {
  local service_name="${1:?}"
  systemctl cat "$service_name" >/dev/null 2>&1
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

health_status() {
  local url="${1:?}"
  local attempts="${2:-2}"
  local delay="${3:-2}"

  if wait_for_health "$url" "$attempts" "$delay"; then
    printf 'OK'
  else
    printf 'falhou'
  fi
}

compact_multiline() {
  local value="${1:-}"
  if [ -z "${value//[[:space:]]/}" ]; then
    printf '—'
  else
    printf '%s' "$value"
  fi
}

status_block() {
  local title="$1"
  local content="$2"
  printf '%s\n%s' "$title" "$(compact_multiline "$content")"
}

read_last_marker() {
  if [ -f "$STATE_MARKER_FILE" ]; then
    cat "$STATE_MARKER_FILE" 2>/dev/null || true
  fi
}

write_last_marker() {
  printf '%s' "$1" > "$STATE_MARKER_FILE"
}

clear_last_marker() {
  rm -f "$STATE_MARKER_FILE"
}

repo_dirty_output() {
  git_as_ubuntu status --porcelain --untracked-files=all || true
}

ensure_frontend_built_and_published() {
  [ -d "$FRONT_DIR" ] || return 1

  run_as_ubuntu "cd \"$FRONT_DIR\" && { [ -d node_modules ] || npm install; }"
  run_as_ubuntu "cd \"$FRONT_DIR\" && npm run build"

  mkdir -p "$FRONT_PUBLISH_DIR"
  rm -rf "${FRONT_PUBLISH_DIR:?}/"*
  cp -r "$FRONT_DIR/dist/." "$FRONT_PUBLISH_DIR/"
}

restart_activity_backend() {
  [ -d "$BACK_DIR" ] || return 1

  run_as_ubuntu "cd \"$BACK_DIR\" && { [ -d node_modules ] || npm install; }"
  run_as_ubuntu "cd \"$BACK_DIR\" && npm run build"

  if systemd_service_exists "$ACTIVITY_SERVICE"; then
    systemctl restart "$ACTIVITY_SERVICE"
  else
    fuser -k "${BACK_PORT}/tcp" >/dev/null 2>&1 || true
    run_as_ubuntu "cd \"$BACK_DIR\"; set -a; [ -f \"$REPO_DIR/.env\" ] && . \"$REPO_DIR/.env\" || true; [ -f ./.env ] && . ./.env || true; set +a; nohup node dist/index.js >> sinuca-server.log 2>&1 &"
  fi
}

restore_previous_state() {
  local rollback_reason="${1:-Falha durante a aplicação do update.}"
  local repo_status="falhou"
  local bot_restore="não necessário"
  local front_restore="não necessário"
  local back_restore="não necessário"
  local bot_health_restore="não verificado"
  local activity_health_restore="não verificado"

  ROLLBACK_DONE=1
  trap - ERR
  set +e

  if [ -n "$CURRENT_COMMIT" ]; then
    if git_as_ubuntu reset --hard "$CURRENT_COMMIT" >/dev/null 2>&1; then
      repo_status="ok (${CURRENT_COMMIT:0:7})"
    fi
  fi

  if [ "$repo_status" != "falhou" ]; then
    if [[ -x "$REPO_DIR/.venv/bin/pip" && -f "$REPO_DIR/requirements.txt" ]]; then
      sudo -u ubuntu -H "$REPO_DIR/.venv/bin/pip" install -r "$REPO_DIR/requirements.txt" >/dev/null 2>&1 || true
    fi

    if (( BOT_CHANGED == 1 )); then
      if systemctl restart "$SERVICE" >/dev/null 2>&1; then
        bot_restore="serviço reiniciado"
      else
        bot_restore="falhou ao reiniciar"
      fi
    fi

    if (( FRONT_CHANGED == 1 )); then
      if ensure_frontend_built_and_published >/dev/null 2>&1; then
        front_restore="frontend republicado"
      else
        front_restore="falhou ao republicar"
      fi
    fi

    if (( BACK_CHANGED == 1 )); then
      if restart_activity_backend >/dev/null 2>&1; then
        back_restore="backend reiniciado"
      else
        back_restore="falhou ao reiniciar"
      fi
    fi
  fi

  if (( BOT_CHANGED == 1 )); then
    bot_health_restore="$(health_status "$BOT_HEALTH_URL" 12 5)"
  else
    bot_health_restore="$BOT_HEALTHCHECK_STATUS"
  fi

  if (( BACK_CHANGED == 1 )); then
    activity_health_restore="$(health_status "$BACK_HEALTH_URL" 18 5)"
  else
    activity_health_restore="$ACTIVITY_HEALTHCHECK_STATUS"
  fi

  ROLLBACK_STATUS="$(cat <<EOM
repo
${repo_status}

bot
${bot_restore}

health bot
${bot_health_restore}

frontend
${front_restore}

backend
${back_restore}

health activity
${activity_health_restore}
EOM
)"

  write_last_marker "failed:${REMOTE_COMMIT:-unknown}"

  local rollback_bot_block
  local rollback_activity_block
  local rollback_body

  rollback_bot_block="$(cat <<EOM
health antes
$PRE_BOT_HEALTHCHECK_STATUS

health depois
$BOT_HEALTHCHECK_STATUS
EOM
)"

  rollback_activity_block="$(cat <<EOM
health antes
$PRE_ACTIVITY_HEALTHCHECK_STATUS

health depois
$ACTIVITY_HEALTHCHECK_STATUS
EOM
)"

  rollback_body="$(cat <<EOM
Resumo: O update falhou e o rollback automático foi executado para voltar ao commit anterior.
Resultado: rollback executado
Host: $HOSTNAME
Branch: $BRANCH
Commit: ${SHORT_TO} → ${SHORT_FROM}
Repositório: limpo
Mudança: $COMMIT_SUBJECT
Arquivos: $(compact_multiline "$CHANGED_FILES_BLOCK")
Motivo: $rollback_reason
Bot: $(compact_multiline "$rollback_bot_block")
Activity: $(compact_multiline "$rollback_activity_block")
Rollback: $(compact_multiline "$ROLLBACK_STATUS")
Duração: $(human_duration "$SECONDS")
Hora: $(date '+%d/%m/%Y %H:%M:%S')
EOM
)"

  send_error "Update revertido automaticamente" "$rollback_body"
  logger -t "$LOG_TAG" "Rollback executado para ${REMOTE_COMMIT:-desconhecido}: $rollback_reason"
}

on_error() {
  local exit_code="$?"
  local line_no="${BASH_LINENO[0]:-?}"

  if (( ROLLBACK_DONE == 0 )) && (( UPDATE_STARTED == 1 )); then
    restore_previous_state "$FAIL_REASON (linha ${line_no})"
  else
    local body
    body="$(cat <<EOM
Resumo: O updater falhou antes de concluir o processo.
Resultado: falha
Host: $HOSTNAME
Branch: $BRANCH
Commit: ${SHORT_FROM} → ${SHORT_TO}
Repositório: limpo
Motivo: ${FAIL_REASON} (linha ${line_no})
Duração: $(human_duration "$SECONDS")
Hora: $(date '+%d/%m/%Y %H:%M:%S')
EOM
)"
    send_error "Falha no auto update" "$body"
    logger -t "$LOG_TAG" "Falha sem rollback: ${FAIL_REASON} (linha ${line_no})"
  fi

  exit "$exit_code"
}

trap 'on_error' ERR

SECONDS=0
cd "$REPO_DIR"

CURRENT_COMMIT="$(git_as_ubuntu rev-parse HEAD)"
SHORT_FROM="${CURRENT_COMMIT:0:7}"
PRE_BOT_HEALTHCHECK_STATUS="$(health_status "$BOT_HEALTH_URL" 2 2)"
PRE_ACTIVITY_HEALTHCHECK_STATUS="$(health_status "$BACK_HEALTH_URL" 2 2)"

git_as_ubuntu fetch origin "$BRANCH"
REMOTE_COMMIT="$(git_as_ubuntu rev-parse "origin/$BRANCH")"
SHORT_TO="${REMOTE_COMMIT:0:7}"

if [[ "$CURRENT_COMMIT" == "$REMOTE_COMMIT" ]]; then
  clear_last_marker
  logger -t "$LOG_TAG" "Sem mudanças em $BRANCH"
  exit 0
fi

LAST_MARKER="$(read_last_marker)"
if [[ "$LAST_MARKER" == "failed:$REMOTE_COMMIT" ]] && [[ "$FORCE_UPDATE" != "1" ]]; then
  logger -t "$LOG_TAG" "Commit $SHORT_TO já falhou anteriormente; aguardando novo commit remoto (use FORCE_UPDATE=1 para reprocessar)"
  exit 0
fi

COMMIT_SUBJECT="$(git_as_ubuntu log -1 --pretty=%s "$REMOTE_COMMIT")"
DIRTY_RAW="$(repo_dirty_output)"
if [[ -n "$DIRTY_RAW" ]]; then
  DIRTY_DETAILS="$(printf '%s\n' "$DIRTY_RAW" | head -n 20 | sed 's/^/- /')"
  DIRTY_MARKER="dirty:$REMOTE_COMMIT"

  if [[ "$LAST_MARKER" != "$DIRTY_MARKER" ]]; then
    BODY="$(cat <<EOM
Resumo: Há update novo no remoto, mas o auto update foi pausado para não sobrescrever alterações locais.
Resultado: pausado
Host: $HOSTNAME
Branch: $BRANCH
Commit: ${SHORT_FROM} → ${SHORT_TO}
Repositório: sujo
Mudança: $COMMIT_SUBJECT
Arquivos locais: $(compact_multiline "$DIRTY_DETAILS")
Hora: $(date '+%d/%m/%Y %H:%M:%S')
EOM
)"
    send_warn "Auto update pausado por repositório sujo" "$BODY"
    write_last_marker "$DIRTY_MARKER"
  fi

  logger -t "$LOG_TAG" "Update pausado: repositório sujo em $BRANCH"
  exit 0
fi

CHANGED_FILES_RAW="$(git_as_ubuntu diff --name-only "$CURRENT_COMMIT" "$REMOTE_COMMIT" || true)"
if [[ -n "$CHANGED_FILES_RAW" ]]; then
  CHANGED_FILES_BLOCK="$(printf '%s\n' "$CHANGED_FILES_RAW" | head -n 20 | sed 's/^/- /')"
else
  CHANGED_FILES_BLOCK="- nenhum arquivo listado"
fi

if printf '%s\n' "$CHANGED_FILES_RAW" | grep -Eq '^activity /sinuca/'; then
  FRONT_CHANGED=1
fi
if printf '%s\n' "$CHANGED_FILES_RAW" | grep -Eq '^activity /sinuca-server/'; then
  BACK_CHANGED=1
fi
if printf '%s\n' "$CHANGED_FILES_RAW" | grep -Evq '^(activity /sinuca/|activity /sinuca-server/|$)'; then
  BOT_CHANGED=1
fi

logger -t "$LOG_TAG" "Atualizando de $CURRENT_COMMIT para $REMOTE_COMMIT"
UPDATE_STARTED=1

FAIL_REASON="Falha ao aplicar git pull"
git_as_ubuntu pull --ff-only origin "$BRANCH"

if [[ -x "$REPO_DIR/.venv/bin/pip" && -f "$REPO_DIR/requirements.txt" ]]; then
  FAIL_REASON="Falha ao instalar dependências Python"
  sudo -u ubuntu -H "$REPO_DIR/.venv/bin/pip" install -r "$REPO_DIR/requirements.txt"
fi

if (( BOT_CHANGED == 1 )); then
  FAIL_REASON="Falha ao reiniciar o bot principal"
  systemctl restart "$SERVICE"
  sleep 3
fi
BOT_HEALTHCHECK_STATUS="$(health_status "$BOT_HEALTH_URL" 12 5)"

if (( FRONT_CHANGED == 1 )); then
  FAIL_REASON="Falha ao publicar o frontend da Activity"
  ensure_frontend_built_and_published
  FRONT_STATUS="frontend publicado em $FRONT_PUBLISH_DIR"
fi

if (( BACK_CHANGED == 1 )); then
  FAIL_REASON="Falha ao reiniciar o backend da Activity"
  restart_activity_backend
  BACK_STATUS="backend reiniciado na porta $BACK_PORT"
fi
ACTIVITY_HEALTHCHECK_STATUS="$(health_status "$BACK_HEALTH_URL" 18 5)"

if (( BOT_CHANGED == 1 )) && [[ "$BOT_HEALTHCHECK_STATUS" != "OK" ]]; then
  FAIL_REASON="O /health do bot falhou após aplicar o update"
  false
fi

if (( BACK_CHANGED == 1 )) && [[ "$ACTIVITY_HEALTHCHECK_STATUS" != "OK" ]]; then
  FAIL_REASON="O /health da Activity falhou após reiniciar o backend"
  false
fi

if (( FRONT_CHANGED == 0 )); then
  FRONT_STATUS="sem mudanças"
fi
if (( BACK_CHANGED == 0 )); then
  BACK_STATUS="sem mudanças"
fi

clear_last_marker

BOT_BLOCK="$(cat <<EOM
$(if (( BOT_CHANGED == 1 )); then printf 'serviço\nreiniciado'; else printf 'sem mudanças'; fi)

health antes
$PRE_BOT_HEALTHCHECK_STATUS

health agora
$BOT_HEALTHCHECK_STATUS
EOM
)"

ACTIVITY_BLOCK="$(cat <<EOM
frontend
$FRONT_STATUS

backend
$BACK_STATUS

health antes
$PRE_ACTIVITY_HEALTHCHECK_STATUS

health agora
$ACTIVITY_HEALTHCHECK_STATUS
EOM
)"

OVERALL_OK=1
if (( BOT_CHANGED == 1 )) && [[ "$BOT_HEALTHCHECK_STATUS" != "OK" ]]; then
  OVERALL_OK=0
fi
if (( BACK_CHANGED == 1 )) && [[ "$ACTIVITY_HEALTHCHECK_STATUS" != "OK" ]]; then
  OVERALL_OK=0
fi

if (( OVERALL_OK == 1 )); then
  if [[ "$PRE_BOT_HEALTHCHECK_STATUS" == "falhou" && "$BOT_HEALTHCHECK_STATUS" == "falhou" ]] || \
     [[ "$PRE_ACTIVITY_HEALTHCHECK_STATUS" == "falhou" && "$ACTIVITY_HEALTHCHECK_STATUS" == "falhou" ]]; then
    ALERT_TYPE="warn"
    ALERT_TITLE="Update aplicado com alerta"
    ALERT_SUMMARY="O update entrou, mas já existia componente com healthcheck ruim antes e ele continuou ruim depois."
    RESULT_TEXT="aplicado com alerta"
  else
    ALERT_TYPE="success"
    ALERT_TITLE="Update aplicado com sucesso"
    ALERT_SUMMARY="O update foi aplicado e as partes alteradas ficaram saudáveis após a validação."
    RESULT_TEXT="sucesso"
  fi
else
  ALERT_TYPE="warn"
  ALERT_TITLE="Update aplicado com alerta"
  ALERT_SUMMARY="O update entrou, mas pelo menos uma validação pós-update ficou em alerta."
  RESULT_TEXT="aplicado com alerta"
fi

BODY="$(cat <<EOM
Resumo: $ALERT_SUMMARY
Resultado: $RESULT_TEXT
Host: $HOSTNAME
Branch: $BRANCH
Commit: ${SHORT_FROM} → ${SHORT_TO}
Repositório: limpo
Mudança: $COMMIT_SUBJECT
Arquivos: $(compact_multiline "$CHANGED_FILES_BLOCK")
Bot: $(compact_multiline "$BOT_BLOCK")
Activity: $(compact_multiline "$ACTIVITY_BLOCK")
Rollback: $ROLLBACK_STATUS
Duração: $(human_duration "$SECONDS")
Hora: $(date '+%d/%m/%Y %H:%M:%S')
EOM
)"
"$REPO_DIR/alert.sh" "$ALERT_TYPE" "$ALERT_TITLE" "$BODY"
logger -t "$LOG_TAG" "$ALERT_TITLE"
