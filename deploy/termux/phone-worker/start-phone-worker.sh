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
MUSIC_LAVALINK_AUTO_START="${PHONE_WORKER_START_LAVALINK:-${PHONE_LAVALINK_AUTO_START:-true}}"
PHONE_LAVALINK_START_COMMAND="${PHONE_LAVALINK_START_COMMAND:-$HOME/start-phone-lavalink.sh}"
MUSIC_AGENT_AUTO_START="${PHONE_WORKER_START_MUSIC_AGENT:-${MUSIC_AGENT_ENABLED:-false}}"
MUSIC_AGENT_START_COMMAND="${MUSIC_AGENT_START_COMMAND:-$WORKER_DIR/start-phone-music-agent.sh}"

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

upsert_env_value() {
  local key="$1"
  local value="$2"
  mkdir -p "$(dirname "$ENV_FILE")"
  if [[ -f "$ENV_FILE" ]] && grep -qE "^${key}=" "$ENV_FILE" 2>/dev/null; then
    local tmp="${ENV_FILE}.tmp.$$"
    awk -v k="$key" -v v="$value" 'BEGIN{done=0} $0 ~ "^" k "=" {print k "=" v; done=1; next} {print} END{if(!done) print k "=" v}' "$ENV_FILE" > "$tmp" && mv -f "$tmp" "$ENV_FILE"
  else
    printf '\n%s=%s\n' "$key" "$value" >> "$ENV_FILE"
  fi
  export "$key=$value"
}

append_csv_env_value() {
  local key="$1"
  local item="$2"
  local current="${!key:-}"
  local normalized
  normalized="$(printf '%s' "$current" | tr ',' '\n' | sed 's/^ *//;s/ *$//' | grep -Fx "$item" || true)"
  if [[ -n "$normalized" ]]; then
    return 0
  fi
  if [[ -n "$current" ]]; then
    upsert_env_value "$key" "${current},${item}"
  else
    upsert_env_value "$key" "$item"
  fi
}

ensure_music_worker_env_if_needed() {
  is_turbo_profile || return 0
  for role in music music-node music-lavalink music-ytdlp music-agent; do
    append_csv_env_value CORE_WORKER_ROLES "$role"
  done
  for capability in music music-node music-lavalink music-ytdlp music-ytdlp-resolve music-agent music-agent-control; do
    append_csv_env_value CORE_WORKER_CAPABILITIES "$capability"
  done
  local cookies="${PHONE_WORKER_MUSIC_YTDLP_COOKIES_FILE:-${MUSIC_WORKER_YTDLP_COOKIES_FILE:-}}"
  if [[ -z "$cookies" ]]; then
    cookies="$WORKER_DIR/secrets/youtube-cookies.txt"
  fi
  if [[ -s "$cookies" ]]; then
    upsert_env_value PHONE_WORKER_MUSIC_YTDLP_COOKIES_FILE "$cookies"
    upsert_env_value MUSIC_WORKER_YTDLP_COOKIES_FILE "$cookies"
    log "perfil turbo: cookies yt-dlp do worker configurados"
  else
    mkdir -p "$(dirname "$cookies")" 2>/dev/null || true
    log "perfil turbo: cookies yt-dlp do worker não encontrados em $cookies; worker tentará sem cookies"
  fi
  upsert_env_value PHONE_WORKER_MUSIC_YTDLP_JS_RUNTIMES "${PHONE_WORKER_MUSIC_YTDLP_JS_RUNTIMES:-node}"
  upsert_env_value MUSIC_WORKER_YTDLP_JS_RUNTIMES "${MUSIC_WORKER_YTDLP_JS_RUNTIMES:-node}"
  upsert_env_value PHONE_WORKER_MUSIC_YTDLP_DEFAULT_SEARCH "${PHONE_WORKER_MUSIC_YTDLP_DEFAULT_SEARCH:-ytsearch}"
  upsert_env_value MUSIC_WORKER_YTDLP_DEFAULT_SEARCH "${MUSIC_WORKER_YTDLP_DEFAULT_SEARCH:-ytsearch}"
}

is_turbo_profile() {
  local profile="${CORE_WORKER_PROFILE:-${PHONE_WORKER_PROFILE:-}}"
  profile="$(printf '%s' "$profile" | tr '[:upper:]' '[:lower:]' | tr -d ' \t\r\n"' | tr -d "'")"
  [[ "$profile" == "turbo" ]]
}

