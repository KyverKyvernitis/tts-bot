#!/usr/bin/env bash
set -Eeuo pipefail

REPO_DIR="/home/ubuntu/bot"
BRANCH="main"
SERVICE="tts-bot"
CALLKEEPER_SERVICE="callkeeper"
LAVALINK_SERVICE="lavalink"
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
MANUAL_FAILURE_ALERT_SENT=0

FRONT_CHANGED=0
BACK_CHANGED=0
BOT_CHANGED=0
CALLKEEPER_CHANGED=0
REQUIREMENTS_CHANGED=0
AUDIO_SYSTEMD_CHANGED=0
CLEANUP_CHANGED=0
PHONE_LAVALINK_WATCH_CHANGED=0
PHONE_WORKER_WATCH_CHANGED=0

BOT_HEALTHCHECK_STATUS="não verificado"
CALLKEEPER_STATUS="não alterado"
AUDIO_SERVICES_STATUS="não alterado"
CLEANUP_STATUS="não alterada"
PHONE_LAVALINK_WATCH_STATUS="não alterado"
PHONE_WORKER_WATCH_STATUS="não alterado"
FRONT_STATUS="não alterado"
BACK_STATUS="não alterado"
ACTIVITY_HEALTHCHECK_STATUS="não verificado"
ROLLBACK_STATUS="não foi necessário"
CHANGED_FILES_RAW=""
UPDATER_UNIT="tts-bot-updater.service"
RUN_LOG_FILE="${TMPDIR:-/tmp}/tts-bot-updater.$$.log"
: > "$RUN_LOG_FILE"
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
  local title="${1:-Falha no auto update}"
  local body="${2:-}"
  local attach=""
  if [[ -f "$RUN_LOG_FILE" && -s "$RUN_LOG_FILE" ]]; then
    attach="$RUN_LOG_FILE"
  fi
  sudo -u ubuntu /home/ubuntu/bot/alert.sh error "$title" "$body" "$attach" "tts-bot-updater.log" || true
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


service_restart_count() {
  local unit="${1:?}"
  local value
  value="$(systemctl show "$unit" -p NRestarts --value 2>/dev/null || echo 0)"
  if [[ "$value" =~ ^[0-9]+$ ]]; then
    printf '%s' "$value"
  else
    printf '0'
  fi
}

journal_since_epoch() {
  local unit="${1:?}"
  local since_epoch="${2:?}"
  journalctl -u "$unit" --since "@${since_epoch}" --no-pager 2>/dev/null || true
}

fatal_boot_log_patterns() {
  cat <<'EOF'
SyntaxError|IndentationError|TabError|ImportError|ModuleNotFoundError|No module named|cannot import name|ExtensionFailed|ExtensionNotFound|Failed to load extension|discord\.ext\.commands\.errors\.ExtensionFailed|discord\.ext\.commands\.errors\.ExtensionNotFound|RuntimeError:.*(boot|startup|setup|load|cog)|AttributeError:.*(setup|load_extension|cog)|Start request repeated too quickly|Failed with result|Main process exited.*status=1
EOF
}

has_fatal_boot_logs() {
  local unit="${1:?}"
  local since_epoch="${2:?}"
  local logs patterns
  logs="$(journal_since_epoch "$unit" "$since_epoch")"
  [[ -n "${logs//[[:space:]]/}" ]] || return 1
  patterns="$(fatal_boot_log_patterns)"
  printf '%s\n' "$logs" | grep -Eiq "$patterns"
}

run_preflight_checks() {
  local py="$REPO_DIR/.venv/bin/python"
  local file checked_py=0 checked_sh=0
  [[ -x "$py" ]] || py="$(command -v python3 || true)"

  if [[ -n "$py" ]]; then
    STAGE="preflight Python"
    while IFS= read -r file; do
      [[ -n "$file" ]] || continue
      [[ -f "$REPO_DIR/$file" ]] || continue
      checked_py=1
      sudo -u ubuntu -H "$py" -m py_compile "$REPO_DIR/$file"
    done < <(printf '%s\n' "$CHANGED_FILES_RAW" | grep -E '\.py$' | grep -v '^activity ' || true)
  fi

  STAGE="preflight Bash"
  while IFS= read -r file; do
    [[ -n "$file" ]] || continue
    [[ -f "$REPO_DIR/$file" ]] || continue
    checked_sh=1
    bash -n "$REPO_DIR/$file"
  done < <(printf '%s\n' "$CHANGED_FILES_RAW" | grep -E '\.sh$' || true)

  if (( checked_py == 1 || checked_sh == 1 )); then
    logger -t "$LOG_TAG" "Preflight OK: Python=$checked_py Bash=$checked_sh"
  else
    logger -t "$LOG_TAG" "Preflight sem arquivos Python/Bash alterados"
  fi
}

