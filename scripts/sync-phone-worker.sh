#!/usr/bin/env bash
set -u

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

log() {
  printf '[phone-worker-sync] %s\n' "$*"
}

if [ -f "$ROOT_DIR/.env" ]; then
  set -a
  # shellcheck disable=SC1091
  . "$ROOT_DIR/.env"
  set +a
fi

PHONE_HOST="${PHONE_WORKER_HOST:-${PHONE_LAVALINK_HOST:-}}"
PHONE_USER="${PHONE_WORKER_SSH_USER:-}"
PHONE_PORT="${PHONE_WORKER_SSH_PORT:-8022}"
PHONE_START_COMMAND="${PHONE_WORKER_START_COMMAND:-/data/data/com.termux/files/home/phone-worker/start-phone-worker.sh}"

if [ -z "$PHONE_HOST" ] || [ -z "$PHONE_USER" ]; then
  log "config incompleta: PHONE_WORKER_HOST/PHONE_WORKER_SSH_USER ausente"
  exit 2
fi

SRC_DIR="$ROOT_DIR/deploy/termux/phone-worker"
REMOTE_HOME="/data/data/com.termux/files/home"
REMOTE_DIR="$REMOTE_HOME/phone-worker"

if [ ! -d "$SRC_DIR" ]; then
  log "diretório não encontrado: $SRC_DIR"
  exit 3
fi

SSH_BASE=(ssh -p "$PHONE_PORT" -o BatchMode=yes -o ConnectTimeout=6 -o StrictHostKeyChecking=accept-new "$PHONE_USER@$PHONE_HOST")
SCP_BASE=(scp -P "$PHONE_PORT" -o BatchMode=yes -o ConnectTimeout=6 -o StrictHostKeyChecking=accept-new)

log "testando SSH em $PHONE_USER@$PHONE_HOST:$PHONE_PORT"
if ! "${SSH_BASE[@]}" 'echo ok' >/dev/null 2>&1; then
  log "celular offline ou SSH indisponível"
  mkdir -p "$ROOT_DIR/data"
  date -Is > "$ROOT_DIR/data/phone_worker_sync_pending.flag"
  exit 10
fi

log "preparando diretórios no celular"
"${SSH_BASE[@]}" "mkdir -p '$REMOTE_DIR'"

log "copiando arquivos do phone-worker"
"${SCP_BASE[@]}" \
  "$SRC_DIR/phone_worker.py" \
  "$PHONE_USER@$PHONE_HOST:$REMOTE_DIR/phone_worker.py"

# Scripts auxiliares ficam em dois lugares:
# - ~/phone-worker/*.sh para permitir `cd ~/phone-worker && bash ./start-phone-worker.sh`
# - ~/*.sh para compatibilidade com instalações antigas e atalhos já existentes.
for f in start-phone-worker.sh watch-phone-worker.sh pair-phone-worker.sh install.sh README.md phone-worker.env.example; do
  if [ -f "$SRC_DIR/$f" ]; then
    "${SCP_BASE[@]}" "$SRC_DIR/$f" "$PHONE_USER@$PHONE_HOST:$REMOTE_DIR/$f"
    case "$f" in
      start-phone-worker.sh|watch-phone-worker.sh|pair-phone-worker.sh)
        "${SCP_BASE[@]}" "$SRC_DIR/$f" "$PHONE_USER@$PHONE_HOST:$REMOTE_HOME/$f"
        ;;
    esac
  fi
done

log "ajustando permissões no celular"
"${SSH_BASE[@]}" "
chmod +x '$REMOTE_DIR/phone_worker.py' '$REMOTE_DIR/start-phone-worker.sh' '$REMOTE_DIR/watch-phone-worker.sh' '$REMOTE_DIR/pair-phone-worker.sh' 2>/dev/null || true
chmod +x '$REMOTE_HOME/start-phone-worker.sh' '$REMOTE_HOME/watch-phone-worker.sh' '$REMOTE_HOME/pair-phone-worker.sh' 2>/dev/null || true
"

log "reiniciando phone-worker no celular"
"${SSH_BASE[@]}" "
tmux kill-session -t phone-worker 2>/dev/null || true
pkill -f '[p]hone_worker.py' 2>/dev/null || true
sleep 1
$PHONE_START_COMMAND
"

sleep 2

log "testando health pela VPS"
if python3 "$ROOT_DIR/scripts/phone-worker-client.py" health >/dev/null 2>&1; then
  rm -f "$ROOT_DIR/data/phone_worker_sync_pending.flag"
  log "sincronizado e saudável"
  exit 0
fi

log "sincronizou, mas health falhou"
mkdir -p "$ROOT_DIR/data"
date -Is > "$ROOT_DIR/data/phone_worker_sync_pending.flag"
exit 11
