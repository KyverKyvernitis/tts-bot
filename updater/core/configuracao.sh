#!/usr/bin/env bash
# Módulo carregado por atualizar.sh; não executar diretamente.
# Configuração de prioridade e recursos do processo.

UPDATER_NICE_LEVEL="${TTS_BOT_UPDATER_NICE_LEVEL:-10}"
UPDATER_IONICE_CLASS="${TTS_BOT_UPDATER_IONICE_CLASS:-2}"
UPDATER_IONICE_LEVEL="${TTS_BOT_UPDATER_IONICE_LEVEL:-7}"
UPDATER_FAST_NICE_LEVEL="${TTS_BOT_UPDATER_FAST_NICE_LEVEL:-5}"
UPDATER_FAST_IONICE_CLASS="${TTS_BOT_UPDATER_FAST_IONICE_CLASS:-2}"
UPDATER_FAST_IONICE_LEVEL="${TTS_BOT_UPDATER_FAST_IONICE_LEVEL:-4}"
UPDATER_PRIORITY_PROFILE=""

set_updater_priority_profile() {
  local profile="${1:-safe}" nice_level ionice_class ionice_level failed=0
  case "$profile" in
    fast)
      nice_level="$UPDATER_FAST_NICE_LEVEL"
      ionice_class="$UPDATER_FAST_IONICE_CLASS"
      ionice_level="$UPDATER_FAST_IONICE_LEVEL"
      ;;
    safe)
      nice_level="$UPDATER_NICE_LEVEL"
      ionice_class="$UPDATER_IONICE_CLASS"
      ionice_level="$UPDATER_IONICE_LEVEL"
      ;;
    *)
      return 2
      ;;
  esac

  [[ "${UPDATER_PRIORITY_PROFILE:-}" == "$profile" ]] && return 0
  renice -n "$nice_level" -p "$$" >/dev/null 2>&1 || failed=1
  if command -v ionice >/dev/null 2>&1; then
    ionice -c "$ionice_class" -n "$ionice_level" -p "$$" >/dev/null 2>&1 || failed=1
  fi
  UPDATER_PRIORITY_PROFILE="$profile"
  if (( failed == 1 )); then
    logger -t "$LOG_TAG" "perfil de prioridade $profile aplicado parcialmente (nice=$nice_level ionice=$ionice_class:$ionice_level)" 2>/dev/null || true
  else
    logger -t "$LOG_TAG" "perfil de prioridade $profile (nice=$nice_level ionice=$ionice_class:$ionice_level)" 2>/dev/null || true
  fi
  return 0
}

set_updater_priority_profile safe
