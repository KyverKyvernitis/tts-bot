#!/usr/bin/env bash
set -Eeuo pipefail

BOT_DIR="${BOT_DIR:-/home/ubuntu/bot}"
AUDIO_TMP_DIR="${AUDIO_TMP_DIR:-$BOT_DIR/tmp_audio}"
RUNTIME_DIR="${RUNTIME_DIR:-$AUDIO_TMP_DIR/runtime}"
CACHE_DIR="${CACHE_DIR:-$AUDIO_TMP_DIR/cache}"
CREDENTIALS_DIR="${CREDENTIALS_DIR:-$AUDIO_TMP_DIR/credentials}"
LOG_FILE="${LOG_FILE:-$BOT_DIR/cleanup-audio-temp.log}"

# Limites conservadores para VPS pequena: evita tmp_audio crescer sem apagar
# arquivos recém-criados que ainda podem estar tocando.
RUNTIME_MAX_AGE_MINUTES="${RUNTIME_MAX_AGE_MINUTES:-360}"      # 6h
CACHE_MAX_AGE_MINUTES="${CACHE_MAX_AGE_MINUTES:-10080}"        # 7 dias
MAX_BYTES="${MAX_BYTES:-134217728}"                            # 128 MiB total
CACHE_MAX_BYTES="${CACHE_MAX_BYTES:-100663296}"                # 96 MiB cache
LOG_MAX_BYTES="${LOG_MAX_BYTES:-262144}"                       # 256 KiB

ensure_required_dirs() {
  mkdir -p "$AUDIO_TMP_DIR" "$RUNTIME_DIR" "$CACHE_DIR" "$CREDENTIALS_DIR"
  chmod 700 "$AUDIO_TMP_DIR" "$RUNTIME_DIR" "$CACHE_DIR" "$CREDENTIALS_DIR" 2>/dev/null || true
}

ensure_required_dirs

rotate_log() {
  if [[ -f "$LOG_FILE" ]]; then
    local size
    size=$(stat -c '%s' "$LOG_FILE" 2>/dev/null || echo 0)
    if [[ "$size" -gt "$LOG_MAX_BYTES" ]]; then
      mv -f "$LOG_FILE" "$LOG_FILE.1" 2>/dev/null || true
    fi
  fi
}

log() {
  rotate_log
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] $1" >> "$LOG_FILE" 2>/dev/null || true
}

dir_size() {
  du -sb "$1" 2>/dev/null | awk '{print $1}' || echo 0
}

delete_old_files() {
  local dir="$1"
  local max_age="$2"
  local label="$3"
  [[ -d "$dir" ]] || return 0

  local deleted
  deleted=$(find "$dir" -type f -mmin +"$max_age" -print -delete 2>/dev/null | wc -l | tr -d ' ')
  if [[ "${deleted:-0}" -gt 0 ]]; then
    log "${label}: ${deleted} arquivo(s) antigo(s) removido(s)"
  fi
}

trim_dir_to_limit() {
  local dir="$1"
  local limit="$2"
  local label="$3"
  [[ -d "$dir" ]] || return 0

  local current_size
  current_size=$(dir_size "$dir")
  current_size=${current_size:-0}

  if [[ "$current_size" -le "$limit" ]]; then
    return 0
  fi

  log "${label}: acima do limite (${current_size} bytes > ${limit} bytes)"

  while [[ "$current_size" -gt "$limit" ]]; do
    local oldest_file
    oldest_file=$(find "$dir" -type f -printf '%T@ %p\n' 2>/dev/null | sort -n | head -n 1 | cut -d' ' -f2-)
    [[ -n "$oldest_file" ]] || break
    rm -f "$oldest_file" 2>/dev/null || break
    current_size=$(dir_size "$dir")
    current_size=${current_size:-0}
  done

  log "${label}: reduzido para ${current_size} bytes"
}

# Arquivos de runtime: podem ser removidos mais cedo, mas a pasta runtime
# precisa continuar existindo para tempfile.mkstemp(dir=runtime) no TTS.
if [[ -d "$RUNTIME_DIR" ]]; then
  find "$RUNTIME_DIR" -type f -mmin +"$RUNTIME_MAX_AGE_MINUTES" -print -delete 2>/dev/null | {
    count=$(wc -l | tr -d ' ')
    if [[ "${count:-0}" -gt 0 ]]; then
      log "runtime: ${count} arquivo(s) antigo(s) removido(s)"
    fi
  }
fi

# Cache pode ficar mais tempo, mas não pode crescer sem limite.
delete_old_files "$CACHE_DIR" "$CACHE_MAX_AGE_MINUTES" "cache"
trim_dir_to_limit "$CACHE_DIR" "$CACHE_MAX_BYTES" "cache"
trim_dir_to_limit "$AUDIO_TMP_DIR" "$MAX_BYTES" "tmp_audio"

# Não delete as pastas estruturais usadas pelo TTS, mesmo se vazias.
find "$AUDIO_TMP_DIR" -type d -empty \
  ! -path "$AUDIO_TMP_DIR" \
  ! -path "$RUNTIME_DIR" \
  ! -path "$CACHE_DIR" \
  ! -path "$CREDENTIALS_DIR" \
  -delete 2>/dev/null || true
ensure_required_dirs
