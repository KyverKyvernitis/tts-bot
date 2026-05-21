#!/data/data/com.termux/files/usr/bin/bash
# Supervisor local do Core Worker/phone-worker em Termux.
# Garante um único processo, rotaciona logs e inicia sem depender de tmux.
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
PYTHON_BIN="${PHONE_WORKER_PYTHON:-python}"
START_WAIT="${PHONE_WORKER_START_WAIT_SECONDS:-3}"
LOG_FILE="${PHONE_WORKER_LOG_FILE:-$WORKER_DIR/phone-worker.log}"
PID_FILE="${PHONE_WORKER_PID_FILE:-$WORKER_DIR/phone-worker.pid}"
LOCK_DIR="${PHONE_WORKER_LOCK_DIR:-$WORKER_DIR/.phone-worker-start.lock}"
STATUS_FILE="${PHONE_WORKER_STATUS_FILE:-$WORKER_DIR/phone-worker.status}"
MAX_LOG_BYTES="${PHONE_WORKER_LOG_MAX_BYTES:-1048576}"
KILL_DUPLICATES="${PHONE_WORKER_START_KILL_DUPLICATES:-true}"
SSHD_AUTO_START="${PHONE_WORKER_SSHD_AUTO_START:-true}"
SSHD_PORT="${PHONE_WORKER_SSH_PORT:-8022}"

log() {
  printf '[phone-worker-start] %s\n' "$*"
}

truthy() {
  local value="${1:-}"
  value="$(printf '%s' "$value" | tr '[:upper:]' '[:lower:]' | tr -d ' \t\r\n"')"
  value="${value//\'/}"
  [[ "$value" == "1" || "$value" == "true" || "$value" == "yes" || "$value" == "y" || "$value" == "on" || "$value" == "sim" ]]
}

sshd_listening() {
  command -v ss >/dev/null 2>&1 || return 1
  ss -lnt 2>/dev/null | grep -Eq "[:.]${SSHD_PORT}[[:space:]]|:${SSHD_PORT}$"
}

ensure_sshd_running() {
  truthy "$SSHD_AUTO_START" || return 0
  command -v sshd >/dev/null 2>&1 || return 0
  if sshd_listening; then
    return 0
  fi
  if command -v pgrep >/dev/null 2>&1 && pgrep -f 'sshd' >/dev/null 2>&1; then
    log "sshd rodando, mas porta ${SSHD_PORT} não apareceu; mantendo processo existente"
    return 0
  fi
  log "sshd parado; tentando iniciar porta ${SSHD_PORT}"
  sshd -p "$SSHD_PORT" >/dev/null 2>&1 || sshd >/dev/null 2>&1 || true
}

now_iso() {
  date '+%Y-%m-%dT%H:%M:%S%z' 2>/dev/null || date
}

mkdir -p "$WORKER_DIR"

if ! mkdir "$LOCK_DIR" 2>/dev/null; then
  log "start já em andamento; aguardando lock liberar"
  waited=0
  while [[ -d "$LOCK_DIR" && "$waited" -lt 20 ]]; do
    sleep 1
    waited=$((waited + 1))
  done
  if [[ -d "$LOCK_DIR" ]]; then
    log "lock antigo encontrado; removendo: $LOCK_DIR"
    rm -rf "$LOCK_DIR" 2>/dev/null || true
    mkdir "$LOCK_DIR" 2>/dev/null || true
  fi
fi
trap 'rm -rf "$LOCK_DIR" 2>/dev/null || true' EXIT INT TERM

termux-wake-lock 2>/dev/null || true
ensure_sshd_running

health_ok() {
  if [[ -n "$TOKEN" ]]; then
    curl --max-time 4 -fsS -H "Authorization: Bearer $TOKEN" "http://127.0.0.1:$PORT/health" >/dev/null 2>&1
  else
    curl --max-time 4 -fsS "http://127.0.0.1:$PORT/health" >/dev/null 2>&1
  fi
}

health_json() {
  if [[ -n "$TOKEN" ]]; then
    curl --max-time 4 -fsS -H "Authorization: Bearer $TOKEN" "http://127.0.0.1:$PORT/health" 2>/dev/null || true
  else
    curl --max-time 4 -fsS "http://127.0.0.1:$PORT/health" 2>/dev/null || true
  fi
}

running_version() {
  health_json | "$PYTHON_BIN" -c 'import json,sys;
try:
 data=json.load(sys.stdin); print(str(data.get("version") or ""))
except Exception: pass' 2>/dev/null || true
}

file_version() {
  "$PYTHON_BIN" - "$WORKER_DIR/phone_worker.py" <<'PYVER' 2>/dev/null || true
import re, sys
try:
    text=open(sys.argv[1], encoding="utf-8", errors="ignore").read()
except Exception:
    text=""
m=re.search(r'^PHONE_WORKER_VERSION\s*=\s*["\']([^"\']+)["\']', text, re.M)
print(m.group(1) if m else "")
PYVER
}

version_lt() {
  "$PYTHON_BIN" - "$1" "$2" <<'PYVERCMP' 2>/dev/null
import re, sys
def parts(v):
    xs=[int(x) for x in re.findall(r"\d+", v or "")[:4]]
    return tuple(xs or [0])
sys.exit(0 if parts(sys.argv[1]) < parts(sys.argv[2]) else 1)
PYVERCMP
}