deps_install_enabled() {
  local mode="${PHONE_WORKER_TURBO_DEPS_INSTALL:-${PHONE_WORKER_TTS_DEPS_INSTALL:-auto}}"
  mode="$(printf '%s' "$mode" | tr '[:upper:]' '[:lower:]' | tr -d ' \t\r\n"' | tr -d "'")"
  [[ "$mode" == "0" || "$mode" == "false" || "$mode" == "off" || "$mode" == "no" ]] && return 1
  return 0
}

ensure_turbo_termux_packages_if_needed() {
  is_turbo_profile || return 0
  deps_install_enabled || return 0
  command -v pkg >/dev/null 2>&1 || return 0
  local missing=()
  command -v curl >/dev/null 2>&1 || missing+=(curl)
  command -v wget >/dev/null 2>&1 || missing+=(wget)
  command -v ffmpeg >/dev/null 2>&1 || missing+=(ffmpeg)
  command -v ffprobe >/dev/null 2>&1 || missing+=(ffmpeg)
  command -v node >/dev/null 2>&1 || missing+=(nodejs)
  command -v sox >/dev/null 2>&1 || missing+=(sox)
  command -v espeak >/dev/null 2>&1 || missing+=(espeak)
  if [[ "${#missing[@]}" -gt 0 ]]; then
    log "perfil turbo: instalando pacote(s) Termux ausentes: ${missing[*]}"
    pkg install -y "${missing[@]}" >/dev/null 2>&1 || \
      log "não consegui instalar todos os pacotes turbo; capabilities dependentes podem falhar"
  fi
}

ensure_turbo_python_tts_deps_if_needed() {
  is_turbo_profile || return 0
  deps_install_enabled || return 0
  "$PYTHON_BIN" - <<'PYTTSDEPS' >/dev/null 2>&1 && return 0
import edge_tts  # noqa: F401
import gtts  # noqa: F401
PYTTSDEPS
  log "perfil turbo: instalando dependências leves do TTS (edge/gTTS)"
  "$PYTHON_BIN" -m pip install --upgrade edge-tts gTTS >/dev/null 2>&1 || \
    log "não consegui instalar edge/gTTS automaticamente; o benchmark vai mostrar erro curto"
}

ensure_music_ytdlp_deps_if_needed() {
  is_turbo_profile || return 0
  deps_install_enabled || return 0
  "$PYTHON_BIN" - <<'PYYTDLPDEPS' >/dev/null 2>&1 && return 0
import yt_dlp  # noqa: F401
import yt_dlp_ejs  # noqa: F401
PYYTDLPDEPS
  log "perfil turbo: instalando suporte yt-dlp/EJS para música"
  "$PYTHON_BIN" -m pip install --upgrade "yt-dlp[default]" >/dev/null 2>&1 || \
    log "não consegui instalar yt-dlp[default] automaticamente; música YouTube pode falhar no challenge"
}

ensure_music_agent_deps_if_needed() {
  is_turbo_profile || return 0
  deps_install_enabled || return 0
  truthy "$MUSIC_AGENT_AUTO_START" || return 0
  "$PYTHON_BIN" - <<'PYMUSICAGENTDEPS' >/dev/null 2>&1 && return 0
import aiohttp, discord, wavelink, yt_dlp  # noqa: F401
PYMUSICAGENTDEPS
  log "perfil turbo: instalando dependências do Music Agent"
  "$PYTHON_BIN" -m pip install --upgrade aiohttp 'discord.py[voice]>=2.7.1,<2.8' 'wavelink>=3.4,<3.6' 'yt-dlp[default]' >/dev/null 2>&1 || \
    log "não consegui instalar dependências do Music Agent automaticamente"
}

