#!/data/data/com.termux/files/usr/bin/bash
# Supervisor simples do Music Agent do phone worker.
set -u

ENV_FILE="${PHONE_WORKER_ENV:-$HOME/.phone-worker.env}"
if [[ -f "$ENV_FILE" ]]; then
  set -a
  # shellcheck disable=SC1090
  source "$ENV_FILE"
  set +a
fi

WORKER_DIR="${PHONE_WORKER_DIR:-$HOME/phone-worker}"
PYTHON_BIN="${PHONE_WORKER_PYTHON:-python}"
HOST="${MUSIC_AGENT_HOST:-127.0.0.1}"
PORT="${MUSIC_AGENT_PORT:-8786}"
TOKEN="${MUSIC_AGENT_TOKEN:-${PHONE_WORKER_TOKEN:-}}"
LOG_FILE="${MUSIC_AGENT_LOG_FILE:-$WORKER_DIR/music_agent.log}"
PID_FILE="${MUSIC_AGENT_PID_FILE:-$WORKER_DIR/music_agent.pid}"
START_WAIT="${MUSIC_AGENT_START_WAIT_SECONDS:-5}"
KILL_DUPLICATES="${MUSIC_AGENT_KILL_DUPLICATES:-true}"

log() { printf '[music-agent-start] %s\n' "$*"; }
truthy() {
  local value="${1:-}"
  value="$(printf '%s' "$value" | tr '[:upper:]' '[:lower:]' | tr -d ' \t\r\n"' | tr -d "'")"
  [[ "$value" == "1" || "$value" == "true" || "$value" == "yes" || "$value" == "y" || "$value" == "on" || "$value" == "sim" ]]
}

if [[ ! -f "$WORKER_DIR/music_agent.py" ]]; then
  log "music_agent.py não encontrado em $WORKER_DIR"
  exit 1
fi
if ! command -v "$PYTHON_BIN" >/dev/null 2>&1; then
  log "python não encontrado"
  exit 1
fi

health_ok() {
  if [[ -n "$TOKEN" ]]; then
    curl --max-time 4 -fsS -H "Authorization: Bearer $TOKEN" "http://127.0.0.1:$PORT/health" >/dev/null 2>&1
  else
    curl --max-time 4 -fsS "http://127.0.0.1:$PORT/health" >/dev/null 2>&1
  fi
}

list_pids() {
  pgrep -f 'music_agent.py' 2>/dev/null || true
}

kill_agent() {
  list_pids | while read -r pid; do
    case "$pid" in ''|*[!0-9]*) continue ;; esac
    [[ "$pid" == "$$" ]] && continue
    kill "$pid" 2>/dev/null || true
  done
  sleep 1
  list_pids | while read -r pid; do
    case "$pid" in ''|*[!0-9]*) continue ;; esac
    [[ "$pid" == "$$" ]] && continue
    kill -9 "$pid" 2>/dev/null || true
  done
}

ensure_deps() {
  "$PYTHON_BIN" - <<'PYDEPS' >/dev/null 2>&1 && return 0
import aiohttp, discord, wavelink, yt_dlp  # noqa: F401
PYDEPS
  log "instalando dependências do Music Agent: aiohttp discord.py[voice] wavelink yt-dlp[default]"
  "$PYTHON_BIN" -m pip install --upgrade aiohttp 'discord.py[voice]>=2.7.1,<2.8' 'wavelink>=3.4,<3.6' 'yt-dlp[default]' >/dev/null 2>&1 || \
    log "não consegui instalar todas as dependências automaticamente"
}

ensure_deps
mkdir -p "$(dirname "$LOG_FILE")"

if health_ok; then
  log "Music Agent já está online em $HOST:$PORT"
  exit 0
fi

if truthy "$KILL_DUPLICATES"; then
  kill_agent
fi

log "iniciando Music Agent em $HOST:$PORT"
(
  cd "$WORKER_DIR" || exit 1
  exec "$PYTHON_BIN" music_agent.py
) >> "$LOG_FILE" 2>&1 &
pid=$!
printf '%s\n' "$pid" > "$PID_FILE" 2>/dev/null || true
sleep "$START_WAIT"

if health_ok; then
  log "Music Agent iniciado com sucesso; pid=$pid"
  exit 0
fi
if kill -0 "$pid" 2>/dev/null; then
  log "Music Agent iniciou, mas health ainda não respondeu; veja $LOG_FILE"
  exit 0
fi
log "falha ao iniciar Music Agent; veja: tail -80 '$LOG_FILE'"
exit 1
