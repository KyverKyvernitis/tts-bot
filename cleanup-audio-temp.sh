#!/usr/bin/env bash
set -Eeuo pipefail

AUDIO_TMP_DIR="/home/ubuntu/bot/tmp_audio"
LOG_FILE="/home/ubuntu/bot/cleanup-audio-temp.log"
MAX_BYTES=$((500 * 1024 * 1024))

mkdir -p "$AUDIO_TMP_DIR"
chmod 700 "$AUDIO_TMP_DIR"

log() {
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] $1" >> "$LOG_FILE"
}

current_size=$(du -sb "$AUDIO_TMP_DIR" 2>/dev/null | awk '{print $1}')
current_size=${current_size:-0}

if [ "$current_size" -gt "$MAX_BYTES" ]; then
  log "tmp_audio acima do limite: ${current_size} bytes"

  while [ "$current_size" -gt "$MAX_BYTES" ]; do
    oldest_file=$(find "$AUDIO_TMP_DIR" -type f | xargs -r ls -1tr 2>/dev/null | head -n 1)
    [ -z "$oldest_file" ] && break

    rm -f "$oldest_file"
    current_size=$(du -sb "$AUDIO_TMP_DIR" 2>/dev/null | awk '{print $1}')
    current_size=${current_size:-0}
  done

  log "tmp_audio reduzido para ${current_size} bytes"
fi
