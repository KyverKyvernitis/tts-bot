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
MUSIC_AGENT_ENV_FILE="${MUSIC_AGENT_ENV:-$WORKER_DIR/secrets/music-agent.env}"
if [[ -f "$MUSIC_AGENT_ENV_FILE" ]]; then
  set -a
  # shellcheck disable=SC1090
  source "$MUSIC_AGENT_ENV_FILE"
  set +a
fi
PYTHON_BIN="${PHONE_WORKER_PYTHON:-python}"
HOST="${MUSIC_AGENT_HOST:-127.0.0.1}"
PORT="${MUSIC_AGENT_PORT:-8780}"
TOKEN="${MUSIC_AGENT_TOKEN:-}"
if [[ -z "$TOKEN" ]]; then
  mkdir -p "$(dirname "$MUSIC_AGENT_ENV_FILE")"
  if command -v python >/dev/null 2>&1; then
    TOKEN="$(python - <<'PYTOKEN'
import secrets
print(secrets.token_urlsafe(32))
PYTOKEN
)"
  else
    TOKEN="$(date +%s%N)-$RANDOM-$RANDOM"
  fi
  if grep -qE '^MUSIC_AGENT_TOKEN=' "$MUSIC_AGENT_ENV_FILE" 2>/dev/null; then
    tmp_file="${MUSIC_AGENT_ENV_FILE}.tmp"
    sed -E "s|^MUSIC_AGENT_TOKEN=.*|MUSIC_AGENT_TOKEN=$TOKEN|" "$MUSIC_AGENT_ENV_FILE" > "$tmp_file" && mv "$tmp_file" "$MUSIC_AGENT_ENV_FILE"
  else
    printf '\nMUSIC_AGENT_TOKEN=%s\n' "$TOKEN" >> "$MUSIC_AGENT_ENV_FILE"
  fi
  chmod 600 "$MUSIC_AGENT_ENV_FILE" 2>/dev/null || true
  export MUSIC_AGENT_TOKEN="$TOKEN"
fi
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

falsey() {
  local value="${1:-}"
  value="$(printf '%s' "$value" | tr '[:upper:]' '[:lower:]' | tr -d ' \t\r\n"' | tr -d "'")"
  [[ "$value" == "0" || "$value" == "false" || "$value" == "no" || "$value" == "n" || "$value" == "off" || "$value" == "nao" || "$value" == "não" ]]
}

agent_deps_install_enabled() {
  local mode="${MUSIC_AGENT_DEPS_INSTALL_MODE:-${PHONE_WORKER_TURBO_DEPS_INSTALL_MODE:-${PHONE_WORKER_DEPS_INSTALL_MODE:-auto}}}"
  falsey "$mode" && return 1
  return 0
}

heavy_python_deps_enabled() {
  local mode="${MUSIC_AGENT_HEAVY_PYTHON_DEPS_INSTALL:-${PHONE_WORKER_HEAVY_PYTHON_DEPS_INSTALL:-false}}"
  truthy "$mode"
}

cleanup_stale_heavy_dependency_builds() {
  truthy "${MUSIC_AGENT_KILL_STALE_HEAVY_DEP_BUILDS:-${PHONE_WORKER_KILL_STALE_HEAVY_DEP_BUILDS:-true}}" || return 0
  heavy_python_deps_enabled && return 0
  command -v ps >/dev/null 2>&1 || return 0
  ps -ef 2>/dev/null | grep -Ei 'google-cloud-texttospeech|grpcio|GRPC_XDS|pyb/temp\.android|pip/_vendor/pyproject_hooks|build_wheel' | grep -v grep | awk '{print $2}' | while read -r pid; do
    case "$pid" in ''|*[!0-9]*) continue ;; esac
    [[ "$pid" == "$$" ]] && continue
    log "encerrando build pesado opcional preso; pid=$pid"
    kill "$pid" 2>/dev/null || true
  done
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
  "$PYTHON_BIN" - "$WORKER_DIR/music_agent.py" <<'PYVER' 2>/dev/null || true