verify_bot_after_restart() {
  local restart_epoch="${1:?}"
  local restarts_before="${2:-0}"
  local checkpoints=(8 20 35)
  local waited=0 checkpoint sleep_for restarts_after health_ok=0

  for checkpoint in "${checkpoints[@]}"; do
    sleep_for=$((checkpoint - waited))
    if (( sleep_for > 0 )); then
      sleep "$sleep_for"
      waited="$checkpoint"
    fi

    if systemctl is-failed --quiet "$SERVICE"; then
      BOT_HEALTHCHECK_STATUS="falhou: serviço em failed"
      return 1
    fi

    if ! systemctl is-active --quiet "$SERVICE"; then
      BOT_HEALTHCHECK_STATUS="falhou: serviço não ficou active"
      return 1
    fi

    restarts_after="$(service_restart_count "$SERVICE")"
    if [[ "$restarts_after" =~ ^[0-9]+$ && "$restarts_before" =~ ^[0-9]+$ ]]; then
      if (( restarts_after > restarts_before + 1 )); then
        BOT_HEALTHCHECK_STATUS="falhou: restart loop detectado (${restarts_before}→${restarts_after})"
        return 1
      fi
    fi

    if has_fatal_boot_logs "$SERVICE" "$restart_epoch"; then
      BOT_HEALTHCHECK_STATUS="falhou: erro fatal de boot nas logs"
      return 1
    fi

    if curl -fsS --max-time 3 "$BOT_HEALTH_URL" >/dev/null 2>&1; then
      health_ok=1
    fi
  done

  if (( health_ok == 1 )); then
    BOT_HEALTHCHECK_STATUS="OK"
  else
    BOT_HEALTHCHECK_STATUS="ativo; health HTTP sem resposta"
  fi
  return 0
}

env_truthy() {
  local key="${1:?}"
  local value=""
  if [[ -f "$REPO_DIR/.env" ]]; then
    value="$(grep -E "^${key}=" "$REPO_DIR/.env" 2>/dev/null | tail -n 1 | cut -d= -f2- | tr -d ' "' || true)"
  fi
  value="${value,,}"
  [[ "$value" == "1" || "$value" == "true" || "$value" == "yes" || "$value" == "y" || "$value" == "on" || "$value" == "sim" ]]
}

