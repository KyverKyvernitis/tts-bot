#!/usr/bin/env bash
set -u

export GIT_SSH_COMMAND='ssh -i /home/ubuntu/.ssh/id_ed25519 -o IdentitiesOnly=yes'

cd /home/ubuntu/bot || exit 1

LOG_FILE="/home/ubuntu/bot/update.log"
TMP_ERR="/tmp/tts-bot-update.err"
DIRTY_STATE_FILE="/home/ubuntu/bot/.update_last_state"
FAILED_REMOTE_HASH_FILE="/home/ubuntu/bot/.update_last_failed_remote_hash"
BRANCH="main"
SERVICE_NAME="tts-bot"
HEALTH_URL="http://127.0.0.1:10000/health"

log() {
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] $1" >> "$LOG_FILE"
}

tail_err() {
  if [ -f "$TMP_ERR" ]; then
    tail -n 20 "$TMP_ERR"
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

sanity_check() {
  local attempts=12
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

  sudo systemctl restart "$SERVICE_NAME" || true
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

run_step() {
  local step_name="$1"
  shift

  : > "$TMP_ERR"
  "$@" >/dev/null 2>"$TMP_ERR"
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

OLD_SHORT="$(git rev-parse --short "$LOCAL_HASH" 2>/dev/null)"
NEW_SHORT="$(git rev-parse --short "$REMOTE_HASH" 2>/dev/null)"
COMMIT_MSG="$(git log -1 --pretty=%s "$REMOTE_HASH" 2>/dev/null || true)"

PREV_HASH="$LOCAL_HASH"
PREV_SHORT="$OLD_SHORT"

log "Novo commit detectado. Atualizando..."

if ! run_step "git reset --hard" git reset --hard "$REMOTE_HASH"; then
  exit 1
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
  exit 1
fi

if ! run_step "systemctl restart tts-bot" sudo systemctl restart "$SERVICE_NAME"; then
  rollback_update "Falha ao reiniciar o serviço" 1
  exit 1
fi

if ! sanity_check; then
  rollback_update "Sanity check falhou após a atualização" 1
  exit 1
fi

remove_file "$FAILED_REMOTE_HASH_FILE"

BODY="Branch: $BRANCH
De: $OLD_SHORT
Para: $NEW_SHORT
Commit: $COMMIT_MSG

tts-bot reiniciado com sucesso e verificado."

log "Atualização concluída com sucesso."
/home/ubuntu/bot/alert.sh update "Update automático aplicado" "$BODY"

rm -f "$TMP_ERR"
exit 0
