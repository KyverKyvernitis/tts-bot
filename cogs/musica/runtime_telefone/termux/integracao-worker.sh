#!/data/data/com.termux/files/usr/bin/bash
# Integração do domínio de música com o supervisor genérico do Phone Worker.
# Este arquivo é carregado por start-phone-worker.sh quando o módulo de música
# está presente na release ativa.

MUSIC_AGENT_ENV_FILE="${MUSIC_AGENT_ENV:-$WORKER_DIR/secrets/music-agent.env}"
if [[ -f "$MUSIC_AGENT_ENV_FILE" ]]; then
  set -a
  # shellcheck disable=SC1090
  source "$MUSIC_AGENT_ENV_FILE"
  set +a
fi
MUSIC_AGENT_AUTO_START="${PHONE_WORKER_START_MUSIC_AGENT:-${MUSIC_AGENT_ENABLED:-auto}}"
MUSIC_AGENT_START_COMMAND_EXPLICIT=0
if [[ -n "${MUSIC_AGENT_START_COMMAND:-}" ]]; then
  MUSIC_AGENT_START_COMMAND_EXPLICIT=1
fi

musica_ensure_worker_env_if_needed() {
  is_turbo_profile || return 0
  for role in music music-ytdlp music-agent; do
    append_csv_env_value CORE_WORKER_ROLES "$role"
  done
  for capability in music music-ytdlp music-ytdlp-resolve music-agent music-agent-control music-voice; do
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

musica_cleanup_safe_mode() {
  safe_mode_enabled || return 0
  truthy "${PHONE_WORKER_KEEP_HEAVY_SERVICES_IN_SAFE_MODE:-false}" && return 0
  command -v pkill >/dev/null 2>&1 || return 0
  log "modo seguro ativo; encerrando Music Agent opcional"
  pkill -f '[c]ogs\.musica\.runtime_telefone\.agente\.servidor' 2>/dev/null || true
  # Compatibilidade de limpeza para releases anteriores à concentração em cogs.
  pkill -f '[m]usic_agent.py' 2>/dev/null || true
}

musica_ensure_ytdlp_deps_if_needed() {
  safe_pip_install_module "yt-dlp" "yt_dlp" "yt-dlp" light || true
  safe_pip_install_module "yt-dlp-ejs" "yt_dlp_ejs" "yt-dlp-ejs" light || true
}

musica_ensure_agent_deps_if_needed() {
  autostart_enabled "$MUSIC_AGENT_AUTO_START" || { log "Music Agent não será iniciado automaticamente (modo seguro/auto-start off)"; return 0; }
  safe_pip_install_module "aiohttp" "aiohttp" "aiohttp" light || true
  safe_pip_install_module "discord.py" "discord" "discord.py>=2.7.1,<2.8" light || true
  safe_pip_install_module "PyNaCl" "nacl" "PyNaCl" light || true
  safe_pip_install_module "davey" "davey" "davey" light || true
  safe_pip_install_module "yt-dlp" "yt_dlp" "yt-dlp" light || true
  safe_pip_install_module "edge-tts" "edge_tts" "edge-tts==7.2.8" light || true
  safe_pip_install_module "gTTS" "gtts" "gTTS==2.5.4" light || true
  "$PYTHON_BIN" - <<'PYMUSICAGENTCHECK' >/dev/null 2>&1 && log "dependências do Music Agent prontas" || log "Music Agent ainda possui dependências ausentes; será reportado no health"
import aiohttp, discord, nacl, yt_dlp, davey, edge_tts, gtts  # noqa: F401
PYMUSICAGENTCHECK
}

musica_active_agent_start_command() {
  if [[ "$MUSIC_AGENT_START_COMMAND_EXPLICIT" == "1" && -x "${MUSIC_AGENT_START_COMMAND:-}" ]]; then
    printf '%s\n' "$MUSIC_AGENT_START_COMMAND"
    return 0
  fi
  local release candidate
  release="$(active_release_dir)"
  for candidate in \
    "$release/cogs/musica/runtime_telefone/termux/iniciar-agente-musica.sh" \
    "$WORKER_DIR/cogs/musica/runtime_telefone/termux/iniciar-agente-musica.sh"; do
    if [[ -x "$candidate" ]]; then
      printf '%s\n' "$candidate"
      return 0
    fi
  done
  printf '%s\n' "$release/cogs/musica/runtime_telefone/termux/iniciar-agente-musica.sh"
}

musica_ensure_agent_if_needed() {
  autostart_enabled "$MUSIC_AGENT_AUTO_START" || { log "Music Agent não será iniciado automaticamente (modo seguro/auto-start off)"; return 0; }
  local release start_command
  release="$(active_release_dir)"
  start_command="$(musica_active_agent_start_command)"
  if [[ ! -x "$start_command" ]]; then
    log "start do Music Agent não encontrado em $start_command"
    return 0
  fi
  if [[ -z "${MUSIC_AGENT_BOT_TOKEN:-${DISCORD_TOKEN:-${BOT_TOKEN:-}}}" ]]; then
    log "Music Agent habilitado, mas token do bot não está configurado no worker"
    return 0
  fi
  if [[ -z "${MUSIC_AGENT_TOKEN:-}" && -n "${PHONE_WORKER_TOKEN:-}" ]]; then
    upsert_env_value MUSIC_AGENT_TOKEN "$PHONE_WORKER_TOKEN"
  fi
  log "garantindo Music Agent do worker; release=$release"
  PHONE_WORKER_RELEASE_DIR="$release" \
  PHONE_WORKER_DIR="$WORKER_DIR" \
  MUSIC_AGENT_ENV="$MUSIC_AGENT_ENV_FILE" \
    "$start_command" >/dev/null 2>&1 || \
    log "não consegui iniciar Music Agent automaticamente; música direta no worker pode ficar indisponível"
}