ensure_turbo_piper_cli_if_needed() {
  is_turbo_profile || return 0
  deps_install_enabled || return 0
  if command -v piper >/dev/null 2>&1; then
    return 0
  fi
  if [[ -n "${PHONE_WORKER_PIPER_COMMAND:-}" && -x "${PHONE_WORKER_PIPER_COMMAND:-}" ]] && command -v piper >/dev/null 2>&1; then
    return 0
  fi
  command -v apt >/dev/null 2>&1 || return 0
  command -v wget >/dev/null 2>&1 || return 0
  log "perfil turbo: piper CLI ausente; tentando instalar pacote .deb do Piper para Termux"
  local tmpdir="${TMPDIR:-/tmp}/piper-termux-install"
  mkdir -p "$tmpdir"
  local deb_url="${PHONE_WORKER_PIPER_DEB_URL:-}"
  if [[ -z "$deb_url" ]]; then
    deb_url="$($PYTHON_BIN - <<'PYPIPERDEB' 2>/dev/null || true
import json, urllib.request
url='https://api.github.com/repos/gyroing/piper-tts-for-termux/releases/latest'
data=json.load(urllib.request.urlopen(url, timeout=20))
for a in data.get('assets') or []:
    u=str(a.get('browser_download_url') or '')
    if u.endswith('.deb'):
        print(u)
        break
PYPIPERDEB
)"
  fi
  if [[ -z "$deb_url" ]]; then
    log "não encontrei URL .deb do Piper; configure PHONE_WORKER_PIPER_DEB_URL se quiser auto-instalar"
    return 0
  fi
  wget -q -O "$tmpdir/piper-termux.deb" "$deb_url" && \
    apt install -y "$tmpdir/piper-termux.deb" >/dev/null 2>&1 || \
    log "não consegui instalar Piper .deb automaticamente"
}

ensure_turbo_piper_model_if_needed() {
  is_turbo_profile || return 0
  deps_install_enabled || return 0
  local auto_model="${PHONE_WORKER_PIPER_MODEL_AUTO_DOWNLOAD:-true}"
  auto_model="$(printf '%s' "$auto_model" | tr '[:upper:]' '[:lower:]' | tr -d ' \t\r\n"' | tr -d "'")"
  [[ "$auto_model" == "0" || "$auto_model" == "false" || "$auto_model" == "off" || "$auto_model" == "no" ]] && return 0
  local model="${PHONE_WORKER_PIPER_MODEL:-$HOME/piper-models/pt_BR/edresson-low/pt_BR-edresson-low.onnx}"
  local config="${PHONE_WORKER_PIPER_CONFIG:-${model}.json}"
  local model_url="${PHONE_WORKER_PIPER_MODEL_URL:-https://huggingface.co/rhasspy/piper-voices/resolve/main/pt/pt_BR/edresson/low/pt_BR-edresson-low.onnx?download=true}"
  local config_url="${PHONE_WORKER_PIPER_CONFIG_URL:-https://huggingface.co/rhasspy/piper-voices/resolve/main/pt/pt_BR/edresson/low/pt_BR-edresson-low.onnx.json?download=true}"
  command -v wget >/dev/null 2>&1 || return 0
  mkdir -p "$(dirname "$model")"
  if [[ ! -s "$model" ]]; then
    log "perfil turbo: baixando modelo Piper padrão"
    wget -q -c -O "$model" "$model_url" || log "não consegui baixar modelo Piper padrão"
  fi
  if [[ ! -s "$config" ]]; then
    log "perfil turbo: baixando config do modelo Piper padrão"
    wget -q -c -O "$config" "$config_url" || log "não consegui baixar config Piper padrão"
  fi
  [[ -s "$model" ]] && upsert_env_value PHONE_WORKER_PIPER_MODEL "$model"
  [[ -s "$config" ]] && upsert_env_value PHONE_WORKER_PIPER_CONFIG "$config"
  if [[ -z "${PHONE_WORKER_PIPER_MODEL_NAME:-}" ]]; then
    upsert_env_value PHONE_WORKER_PIPER_MODEL_NAME "pt_BR-edresson-low"
  fi
}

