#!/usr/bin/env bash
set -u

export GIT_SSH_COMMAND='ssh -i /home/ubuntu/.ssh/id_ed25519 -o IdentitiesOnly=yes'

cd /home/ubuntu/bot || exit 1

LOG_FILE="/home/ubuntu/bot/update.log"
TMP_ERR="/tmp/tts-bot-update.err"
STATE_FILE="/home/ubuntu/bot/.update_last_state"
BRANCH="main"

log() {
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] $1" >> "$LOG_FILE"
}

tail_err() {
  if [ -f "$TMP_ERR" ]; then
    tail -n 20 "$TMP_ERR"
  fi
}

get_state() {
  if [ -f "$STATE_FILE" ]; then
    cat "$STATE_FILE" 2>/dev/null || true
  fi
}

set_state() {
  echo "$1" > "$STATE_FILE"
}

clear_state() {
  rm -f "$STATE_FILE"
}

sanity_check() {
  sleep 10

  if ! systemctl is-active --quiet tts-bot; then
    echo "Serviço tts-bot não ficou ativo após o restart."
    return 1
  fi

  if [ -x /home/ubuntu/bot/healthcheck.sh ]; then
    if ! /home/ubuntu/bot/healthcheck.sh >/dev/null 2>&1; then
      echo "healthcheck.sh falhou após a atualização."
      return 1
    fi
  fi

  return 0
}

rollback_update() {
  local reason="$1"

  log "Falha pós-update: $reason"
  log "Iniciando rollback para $PREV_SHORT..."

  git reset --hard "$PREV_HASH" >/dev/null 2>&1 || true

  if [ -x /home/ubuntu/bot/.venv/bin/python ]; then
    /home/ubuntu/bot/.venv/bin/python -m pip install -r requirements.txt >/dev/null 2>&1 || true
  elif [ -f /home/ubuntu/bot/.venv/bin/activate ]; then
    . /home/ubuntu/bot/.venv/bin/activate
    pip install -r requirements.txt >/dev/null 2>&1 || true
  fi

  sudo systemctl restart tts-bot || true
  sleep 8

  BODY="Branch: $BRANCH
Tentativa de update para: $NEW_SHORT
Rollback para: $PREV_SHORT
Motivo: $reason"

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
  LAST_STATE="$(get_state)"
  if [ "$LAST_STATE" != "dirty" ]; then
    log "Repo sujo. Update automático ignorado."
    BODY="O repositório local está com alterações não commitadas.
Por segurança, o update automático foi cancelado."
    /home/ubuntu/bot/alert.sh warn "Update ignorado" "$BODY"
    set_state "dirty"
  fi

  rm -f "$TMP_ERR"
  exit 0
fi

LAST_STATE="$(get_state)"
if [ "$LAST_STATE" = "dirty" ]; then
  log "Repo voltou a ficar limpo."
  BODY="O repositório voltou a ficar limpo.
O update automático pode funcionar normalmente de novo."
  /home/ubuntu/bot/alert.sh success "Update liberado novamente" "$BODY"
fi
clear_state

LOCAL_HASH="$(git rev-parse HEAD 2>/dev/null)"
REMOTE_HASH="$(git rev-parse origin/$BRANCH 2>/dev/null)"

if [ -z "$LOCAL_HASH" ] || [ -z "$REMOTE_HASH" ]; then
  BODY="Etapa: comparação de hashes

Não foi possível obter LOCAL_HASH ou REMOTE_HASH."
  log "Falha ao obter hashes."
  /home/ubuntu/bot/alert.sh error "Falha no update automático" "$BODY"
  exit 1
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
  rollback_update "Falha ao instalar dependências"
  exit 1
fi

if ! run_step "systemctl restart tts-bot" sudo systemctl restart tts-bot; then
  rollback_update "Falha ao reiniciar o serviço"
  exit 1
fi

if ! sanity_check; then
  rollback_update "Sanity check falhou após a atualização"
  exit 1
fi

BODY="Branch: $BRANCH
De: $OLD_SHORT
Para: $NEW_SHORT
Commit: $COMMIT_MSG

tts-bot reiniciado com sucesso e verificado."

log "Atualização concluída com sucesso."
/home/ubuntu/bot/alert.sh update "Update automático aplicado" "$BODY"

rm -f "$TMP_ERR"
exit 0
