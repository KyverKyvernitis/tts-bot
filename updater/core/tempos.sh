#!/usr/bin/env bash
# Módulo carregado por atualizar.sh; não executar diretamente.
# Formatação e coleta de tempos.

human_duration() {
  local total="${1:-0}"
  local m=$((total / 60))
  local s=$((total % 60))
  if (( m > 0 )); then
    printf "%dmin %02ds" "$m" "$s"
  else
    printf "%ds" "$s"
  fi
}

update_now_ms() {
  local value
  value="$(date +%s%3N 2>/dev/null || true)"
  if [[ "$value" =~ ^[0-9]{13}$ ]]; then
    printf '%s' "$value"
    return 0
  fi
  python3 - <<'PYMS'
import time
print(time.time_ns() // 1_000_000)
PYMS
}
UPDATER_PROCESS_STARTED_MS="$(update_now_ms)"


format_update_duration_ms() {
  local total_ms="${1:-0}"
  [[ "$total_ms" =~ ^[0-9]+$ ]] || total_ms=0
  if (( total_ms <= 0 )); then
    printf '<1ms'
    return 0
  fi
  if (( total_ms < 1000 )); then
    printf '%dms' "$total_ms"
    return 0
  fi
  if (( total_ms < 60000 )); then
    local tenths=$(((total_ms + 50) / 100))
    local seconds=$((tenths / 10))
    local decimal=$((tenths % 10))
    if (( decimal == 0 )); then
      printf '%ds' "$seconds"
    else
      printf '%d,%ds' "$seconds" "$decimal"
    fi
    return 0
  fi
  local rounded_seconds=$(((total_ms + 500) / 1000))
  local minutes=$((rounded_seconds / 60))
  local seconds=$((rounded_seconds % 60))
  printf '%dmin %02ds' "$minutes" "$seconds"
}

format_update_file_count() {
  local count="${1:-0}"
  [[ "$count" =~ ^[0-9]+$ ]] || count=0
  if (( count == 1 )); then
    printf '1 arquivo'
  else
    printf '%d arquivos' "$count"
  fi
}

mark_update_timing() {
  local label="${1:-etapa}"
  local now="$SECONDS"
  local delta=$((now - UPDATER_STEP_LAST))
  UPDATER_STEP_LAST="$now"
  if [[ -n "$UPDATER_TIMINGS" ]]; then
    UPDATER_TIMINGS+=", "
  fi
  UPDATER_TIMINGS+="${label}=${delta}s"
  logger -t "$LOG_TAG" "timing ${label}=${delta}s total=${now}s"
}

append_update_timing_ms() {
  local label="${1:-etapa}" elapsed_ms="${2:-0}" elapsed_text
  [[ "$elapsed_ms" =~ ^[0-9]+$ ]] || elapsed_ms=0
  elapsed_text="$(format_update_duration_ms "$elapsed_ms")"
  if [[ -n "$UPDATER_TIMINGS" ]]; then
    UPDATER_TIMINGS+=", "
  fi
  UPDATER_TIMINGS+="${label}=${elapsed_text}"
  logger -t "$LOG_TAG" "timing ${label}=${elapsed_ms}ms" 2>/dev/null || true
}

log_update_operation_timing_ms() {
  local label="${1:-operação}" start_ms="${2:-0}" end_ms elapsed_ms elapsed_text
  end_ms="$(update_now_ms)"
  [[ "$start_ms" =~ ^[0-9]+$ ]] || start_ms="$end_ms"
  elapsed_ms=$((end_ms - start_ms))
  (( elapsed_ms < 0 )) && elapsed_ms=0
  elapsed_text="$(format_update_duration_ms "$elapsed_ms")"
  printf '[timing] %s=%s (%sms)\n' "$label" "$elapsed_text" "$elapsed_ms"
  logger -t "$LOG_TAG" "timing ${label}=${elapsed_ms}ms" 2>/dev/null || true
}