ensure_turbo_piper_wrapper_if_needed() {
  is_turbo_profile || return 0
  deps_install_enabled || return 0
  local wrapper="$WORKER_DIR/bin/piper-worker"
  if [[ -x "$wrapper" ]]; then
    if [[ -z "${PHONE_WORKER_PIPER_COMMAND:-}" ]]; then
      upsert_env_value PHONE_WORKER_PIPER_COMMAND "$wrapper"
    fi
    return 0
  fi
  command -v piper >/dev/null 2>&1 || return 0
  command -v ffmpeg >/dev/null 2>&1 || return 0
  mkdir -p "$WORKER_DIR/bin"
  cat > "$wrapper" <<'PIPERWRAP'
#!/data/data/com.termux/files/usr/bin/bash
set -euo pipefail
MODEL=""
CONFIG=""
OUTPUT=""
while [ "$#" -gt 0 ]; do
  case "$1" in
    --model) MODEL="${2:-}"; shift 2 ;;
    --config) CONFIG="${2:-}"; shift 2 ;;
    --output_file|--output-file) OUTPUT="${2:-}"; shift 2 ;;
    --) shift; break ;;
    *) shift ;;
  esac
done
if [ -z "$MODEL" ] || [ ! -s "$MODEL" ]; then echo "modelo Piper inválido: $MODEL" >&2; exit 2; fi
if [ -z "$OUTPUT" ]; then echo "output_file não informado" >&2; exit 2; fi
VOICE_DIR="$(dirname "$MODEL")"
VOICE_NAME="$(basename "$MODEL")"
VOICE_NAME="${VOICE_NAME%.onnx}"
TEXT="$(cat)"
if [ -z "${TEXT// }" ]; then echo "texto vazio" >&2; exit 2; fi
RAW="$(mktemp "$TMPDIR/piper-raw-XXXXXX.raw")"
trap 'rm -f "$RAW"' EXIT
export PIPER_VOICE_PATH="$VOICE_DIR"
piper -m "$VOICE_NAME" -f "$RAW" -- "$TEXT"
ffmpeg -y -hide_banner -loglevel error -f f32le -ar 22050 -ac 1 -i "$RAW" "$OUTPUT"
PIPERWRAP
  chmod +x "$wrapper"
  upsert_env_value PHONE_WORKER_PIPER_COMMAND "$wrapper"
  log "perfil turbo: wrapper Piper criado em $wrapper"
}

ensure_lavalink_for_turbo_if_needed() {
  is_turbo_profile || return 0
  truthy "$MUSIC_LAVALINK_AUTO_START" || return 0
  if [[ ! -x "$PHONE_LAVALINK_START_COMMAND" ]]; then
    log "perfil turbo: start do Lavalink não encontrado em $PHONE_LAVALINK_START_COMMAND"
    return 0
  fi
  log "perfil turbo: garantindo Lavalink do worker"
  "$PHONE_LAVALINK_START_COMMAND" >/dev/null 2>&1 || \
    log "não consegui iniciar Lavalink automaticamente; música pode ficar indisponível"
}


ensure_music_agent_for_turbo_if_needed() {
  is_turbo_profile || return 0
  truthy "$MUSIC_AGENT_AUTO_START" || return 0
  if [[ ! -x "$MUSIC_AGENT_START_COMMAND" ]]; then
    log "perfil turbo: start do Music Agent não encontrado em $MUSIC_AGENT_START_COMMAND"
    return 0
  fi
  if [[ -z "${MUSIC_AGENT_BOT_TOKEN:-${DISCORD_TOKEN:-${BOT_TOKEN:-}}}" ]]; then
    log "perfil turbo: Music Agent habilitado, mas token do bot não está configurado no worker"
    return 0
  fi
  if [[ -z "${MUSIC_AGENT_TOKEN:-}" && -n "${PHONE_WORKER_TOKEN:-}" ]]; then
    upsert_env_value MUSIC_AGENT_TOKEN "$PHONE_WORKER_TOKEN"
  fi
  log "perfil turbo: garantindo Music Agent do worker"
  "$MUSIC_AGENT_START_COMMAND" >/dev/null 2>&1 || \
    log "não consegui iniciar Music Agent automaticamente; música direta no worker pode ficar indisponível"
}

ensure_turbo_deps_if_needed() {
  ensure_turbo_termux_packages_if_needed
  ensure_turbo_python_tts_deps_if_needed
  ensure_music_ytdlp_deps_if_needed
  ensure_music_agent_deps_if_needed
  ensure_turbo_piper_cli_if_needed
  ensure_turbo_piper_model_if_needed
  ensure_turbo_piper_wrapper_if_needed
}

ensure_music_worker_env_if_needed
ensure_turbo_deps_if_needed
ensure_lavalink_for_turbo_if_needed
ensure_music_agent_for_turbo_if_needed

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