wait_for_lavalink_ready() {
  if [[ -x "$REPO_DIR/scripts/wait-audio-node-ready.py" ]]; then
    sudo -u ubuntu -H "$REPO_DIR/scripts/wait-audio-node-ready.py" --timeout "${AUDIO_NODE_STARTUP_WAIT_SECONDS:-20}"
    return $?
  fi
  return 0
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

collect_local_tracked_changes() {
  # Untracked locais como data/, cookies e healthcheck não bloqueiam o merge.
  # O que bloqueia o git pull são mudanças em arquivos rastreados.
  sudo -u ubuntu -H git status --short --untracked-files=no 2>/dev/null | trim_alert_text 1800
}

collect_local_tracked_files() {
  {
    sudo -u ubuntu -H git diff --name-only 2>/dev/null || true
    sudo -u ubuntu -H git diff --name-only --cached 2>/dev/null || true
  } | awk 'NF && !seen[$0]++' | head -n 40 | sed 's/^/• /' | trim_alert_text 1500
}

fail_local_changes_before_pull() {
  local status_text files_text duration body
  status_text="$(collect_local_tracked_changes)"
  if [[ -z "${status_text//[[:space:]]/}" ]]; then
    return 0
  fi

  MANUAL_FAILURE_ALERT_SENT=1
  files_text="$(collect_local_tracked_files)"
  duration="$(human_duration "$SECONDS")"

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
Commit sujo: não
Diagnóstico: existem mudanças locais que seriam sobrescritas pelo merge.
Arquivos locais:
${files_text:-nenhum arquivo listado}
Status git:
$status_text
Ação sugerida: Rode git stash push -u -m \"backup-local-before-auto-update-$(date +%F-%H%M%S)\" antes de liberar o updater, ou commit/reverta manualmente as alterações locais.
Duração: $duration
Hora: $(date '+%d/%m/%Y %H:%M:%S')"

  send_error "Falha no auto update: alterações locais" "$body"
  exit 1
}


deploy_audio_services() {
  if (( AUDIO_SYSTEMD_CHANGED == 0 )); then
    AUDIO_SERVICES_STATUS="não alterado"
    return 0
  fi

  STAGE="configuração dos serviços de áudio"
  local installed=0 lavalink_unit_changed=0

  if printf '%s\n' "$CHANGED_FILES_RAW" | grep -q '^deploy/systemd/lavalink\.service$'; then
    lavalink_unit_changed=1
    if [[ -f "$REPO_DIR/deploy/systemd/lavalink.service" ]]; then
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
    if env_truthy LAVALINK_ENABLED; then
      systemctl enable "$LAVALINK_SERVICE" >/dev/null 2>&1 || true
      systemctl restart "$LAVALINK_SERVICE" || true
      if systemctl is-active --quiet "$LAVALINK_SERVICE"; then
        AUDIO_SERVICES_STATUS="Lavalink ativo"
      else
        AUDIO_SERVICES_STATUS="Lavalink configurado, mas não ficou ativo"
      fi
    else
      AUDIO_SERVICES_STATUS="Lavalink não iniciado porque LAVALINK_ENABLED=false"
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

  if [[ -f "$REPO_DIR/deploy/systemd/cleanup-audio-temp.service" ]]; then
    cp "$REPO_DIR/deploy/systemd/cleanup-audio-temp.service" /etc/systemd/system/cleanup-audio-temp.service
    installed=1
  fi
  if [[ -f "$REPO_DIR/deploy/systemd/cleanup-audio-temp.timer" ]]; then
    cp "$REPO_DIR/deploy/systemd/cleanup-audio-temp.timer" /etc/systemd/system/cleanup-audio-temp.timer
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
  if (( PHONE_LAVALINK_WATCH_CHANGED == 0 )); then
    PHONE_LAVALINK_WATCH_STATUS="não alterado"
    return 0
  fi

  STAGE="configuração do watcher do Lavalink auxiliar"
  local installed=0

  if [[ -f "$REPO_DIR/deploy/systemd/phone-lavalink-watch.service" ]]; then
    cp "$REPO_DIR/deploy/systemd/phone-lavalink-watch.service" /etc/systemd/system/phone-lavalink-watch.service
    installed=1
  fi
  if [[ -f "$REPO_DIR/deploy/systemd/phone-lavalink-watch.timer" ]]; then
    cp "$REPO_DIR/deploy/systemd/phone-lavalink-watch.timer" /etc/systemd/system/phone-lavalink-watch.timer
    installed=1
  fi

  if (( installed == 0 )); then
    PHONE_LAVALINK_WATCH_STATUS="units não encontradas"
    return 0
  fi

  systemctl daemon-reload

  local watch_value=""
  if [[ -f "$REPO_DIR/.env" ]]; then
    watch_value="$(grep -E '^PHONE_LAVALINK_WATCH_ENABLED=' "$REPO_DIR/.env" 2>/dev/null | tail -n 1 | cut -d= -f2- | tr -d ' "' || true)"
    watch_value="${watch_value,,}"
  fi

  if [[ "$watch_value" == "0" || "$watch_value" == "false" || "$watch_value" == "no" || "$watch_value" == "off" || "$watch_value" == "nao" || "$watch_value" == "não" ]]; then
    systemctl disable --now phone-lavalink-watch.timer >/dev/null 2>&1 || true
    PHONE_LAVALINK_WATCH_STATUS="timer desativado por PHONE_LAVALINK_WATCH_ENABLED=false"
    return 0
  fi

  if env_truthy AUX_LAVALINK_ENABLED; then
    systemctl enable --now phone-lavalink-watch.timer >/dev/null 2>&1 || true
    systemctl start phone-lavalink-watch.service >/dev/null 2>&1 || true
    if systemctl is-active --quiet phone-lavalink-watch.timer; then
      PHONE_LAVALINK_WATCH_STATUS="timer ativo"
    else
      PHONE_LAVALINK_WATCH_STATUS="timer instalado, mas não ativo"
    fi
  else
    systemctl disable --now phone-lavalink-watch.timer >/dev/null 2>&1 || true
    PHONE_LAVALINK_WATCH_STATUS="instalado; inativo porque AUX_LAVALINK_ENABLED=false"
  fi
}


deploy_phone_worker_watch() {
  if (( PHONE_WORKER_WATCH_CHANGED == 0 )); then
    PHONE_WORKER_WATCH_STATUS="não alterado"
    return 0
  fi

  STAGE="configuração do watcher do phone worker"
  local installed=0

  if [[ -f "$REPO_DIR/deploy/systemd/phone-worker-watch.service" ]]; then
    cp "$REPO_DIR/deploy/systemd/phone-worker-watch.service" /etc/systemd/system/phone-worker-watch.service
    installed=1
  fi
  if [[ -f "$REPO_DIR/deploy/systemd/phone-worker-watch.timer" ]]; then
    cp "$REPO_DIR/deploy/systemd/phone-worker-watch.timer" /etc/systemd/system/phone-worker-watch.timer
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

  if [[ "$worker_value" == "1" || "$worker_value" == "true" || "$worker_value" == "yes" || "$worker_value" == "on" || "$worker_value" == "sim" ]]; then
    systemctl enable --now phone-worker-watch.timer >/dev/null 2>&1 || true
    systemctl start phone-worker-watch.service >/dev/null 2>&1 || true
    if systemctl is-active --quiet phone-worker-watch.timer; then
      PHONE_WORKER_WATCH_STATUS="timer ativo"
    else
      PHONE_WORKER_WATCH_STATUS="timer instalado, mas não ativo"
    fi
  else
    systemctl disable --now phone-worker-watch.timer >/dev/null 2>&1 || true
    PHONE_WORKER_WATCH_STATUS="instalado; inativo porque PHONE_WORKER_ENABLED não está true"
  fi
}


deploy_bot() {
  deploy_audio_services
  deploy_cleanup_timer
  deploy_phone_lavalink_watch
  deploy_phone_worker_watch

  if (( REQUIREMENTS_CHANGED == 1 )); then
    STAGE="dependências do bot"
    if [[ -x "$REPO_DIR/.venv/bin/pip" && -f "$REPO_DIR/requirements.txt" ]]; then
      sudo -u ubuntu -H "$REPO_DIR/.venv/bin/pip" install -r "$REPO_DIR/requirements.txt"
    fi
  fi

  if (( BOT_CHANGED == 1 )); then
    local restart_epoch restarts_before
    restarts_before="$(service_restart_count "$SERVICE")"

    STAGE="reinício do bot"
    restart_epoch="$(date +%s)"
    systemctl restart "$SERVICE"

    if env_truthy LAVALINK_ENABLED; then
      STAGE="espera curta do Lavalink"
      wait_for_lavalink_ready || true
    fi

    STAGE="validação fatal do bot"
    verify_bot_after_restart "$restart_epoch" "$restarts_before"
    return $?
  fi

  STAGE="healthcheck do bot"
  if wait_for_health "$BOT_HEALTH_URL" 2 2; then
    BOT_HEALTHCHECK_STATUS="OK"
  else
    BOT_HEALTHCHECK_STATUS="não alterado; health HTTP sem resposta"
  fi
  return 0
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

  STAGE="dependências do frontend"
  run_as_ubuntu "cd \"$FRONT_DIR\" && if [ -f package-lock.json ]; then npm ci; else npm install; fi"

  STAGE="build do frontend"
  run_as_ubuntu "cd \"$FRONT_DIR\" && npm run build"

  STAGE="publicação do frontend"
  mkdir -p "$FRONT_PUBLISH_DIR"
  rm -rf "${FRONT_PUBLISH_DIR:?}/"*
  cp -r "$FRONT_DIR/dist/." "$FRONT_PUBLISH_DIR/"

  STAGE="limpeza do frontend"
  run_as_ubuntu "cd \"$FRONT_DIR\" && rm -rf node_modules && npm cache clean --force >/dev/null 2>&1 || true"

  FRONT_STATUS="frontend publicado em $FRONT_PUBLISH_DIR; node_modules removido após build"
  return 0
}

deploy_backend() {
  if (( BACK_CHANGED == 0 && FRONT_CHANGED == 0 )); then
    BACK_STATUS="não alterado"
    ACTIVITY_HEALTHCHECK_STATUS="não alterada"
    return 0
  fi

  if (( BACK_CHANGED == 0 )); then
    BACK_STATUS="não alterado"
    STAGE="healthcheck informativo da activity"
    if wait_for_health "$BACK_HEALTH_URL" 3 2; then
      ACTIVITY_HEALTHCHECK_STATUS="OK"
    else
      ACTIVITY_HEALTHCHECK_STATUS="indisponível; backend não foi alterado"
    fi
    return 0
  fi

  if [[ ! -d "$BACK_DIR" ]]; then
    BACK_STATUS="backend não encontrado em $BACK_DIR"
    return 1
  fi

  STAGE="dependências do backend"
  run_as_ubuntu "cd \"$BACK_DIR\" && if [ -f package-lock.json ]; then npm ci; else npm install; fi"

  STAGE="build do backend"
  run_as_ubuntu "cd \"$BACK_DIR\" && npm run build"

  STAGE="limpeza do backend"
  run_as_ubuntu "cd \"$BACK_DIR\" && npm prune --omit=dev && npm cache clean --force >/dev/null 2>&1 || true"

  STAGE="reinício do backend"
  fuser -k "${BACK_PORT}/tcp" >/dev/null 2>&1 || true
  run_as_ubuntu "cd \"$BACK_DIR\"; set -a; [ -f \"$REPO_DIR/.env\" ] && . \"$REPO_DIR/.env\" || true; [ -f .env ] && . ./.env || true; set +a; nohup node dist/index.js >> sinuca-server.log 2>&1 &"
  sleep 3
  BACK_STATUS="backend reiniciado na porta $BACK_PORT"

  STAGE="healthcheck da activity"
  if wait_for_health "$BACK_HEALTH_URL" 8 3; then
    ACTIVITY_HEALTHCHECK_STATUS="OK"
    BACK_STATUS="backend publicado e validado em $BACK_HEALTH_URL"
    return 0
  fi

  ACTIVITY_HEALTHCHECK_STATUS="falhou"
  BACK_STATUS="backend reiniciado, mas healthcheck falhou em $BACK_HEALTH_URL"
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
  if (( MANUAL_FAILURE_ALERT_SENT == 1 )); then
    exit "$exit_code"
  fi
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
if printf '%s\n' "$CHANGED_FILES_RAW" | grep -Eq '^requirements\.txt$'; then
  REQUIREMENTS_CHANGED=1
fi
if printf '%s\n' "$CHANGED_FILES_RAW" | grep -Eq '^deploy/systemd/(lavalink|tts-bot)\.service$'; then
  AUDIO_SYSTEMD_CHANGED=1
fi
if printf '%s\n' "$CHANGED_FILES_RAW" | grep -Eq '^(cleanup-audio-temp\.sh|deploy/systemd/cleanup-audio-temp\.(service|timer))$'; then
  CLEANUP_CHANGED=1
fi
if printf '%s\n' "$CHANGED_FILES_RAW" | grep -Eq '^(scripts/phone-lavalink-watch\.sh|deploy/systemd/phone-lavalink-watch\.(service|timer)|deploy/termux/phone-lavalink/)'; then
  PHONE_LAVALINK_WATCH_CHANGED=1
fi
if printf '%s\n' "$CHANGED_FILES_RAW" | grep -Eq '^(scripts/phone-worker-watch\.sh|scripts/phone-worker-client\.py|deploy/systemd/phone-worker-watch\.(service|timer)|deploy/termux/phone-worker/)'; then
  PHONE_WORKER_WATCH_CHANGED=1
fi
if printf '%s\n' "$CHANGED_FILES_RAW" | grep -Eq '^(callkeeper_service\.py|callkeeper_runtime/|deploy/systemd/callkeeper\.service|config\.py|db\.py|requirements\.txt)$'; then
  CALLKEEPER_CHANGED=1
fi

STAGE="verificação de alterações locais"
fail_local_changes_before_pull

logger -t "$LOG_TAG" "Atualizando de $CURRENT_COMMIT para $REMOTE_COMMIT"

STAGE="git pull"
sudo -u ubuntu -H git pull --ff-only origin "$BRANCH"
UPDATE_APPLIED=1

FAILED_STAGE=""

run_preflight_checks

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
if (( FRONT_CHANGED == 1 || BACK_CHANGED == 1 )); then
  [[ "$ACTIVITY_HEALTHCHECK_STATUS" == "OK" ]] || OVERALL_OK=0
fi

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
Serviços de áudio: $AUDIO_SERVICES_STATUS
Limpeza de áudio: $CLEANUP_STATUS
Watcher Lavalink celular: $PHONE_LAVALINK_WATCH_STATUS
Phone worker: $PHONE_WORKER_WATCH_STATUS
CallKeeper: $CALLKEEPER_STATUS
Frontend: $FRONT_STATUS
Backend: $BACK_STATUS
Activity: $ACTIVITY_HEALTHCHECK_STATUS
Rollback: $ROLLBACK_STATUS
Duração: $DURATION
Hora: $(date '+%d/%m/%Y %H:%M:%S')"

/home/ubuntu/bot/alert.sh "$ALERT_TYPE" "$ALERT_TITLE" "$BODY"
logger -t "$LOG_TAG" "$ALERT_TITLE"
