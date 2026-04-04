#!/usr/bin/env bash
set -u

export HOME="/home/ubuntu"
export NVM_DIR="$HOME/.nvm"
export GIT_SSH_COMMAND='ssh -i /home/ubuntu/.ssh/id_ed25519 -o IdentitiesOnly=yes'
export PATH="$HOME/.local/bin:/usr/local/bin:/usr/bin:/bin:$PATH"

if [ -s "$NVM_DIR/nvm.sh" ]; then
  # shellcheck disable=SC1091
  . "$NVM_DIR/nvm.sh" >/dev/null 2>&1 || true
fi

resolve_runtime_bin() {
  local name="$1"
  local candidate=""

  candidate="$(command -v "$name" 2>/dev/null || true)"
  if [ -n "$candidate" ] && [ -x "$candidate" ]; then
    printf '%s\n' "$candidate"
    return 0
  fi

  for candidate in \
    "$NVM_DIR"/versions/node/*/bin/"$name" \
    "$HOME/.local/bin/""$name" \
    "/usr/local/bin/""$name" \
    "/usr/bin/""$name" \
    "/bin/""$name"; do
    if [ -x "$candidate" ]; then
      printf '%s\n' "$candidate"
      return 0
    fi
  done

  return 1
}

NODE_BIN="$(resolve_runtime_bin node || true)"
NPM_BIN="$(resolve_runtime_bin npm || true)"

cd /home/ubuntu/bot || exit 1

LOG_FILE="/home/ubuntu/bot/update.log"
TMP_ERR="/tmp/tts-bot-update.err"
LOCK_FILE="/tmp/tts-bot-update.lock"
DIRTY_STATE_FILE="/home/ubuntu/bot/.update_last_state"
FAILED_REMOTE_HASH_FILE="/home/ubuntu/bot/.update_last_failed_remote_hash"
BRANCH="main"
SERVICE_NAME="tts-bot"
HEALTH_URL="http://127.0.0.1:10000/health"
ACTIVITY_FRONTEND_DIR="/home/ubuntu/bot/activity /sinuca"
ACTIVITY_BACKEND_DIR="/home/ubuntu/bot/activity /sinuca-server"
ACTIVITY_WEB_ROOT="/var/www/sinuca"
ACTIVITY_SERVICE_NAME="sinuca-activity-server"
ACTIVITY_HEALTH_URL="http://127.0.0.1:8787/health"
ACTIVITY_START_SCRIPT="/home/ubuntu/bot/start-sinuca-server.sh"
ACTIVITY_LOG_FILE="/home/ubuntu/bot/activity /sinuca-server/sinuca-server.log"
RETRY_LATEST_CODE=42
PENDING_REMOTE_HASH=""

require_runtime_tools() {
  if [ -n "$NODE_BIN" ] && [ -n "$NPM_BIN" ]; then
    return 0
  fi

  local body="Node/NPM não encontrado no ambiente atual.
HOME=$HOME
PATH=$PATH
NODE_BIN=${NODE_BIN:-ausente}
NPM_BIN=${NPM_BIN:-ausente}"

  log "Falha: runtime Node/NPM não encontrado para o auto update."
  /home/ubuntu/bot/alert.sh error "Falha no update automático" "$body"
  return 1
}

log() {
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] $1" >> "$LOG_FILE"
}

tail_err() {
  if [ -f "$TMP_ERR" ]; then
    tail -n 40 "$TMP_ERR"
  fi
}

read_file() {
  local path="$1"
  if [ -f "$path" ]; then
    cat "$path" 2>/dev/null || true
  fi
}

write_file() {
  local path="$1"
  local value="$2"
  printf '%s' "$value" > "$path"
}

remove_file() {
  rm -f "$1"
}

activity_service_exists() {
  sudo systemctl cat "$ACTIVITY_SERVICE_NAME" >/dev/null 2>&1
}

run_step() {
  local step_name="$1"
  shift

  : > "$TMP_ERR"
  "$@" >"$TMP_ERR" 2>&1
  local code=$?

  if [ $code -ne 0 ]; then
    local err_text
    err_text="$(tail_err)"

    local body="Etapa: $step_name

Últimas linhas do erro:
$err_text"

    log "Falha na etapa: $step_name"
    /home/ubuntu/bot/alert.sh error "Falha no update automático" "$body"
    rm -f "$TMP_ERR"
    return $code
  fi

  return 0
}

silent_fetch_remote() {
  git fetch origin "$BRANCH" >/dev/null 2>&1 || return 1
  return 0
}

current_remote_hash() {
  git rev-parse "origin/$BRANCH" 2>/dev/null || true
}

check_for_newer_remote() {
  local current_remote=""

  if ! silent_fetch_remote; then
    return 1
  fi

  current_remote="$(current_remote_hash)"
  if [ -n "$current_remote" ] && [ "$current_remote" != "$DEPLOY_TARGET" ]; then
    PENDING_REMOTE_HASH="$current_remote"
    return 0
  fi

  return 1
}

sanity_check() {
  local attempts=24
  local sleep_seconds=5
  local i
  local health_json=""
  local parsed=""

  for i in $(seq 1 "$attempts"); do
    if ! systemctl is-active --quiet "$SERVICE_NAME"; then
      echo "Serviço $SERVICE_NAME não ficou ativo após o restart."
      return 1
    fi

    health_json="$(curl -fsS --max-time 10 "$HEALTH_URL" 2>/dev/null || true)"
    if [ -n "$health_json" ]; then
      parsed="$(python3 - <<'PY' "$health_json"
import json
import sys

raw = sys.argv[1]
try:
    data = json.loads(raw)
except Exception:
    print("PARSE_OK=0")
    sys.exit(0)

healthy = 1 if data.get("healthy") is True else 0
starting = 1 if data.get("starting") is True else 0
status_text = "" if data.get("status") is None else str(data.get("status"))

print("PARSE_OK=1")
print(f"HEALTHY={healthy}")
print(f"STARTING={starting}")
print(f"STATUS_TEXT={status_text}")
PY
)"
      eval "$parsed"

      if [ "${PARSE_OK:-0}" = "1" ] && [ "${HEALTHY:-0}" = "1" ] && [ "${STARTING:-0}" = "0" ]; then
        return 0
      fi
    fi

    sleep "$sleep_seconds"
  done

  echo "Sanity check falhou. Último /health: ${health_json:-sem resposta}"
  return 1
}

activity_sanity_check() {
  local attempts=24
  local sleep_seconds=5
  local i
  local health_json=""

  for i in $(seq 1 "$attempts"); do
    if activity_service_exists && ! systemctl is-active --quiet "$ACTIVITY_SERVICE_NAME"; then
      echo "Serviço $ACTIVITY_SERVICE_NAME não ficou ativo após o restart."
      return 1
    fi

    health_json="$(curl -fsS --max-time 10 "$ACTIVITY_HEALTH_URL" 2>/dev/null || true)"
    if echo "$health_json" | grep -Eq '"ok"[[:space:]]*:[[:space:]]*true'; then
      return 0
    fi

    sleep "$sleep_seconds"
  done

  echo "Sanity check da Activity falhou. Último /health: ${health_json:-sem resposta}"
  return 1
}

publish_activity_frontend() {
  local tmp_release=""

  tmp_release="$(mktemp -d /tmp/sinuca-frontend.XXXXXX)" || return 1
  cp -a "$ACTIVITY_FRONTEND_DIR/dist/." "$tmp_release/" >"$TMP_ERR" 2>&1 || {
    rm -rf "$tmp_release"
    return 1
  }

  sudo mkdir -p "$ACTIVITY_WEB_ROOT" >"$TMP_ERR" 2>&1 || {
    rm -rf "$tmp_release"
    return 1
  }

  sudo find "$ACTIVITY_WEB_ROOT" -mindepth 1 -maxdepth 1 -exec rm -rf {} + >"$TMP_ERR" 2>&1 || {
    rm -rf "$tmp_release"
    return 1
  }

  sudo cp -a "$tmp_release/." "$ACTIVITY_WEB_ROOT/" >"$TMP_ERR" 2>&1 || {
    rm -rf "$tmp_release"
    return 1
  }

  rm -rf "$tmp_release"
  return 0
}

restart_activity_backend() {
  if activity_service_exists; then
    sudo systemctl restart "$ACTIVITY_SERVICE_NAME"
    return $?
  fi

  sudo fuser -k 8787/tcp >/dev/null 2>&1 || true
  nohup "$ACTIVITY_START_SCRIPT" >> "$ACTIVITY_LOG_FILE" 2>&1 &
  sleep 2
  return 0
}

deploy_activity_frontend() {
  cd "$ACTIVITY_FRONTEND_DIR" || return 1

  if ! run_step "activity frontend npm install" "$NPM_BIN" install; then
    return 1
  fi

  if check_for_newer_remote; then
    log "Novo commit detectado durante dependências do frontend da Activity. O deploy atual será finalizado e depois reiniciado no commit mais recente."
  fi

  if ! run_step "activity frontend build" "$NPM_BIN" run build; then
    return 1
  fi

  if check_for_newer_remote; then
    log "Novo commit detectado após o build do frontend da Activity. O deploy atual será finalizado e depois reiniciado no commit mais recente."
  fi

  : > "$TMP_ERR"
  if ! publish_activity_frontend; then
    local err_text
    err_text="$(tail_err)"
    local body="Etapa: publicar frontend da Activity

Últimas linhas do erro:
$err_text"
    log "Falha ao publicar frontend da Activity."
    /home/ubuntu/bot/alert.sh error "Falha no deploy da Activity" "$body"
    rm -f "$TMP_ERR"
    return 1
  fi

  rm -f "$TMP_ERR"
  return 0
}

deploy_activity_backend() {
  cd "$ACTIVITY_BACKEND_DIR" || return 1

  if ! run_step "activity backend npm install" "$NPM_BIN" install; then
    return 1
  fi

  if check_for_newer_remote; then
    log "Novo commit detectado durante dependências do backend da Activity. O deploy atual será finalizado e depois reiniciado no commit mais recente."
  fi

  if ! run_step "activity backend build" "$NPM_BIN" run build; then
    return 1
  fi

  if check_for_newer_remote; then
    log "Novo commit detectado após o build do backend da Activity. O deploy atual será finalizado e depois reiniciado no commit mais recente."
  fi

  if ! run_step "reiniciar backend da Activity" restart_activity_backend; then
    return 1
  fi

  if ! activity_sanity_check; then
    local reason="Sanity check da Activity falhou após a atualização"
    log "$reason"
    /home/ubuntu/bot/alert.sh error "Falha no deploy da Activity" "$reason"
    return 1
  fi

  return 0
}

rollback_update() {
  local reason="$1"
  local mark_bad_remote="${2:-0}"

  log "Falha pós-update: $reason"
  log "Iniciando rollback para $PREV_SHORT..."

  if [ "$mark_bad_remote" = "1" ]; then
    write_file "$FAILED_REMOTE_HASH_FILE" "$REMOTE_HASH"
    log "Commit remoto $NEW_SHORT marcado como ruim até surgir um novo commit."
  fi

  git reset --hard "$PREV_HASH" >/dev/null 2>&1 || true

  if [ -x /home/ubuntu/bot/.venv/bin/python ]; then
    /home/ubuntu/bot/.venv/bin/python -m pip install -r requirements.txt >/dev/null 2>&1 || true
  elif [ -f /home/ubuntu/bot/.venv/bin/activate ]; then
    . /home/ubuntu/bot/.venv/bin/activate
    pip install -r requirements.txt >/dev/null 2>&1 || true
  fi

  (
    cd "$ACTIVITY_FRONTEND_DIR" &&
    "$NPM_BIN" install >/dev/null 2>&1 &&
    "$NPM_BIN" run build >/dev/null 2>&1
  ) || true
  publish_activity_frontend >/dev/null 2>&1 || true

  (
    cd "$ACTIVITY_BACKEND_DIR" &&
    "$NPM_BIN" install >/dev/null 2>&1 &&
    "$NPM_BIN" run build >/dev/null 2>&1
  ) || true
  restart_activity_backend >/dev/null 2>&1 || true

  sudo systemctl restart "$SERVICE_NAME" >/dev/null 2>&1 || true
  sleep 8

  BODY="Branch: $BRANCH
Tentativa de update para: $NEW_SHORT
Rollback para: $PREV_SHORT
Motivo: $reason"

  if [ "$mark_bad_remote" = "1" ]; then
    BODY="$BODY

Esse commit remoto foi marcado como ruim e não será tentado novamente até surgir outro commit no GitHub."
  fi

  /home/ubuntu/bot/alert.sh error "Update falhou e rollback foi aplicado" "$BODY"
}

deploy_current_target() {
  if ! run_step "git reset --hard" git reset --hard "$DEPLOY_TARGET"; then
    return 1
  fi

  if [ -x /home/ubuntu/bot/.venv/bin/python ]; then
    PIP_CMD=(/home/ubuntu/bot/.venv/bin/python -m pip)
  else
    if [ -f /home/ubuntu/bot/.venv/bin/activate ]; then
      . /home/ubuntu/bot/.venv/bin/activate
    fi
    PIP_CMD=(pip)
  fi

  if ! run_step "pip install -r requirements.txt" "${PIP_CMD[@]}" install -r requirements.txt; then
    rollback_update "Falha ao instalar dependências" 0
    return 1
  fi

  if check_for_newer_remote; then
    log "Novo commit detectado após instalar dependências do bot. O deploy atual será finalizado e depois reiniciado no commit mais recente."
  fi

  if ! run_step "systemctl restart tts-bot" sudo systemctl restart "$SERVICE_NAME"; then
    rollback_update "Falha ao reiniciar o serviço" 1
    return 1
  fi

  if ! sanity_check; then
    rollback_update "Sanity check falhou após a atualização" 1
    return 1
  fi

  if check_for_newer_remote; then
    log "Novo commit detectado após subir o bot. O deploy atual será finalizado e depois reiniciado no commit mais recente."
  fi

  deploy_activity_frontend
  local code=$?
  if [ "$code" -ne 0 ]; then
    rollback_update "Falha no deploy do frontend da Activity" 1
    return 1
  fi

  deploy_activity_backend
  code=$?
  if [ "$code" -ne 0 ]; then
    rollback_update "Falha no deploy do backend da Activity" 1
    return 1
  fi

  if check_for_newer_remote; then
    log "Novo commit detectado após subir a Activity. O deploy atual será finalizado e depois reiniciado no commit mais recente."
  fi

  if [ -n "$PENDING_REMOTE_HASH" ] && [ "$PENDING_REMOTE_HASH" != "$DEPLOY_TARGET" ]; then
    return "$RETRY_LATEST_CODE"
  fi

  return 0
}

exec 9>"$LOCK_FILE"
if ! flock -n 9; then
  log "Outro update já está em execução."
  exit 0
fi

if ! require_runtime_tools; then
  exit 1
fi

if ! run_step "git fetch" git fetch origin "$BRANCH"; then
  exit 1
fi

DIRTY_STATE="$(git status --porcelain --untracked-files=no 2>/dev/null || true)"

if [ -n "$DIRTY_STATE" ]; then
  LAST_STATE="$(read_file "$DIRTY_STATE_FILE")"
  if [ "$LAST_STATE" != "dirty" ]; then
    log "Repo sujo. Update automático ignorado."
    BODY="O repositório local está com alterações não commitadas.
Por segurança, o update automático foi cancelado."
    /home/ubuntu/bot/alert.sh warn "Update ignorado" "$BODY"
    write_file "$DIRTY_STATE_FILE" "dirty"
  fi

  rm -f "$TMP_ERR"
  exit 0
fi

LAST_STATE="$(read_file "$DIRTY_STATE_FILE")"
if [ "$LAST_STATE" = "dirty" ]; then
  log "Repo voltou a ficar limpo."
  BODY="O repositório voltou a ficar limpo.
O update automático pode funcionar normalmente de novo."
  /home/ubuntu/bot/alert.sh success "Update liberado novamente" "$BODY"
fi
remove_file "$DIRTY_STATE_FILE"

LOCAL_HASH="$(git rev-parse HEAD 2>/dev/null)"
REMOTE_HASH="$(git rev-parse origin/$BRANCH 2>/dev/null)"

if [ -z "$LOCAL_HASH" ] || [ -z "$REMOTE_HASH" ]; then
  BODY="Etapa: comparação de hashes

Não foi possível obter LOCAL_HASH ou REMOTE_HASH."
  log "Falha ao obter hashes."
  /home/ubuntu/bot/alert.sh error "Falha no update automático" "$BODY"
  exit 1
fi

FAILED_REMOTE_HASH="$(read_file "$FAILED_REMOTE_HASH_FILE")"

if [ -n "$FAILED_REMOTE_HASH" ] && [ "$FAILED_REMOTE_HASH" = "$REMOTE_HASH" ]; then
  FAILED_SHORT="$(git rev-parse --short "$FAILED_REMOTE_HASH" 2>/dev/null || echo "$FAILED_REMOTE_HASH")"
  log "Commit remoto $FAILED_SHORT já foi marcado como ruim. Update ignorado até surgir novo commit."
  rm -f "$TMP_ERR"
  exit 0
fi

if [ -n "$FAILED_REMOTE_HASH" ] && [ "$FAILED_REMOTE_HASH" != "$REMOTE_HASH" ]; then
  OLD_FAILED_SHORT="$(git rev-parse --short "$FAILED_REMOTE_HASH" 2>/dev/null || echo "$FAILED_REMOTE_HASH")"
  NEW_REMOTE_SHORT="$(git rev-parse --short "$REMOTE_HASH" 2>/dev/null || echo "$REMOTE_HASH")"

  log "Novo commit remoto detectado após commit ruim anterior. Liberando novas tentativas."
  BODY="Commit remoto ruim anterior: $OLD_FAILED_SHORT
Novo commit remoto detectado: $NEW_REMOTE_SHORT

O auto-update voltará a tentar atualizar normalmente."
  /home/ubuntu/bot/alert.sh info "Novo commit detectado após rollback" "$BODY"

  remove_file "$FAILED_REMOTE_HASH_FILE"
fi

if [ "$LOCAL_HASH" = "$REMOTE_HASH" ]; then
  rm -f "$TMP_ERR"
  exit 0
fi

while true; do
  OLD_SHORT="$(git rev-parse --short "$LOCAL_HASH" 2>/dev/null)"
  NEW_SHORT="$(git rev-parse --short "$REMOTE_HASH" 2>/dev/null)"
  COMMIT_MSG="$(git log -1 --pretty=%s "$REMOTE_HASH" 2>/dev/null || true)"

  PREV_HASH="$LOCAL_HASH"
  PREV_SHORT="$OLD_SHORT"
  DEPLOY_TARGET="$REMOTE_HASH"
  PENDING_REMOTE_HASH=""

  log "Novo commit detectado. Atualizando para $NEW_SHORT..."

  deploy_current_target
  deploy_code=$?
  if [ "$deploy_code" -ne 0 ] && [ "$deploy_code" -ne "$RETRY_LATEST_CODE" ]; then
    exit 1
  fi

  if [ "$deploy_code" -eq "$RETRY_LATEST_CODE" ]; then
    NEXT_HASH="$PENDING_REMOTE_HASH"
    if [ -z "$NEXT_HASH" ]; then
      if ! silent_fetch_remote; then
        exit 1
      fi
      NEXT_HASH="$(current_remote_hash)"
    fi

    if [ -z "$NEXT_HASH" ] || [ "$NEXT_HASH" = "$DEPLOY_TARGET" ]; then
      BODY="Commit novo foi detectado durante o deploy, mas não foi possível resolver o novo HEAD remoto com segurança."
      /home/ubuntu/bot/alert.sh warn "Deploy reiniciado" "$BODY"
      exit 1
    fi

    NEXT_SHORT="$(git rev-parse --short "$NEXT_HASH" 2>/dev/null || echo "$NEXT_HASH")"
    BODY="Um commit mais novo chegou durante o deploy do commit $NEW_SHORT.

O processo atual foi descartado e o update será refeito para o commit mais recente: $NEXT_SHORT."
    /home/ubuntu/bot/alert.sh update "Deploy reiniciado para commit mais novo" "$BODY"

    LOCAL_HASH="$DEPLOY_TARGET"
    REMOTE_HASH="$NEXT_HASH"
    continue
  fi

  remove_file "$FAILED_REMOTE_HASH_FILE"

  BODY="Branch: $BRANCH
De: $OLD_SHORT
Para: $NEW_SHORT
Commit: $COMMIT_MSG

tts-bot reiniciado com sucesso e verificado.
Activity: frontend publicado em $ACTIVITY_WEB_ROOT e backend validado em $ACTIVITY_HEALTH_URL."
  /home/ubuntu/bot/alert.sh update "Update automático aplicado" "$BODY"

  if ! activity_service_exists; then
    /home/ubuntu/bot/notify-activity-start.sh >/dev/null 2>&1 || true
  fi

  log "Atualização concluída com sucesso."
  rm -f "$TMP_ERR"
  exit 0
done