import re, sys
try:
    text=open(sys.argv[1], encoding="utf-8", errors="ignore").read()
except Exception:
    text=""
m=re.search(r'^AGENT_VERSION\s*=\s*["\']([^"\']+)["\']', text, re.M)
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

ensure_termux_packages() {
  truthy "${MUSIC_AGENT_AUTO_INSTALL_TERMUX_PACKAGES:-true}" || return 0
  command -v pkg >/dev/null 2>&1 || return 0
  local missing=()
  for bin in ffmpeg pkg-config clang make rustc; do
    command -v "$bin" >/dev/null 2>&1 || missing+=("$bin")
  done
  if [[ ${#missing[@]} -eq 0 ]]; then
    return 0
  fi
  log "pacotes Termux de voz ausentes (${missing[*]}); instalando dependências essenciais"
  pkg install -y python clang make pkg-config libffi libsodium openssl rust ffmpeg >/dev/null 2>&1 || \
    log "não consegui instalar todos os pacotes Termux automaticamente"
}

ensure_deps() {
  agent_deps_install_enabled || {
    log "auto-install de dependências do Music Agent desativado por env"
    return 0
  }
  ensure_termux_packages
  "$PYTHON_BIN" - <<'PYDEPS' >/dev/null 2>&1 && return 0
import aiohttp, discord, nacl, davey, wavelink, yt_dlp, gtts, edge_tts  # noqa: F401
PYDEPS
  log "dependências leves do Music Agent/TTS ausentes; instalando sem Google Cloud TTS/grpcio"
  local pip_cmd=("$PYTHON_BIN" -m pip install --upgrade aiohttp 'discord.py>=2.7.1,<2.8' PyNaCl davey 'wavelink>=3.4,<3.6' 'yt-dlp[default]' gTTS edge-tts)
  if command -v timeout >/dev/null 2>&1; then
    timeout "${MUSIC_AGENT_DEPS_INSTALL_TIMEOUT_SECONDS:-600}" "${pip_cmd[@]}" >/dev/null 2>&1 || \
      log "não consegui instalar todas as dependências leves automaticamente dentro do timeout"
  else
    "${pip_cmd[@]}" >/dev/null 2>&1 || \
      log "não consegui instalar todas as dependências leves automaticamente"
  fi
  if heavy_python_deps_enabled; then
    "$PYTHON_BIN" - <<'PYGCLOUD' >/dev/null 2>&1 || {
import google.cloud.texttospeech_v1  # noqa: F401
PYGCLOUD
      log "Google Cloud TTS/grpcio ausente; instalando porque heavy deps foram ativadas explicitamente"
      local heavy_cmd=("$PYTHON_BIN" -m pip install --upgrade google-cloud-texttospeech)
      if command -v timeout >/dev/null 2>&1; then
        timeout "${MUSIC_AGENT_HEAVY_DEPS_INSTALL_TIMEOUT_SECONDS:-600}" "${heavy_cmd[@]}" >/dev/null 2>&1 || \
          log "não consegui instalar Google Cloud TTS automaticamente dentro do timeout"
      else
        "${heavy_cmd[@]}" >/dev/null 2>&1 || \
          log "não consegui instalar Google Cloud TTS automaticamente"
      fi
    }
  fi
}

mkdir -p "$(dirname "$LOG_FILE")"
cleanup_stale_heavy_dependency_builds

if health_ok; then
  running_ver="$(running_version)"
  file_ver="$(file_version)"
  if [[ -n "$running_ver" && -n "$file_ver" ]] && version_lt "$running_ver" "$file_ver"; then
    log "Music Agent online está desatualizado; runtime=$running_ver arquivo=$file_ver; reiniciando"
    kill_agent
  else
    log "Music Agent já está online em $HOST:$PORT versão=${running_ver:-?}"
    exit 0
  fi
fi

# Só verifica/instala dependências quando o Music Agent não está online ou será reiniciado.
# Isso evita que cada watchdog/start tente pip install e trave o celular mesmo com o agente saudável.
ensure_deps

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