list_worker_pids() {
  if command -v pgrep >/dev/null 2>&1; then
    pgrep -f 'phone_worker.py' 2>/dev/null || true
    return
  fi
  ps -ef 2>/dev/null | awk '/phone_worker\.py/ && !/awk/ {print $2}' || true
}

worker_pid_count() {
  list_worker_pids | awk 'NF {c++} END {print c+0}'
}

kill_worker_processes() {
  list_worker_pids | while read -r pid; do
    case "$pid" in
      ''|*[!0-9]*) continue ;;
    esac
    if [[ "$pid" != "$$" ]]; then
      kill "$pid" 2>/dev/null || true
    fi
  done
  sleep 1
  list_worker_pids | while read -r pid; do
    case "$pid" in
      ''|*[!0-9]*) continue ;;
    esac
    if [[ "$pid" != "$$" ]]; then
      kill -9 "$pid" 2>/dev/null || true
    fi
  done
}

rotate_log_if_needed() {
  mkdir -p "$(dirname "$LOG_FILE")"
  if [[ -f "$LOG_FILE" ]]; then
    size=$(wc -c < "$LOG_FILE" 2>/dev/null || echo 0)
    if [[ "$size" -gt "$MAX_LOG_BYTES" ]]; then
      mv -f "$LOG_FILE" "$LOG_FILE.1" 2>/dev/null || true
      : > "$LOG_FILE"
    fi
  fi
}

write_status() {
  mkdir -p "$(dirname "$STATUS_FILE")"
  printf '%s\n' "$1" > "$STATUS_FILE" 2>/dev/null || true
}

if ! command -v curl >/dev/null 2>&1; then
  log "curl não encontrado. Rode: pkg install curl -y"
fi
if ! command -v "$PYTHON_BIN" >/dev/null 2>&1; then
  log "python não encontrado. Rode: pkg install python -y"
  exit 1
fi
if [[ ! -f "$WORKER_DIR/phone_worker.py" ]]; then
  log "phone_worker.py não encontrado em $WORKER_DIR"
  exit 1
fi

ensure_tts_benchmark_deps_if_needed() {
  local profile="${CORE_WORKER_PROFILE:-${PHONE_WORKER_PROFILE:-}}"
  profile="$(printf '%s' "$profile" | tr '[:upper:]' '[:lower:]' | tr -d ' \t\r\n"' | tr -d "'")"
  [[ "$profile" == "turbo" ]] || return 0
  local mode="${PHONE_WORKER_TTS_DEPS_INSTALL:-auto}"
  mode="$(printf '%s' "$mode" | tr '[:upper:]' '[:lower:]' | tr -d ' \t\r\n"' | tr -d "'")"
  [[ "$mode" == "0" || "$mode" == "false" || "$mode" == "off" || "$mode" == "no" ]] && return 0
  "$PYTHON_BIN" - <<'PYTTSDEPS' >/dev/null 2>&1 && return 0
import edge_tts  # noqa: F401
import gtts  # noqa: F401
from google.cloud import texttospeech_v1  # noqa: F401
PYTTSDEPS
  log "perfil turbo: instalando dependências opcionais do benchmark TTS"
  "$PYTHON_BIN" -m pip install --upgrade edge-tts gTTS google-cloud-texttospeech >/dev/null 2>&1 || \
    log "não consegui instalar dependências TTS automaticamente; o benchmark vai mostrar o erro curto"
}

ensure_tts_benchmark_deps_if_needed

count="$(worker_pid_count)"
if health_ok && [[ "$count" -le 1 ]]; then
  running_ver="$(running_version)"
  file_ver="$(file_version)"
  if [[ -n "$running_ver" && -n "$file_ver" ]] && version_lt "$running_ver" "$file_ver"; then
    log "worker online está desatualizado; runtime=$running_ver arquivo=$file_ver; reiniciando"
    write_status "restart_for_update runtime=$running_ver file=$file_ver $(now_iso)"
    kill_worker_processes
  else
    log "worker já está online; pid(s)=$count"
    write_status "ok already_online $(now_iso)"
    exit 0
  fi
fi

if [[ "$KILL_DUPLICATES" != "false" ]]; then
  log "limpando processos antigos/duplicados do phone-worker"
  kill_worker_processes
fi

rm -f "$PID_FILE" 2>/dev/null || true
rotate_log_if_needed

log "iniciando worker em $HOST:$PORT"
(
  cd "$WORKER_DIR" || exit 1
  exec "$PYTHON_BIN" phone_worker.py --host "$HOST" --port "$PORT"
) >> "$LOG_FILE" 2>&1 &
child_pid=$!
printf '%s\n' "$child_pid" > "$PID_FILE" 2>/dev/null || true
write_status "starting pid=$child_pid $(now_iso)"

sleep "$START_WAIT"

if health_ok; then
  log "worker iniciado com sucesso; pid=$child_pid"
  write_status "ok pid=$child_pid $(now_iso)"
  exit 0
fi

if kill -0 "$child_pid" 2>/dev/null; then
  log "processo iniciou, mas health ainda não respondeu; pid=$child_pid"
  write_status "starting_health_pending pid=$child_pid $(now_iso)"
  exit 0
fi

log "falha ao iniciar worker. Veja: tail -n 80 '$LOG_FILE'"
write_status "failed pid=$child_pid $(now_iso)"
exit 1
