#!/data/data/com.termux/files/usr/bin/bash
# Inicia/reinicia o worker auxiliar do celular em Termux.
set -u

ENV_FILE="${PHONE_WORKER_ENV:-$HOME/.phone-worker.env}"
if [[ -f "$ENV_FILE" ]]; then
  set -a
  # shellcheck disable=SC1090
  source "$ENV_FILE"
  set +a
fi

WORKER_DIR="${PHONE_WORKER_DIR:-$HOME/phone-worker}"
PORT="${PHONE_WORKER_PORT:-8766}"
HOST="${PHONE_WORKER_HOST:-0.0.0.0}"
TOKEN="${PHONE_WORKER_TOKEN:-}"
SESSION="${PHONE_WORKER_TMUX_SESSION:-phone-worker}"
PYTHON_BIN="${PHONE_WORKER_PYTHON:-python}"
START_WAIT="${PHONE_WORKER_START_WAIT_SECONDS:-3}"
LOG_FILE="${PHONE_WORKER_LOG_FILE:-$WORKER_DIR/phone-worker.log}"

log() {
  printf '[phone-worker] %s\n' "$*"
}

termux-wake-lock 2>/dev/null || true

health_ok() {
  if [[ -n "$TOKEN" ]]; then
    curl --max-time 4 -fsS -H "Authorization: Bearer $TOKEN" "http://127.0.0.1:$PORT/health" >/dev/null 2>&1
  else
    curl --max-time 4 -fsS "http://127.0.0.1:$PORT/health" >/dev/null 2>&1
  fi
}

if health_ok; then
  log "Worker já está online."
  exit 0
fi

if ! command -v tmux >/dev/null 2>&1; then
  log "tmux não encontrado. Rode: pkg install tmux -y"
  exit 1
fi
if ! command -v "$PYTHON_BIN" >/dev/null 2>&1; then
  log "python não encontrado. Rode: pkg install python -y"
  exit 1
fi
if [[ ! -f "$WORKER_DIR/phone_worker.py" ]]; then
  log "phone_worker.py não encontrado em $WORKER_DIR"
  exit 1
fi

mkdir -p "$WORKER_DIR"
log "Worker offline. Reiniciando sessão $SESSION..."
tmux kill-session -t "$SESSION" 2>/dev/null || true
pkill -f 'phone_worker.py' 2>/dev/null || true

tmux new-session -d -s "$SESSION" \
  "cd '$WORKER_DIR' && PHONE_WORKER_TOKEN='$TOKEN' PHONE_WORKER_PORT='$PORT' PHONE_WORKER_HOST='$HOST' exec '$PYTHON_BIN' phone_worker.py --host '$HOST' --port '$PORT' >> '$LOG_FILE' 2>&1"

sleep "$START_WAIT"
if health_ok; then
  log "Worker iniciado com sucesso."
  exit 0
fi

log "Falha ao iniciar worker. Veja: tmux attach -t $SESSION ou tail -n 80 '$LOG_FILE'"
exit 1
