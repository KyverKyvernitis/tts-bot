#!/usr/bin/env bash
set -Eeuo pipefail

REPO_DIR="/home/ubuntu/bot"
BRANCH="main"
SERVICE="tts-bot"
CALLKEEPER_SERVICE="callkeeper"
LAVALINK_SERVICE="lavalink"
LOG_TAG="tts-bot-updater"
DIRTY_MARKER_FILE="$REPO_DIR/.fatal-update-dirty"
LOCAL_CHANGES_MARKER_FILE="$REPO_DIR/.fatal-update-local-changes"
CANDIDATE_ROOT="${DISCORD_AUTO_UPDATE_STAGING_DIR:-$(dirname "$REPO_DIR")/bot-update-staging}/candidates"
CANDIDATE_PENDING_FILE="$CANDIDATE_ROOT/pending.json"
ROLLBACK_REQUEST_ROOT="$CANDIDATE_ROOT/rollback"
ROLLBACK_REQUEST_PENDING_FILE="$ROLLBACK_REQUEST_ROOT/pending.json"
ROLLBACK_REQUEST_ACTIVE_FILE="$ROLLBACK_REQUEST_ROOT/active.json"

# O updater pode substituir scripts/tts-bot-update.sh enquanto ele mesmo está
# rodando. Bash lê partes do arquivo sob demanda; se o arquivo for alterado no
# meio da execução, o processo pode continuar lendo uma versão diferente e
# quebrar com variáveis antigas/novas fora de sincronia. Por isso o processo
# real sempre roda a partir de uma cópia temporária estável.
if [[ "${TTS_BOT_UPDATER_RUNNING_COPY:-0}" != "1" ]]; then
  UPDATER_RUNTIME_COPY="${TMPDIR:-/tmp}/tts-bot-update.$$.run"
  cp "$0" "$UPDATER_RUNTIME_COPY"
  chmod 0700 "$UPDATER_RUNTIME_COPY" 2>/dev/null || true
  export TTS_BOT_UPDATER_RUNNING_COPY=1
  export TTS_BOT_UPDATER_RUNTIME_COPY
  exec /usr/bin/env bash "$UPDATER_RUNTIME_COPY" "$@"
fi
UPDATER_RUNTIME_COPY="${TTS_BOT_UPDATER_RUNTIME_COPY:-}"

FRONT_DIR="$REPO_DIR/activity /sinuca"
BACK_DIR="$REPO_DIR/activity /sinuca-server"
FRONT_PUBLISH_DIR="/var/www/sinuca"
BACK_PORT="8787"
BACK_HEALTH_URL="http://127.0.0.1:${BACK_PORT}/health"
BOT_HEALTH_URL="http://127.0.0.1:10000/health"

STAGE="inicialização"
FAILED_STAGE=""
CURRENT_COMMIT=""
REMOTE_COMMIT=""
PREVIOUS_COMMIT=""
COMMIT_SUBJECT=""
UPDATE_APPLIED=0
ROLLBACK_DONE=0
MANUAL_FAILURE_ALERT_SENT=0
LOCAL_CANDIDATE_MODE=0
LOCAL_CANDIDATE_ID=""
LOCAL_CANDIDATE_DIR=""
LOCAL_CANDIDATE_BASE_COMMIT=""
LOCAL_CANDIDATE_COMMIT_MESSAGE=""
LOCAL_CANDIDATE_PENDING_FILE=""
LOCAL_CANDIDATE_FILES_DIR=""
LOCAL_CANDIDATE_PUBLISHED=0
ROLLBACK_CONTROL_MODE=0
ROLLBACK_REQUEST_ID=""
ROLLBACK_REQUEST_FILE=""
ROLLBACK_REQUEST_ACTION=""
ROLLBACK_REQUEST_BRANCH=""
ROLLBACK_EXPECTED_HEAD=""
ROLLBACK_REVERT_COMMIT=""
ROLLBACK_MESSAGE_CHANNEL_ID=""
ROLLBACK_MESSAGE_ID=""
ROLLBACK_SOURCE_AUTHOR_ID=""
ROLLBACK_REQUESTED_BY=""
ROLLBACK_PREVIOUS_RECORD_JSON="{}"
ROLLBACK_NEW_COMMIT=""

FRONT_CHANGED=0
BACK_CHANGED=0
BOT_CHANGED=0
CALLKEEPER_CHANGED=0
REQUIREMENTS_CHANGED=0
AUDIO_SYSTEMD_CHANGED=0
CLEANUP_CHANGED=0
PHONE_LAVALINK_WATCH_CHANGED=0
PHONE_WORKER_WATCH_CHANGED=0
VPS_SYSTEMD_UNITS_CHANGED=0
ALERT_CHANGED=0
PHONE_WORKER_SYNC_REQUIRED=0
CORE_WORKER_APK_CHANGED=0
CORE_WORKER_AUTOMATION_REQUIRED=0

BOT_HEALTHCHECK_STATUS="não verificado"
BOT_HEALTH_JSON=""
BOT_HEALTH_DETAIL_STATUS="não verificado"
BOT_COGS_STATUS="não verificado"
BOT_WARNINGS_STATUS="sem avisos"
PREFLIGHT_PY_STATUS="não verificado"
PREFLIGHT_BASH_STATUS="não verificado"
PREFLIGHT_COG_IMPORT_STATUS="não verificado"
UPDATE_HAS_WARNINGS=0
CALLKEEPER_STATUS="não alterado"
AUDIO_SERVICES_STATUS="não alterado"
CLEANUP_STATUS="não alterada"
PHONE_LAVALINK_WATCH_STATUS="não alterado"
PHONE_WORKER_WATCH_STATUS="não alterado"
VPS_SYSTEMD_UNITS_STATUS="não alterado"
PHONE_WORKER_SYNC_STATUS="sem mudanças"
CORE_WORKER_AGENT_UPDATE_STATUS="sem mudanças"
CORE_WORKER_APK_BUILD_STATUS="sem mudanças"
CORE_WORKER_NOTIFY_STATUS="sem mudanças"
FRONT_STATUS="não alterado"
BACK_STATUS="não alterado"
ACTIVITY_HEALTHCHECK_STATUS="não verificado"
ROLLBACK_STATUS="não foi necessário"
# Variáveis opcionais usadas apenas quando certos scripts/instaladores mudam.
# Com `set -u`, elas precisam existir desde o topo para a etapa final de
# webhook/mensagem nunca derrubar o updater após o commit/push já ter passado.
ALERT_UNIT_STATUS="não alterado"
CRONTAB_HEALTH_STATUS="não alterado"
CHANGED_FILES_RAW=""
CHANGED_DIFF_NUMSTAT_RAW=""
DIFF_TOTAL_SUMMARY=""
FAST_RELOAD_STATUS="não usado"
UPDATER_UNIT="tts-bot-updater.service"
RUN_LOG_FILE="${TMPDIR:-/tmp}/tts-bot-updater.$$.log"
ZIP_STATUS_CONTROL_JSON=""
UPDATE_TITLE_EMOJI="<a:areia:1496606578395189473>"
UPDATE_STAGE_EMOJI="<a:loading:1510065277868445796>"
ZIP_PROGRESS_HISTORY=""
UPDATER_STEP_LAST=0
UPDATER_TIMINGS=""
: > "$RUN_LOG_FILE"
chmod 0644 "$RUN_LOG_FILE" 2>/dev/null || true

# Log persistente do updater para diagnóstico pelo /vps.
# O systemd guarda a saída no journalctl, mas este arquivo facilita anexar
# falhas recentes sem depender só do journal.
PERSISTENT_LOG_DIR="$REPO_DIR/logs"
PERSISTENT_LOG_FILE="$PERSISTENT_LOG_DIR/updater.log"
mkdir -p "$PERSISTENT_LOG_DIR" 2>/dev/null || true
# Rotação simples: se passar de 2MB, renomeia .log -> .log.1 e começa do zero.
if [[ -f "$PERSISTENT_LOG_FILE" ]]; then
  log_size=$(stat -c '%s' "$PERSISTENT_LOG_FILE" 2>/dev/null || echo 0)
  if [[ "$log_size" -gt 2097152 ]]; then
    mv -f "$PERSISTENT_LOG_FILE" "$PERSISTENT_LOG_FILE.1" 2>/dev/null || true
  fi
fi
{
  echo ""
  echo "===== $(date -Iseconds) updater started (pid=$$) ====="
} >> "$PERSISTENT_LOG_FILE" 2>/dev/null || true

# Tee pra ambos os arquivos. O persistente vira logs/updater.log;
# o tmpfile continua existindo pra `collect_run_log_excerpt`.
exec > >(tee -a "$RUN_LOG_FILE" "$PERSISTENT_LOG_FILE") 2>&1

cleanup_runtime_artifacts() {
  rm -f "$RUN_LOG_FILE"
  if [[ -n "${UPDATER_RUNTIME_COPY:-}" && -f "$UPDATER_RUNTIME_COPY" ]]; then
    rm -f "$UPDATER_RUNTIME_COPY" 2>/dev/null || true
  fi
}

trim_alert_text() {
  local limit="${1:-4000}"
  python3 - "$limit" <<'PY'
import sys
limit = int(sys.argv[1])
text = sys.stdin.read().replace("\r\n", "\n").replace("\r", "\n").strip()
if not text:
    print("")
    raise SystemExit
if len(text) > limit:
    text = text[: limit - 1].rstrip() + "…"
print(text)
PY
}

collect_run_log_excerpt() {
  if [[ -f "$RUN_LOG_FILE" ]]; then
    tail -n 40 "$RUN_LOG_FILE" 2>/dev/null | sed 's/\[[0-9;]*[A-Za-z]//g' | trim_alert_text 1500
  fi
}

service_unit_for_stage() {
  local stage_lc="${1,,}"
  if [[ "$stage_lc" == *"callkeeper"* ]]; then
    printf '%s.service' "$CALLKEEPER_SERVICE"
  elif [[ "$stage_lc" == *"bot"* ]]; then
    printf '%s.service' "$SERVICE"
  else
    printf '%s' "$UPDATER_UNIT"
  fi
}

collect_journal_excerpt() {
  local unit="${1:-$UPDATER_UNIT}"
  local logs
  logs="$(journalctl -u "$unit" -n 40 --no-pager 2>/dev/null | tail -n 25 || true)"
  if [[ -z "${logs//[[:space:]]/}" ]]; then
    logs="nenhum log adicional encontrado"
  fi
  printf '%s' "$logs" | trim_alert_text 1500
}

phone_worker_log_summary_text() {
  if [[ ! -x "$REPO_DIR/scripts/phone-worker-client.py" && ! -f "$REPO_DIR/scripts/phone-worker-client.py" ]]; then
    printf 'indisponível: cliente ausente'
    return 0
  fi
  if ! env_truthy PHONE_WORKER_UPDATE_LOG_SUMMARY_ENABLED && ! env_truthy PHONE_WORKER_ENABLED; then
    printf 'desativado'
    return 0
  fi
  local source_file="${1:-$RUN_LOG_FILE}"
  if [[ ! -s "$source_file" ]]; then
    printf 'sem logs para analisar'
    return 0
  fi
  local timeout_value="4"
  if [[ -f "$REPO_DIR/.env" ]]; then
    timeout_value="$(grep -E '^PHONE_WORKER_UPDATE_LOG_SUMMARY_TIMEOUT_SECONDS=' "$REPO_DIR/.env" 2>/dev/null | tail -n 1 | cut -d= -f2- | tr -d ' "' || true)"
    [[ -n "$timeout_value" ]] || timeout_value="4"
  fi
  local raw summary
  raw="$(sudo -u ubuntu -H python3 "$REPO_DIR/scripts/phone-worker-client.py" log-summary "$source_file" --timeout "$timeout_value" 2>/dev/null || true)"
  if [[ -z "${raw//[[:space:]]/}" ]]; then
    printf 'indisponível: sem resposta'
    return 0
  fi
  summary="$(PHONE_WORKER_RAW="$raw" python3 - <<'PYJSON' 2>/dev/null || true
import json, os
try:
    data = json.loads(os.environ.get('PHONE_WORKER_RAW') or '{}')
except Exception:
    raise SystemExit
counts = data.get('counts') or {}
top = data.get('top_messages') or []
parts = []
for key in ('critical','error','warning','timeout','traceback','exception','failed','lavalink','yt_dlp','rate_limit'):
    value = int(counts.get(key) or 0)
    if value:
        parts.append(f'{key}={value}')
if not parts:
    parts.append('sem padrões críticos')
if top:
    msg = str((top[0] or {}).get('message') or '')[:140]
    cnt = (top[0] or {}).get('count') or 1
    if msg:
        parts.append(f'top({cnt}x): {msg}')
print(' | '.join(parts)[:900])
PYJSON
)"
  if [[ -n "${summary//[[:space:]]/}" ]]; then
    printf '%s' "$summary"
  else
    printf 'indisponível: resposta inválida'
  fi
}

register_error_context() {
  LAST_ERROR_EXIT_CODE="${1:-1}"
  LAST_ERROR_COMMAND="${2:-desconhecido}"
  LAST_ERROR_SERVICE_UNIT="$(service_unit_for_stage "$STAGE")"
  LAST_ERROR_STDERR="$(collect_run_log_excerpt)"
  LAST_ERROR_LOGS="$(collect_journal_excerpt "$LAST_ERROR_SERVICE_UNIT")"
  if [[ -z "${LAST_ERROR_STDERR//[[:space:]]/}" ]]; then
    LAST_ERROR_STDERR="nenhuma saída adicional capturada"
  fi
}

LAST_ERROR_EXIT_CODE=""
LAST_ERROR_COMMAND=""
LAST_ERROR_SERVICE_UNIT=""
LAST_ERROR_STDERR=""
LAST_ERROR_LOGS=""

send_info() {
  local title="${1:-Auto update}"
  local body="${2:-}"
  sudo -u ubuntu /home/ubuntu/bot/alert.sh info "$title" "$body" || true
}

send_warn() {
  local title="${1:-Auto update}"
  local body="${2:-}"
  sudo -u ubuntu /home/ubuntu/bot/alert.sh warn "$title" "$body" || true
}

send_error() {
  local title="${1:-Falha no auto update}"
  local body="${2:-}"
  local attach=""
  if [[ -f "$RUN_LOG_FILE" && -s "$RUN_LOG_FILE" ]]; then
    attach="$RUN_LOG_FILE"
  fi
  sudo -u ubuntu /home/ubuntu/bot/alert.sh error "$title" "$body" "$attach" "tts-bot-updater.log" || true
}

human_duration() {
  local total="${1:-0}"
  local m=$((total / 60))
  local s=$((total % 60))
  if (( m > 0 )); then
    printf "%dm %02ds" "$m" "$s"
  else
    printf "%ds" "$s"
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

format_changed_processes() {
  local items=()
  if (( BOT_CHANGED == 1 || REQUIREMENTS_CHANGED == 1 )); then
    items+=("bot")
  fi
  if (( FRONT_CHANGED == 1 || BACK_CHANGED == 1 )); then
    items+=("atividade")
  fi
  if (( PHONE_WORKER_SYNC_REQUIRED == 1 || PHONE_WORKER_WATCH_CHANGED == 1 || PHONE_LAVALINK_WATCH_CHANGED == 1 || CORE_WORKER_APK_CHANGED == 1 || CORE_WORKER_AUTOMATION_REQUIRED == 1 )); then
    items+=("worker")
  fi
  if (( AUDIO_SYSTEMD_CHANGED == 1 || CLEANUP_CHANGED == 1 )); then
    items+=("áudio")
  fi
  if (( VPS_SYSTEMD_UNITS_CHANGED == 1 )); then
    items+=("sistema VPS")
  fi
  if (( CALLKEEPER_CHANGED == 1 )) && [[ "${UPDATE_TOUCH_CALLKEEPER:-}" == "1" && "${CALLKEEPER_UPDATE_ALLOWED:-}" == "1" ]]; then
    items+=("CallKeeper")
  fi
  if ((${#items[@]} == 0)); then
    printf 'nenhum processo alterado'
    return 0
  fi
  local joined="" item
  for item in "${items[@]}"; do
    [[ -z "$joined" ]] || joined+=", "
    joined+="$item"
  done
  printf '%s' "$joined"
}

run_as_ubuntu() {
  sudo -u ubuntu -H bash -lc "$1"
}

wait_for_health() {
  local url="${1:?}"
  local attempts="${2:-12}"
  local delay="${3:-5}"
  local i

  for ((i=1; i<=attempts; i++)); do
    if curl -fsS --max-time 5 "$url" >/dev/null 2>&1; then
      return 0
    fi
    sleep "$delay"
  done

  return 1
}

fetch_bot_health_json() {
  curl -fsS --max-time 2 "$BOT_HEALTH_URL" 2>/dev/null || true
}

bot_health_python() {
  local mode="${1:?}"
  BOT_HEALTH_JSON_INPUT="${BOT_HEALTH_JSON:-}" python3 - "$mode" <<'PYHEALTH' 2>/dev/null
import json
import os
import sys

mode = sys.argv[1]
raw = os.environ.get("BOT_HEALTH_JSON_INPUT") or ""
try:
    data = json.loads(raw) if raw.strip() else {}
except Exception as exc:
    if mode == "is_healthy":
        raise SystemExit(1)
    print(f"health inválido: {type(exc).__name__}: {exc}")
    raise SystemExit(0)

if mode == "is_ready_healthy":
    critical = data.get("critical_failed_cogs") or []
    if (
        data.get("healthy") is True
        and data.get("status") == "ok"
        and data.get("discord_ready") is True
        and data.get("discord_closed") is not True
        and data.get("mongo_ok") is True
        and data.get("cog_loading_finished") is True
        and not critical
    ):
        raise SystemExit(0)
    raise SystemExit(1)

if mode == "is_healthy":
    if data.get("healthy") is True:
        raise SystemExit(0)
    raise SystemExit(1)

if mode == "status":
    status = data.get("status") or ("ok" if data.get("healthy") is True else "erro")
    ready = data.get("discord_ready")
    mongo = data.get("mongo_ok")
    latency = data.get("latency_ms")
    parts = [str(status)]
    if ready is not None:
        parts.append(f"discord={'online' if ready else 'não pronto'}")
    if mongo is not None:
        parts.append(f"mongo={'OK' if mongo else 'falhou'}")
    if latency is not None:
        parts.append(f"latência={latency}ms")
    print("; ".join(parts))
    raise SystemExit(0)

if mode == "warnings":
    warnings = data.get("warnings") or []
    warnings = [str(item).strip() for item in warnings if str(item).strip()]
    if warnings:
        print("; ".join(warnings)[:900])
    else:
        print("sem avisos")
    raise SystemExit(0)

if mode == "cogs":
    loaded = data.get("loaded_cogs_count")
    failed = data.get("failed_cogs_count")
    failed_cogs = data.get("failed_cogs") or {}
    critical = data.get("critical_failed_cogs") or []
    if loaded is None:
        loaded = len(data.get("loaded_extensions") or [])
    if failed is None:
        failed = len(failed_cogs)
    parts = [f"{loaded or 0} carregada(s)"]
    if failed:
        kind = "crítica(s)" if critical else "opcional(is)"
        names = []
        for name, details in list(failed_cogs.items())[:5]:
            summary = ""
            if isinstance(details, dict):
                summary = str(details.get("summary") or "").strip()
            names.append(f"{name}" + (f" — {summary}" if summary else ""))
        details = "; ".join(names)
        parts.append(f"{failed} com falha {kind}" + (f": {details}" if details else ""))
    else:
        parts.append("0 com falha")
    print("; ".join(parts)[:1200])
    raise SystemExit(0)

print("—")
PYHEALTH
}

refresh_bot_health_status() {
  BOT_HEALTH_JSON="$(fetch_bot_health_json)"
  if [[ -z "${BOT_HEALTH_JSON//[[:space:]]/}" ]]; then
    BOT_HEALTH_DETAIL_STATUS="HTTP sem resposta"
    BOT_COGS_STATUS="indisponível"
    BOT_WARNINGS_STATUS="health indisponível"
    return 1
  fi

  BOT_HEALTH_DETAIL_STATUS="$(bot_health_python status)"
  BOT_COGS_STATUS="$(bot_health_python cogs)"
  BOT_WARNINGS_STATUS="$(bot_health_python warnings)"

  if bot_health_python is_healthy >/dev/null; then
    return 0
  fi
  return 1
}


service_restart_count() {
  local unit="${1:?}"
  local value
  value="$(systemctl show "$unit" -p NRestarts --value 2>/dev/null || echo 0)"
  if [[ "$value" =~ ^[0-9]+$ ]]; then
    printf '%s' "$value"
  else
    printf '0'
  fi
}

journal_since_epoch() {
  local unit="${1:?}"
  local since_epoch="${2:?}"
  journalctl -u "$unit" --since "@${since_epoch}" --no-pager 2>/dev/null || true
}

fatal_boot_log_patterns() {
  cat <<'EOF'
SyntaxError|IndentationError|TabError|ImportError|ModuleNotFoundError|No module named|cannot import name|ExtensionFailed|ExtensionNotFound|Failed to load extension|discord\.ext\.commands\.errors\.ExtensionFailed|discord\.ext\.commands\.errors\.ExtensionNotFound|RuntimeError:.*(boot|startup|setup|load|cog)|AttributeError:.*(setup|load_extension|cog)|Start request repeated too quickly|Failed with result|Main process exited.*status=1
EOF
}

has_fatal_boot_logs() {
  local unit="${1:?}"
  local since_epoch="${2:?}"
  local logs patterns
  logs="$(journal_since_epoch "$unit" "$since_epoch")"
  [[ -n "${logs//[[:space:]]/}" ]] || return 1
  # O bot.py agora permite que cogs opcionais falhem sem derrubar o processo.
  # Essas linhas podem mencionar AttributeError/ImportError/etc. de forma
  # informativa; não devem acionar rollback se o próprio log diz que o bot
  # continuou online. Erros críticos continuam passando pelo filtro.
  logs="$(printf '%s\n' "$logs" | grep -Ev '\[cogs\].*(continuará online|boot continuou com aviso)' || true)"
  [[ -n "${logs//[[:space:]]/}" ]] || return 1
  patterns="$(fatal_boot_log_patterns)"
  printf '%s\n' "$logs" | grep -Eiq "$patterns"
}

run_preflight_checks() {
  local py="$REPO_DIR/.venv/bin/python"
  local file checked_py=0 checked_sh=0 import_checked=0 import_failed=0 import_output=""
  [[ -x "$py" ]] || py="$(command -v python3 || true)"

  if [[ -n "$py" ]]; then
    STAGE="preflight Python"
    while IFS= read -r file; do
      [[ -n "$file" ]] || continue
      [[ -f "$REPO_DIR/$file" ]] || continue
      checked_py=1
      sudo -u ubuntu -H "$py" -m py_compile "$REPO_DIR/$file"
    done < <(printf '%s\n' "$CHANGED_FILES_RAW" | grep -E '\.py$' | grep -v '^activity ' || true)

    if (( checked_py == 1 )); then
      PREFLIGHT_PY_STATUS="OK"
    else
      PREFLIGHT_PY_STATUS="sem arquivos Python alterados"
    fi

    # `py_compile` não pega erro executado no import, como discord.ui.StringSelect.
    # Para cogs alteradas, tentamos importar o módulo sem conectar ao Discord.
    # Falha aqui vira aviso, não rollback automático: o bot.py decide no boot se
    # a cog é opcional ou crítica.
    STAGE="preflight import de cogs"
    while IFS= read -r file; do
      [[ -n "$file" ]] || continue
      [[ -f "$REPO_DIR/$file" ]] || continue
      [[ "$file" == cogs/*.py ]] || continue
      [[ "$(basename "$file")" == "__init__.py" ]] && continue
      import_checked=1
      module="${file%.py}"
      module="${module//\//.}"
      if ! line="$(cd "$REPO_DIR" && sudo -u ubuntu -H "$py" - <<PYIMPORT 2>&1
import importlib
module = ${module@Q}
importlib.import_module(module)
print(f"OK {module}")
PYIMPORT
)"; then
        import_failed=1
        import_output+="FAIL $module: $(printf '%s' "$line" | tail -n 1)"$'\n'
      fi
    done < <(printf '%s\n' "$CHANGED_FILES_RAW" | grep -E '^cogs/.*\.py$' || true)

    if (( import_checked == 0 )); then
      PREFLIGHT_COG_IMPORT_STATUS="sem cogs Python alteradas"
    elif (( import_failed == 0 )); then
      PREFLIGHT_COG_IMPORT_STATUS="OK"
    else
      UPDATE_HAS_WARNINGS=1
      PREFLIGHT_COG_IMPORT_STATUS="aviso: ${import_failed} import(s) de cog falharam"
      logger -t "$LOG_TAG" "Preflight import de cogs com aviso: ${import_output//$'\n'/ | }"
    fi
  else
    PREFLIGHT_PY_STATUS="python indisponível"
    PREFLIGHT_COG_IMPORT_STATUS="não executado; python indisponível"
  fi

  STAGE="preflight Bash"
  while IFS= read -r file; do
    [[ -n "$file" ]] || continue
    [[ -f "$REPO_DIR/$file" ]] || continue
    checked_sh=1
    bash -n "$REPO_DIR/$file"
  done < <(printf '%s\n' "$CHANGED_FILES_RAW" | grep -E '\.sh$' || true)

  if (( checked_sh == 1 )); then
    PREFLIGHT_BASH_STATUS="OK"
  else
    PREFLIGHT_BASH_STATUS="sem scripts Bash alterados"
  fi

  logger -t "$LOG_TAG" "Preflight: Python=$PREFLIGHT_PY_STATUS Bash=$PREFLIGHT_BASH_STATUS Cogs=$PREFLIGHT_COG_IMPORT_STATUS"
}

verify_bot_after_restart() {
  local restart_epoch="${1:?}"
  local restarts_before="${2:-0}"
  local timeout="${UPDATE_BOT_RESTART_TIMEOUT_SECONDS:-35}"
  local interval="${UPDATE_BOT_RESTART_POLL_SECONDS:-1}"
  local waited=0 restarts_after health_ok=0 last_log_check=0

  [[ "$timeout" =~ ^[0-9]+$ ]] || timeout=35
  [[ "$interval" =~ ^[0-9]+$ ]] || interval=1
  (( timeout < 8 )) && timeout=8
  (( interval < 1 )) && interval=1

  while (( waited <= timeout )); do
    if systemctl is-failed --quiet "$SERVICE"; then
      BOT_HEALTHCHECK_STATUS="falhou: serviço em failed"
      return 1
    fi

    if systemctl is-active --quiet "$SERVICE"; then
      restarts_after="$(service_restart_count "$SERVICE")"
      if [[ "$restarts_after" =~ ^[0-9]+$ && "$restarts_before" =~ ^[0-9]+$ ]]; then
        if (( restarts_after > restarts_before + 1 )); then
          BOT_HEALTHCHECK_STATUS="falhou: restart loop detectado (${restarts_before}→${restarts_after})"
          return 1
        fi
      fi

      # journalctl é útil, mas não deve ser consultado em loop fechado.
      if (( waited == 0 || waited - last_log_check >= 5 )); then
        last_log_check="$waited"
        if has_fatal_boot_logs "$SERVICE" "$restart_epoch"; then
          BOT_HEALTHCHECK_STATUS="falhou: erro fatal de boot nas logs"
          return 1
        fi
      fi

      if refresh_bot_health_status; then
        if bot_health_python is_ready_healthy >/dev/null; then
          health_ok=1
          break
        fi
        logger -t "$LOG_TAG" "Health respondeu, aguardando ready real (${waited}s): $BOT_HEALTH_DETAIL_STATUS"
      elif [[ -n "${BOT_HEALTH_DETAIL_STATUS//[[:space:]]/}" && "$BOT_HEALTH_DETAIL_STATUS" != "HTTP sem resposta" ]]; then
        logger -t "$LOG_TAG" "Health ainda não saudável (${waited}s): $BOT_HEALTH_DETAIL_STATUS"
      fi
    elif (( waited >= 5 )); then
      BOT_HEALTHCHECK_STATUS="falhou: serviço não ficou active"
      return 1
    fi

    sleep "$interval"
    waited=$((waited + interval))
  done

  if (( health_ok == 1 )); then
    if has_real_warning_text "$BOT_WARNINGS_STATUS" || cogs_have_failures "$BOT_COGS_STATUS"; then
      BOT_HEALTHCHECK_STATUS="OK com avisos"
      UPDATE_HAS_WARNINGS=1
    else
      BOT_HEALTHCHECK_STATUS="OK"
    fi
  else
    # Última checagem fatal antes de aceitar qualquer estado parcial.
    if has_fatal_boot_logs "$SERVICE" "$restart_epoch"; then
      BOT_HEALTHCHECK_STATUS="falhou: erro fatal de boot nas logs"
      return 1
    fi
    if [[ "$BOT_HEALTH_DETAIL_STATUS" == "HTTP sem resposta" ]]; then
      BOT_HEALTHCHECK_STATUS="ativo; health HTTP sem resposta"
      UPDATE_HAS_WARNINGS=1
      return 0
    fi
    BOT_HEALTHCHECK_STATUS="falhou: health não ficou pronto em ${timeout}s ($BOT_HEALTH_DETAIL_STATUS)"
    return 1
  fi
  return 0
}


is_placeholder_status_text() {
  local text="${1:-}"
  text="${text//$'\r'/}"
  text="${text//$'\n'/ }"
  text="$(printf '%s' "$text" | tr -s '[:space:]' ' ' | sed 's/^ *//;s/ *$//')"
  local lower="${text,,}"
  [[ -z "$lower" || "$lower" == "—" || "$lower" == "-" || "$lower" == "sem avisos" || "$lower" == "sem mudanças" || "$lower" == "não alterado" || "$lower" == "nao alterado" || "$lower" == "não verificado" || "$lower" == "nao verificado" ]]
}

cogs_have_failures() {
  local text="${1:-}"
  text="${text//$'\r'/}"
  text="${text//$'\n'/ }"
  text="$(printf '%s' "$text" | tr -s '[:space:]' ' ' | sed 's/^ *//;s/ *$//')"
  local lower="${text,,}"
  if [[ -z "$lower" || "$lower" == "—" || "$lower" == "-" || "$lower" == "não verificado" || "$lower" == "nao verificado" ]]; then
    return 1
  fi
  if [[ "$lower" =~ (^|[^0-9])0[[:space:]]+com[[:space:]]+falha ]]; then
    return 1
  fi
  if [[ "$lower" =~ ([1-9][0-9]*)[[:space:]]+com[[:space:]]+falha ]]; then
    return 0
  fi
  if [[ "$lower" == *"cog"* && ( "$lower" == *"falhou"* || "$lower" == *"erro"* || "$lower" == *"failed"* ) ]]; then
    return 0
  fi
  return 1
}

has_real_warning_text() {
  local text="${1:-}"
  text="${text//$'\r'/}"
  text="${text//$'\n'/ }"
  text="$(printf '%s' "$text" | tr -s '[:space:]' ' ' | sed 's/^ *//;s/ *$//')"
  local lower="${text,,}"
  if is_placeholder_status_text "$text"; then
    return 1
  fi
  case "$lower" in
    ok|success|sucesso|ativo|online|sincronizados|limpo|"unit instalada"|"timer ativo"|"sem cogs python alteradas"|"sem arquivos python alterados"|"sem scripts bash alterados")
      return 1
      ;;
  esac
  # Informativo: o sync do phone-worker pode ficar agendado após restart sem ser aviso.
  if [[ "$lower" == *"agendado para automação por jobs após restart"* ]]; then
    return 1
  fi
  if [[ "$lower" == aviso:* || "$lower" == *" com falha"* || "$lower" == falha* || "$lower" == failed* || "$lower" == *"sem resposta"* || "$lower" == *"degraded"* || "$lower" == *"restart loop"* ]]; then
    return 0
  fi
  return 1
}

normalize_final_health_warning_state() {
  # O status "OK com avisos" só pode permanecer se houver aviso real e exibível.
  # Caso contrário, a mensagem final vira contraditória: título amarelo com "Avisos: sem avisos".
  if [[ "$BOT_HEALTHCHECK_STATUS" == "OK com avisos" ]]; then
    if ! has_real_warning_text "$BOT_WARNINGS_STATUS" && ! cogs_have_failures "$BOT_COGS_STATUS"; then
      BOT_HEALTHCHECK_STATUS="OK"
    fi
  fi
}

recompute_update_warning_flag() {
  UPDATE_HAS_WARNINGS=0
  normalize_final_health_warning_state
  if has_real_warning_text "$PREFLIGHT_COG_IMPORT_STATUS"; then
    UPDATE_HAS_WARNINGS=1
  fi
  if has_real_warning_text "$BOT_WARNINGS_STATUS"; then
    UPDATE_HAS_WARNINGS=1
  fi
  if cogs_have_failures "$BOT_COGS_STATUS"; then
    UPDATE_HAS_WARNINGS=1
  fi
  if [[ "$BOT_HEALTHCHECK_STATUS" == *"sem resposta"* ]]; then
    UPDATE_HAS_WARNINGS=1
  fi
  if [[ "$BOT_HEALTHCHECK_STATUS" == "OK com avisos" ]]; then
    UPDATE_HAS_WARNINGS=1
  fi
  if has_real_warning_text "${VPS_SYSTEMD_UNITS_STATUS:-}"; then UPDATE_HAS_WARNINGS=1; fi
  if has_real_warning_text "${AUDIO_SERVICES_STATUS:-}"; then UPDATE_HAS_WARNINGS=1; fi
  if has_real_warning_text "${ALERT_UNIT_STATUS:-}"; then UPDATE_HAS_WARNINGS=1; fi
  if has_real_warning_text "${CRONTAB_HEALTH_STATUS:-}"; then UPDATE_HAS_WARNINGS=1; fi
  if has_real_warning_text "${CLEANUP_STATUS:-}"; then UPDATE_HAS_WARNINGS=1; fi
  if has_real_warning_text "${PHONE_LAVALINK_WATCH_STATUS:-}"; then UPDATE_HAS_WARNINGS=1; fi
  if has_real_warning_text "${PHONE_WORKER_WATCH_STATUS:-}"; then UPDATE_HAS_WARNINGS=1; fi
  if has_real_warning_text "${PHONE_WORKER_SYNC_STATUS:-}"; then UPDATE_HAS_WARNINGS=1; fi
  if has_real_warning_text "${CORE_WORKER_AGENT_UPDATE_STATUS:-}"; then UPDATE_HAS_WARNINGS=1; fi
  if has_real_warning_text "${CORE_WORKER_APK_BUILD_STATUS:-}"; then UPDATE_HAS_WARNINGS=1; fi
  if has_real_warning_text "${CORE_WORKER_NOTIFY_STATUS:-}"; then UPDATE_HAS_WARNINGS=1; fi
  if has_real_warning_text "${CALLKEEPER_STATUS:-}"; then UPDATE_HAS_WARNINGS=1; fi
  if has_real_warning_text "${FRONT_STATUS:-}"; then UPDATE_HAS_WARNINGS=1; fi
  if has_real_warning_text "${BACK_STATUS:-}"; then UPDATE_HAS_WARNINGS=1; fi
  if has_real_warning_text "${ACTIVITY_HEALTHCHECK_STATUS:-}"; then UPDATE_HAS_WARNINGS=1; fi
}


env_truthy() {
  local key="${1:?}"
  local value=""
  if [[ -f "$REPO_DIR/.env" ]]; then
    value="$(grep -E "^${key}=" "$REPO_DIR/.env" 2>/dev/null | tail -n 1 | cut -d= -f2- | tr -d ' "' || true)"
  fi
  value="${value,,}"
  [[ "$value" == "1" || "$value" == "true" || "$value" == "yes" || "$value" == "y" || "$value" == "on" || "$value" == "sim" ]]
}

wait_for_lavalink_ready() {
  # Arquitetura atual: Lavalink da VPS não é usado. Quando música usa phone worker,
  # esperar lavalink.service local só cria travamento/restart-loop.
  if ! env_truthy VPS_LAVALINK_ENABLED; then
    return 0
  fi
  if [[ -x "$REPO_DIR/scripts/wait-audio-node-ready.py" ]]; then
    sudo -u ubuntu -H "$REPO_DIR/scripts/wait-audio-node-ready.py" --timeout "${AUDIO_NODE_STARTUP_WAIT_SECONDS:-20}"
    return $?
  fi
  return 0
}

short_commit() {
  local value="${1:-}"
  if [[ -z "$value" ]]; then
    printf 'desconhecido'
  else
    printf '%s' "${value:0:7}"
  fi
}

marker_value() {
  local key="${1:?}"
  if [[ ! -f "$DIRTY_MARKER_FILE" ]]; then
    return 0
  fi
  awk -F= -v wanted="$key" '$1 == wanted { sub($1 "=", ""); print; exit }' "$DIRTY_MARKER_FILE" 2>/dev/null || true
}

write_dirty_marker() {
  local failed_commit="${1:-}"
  local rollback_commit="${2:-}"
  local failed_stage="${3:-desconhecido}"
  local failed_command="${4:-desconhecido}"

  cat > "$DIRTY_MARKER_FILE" <<EOM
FAILED_REMOTE_COMMIT=$failed_commit
ROLLED_BACK_TO=$rollback_commit
FAILED_STAGE=$failed_stage
FAILED_COMMAND=$failed_command
FAILED_AT=$(date -u '+%Y-%m-%dT%H:%M:%SZ')
EOM
  chown ubuntu:ubuntu "$DIRTY_MARKER_FILE" 2>/dev/null || true
}

clear_dirty_marker() {
  rm -f "$DIRTY_MARKER_FILE"
}

json_field_from_file() {
  local file="${1:?}"
  local field="${2:?}"
  python3 - "$file" "$field" <<'PYJSON'
import json, sys
path, field = sys.argv[1], sys.argv[2]
try:
    data = json.load(open(path, encoding='utf-8'))
except Exception:
    raise SystemExit(1)
value = data
for part in field.split('.'):
    if isinstance(value, dict):
        value = value.get(part)
    else:
        value = None
        break
if value is None:
    raise SystemExit(0)
if isinstance(value, (list, dict)):
    print(json.dumps(value, ensure_ascii=False))
else:
    print(str(value))
PYJSON
}

load_pending_local_candidate() {
  local manifest active_file
  LOCAL_CANDIDATE_PENDING_FILE=""
  LOCAL_CANDIDATE_DIR=""

  if [[ -f "$CANDIDATE_PENDING_FILE" ]]; then
    LOCAL_CANDIDATE_PENDING_FILE="$CANDIDATE_PENDING_FILE"
    LOCAL_CANDIDATE_DIR="$(json_field_from_file "$CANDIDATE_PENDING_FILE" candidate_dir 2>/dev/null || true)"
    if [[ -z "${LOCAL_CANDIDATE_DIR//[[:space:]]/}" || ! -d "$LOCAL_CANDIDATE_DIR" ]]; then
      rm -f "$CANDIDATE_PENDING_FILE" 2>/dev/null || true
      return 1
    fi
    # O pending é consumido logo no início. Assim, um ZIP novo pode criar outro
    # pending sem risco do updater antigo apagar a fila nova ao arquivar o candidato.
    LOCAL_CANDIDATE_PENDING_FILE="$LOCAL_CANDIDATE_DIR/active.json"
    mv "$CANDIDATE_PENDING_FILE" "$LOCAL_CANDIDATE_PENDING_FILE" 2>/dev/null || true
  else
    # Recuperação: se o updater anterior caiu depois de mover pending.json para
    # active.json, o timer não pode fingir "Sem mudanças em main". Ele precisa
    # retomar ou reverter o candidato ativo.
    active_file="$(find "$CANDIDATE_ROOT" -mindepth 2 -maxdepth 2 -type f -name active.json 2>/dev/null | sort | head -n 1 || true)"
    if [[ -z "${active_file//[[:space:]]/}" ]]; then
      return 1
    fi
    LOCAL_CANDIDATE_PENDING_FILE="$active_file"
    LOCAL_CANDIDATE_DIR="$(dirname "$active_file")"
    logger -t "$LOG_TAG" "Retomando candidato local ativo: $(basename "$LOCAL_CANDIDATE_DIR")"
  fi

  manifest="$LOCAL_CANDIDATE_DIR/manifest.json"
  [[ -f "$manifest" ]] || return 1
  LOCAL_CANDIDATE_MODE=1
  LOCAL_CANDIDATE_ID="$(json_field_from_file "$manifest" id 2>/dev/null || true)"
  LOCAL_CANDIDATE_BASE_COMMIT="$(json_field_from_file "$manifest" base_commit 2>/dev/null || true)"
  LOCAL_CANDIDATE_COMMIT_MESSAGE="$(json_field_from_file "$manifest" commit_message 2>/dev/null || true)"
  LOCAL_CANDIDATE_FILES_DIR="$LOCAL_CANDIDATE_DIR/files"
  BRANCH="$(json_field_from_file "$manifest" branch 2>/dev/null || true)"
  [[ -n "${BRANCH//[[:space:]]/}" ]] || BRANCH="main"
  [[ -n "${LOCAL_CANDIDATE_ID//[[:space:]]/}" ]] || LOCAL_CANDIDATE_ID="$(basename "$LOCAL_CANDIDATE_DIR")"
  [[ -n "${LOCAL_CANDIDATE_COMMIT_MESSAGE//[[:space:]]/}" ]] || LOCAL_CANDIDATE_COMMIT_MESSAGE="auto update from discord zip"

  CHANGED_FILES_RAW="$(python3 - "$manifest" <<'PYFILES'
import json, sys
try:
    data = json.load(open(sys.argv[1], encoding='utf-8'))
except Exception:
    raise SystemExit(1)
for item in data.get('changed_files') or []:
    item = str(item).strip()
    if item:
        print(item)
PYFILES
)"
  CHANGED_DIFF_NUMSTAT_RAW="$(python3 - "$manifest" <<'PYDIFF'
import json, sys
try:
    data = json.load(open(sys.argv[1], encoding='utf-8'))
except Exception:
    raise SystemExit(1)
for item in ((data.get('diff_stats') or {}).get('entries') or []):
    path = str(item.get('path') or '').strip()
    if not path:
        continue
    if item.get('binary'):
        print(f'-\t-\t{path}')
    else:
        print(f"{int(item.get('added') or 0)}\t{int(item.get('removed') or 0)}\t{path}")
PYDIFF
)"
  return 0
}
archive_local_candidate() {
  local status="${1:-done}"
  if [[ -z "${LOCAL_CANDIDATE_DIR:-}" ]]; then
    return 0
  fi
  mkdir -p "$CANDIDATE_ROOT/$status" 2>/dev/null || true
  if [[ -f "$LOCAL_CANDIDATE_PENDING_FILE" ]]; then
    rm -f "$LOCAL_CANDIDATE_PENDING_FILE" 2>/dev/null || true
  fi
  if [[ -d "$LOCAL_CANDIDATE_DIR" ]]; then
    mv "$LOCAL_CANDIDATE_DIR" "$CANDIDATE_ROOT/$status/$(basename "$LOCAL_CANDIDATE_DIR").$(date +%Y%m%d%H%M%S)" 2>/dev/null || rm -rf "$LOCAL_CANDIDATE_DIR" 2>/dev/null || true
  fi
}

write_local_candidate_state() {
  (( LOCAL_CANDIDATE_MODE == 1 )) || return 0
  [[ -n "${LOCAL_CANDIDATE_DIR:-}" && -d "$LOCAL_CANDIDATE_DIR" ]] || return 0
  local state_name="${1:-state}"
  local extra_commit="${2:-}"
  python3 - "$LOCAL_CANDIDATE_DIR" "$state_name" "$extra_commit" <<'PYSTATE' 2>/dev/null || true
import json, pathlib, sys, datetime
root = pathlib.Path(sys.argv[1])
state = sys.argv[2]
commit = sys.argv[3]
path = root / "state.json"
data = {}
if path.exists():
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        data = {}
data.update({
    "state": state,
    "commit": commit,
    "updated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
})
path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
PYSTATE
}

notify_zip_status_message() {
  (( LOCAL_CANDIDATE_MODE == 1 )) || return 0
  [[ -n "${LOCAL_CANDIDATE_DIR:-}" && -f "$LOCAL_CANDIDATE_DIR/manifest.json" ]] || return 0
  local status="${1:-info}"
  local title="${2:-Update}"
  local description="${3:-}"
  MANIFEST_PATH="$LOCAL_CANDIDATE_DIR/manifest.json" \
  STATUS_VALUE="$status" TITLE_VALUE="$title" DESCRIPTION_VALUE="$description" \
  ZIP_STATUS_CONTROL_JSON="${ZIP_STATUS_CONTROL_JSON:-}" \
  BOT_HEALTH_URL="$BOT_HEALTH_URL" REPO_DIR="$REPO_DIR" python3 - <<'PYSTATUS' 2>/dev/null || true
import json, os, urllib.request, urllib.error
from pathlib import Path
manifest_path = Path(os.environ["MANIFEST_PATH"])
try:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
except Exception:
    raise SystemExit(0)
info = manifest.get("discord_status") or {}
channel_id = info.get("channel_id")
message_id = info.get("message_id")
if not channel_id or not message_id:
    raise SystemExit(0)
payload = {
    "channel_id": str(channel_id),
    "message_id": str(message_id),
    "status": os.environ.get("STATUS_VALUE") or "info",
    "title": os.environ.get("TITLE_VALUE") or "Update",
    "description": os.environ.get("DESCRIPTION_VALUE") or "",
}
try:
    control_raw = os.environ.get("ZIP_STATUS_CONTROL_JSON") or ""
    if control_raw.strip():
        control = json.loads(control_raw)
        if isinstance(control, dict):
            payload["control"] = control
except Exception:
    pass
url = (os.environ.get("BOT_HEALTH_URL") or "http://127.0.0.1:10000/health").replace("/health", "/internal/update/zip-status")
headers = {"Content-Type": "application/json"}
# Mesmo token do endpoint interno de reload, quando configurado.
try:
    env_path = Path(os.environ.get("REPO_DIR", "/home/ubuntu/bot")) / ".env"
    if env_path.exists():
        for line in env_path.read_text(encoding="utf-8", errors="ignore").splitlines():
            if line.startswith("BOT_INTERNAL_UPDATE_TOKEN="):
                token = line.split("=", 1)[1].strip().strip('"').strip("'")
                if token:
                    headers["X-Update-Token"] = token
                break
except Exception:
    pass
req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"), headers=headers, method="POST")
try:
    with urllib.request.urlopen(req, timeout=4) as resp:
        resp.read()
except Exception:
    # Notificação visual é best-effort; nunca pode quebrar o updater.
    pass
PYSTATUS
}

post_direct_update_message() {
  local channel_id="${1:-}"
  local message_id="${2:-}"
  local status="${3:-info}"
  local title="${4:-Update}"
  local description="${5:-}"
  local control_json="${6:-}"
  [[ -n "$channel_id" && -n "$message_id" ]] || return 0
  CHANNEL_ID_VALUE="$channel_id" MESSAGE_ID_VALUE="$message_id" STATUS_VALUE="$status" \
  TITLE_VALUE="$title" DESCRIPTION_VALUE="$description" ZIP_STATUS_CONTROL_JSON="$control_json" \
  BOT_HEALTH_URL="$BOT_HEALTH_URL" REPO_DIR="$REPO_DIR" python3 - <<'PYDIRECT' 2>/dev/null || true
import json, os, urllib.request
payload = {
    "channel_id": os.environ.get("CHANNEL_ID_VALUE") or "",
    "message_id": os.environ.get("MESSAGE_ID_VALUE") or "",
    "status": os.environ.get("STATUS_VALUE") or "info",
    "title": os.environ.get("TITLE_VALUE") or "Update",
    "description": os.environ.get("DESCRIPTION_VALUE") or "",
}
try:
    raw = os.environ.get("ZIP_STATUS_CONTROL_JSON") or ""
    if raw.strip():
        control = json.loads(raw)
        if isinstance(control, dict):
            payload["control"] = control
except Exception:
    pass
url = (os.environ.get("BOT_HEALTH_URL") or "http://127.0.0.1:10000/health").replace("/health", "/internal/update/zip-status")
headers = {"Content-Type": "application/json"}
try:
    from pathlib import Path
    env_path = Path(os.environ.get("REPO_DIR", "/home/ubuntu/bot")) / ".env"
    if env_path.exists():
        for line in env_path.read_text(encoding="utf-8", errors="ignore").splitlines():
            if line.startswith("BOT_INTERNAL_UPDATE_TOKEN="):
                token = line.split("=", 1)[1].strip().strip('"').strip("'")
                if token:
                    headers["X-Update-Token"] = token
                break
except Exception:
    pass
req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"), headers=headers, method="POST")
try:
    with urllib.request.urlopen(req, timeout=4) as resp:
        resp.read()
except Exception:
    pass
PYDIRECT
}

zip_progress_title() {
  if (( ROLLBACK_CONTROL_MODE == 1 )); then
    if [[ "${ROLLBACK_REQUEST_ACTION:-rollback}" == "redo" ]]; then
      printf '%s Reaplicando update...' "$UPDATE_TITLE_EMOJI"
    else
      printf '%s Revertendo update...' "$UPDATE_TITLE_EMOJI"
    fi
    return 0
  fi
  printf '%s Aplicando update...' "$UPDATE_TITLE_EMOJI"
}

zip_progress_status() {
  if (( ROLLBACK_CONTROL_MODE == 1 )); then
    printf 'progress'
  else
    printf 'applying'
  fi
}

zip_progress_publish() {
  local stage_label="${1:-Preparando}"
  local detail="${2:-}"
  local title status description
  title="$(zip_progress_title)"
  status="$(zip_progress_status)"
  description=""
  if [[ -n "${ZIP_PROGRESS_HISTORY//[[:space:]]/}" ]]; then
    description="$ZIP_PROGRESS_HISTORY"$'\n'
  fi
  description+="$UPDATE_STAGE_EMOJI **$stage_label**"
  if [[ -n "${detail//[[:space:]]/}" ]]; then
    description+=$'\n'"$detail"
  fi
  if (( ROLLBACK_CONTROL_MODE == 1 )); then
    post_direct_update_message "$ROLLBACK_MESSAGE_CHANNEL_ID" "$ROLLBACK_MESSAGE_ID" "$status" "$title" "$description" || true
  else
    notify_zip_status_message "$status" "$title" "$description" || true
  fi
}

zip_progress_done() {
  local done_label="${1:-}"
  [[ -n "${done_label//[[:space:]]/}" ]] || return 0
  if [[ -n "${ZIP_PROGRESS_HISTORY//[[:space:]]/}" ]]; then
    ZIP_PROGRESS_HISTORY+=$'\n'
  fi
  ZIP_PROGRESS_HISTORY+="-# ✅ $done_label"
}

zip_progress_done_and_publish() {
  local done_label="${1:-}"
  local next_label="${2:-Preparando}"
  local detail="${3:-}"
  zip_progress_done "$done_label"
  zip_progress_publish "$next_label" "$detail"
}

zip_progress_process_detail() {
  local processes
  processes="$(format_changed_processes 2>/dev/null || true)"
  if [[ -n "${processes//[[:space:]]/}" && "$processes" != "nenhum processo alterado" ]]; then
    printf '%s' "$processes"
  fi
}

load_pending_rollback_request() {
  ROLLBACK_CONTROL_MODE=0
  ROLLBACK_REQUEST_FILE=""
  mkdir -p "$ROLLBACK_REQUEST_ROOT" 2>/dev/null || true
  if [[ -f "$ROLLBACK_REQUEST_PENDING_FILE" ]]; then
    mv "$ROLLBACK_REQUEST_PENDING_FILE" "$ROLLBACK_REQUEST_ACTIVE_FILE" 2>/dev/null || true
  fi
  [[ -f "$ROLLBACK_REQUEST_ACTIVE_FILE" ]] || return 1
  ROLLBACK_CONTROL_MODE=1
  ROLLBACK_REQUEST_FILE="$ROLLBACK_REQUEST_ACTIVE_FILE"
  ROLLBACK_REQUEST_ID="$(json_field_from_file "$ROLLBACK_REQUEST_FILE" id 2>/dev/null || true)"
  ROLLBACK_REQUEST_ACTION="$(json_field_from_file "$ROLLBACK_REQUEST_FILE" mode 2>/dev/null || true)"
  ROLLBACK_REQUEST_BRANCH="$(json_field_from_file "$ROLLBACK_REQUEST_FILE" branch 2>/dev/null || true)"
  ROLLBACK_EXPECTED_HEAD="$(json_field_from_file "$ROLLBACK_REQUEST_FILE" expected_head 2>/dev/null || true)"
  ROLLBACK_REVERT_COMMIT="$(json_field_from_file "$ROLLBACK_REQUEST_FILE" revert_commit 2>/dev/null || true)"
  ROLLBACK_MESSAGE_CHANNEL_ID="$(json_field_from_file "$ROLLBACK_REQUEST_FILE" message.channel_id 2>/dev/null || true)"
  ROLLBACK_MESSAGE_ID="$(json_field_from_file "$ROLLBACK_REQUEST_FILE" message.message_id 2>/dev/null || true)"
  ROLLBACK_SOURCE_AUTHOR_ID="$(json_field_from_file "$ROLLBACK_REQUEST_FILE" source_author_id 2>/dev/null || true)"
  ROLLBACK_REQUESTED_BY="$(json_field_from_file "$ROLLBACK_REQUEST_FILE" requested_by 2>/dev/null || true)"
  ROLLBACK_PREVIOUS_RECORD_JSON="$(json_field_from_file "$ROLLBACK_REQUEST_FILE" previous_record 2>/dev/null || echo '{}')"
  [[ -n "${ROLLBACK_REQUEST_ID//[[:space:]]/}" ]] || ROLLBACK_REQUEST_ID="rollback-$(date +%Y%m%d%H%M%S)"
  [[ -n "${ROLLBACK_REQUEST_BRANCH//[[:space:]]/}" ]] || ROLLBACK_REQUEST_BRANCH="main"
  BRANCH="$ROLLBACK_REQUEST_BRANCH"
  if [[ "$ROLLBACK_REQUEST_ACTION" != "rollback" && "$ROLLBACK_REQUEST_ACTION" != "redo" ]]; then
    ROLLBACK_REQUEST_ACTION="rollback"
  fi
  [[ -n "${ROLLBACK_EXPECTED_HEAD//[[:space:]]/}" && -n "${ROLLBACK_REVERT_COMMIT//[[:space:]]/}" ]]
}

archive_rollback_request() {
  local status="${1:-done}"
  [[ -n "${ROLLBACK_REQUEST_FILE:-}" ]] || return 0
  mkdir -p "$ROLLBACK_REQUEST_ROOT/$status" 2>/dev/null || true
  if [[ -f "$ROLLBACK_REQUEST_FILE" ]]; then
    mv "$ROLLBACK_REQUEST_FILE" "$ROLLBACK_REQUEST_ROOT/$status/${ROLLBACK_REQUEST_ID:-rollback}.$(date +%Y%m%d%H%M%S).json" 2>/dev/null || rm -f "$ROLLBACK_REQUEST_FILE" 2>/dev/null || true
  fi
}

rollback_control_json() {
  local mode="${1:-rollback}"
  local head_commit="${2:-}"
  local revert_commit="${3:-$head_commit}"
  python3 - "$mode" "$head_commit" "$revert_commit" "$BRANCH" "$ROLLBACK_SOURCE_AUTHOR_ID" <<'PYCTRL'
import json, sys
mode, head, revert, branch, author = sys.argv[1:6]
print(json.dumps({
    "enabled": True,
    "mode": mode,
    "branch": branch or "main",
    "expected_head": head,
    "revert_commit": revert or head,
    "head_commit": head,
    "source_author_id": author,
}, ensure_ascii=False))
PYCTRL
}

prepare_rollback_request_update() {
  ROLLBACK_CONTROL_MODE=1
  LOCAL_CANDIDATE_MODE=0
  zip_progress_publish "Validando estado atual"
  STAGE="fetch remoto"
  sudo -u ubuntu -H git fetch origin "$BRANCH"
  CURRENT_COMMIT="$(sudo -u ubuntu -H git rev-parse HEAD)"
  REMOTE_COMMIT="$(sudo -u ubuntu -H git rev-parse "origin/$BRANCH")"
  PREVIOUS_COMMIT="$CURRENT_COMMIT"
  SHORT_FROM="$(short_commit "$CURRENT_COMMIT")"
  mark_update_timing "fetch"

  if [[ "$CURRENT_COMMIT" != "$REMOTE_COMMIT" || "$CURRENT_COMMIT" != "$ROLLBACK_EXPECTED_HEAD" ]]; then
    local unavailable_title="Reversão indisponível"
    [[ "$ROLLBACK_REQUEST_ACTION" == "redo" ]] && unavailable_title="Reaplicação indisponível"
    post_direct_update_message "$ROLLBACK_MESSAGE_CHANNEL_ID" "$ROLLBACK_MESSAGE_ID" "error" "$unavailable_title" "O estado atual mudou. Nenhuma alteração foi aplicada."
    archive_rollback_request "failed"
    exit 1
  fi

  STAGE="verificação de alterações locais"
  clear_local_changes_marker_if_clean
  fail_local_changes_before_pull

  zip_progress_done_and_publish "Estado validado" "Aplicando reversão"

  STAGE="reversão local"
  git -C "$REPO_DIR" rev-parse --verify "$ROLLBACK_REVERT_COMMIT^{commit}" >/dev/null
  if ! sudo -u ubuntu -H git revert --no-commit "$ROLLBACK_REVERT_COMMIT"; then
    sudo -u ubuntu -H git revert --abort >/dev/null 2>&1 || true
    sudo -u ubuntu -H git reset --hard "$PREVIOUS_COMMIT" >/dev/null 2>&1 || true
    local fail_title="Falha ao reverter"
    [[ "$ROLLBACK_REQUEST_ACTION" == "redo" ]] && fail_title="Falha ao reaplicar"
    local retry_control
    retry_control="$(rollback_control_json "$ROLLBACK_REQUEST_ACTION" "$ROLLBACK_EXPECTED_HEAD" "$ROLLBACK_REVERT_COMMIT" 2>/dev/null || true)"
    post_direct_update_message "$ROLLBACK_MESSAGE_CHANNEL_ID" "$ROLLBACK_MESSAGE_ID" "error" "$fail_title" "O estado local foi restaurado. Nada foi publicado no GitHub." "$retry_control"
    archive_rollback_request "failed"
    exit 1
  fi
  UPDATE_APPLIED=1
  CHANGED_FILES_RAW="$(sudo -u ubuntu -H git diff --cached --name-only || true)"
  CHANGED_DIFF_NUMSTAT_RAW="$(sudo -u ubuntu -H git diff --cached --numstat || true)"
  if [[ -z "${CHANGED_FILES_RAW//[[:space:]]/}" ]]; then
    sudo -u ubuntu -H git reset --hard "$PREVIOUS_COMMIT" >/dev/null 2>&1 || true
    local retry_control
    retry_control="$(rollback_control_json "$ROLLBACK_REQUEST_ACTION" "$ROLLBACK_EXPECTED_HEAD" "$ROLLBACK_REVERT_COMMIT" 2>/dev/null || true)"
    post_direct_update_message "$ROLLBACK_MESSAGE_CHANNEL_ID" "$ROLLBACK_MESSAGE_ID" "warn" "Nenhuma alteração" "O estado já estava equivalente." "$retry_control"
    archive_rollback_request "done"
    exit 0
  fi
  classify_changed_files
  if (( CALLKEEPER_CHANGED == 1 )) && [[ "${UPDATE_TOUCH_CALLKEEPER:-}" != "1" || "${CALLKEEPER_UPDATE_ALLOWED:-}" != "1" ]]; then
    sudo -u ubuntu -H git reset --hard "$PREVIOUS_COMMIT" >/dev/null 2>&1 || true
    local retry_control
    retry_control="$(rollback_control_json "$ROLLBACK_REQUEST_ACTION" "$ROLLBACK_EXPECTED_HEAD" "$ROLLBACK_REVERT_COMMIT" 2>/dev/null || true)"
    post_direct_update_message "$ROLLBACK_MESSAGE_CHANNEL_ID" "$ROLLBACK_MESSAGE_ID" "error" "Update bloqueado" "CallKeeper é protegido e não foi tocado." "$retry_control"
    archive_rollback_request "failed"
    exit 1
  fi
  COMMIT_SUBJECT="${ROLLBACK_REQUEST_ACTION} discord zip update"
  mark_update_timing "rollback_apply"
  zip_progress_done "Reversão preparada"
}

publish_rollback_request_after_validation() {
  (( ROLLBACK_CONTROL_MODE == 1 )) || return 0
  STAGE="commit da reversão"
  local msg
  if [[ "$ROLLBACK_REQUEST_ACTION" == "redo" ]]; then
    msg="redo discord zip update $(short_commit "$ROLLBACK_REVERT_COMMIT")"
  else
    msg="rollback discord zip update $(short_commit "$ROLLBACK_REVERT_COMMIT")"
  fi
  zip_progress_publish "Fazendo commit..."
  git_add_changed_files
  sudo -u ubuntu -H git commit -m "$msg"
  REMOTE_COMMIT="$(sudo -u ubuntu -H git rev-parse HEAD)"
  ROLLBACK_NEW_COMMIT="$REMOTE_COMMIT"
  SHORT_TO="$(short_commit "$REMOTE_COMMIT")"
  mark_update_timing "commit"
  zip_progress_done "Commit criado"
  STAGE="push GitHub"
  zip_progress_publish "Publicando no GitHub..."
  sudo -u ubuntu -H git push origin "HEAD:$BRANCH"
  mark_update_timing "push"
  zip_progress_done "GitHub atualizado"
}

finalize_rollback_request_success() {
  (( ROLLBACK_CONTROL_MODE == 1 )) || return 1
  local duration changed_files diff_summary apply_mode control_json title summary next_mode status_title
  duration="$(human_duration "$SECONDS")"
  changed_files="$(format_changed_files)"
  diff_summary="$(format_diff_total_summary)"
  if [[ "$FAST_RELOAD_STATUS" == "OK" ]]; then
    apply_mode="reload rápido de cog"
  elif (( BOT_CHANGED == 0 )); then
    apply_mode="sem restart do bot"
  else
    apply_mode="restart completo"
  fi
  if [[ "$ROLLBACK_REQUEST_ACTION" == "redo" ]]; then
    title="↪️ Update reaplicado"
    summary="Update reaplicado e tudo está saudável."
    next_mode="rollback"
  else
    title="↩️ Update revertido"
    summary="Reversão aplicada e tudo está saudável."
    next_mode="redo"
  fi
  control_json="$(rollback_control_json "$next_mode" "$REMOTE_COMMIT")"
  local desc
  desc="$summary

$BRANCH · ${SHORT_FROM} → ${SHORT_TO}
$(printf '%s\n' "$CHANGED_FILES_RAW" | awk 'NF {c++} END {print c+0}') arquivo(s) alterado(s) · $diff_summary
Aplicação: $apply_mode · $duration

## Arquivos alterados
$changed_files"
  post_direct_update_message "$ROLLBACK_MESSAGE_CHANNEL_ID" "$ROLLBACK_MESSAGE_ID" "success" "$title" "$desc" "$control_json"
  local body
  body="Resumo: $summary
Branch: $BRANCH
Commit: ${SHORT_FROM} → ${SHORT_TO}
Update: $(printf '%s\n' "$CHANGED_FILES_RAW" | awk 'NF {c++} END {print c+0}') arquivo(s) · $diff_summary
Aplicação: $apply_mode
Processos alterados: $(format_changed_processes)
Arquivos:
$changed_files
Duração: $duration
Hora: $(date '+%d/%m/%Y %H:%M:%S')"
  /home/ubuntu/bot/alert.sh success "$title" "$body" || true
  archive_rollback_request "done"
  logger -t "$LOG_TAG" "$title"
  exit 0
}

copy_local_candidate_files() {
  [[ -d "$LOCAL_CANDIDATE_FILES_DIR" ]] || return 1
  MANIFEST_PATH="$LOCAL_CANDIDATE_DIR/manifest.json" REPO_DIR="$REPO_DIR" FILES_DIR="$LOCAL_CANDIDATE_FILES_DIR" python3 - <<'PYCOPY'
import json, os, pathlib, shutil
repo = pathlib.Path(os.environ['REPO_DIR']).resolve()
files_dir = pathlib.Path(os.environ['FILES_DIR']).resolve()
data = json.loads(pathlib.Path(os.environ['MANIFEST_PATH']).read_text(encoding='utf-8'))
for raw in data.get('changed_files') or []:
    rel = pathlib.PurePosixPath(str(raw))
    if rel.is_absolute() or '..' in rel.parts:
        raise SystemExit(f'caminho inválido no candidato: {raw}')
    src = files_dir.joinpath(*rel.parts).resolve()
    dst = repo.joinpath(*rel.parts).resolve()
    if files_dir not in src.parents and src != files_dir:
        raise SystemExit(f'origem fora do candidato: {raw}')
    if repo not in dst.parents and dst != repo:
        raise SystemExit(f'destino fora do repo: {raw}')
    if not src.is_file():
        raise SystemExit(f'arquivo ausente no candidato: {raw}')
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)
PYCOPY
}

cleanup_local_candidate_new_files_after_reset() {
  if (( LOCAL_CANDIDATE_MODE == 0 )) || [[ -z "${PREVIOUS_COMMIT:-}" ]]; then
    return 0
  fi
  while IFS= read -r rel; do
    [[ -n "$rel" ]] || continue
    if printf '%s\n' "$rel" | grep -Eq '(^|/)\.\.(\/|$)|^/'; then
      continue
    fi
    if sudo -u ubuntu -H git cat-file -e "$PREVIOUS_COMMIT:$rel" 2>/dev/null; then
      continue
    fi
    rm -f "$REPO_DIR/$rel" 2>/dev/null || true
  done <<< "$CHANGED_FILES_RAW"
}

git_add_changed_files() {
  [[ -n "${CHANGED_FILES_RAW//[[:space:]]/}" ]] || return 0
  while IFS= read -r rel; do
    [[ -n "$rel" ]] || continue
    printf '%s\0' "$rel"
  done <<< "$CHANGED_FILES_RAW" | sudo -u ubuntu -H git add --pathspec-from-file=- --pathspec-file-nul
}

prepare_local_candidate_update() {
  LOCAL_CANDIDATE_MODE=1
  zip_progress_publish "Validando candidato" "Conferindo base local e arquivos recebidos."
  STAGE="fetch remoto"
  sudo -u ubuntu -H git fetch origin "$BRANCH"
  REMOTE_COMMIT="$(sudo -u ubuntu -H git rev-parse "origin/$BRANCH")"
  CURRENT_COMMIT="$(sudo -u ubuntu -H git rev-parse HEAD)"
  PREVIOUS_COMMIT="$CURRENT_COMMIT"
  COMMIT_SUBJECT="$LOCAL_CANDIDATE_COMMIT_MESSAGE"
  SHORT_FROM="$(short_commit "$CURRENT_COMMIT")"
  SHORT_TO="local"
  mark_update_timing "fetch"

  if [[ -n "$LOCAL_CANDIDATE_BASE_COMMIT" && "$LOCAL_CANDIDATE_BASE_COMMIT" != "$REMOTE_COMMIT" ]]; then
    MANUAL_FAILURE_ALERT_SENT=1
    archive_local_candidate "failed"
    send_error "Update bloqueado: base mudou" "Resumo: O ZIP foi preparado sobre outro commit. Nada foi aplicado e nada foi enviado ao GitHub.
Branch: $BRANCH
Base do ZIP: $(short_commit "$LOCAL_CANDIDATE_BASE_COMMIT")
GitHub atual: $(short_commit "$REMOTE_COMMIT")
Ação sugerida: envie o ZIP novamente usando a base atual.
Hora: $(date '+%d/%m/%Y %H:%M:%S')"
    exit 1
  fi

  STAGE="verificação de alterações locais"
  clear_local_changes_marker_if_clean
  if candidate_local_changes_are_expected; then
    logger -t "$LOG_TAG" "Alterações locais correspondem ao candidato ativo; retomando aplicação segura."
  else
    fail_local_changes_before_pull
  fi

  if [[ "$CURRENT_COMMIT" != "$REMOTE_COMMIT" ]]; then
    STAGE="sincronização com GitHub antes do candidato"
    sudo -u ubuntu -H git pull --ff-only origin "$BRANCH"
    CURRENT_COMMIT="$(sudo -u ubuntu -H git rev-parse HEAD)"
    PREVIOUS_COMMIT="$CURRENT_COMMIT"
    SHORT_FROM="$(short_commit "$CURRENT_COMMIT")"
    mark_update_timing "sync"
  fi

  if [[ -z "${CHANGED_FILES_RAW//[[:space:]]/}" ]]; then
    archive_local_candidate "done"
    logger -t "$LOG_TAG" "Candidato local sem arquivos alterados"
    exit 0
  fi

  classify_changed_files
  if (( CALLKEEPER_CHANGED == 1 )) && [[ "${UPDATE_TOUCH_CALLKEEPER:-}" != "1" || "${CALLKEEPER_UPDATE_ALLOWED:-}" != "1" ]]; then
    CHANGED_FILES="$(format_changed_files)"
    archive_local_candidate "failed"
    send_error "Update bloqueado: CallKeeper protegido" "Resumo: Update bloqueado antes de aplicar porque contém arquivo protegido do CallKeeper.
Branch: $BRANCH
Arquivos:
$CHANGED_FILES
Ação sugerida: remova arquivos do CallKeeper deste patch ou faça um patch CallKeeper explícito e isolado.
Hora: $(date '+%d/%m/%Y %H:%M:%S')"
    exit 1
  fi

  STAGE="limpeza de artefatos gerados"
  cleanup_known_generated_update_artifacts

  zip_progress_done_and_publish "Candidato validado" "Aplicando arquivos"

  STAGE="aplicação local do candidato"
  UPDATE_APPLIED=1
  copy_local_candidate_files
  git_add_changed_files
  CHANGED_FILES_RAW="$(sudo -u ubuntu -H git diff --cached --name-only || true)"
  CHANGED_DIFF_NUMSTAT_RAW="$(sudo -u ubuntu -H git diff --cached --numstat || true)"
  if [[ -z "${CHANGED_FILES_RAW//[[:space:]]/}" ]]; then
    local head_subject=""
    head_subject="$(sudo -u ubuntu -H git log -1 --pretty=%s HEAD 2>/dev/null || true)"
    if [[ "$CURRENT_COMMIT" == "$REMOTE_COMMIT" && "$head_subject" == "$LOCAL_CANDIDATE_COMMIT_MESSAGE" ]]; then
      LOCAL_CANDIDATE_PUBLISHED=1
      REMOTE_COMMIT="$CURRENT_COMMIT"
      SHORT_TO="$(short_commit "$REMOTE_COMMIT")"
      write_local_candidate_state "published" "$REMOTE_COMMIT"
      logger -t "$LOG_TAG" "Candidato local já publicado; retomando finalização visual/webhook."
      return 0
    fi
    archive_local_candidate "done"
    logger -t "$LOG_TAG" "Candidato local não mudou o repositório"
    exit 0
  fi
  classify_changed_files
  mark_update_timing "candidate_apply"
  zip_progress_done "Arquivos aplicados"
}

publish_local_candidate_after_validation() {
  if (( LOCAL_CANDIDATE_MODE == 0 )); then
    return 0
  fi
  if (( LOCAL_CANDIDATE_PUBLISHED == 1 )); then
    return 0
  fi
  STAGE="commit local validado"
  zip_progress_publish "Fazendo commit..."
  git_add_changed_files
  sudo -u ubuntu -H git commit -m "$LOCAL_CANDIDATE_COMMIT_MESSAGE"
  REMOTE_COMMIT="$(sudo -u ubuntu -H git rev-parse HEAD)"
  SHORT_TO="$(short_commit "$REMOTE_COMMIT")"
  mark_update_timing "commit"
  zip_progress_done "Commit criado"

  write_local_candidate_state "committed" "$REMOTE_COMMIT"

  STAGE="push GitHub pós-validação"
  zip_progress_publish "Publicando no GitHub..."
  sudo -u ubuntu -H git push origin "HEAD:$BRANCH"
  LOCAL_CANDIDATE_PUBLISHED=1
  write_local_candidate_state "published" "$REMOTE_COMMIT"
  mark_update_timing "push"
  zip_progress_done "GitHub atualizado"
}

format_changed_files() {
  if [[ -n "$CHANGED_DIFF_NUMSTAT_RAW" ]]; then
    CHANGED_DIFF_NUMSTAT_INPUT="$CHANGED_DIFF_NUMSTAT_RAW" python3 - <<'PYDIFF'
import os
raw = os.environ.get("CHANGED_DIFF_NUMSTAT_INPUT") or ""
lines = []
for item in raw.splitlines():
    parts = item.split("\t")
    if len(parts) < 3:
        continue
    add, rem, path = parts[0], parts[1], parts[-1]
    if add == "-" or rem == "-":
        lines.append(f"• {path}  binário")
    else:
        lines.append(f"• {path}  +{int(add or 0)} -{int(rem or 0)}")
limit = 20
for line in lines[:limit]:
    print(line)
if len(lines) > limit:
    print(f"+{len(lines) - limit} arquivo(s) restante(s)")
if not lines:
    print("• nenhum arquivo listado")
PYDIFF
  elif [[ -n "$CHANGED_FILES_RAW" ]]; then
    printf '%s\n' "$CHANGED_FILES_RAW" | head -n 20 | sed 's/^/• /'
    local total
    total="$(printf '%s\n' "$CHANGED_FILES_RAW" | awk 'NF {c++} END {print c+0}')"
    if [[ "$total" =~ ^[0-9]+$ && "$total" -gt 20 ]]; then
      printf '+%s arquivo(s) restante(s)\n' "$((total - 20))"
    fi
  else
    printf '• nenhum arquivo listado'
  fi
}

format_diff_total_summary() {
  if [[ -z "$CHANGED_DIFF_NUMSTAT_RAW" ]]; then
    printf 'diff indisponível'
    return 0
  fi
  CHANGED_DIFF_NUMSTAT_INPUT="$CHANGED_DIFF_NUMSTAT_RAW" python3 - <<'PYDIFF'
import os
raw = os.environ.get("CHANGED_DIFF_NUMSTAT_INPUT") or ""
added = removed = binaries = 0
for item in raw.splitlines():
    parts = item.split("\t")
    if len(parts) < 3:
        continue
    add, rem = parts[0], parts[1]
    if add == "-" or rem == "-":
        binaries += 1
        continue
    added += int(add or 0)
    removed += int(rem or 0)
out = f"+{added} -{removed}"
if binaries:
    out += f" · {binaries} binário(s)"
print(out)
PYDIFF
}

classify_changed_files() {
  FRONT_CHANGED=0
  BACK_CHANGED=0
  BOT_CHANGED=0
  CALLKEEPER_CHANGED=0
  REQUIREMENTS_CHANGED=0
  AUDIO_SYSTEMD_CHANGED=0
  CLEANUP_CHANGED=0
  PHONE_LAVALINK_WATCH_CHANGED=0
  PHONE_WORKER_WATCH_CHANGED=0
  VPS_SYSTEMD_UNITS_CHANGED=0
  ALERT_CHANGED=0
  PHONE_WORKER_SYNC_REQUIRED=0
  CORE_WORKER_APK_CHANGED=0
  CORE_WORKER_AUTOMATION_REQUIRED=0

  if printf '%s\n' "$CHANGED_FILES_RAW" | grep -q '^activity /sinuca/'; then
    FRONT_CHANGED=1
  fi
  if printf '%s\n' "$CHANGED_FILES_RAW" | grep -q '^activity /sinuca-server/'; then
    BACK_CHANGED=1
  fi
  if printf '%s\n' "$CHANGED_FILES_RAW" | grep -Eq '^(bot\.py|webserver\.py|config\.py|db\.py|start\.sh|requirements\.txt|cogs/|music_system/|utility/)'; then
    BOT_CHANGED=1
  fi
  if printf '%s\n' "$CHANGED_FILES_RAW" | grep -Eq '^deploy/systemd(/vps)?/tts-bot\.service$'; then
    BOT_CHANGED=1
  fi
  if printf '%s\n' "$CHANGED_FILES_RAW" | grep -Eq '^requirements\.txt$'; then
    REQUIREMENTS_CHANGED=1
  fi
  if printf '%s\n' "$CHANGED_FILES_RAW" | grep -Eq '^deploy/systemd/(lavalink|tts-bot|tts-bot-alert@)\.service$'; then
    AUDIO_SYSTEMD_CHANGED=1
  fi
  if printf '%s\n' "$CHANGED_FILES_RAW" | grep -Eq '^(alert\.sh|deploy/systemd(/vps)?/tts-bot-alert@\.service)$'; then
    ALERT_CHANGED=1
  fi
  if printf '%s\n' "$CHANGED_FILES_RAW" | grep -Eq '^(deploy/systemd/vps/|deploy/sudoers\.d/|scripts/install-vps-systemd-units\.sh$)'; then
    VPS_SYSTEMD_UNITS_CHANGED=1
    AUDIO_SYSTEMD_CHANGED=1
    CLEANUP_CHANGED=1
    PHONE_LAVALINK_WATCH_CHANGED=1
    PHONE_WORKER_WATCH_CHANGED=1
  fi
  if printf '%s\n' "$CHANGED_FILES_RAW" | grep -Eq '^(cleanup-audio-temp\.sh|deploy/systemd/cleanup-audio-temp\.(service|timer))$'; then
    CLEANUP_CHANGED=1
  fi
  if printf '%s\n' "$CHANGED_FILES_RAW" | grep -Eq '^(scripts/phone-lavalink-watch\.sh|deploy/systemd/phone-lavalink-watch\.(service|timer)|deploy/termux/phone-lavalink/)'; then
    PHONE_LAVALINK_WATCH_CHANGED=1
  fi
  if printf '%s\n' "$CHANGED_FILES_RAW" | grep -Eq '^(scripts/phone-worker-watch\.sh|scripts/phone-worker-client\.py|deploy/systemd/phone-worker-watch\.(service|timer)|deploy/termux/phone-worker/)'; then
    PHONE_WORKER_WATCH_CHANGED=1
  fi
  if printf '%s\n' "$CHANGED_FILES_RAW" | grep -Eq '^deploy/termux/phone-worker/'; then
    PHONE_WORKER_SYNC_REQUIRED=1
    CORE_WORKER_AUTOMATION_REQUIRED=1
  fi
  if printf '%s\n' "$CHANGED_FILES_RAW" | grep -Eq '^android/core-worker-app/'; then
    CORE_WORKER_APK_CHANGED=1
    CORE_WORKER_AUTOMATION_REQUIRED=1
  fi
  if printf '%s\n' "$CHANGED_FILES_RAW" | grep -Eq '^(callkeeper_service\.py|callkeeper_runtime/|cogs/call_keeper\.py|deploy/systemd(/vps)?/callkeeper\.service)$'; then
    CALLKEEPER_CHANGED=1
  fi
}

fast_reload_modules_for_changed_files() {
  CHANGED_FILES_RAW_INPUT="$CHANGED_FILES_RAW" python3 - <<'PYFAST'
import os, pathlib, sys
raw = [line.strip() for line in (os.environ.get("CHANGED_FILES_RAW_INPUT") or "").splitlines() if line.strip()]
if not raw:
    raise SystemExit(1)
modules = []
for path in raw:
    parts = pathlib.PurePosixPath(path).parts
    if len(parts) != 2 or parts[0] != "cogs" or not parts[1].endswith(".py"):
        raise SystemExit(1)
    if parts[1] in {"__init__.py", "call_keeper.py"}:
        raise SystemExit(1)
    modules.append("cogs." + parts[1][:-3])
print("\n".join(dict.fromkeys(modules)))
PYFAST
}

try_fast_cog_reload() {
  local modules_text="${1:-}"
  [[ -n "${modules_text//[[:space:]]/}" ]] || return 1
  local payload token header_args=() response http_code
  payload="$(MODULES_TEXT="$modules_text" python3 - <<'PYPAYLOAD'
import json, os
mods = [line.strip() for line in (os.environ.get("MODULES_TEXT") or "").splitlines() if line.strip()]
print(json.dumps({"modules": mods}, ensure_ascii=False))
PYPAYLOAD
)"
  token=""
  if [[ -f "$REPO_DIR/.env" ]]; then
    token="$(grep -E '^BOT_INTERNAL_UPDATE_TOKEN=' "$REPO_DIR/.env" 2>/dev/null | tail -n 1 | cut -d= -f2- | tr -d ' "' || true)"
  fi
  if [[ -n "$token" ]]; then
    header_args=(-H "X-Update-Token: $token")
  fi
  response="$(mktemp)"
  http_code="$(curl -sS -o "$response" -w '%{http_code}' --max-time 30 -H 'Content-Type: application/json' "${header_args[@]}" -d "$payload" http://127.0.0.1:10000/internal/update/reload-cogs 2>/dev/null || true)"
  if [[ "$http_code" != "200" ]]; then
    FAST_RELOAD_STATUS="falhou; fallback restart (${http_code:-sem HTTP})"
    logger -t "$LOG_TAG" "Fast reload falhou HTTP=${http_code:-sem HTTP}: $(cat "$response" 2>/dev/null | tail -c 300)"
    rm -f "$response"
    return 1
  fi
  if ! python3 - "$response" <<'PYOK' >/dev/null 2>&1; then
import json, sys
p = sys.argv[1]
data = json.load(open(p, encoding='utf-8'))
raise SystemExit(0 if data.get('ok') is True else 1)
PYOK
    FAST_RELOAD_STATUS="falhou; fallback restart"
    logger -t "$LOG_TAG" "Fast reload retornou falha: $(cat "$response" 2>/dev/null | tail -c 500)"
    rm -f "$response"
    return 1
  fi
  rm -f "$response"
  STAGE="healthcheck pós reload rápido"
  if refresh_bot_health_status; then
    if has_real_warning_text "$BOT_WARNINGS_STATUS" || cogs_have_failures "$BOT_COGS_STATUS"; then
      BOT_HEALTHCHECK_STATUS="OK com avisos"
      UPDATE_HAS_WARNINGS=1
    else
      BOT_HEALTHCHECK_STATUS="OK (reload rápido)"
    fi
    FAST_RELOAD_STATUS="OK"
    return 0
  fi
  FAST_RELOAD_STATUS="reload executado; health falhou; fallback restart"
  return 1
}

cleanup_known_generated_update_artifacts() {
  # Estes arquivos/pastas são gerados por build/publicação do Core Worker e
  # não devem bloquear o auto updater. Não remove código fonte nem registry.
  rm -rf "$REPO_DIR/android/core-worker-app/app/build" 2>/dev/null || true
  rm -rf "$REPO_DIR/android/core-worker-app/.gradle" 2>/dev/null || true
  rm -f "$REPO_DIR/android/core-worker-app/app/build.gradle.bak"* 2>/dev/null || true
  # Não removemos android/core-worker-app/releases aqui: é onde latest.json/APKs
  # privados ficam publicados para os celulares. O auto updater já ignora essa
  # pasta ao criar commits e alterações não rastreadas não bloqueiam git pull.
}

local_changes_fingerprint() {
  {
    sudo -u ubuntu -H git status --short --untracked-files=no 2>/dev/null || true
    sudo -u ubuntu -H git diff --name-only 2>/dev/null || true
    sudo -u ubuntu -H git diff --name-only --cached 2>/dev/null || true
  } | sha256sum | awk '{print $1}'
}

clear_local_changes_marker_if_clean() {
  local status_text
  status_text="$(collect_local_tracked_changes)"
  if [[ -z "${status_text//[[:space:]]/}" ]]; then
    rm -f "$LOCAL_CHANGES_MARKER_FILE" 2>/dev/null || true
  fi
}

collect_local_tracked_changes() {
  # Untracked locais como data/, cookies e healthcheck não bloqueiam o merge.
  # O que bloqueia o git pull são mudanças em arquivos rastreados.
  sudo -u ubuntu -H git status --short --untracked-files=no 2>/dev/null | trim_alert_text 1800
}

collect_local_tracked_files() {
  {
    sudo -u ubuntu -H git diff --name-only 2>/dev/null || true
    sudo -u ubuntu -H git diff --name-only --cached 2>/dev/null || true
  } | awk 'NF && !seen[$0]++' | head -n 40 | sed 's/^/• /' | trim_alert_text 1500
}

candidate_local_changes_are_expected() {
  (( LOCAL_CANDIDATE_MODE == 1 )) || return 1
  [[ -n "${CHANGED_FILES_RAW//[[:space:]]/}" ]] || return 1
  CHANGED_FILES_RAW="$CHANGED_FILES_RAW" REPO_DIR="$REPO_DIR" python3 - <<'PYCANDIDATE_DIRTY'
import subprocess, sys, os
expected = {line.strip() for line in os.environ.get('CHANGED_FILES_RAW', '').splitlines() if line.strip()}
if not expected:
    raise SystemExit(1)

def git_lines(*args):
    cp = subprocess.run(['git', *args], cwd=os.environ.get('REPO_DIR', '/home/ubuntu/bot'), text=True, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    return {line.strip() for line in cp.stdout.splitlines() if line.strip()}

dirty = set()
dirty |= git_lines('diff', '--name-only')
dirty |= git_lines('diff', '--name-only', '--cached')
if not dirty:
    raise SystemExit(0)
extra = dirty - expected
if extra:
    for item in sorted(extra):
        print(item)
    raise SystemExit(1)
raise SystemExit(0)
PYCANDIDATE_DIRTY
}

fail_local_changes_before_pull() {
  local status_text files_text duration body
  status_text="$(collect_local_tracked_changes)"
  if [[ -z "${status_text//[[:space:]]/}" ]]; then
    return 0
  fi

  local fingerprint previous_fingerprint
  fingerprint="$(local_changes_fingerprint)"
  previous_fingerprint=""
  if [[ -f "$LOCAL_CHANGES_MARKER_FILE" ]]; then
    previous_fingerprint="$(awk -F= '$1 == "FINGERPRINT" { sub($1 "=", ""); print; exit }' "$LOCAL_CHANGES_MARKER_FILE" 2>/dev/null || true)"
  fi
  if [[ -n "$fingerprint" && "$fingerprint" == "$previous_fingerprint" ]]; then
    logger -t "$LOG_TAG" "Alterações locais ainda bloqueiam o update; alerta já enviado para este mesmo estado."
    exit 1
  fi
  MANUAL_FAILURE_ALERT_SENT=1
  files_text="$(collect_local_tracked_files)"
  duration="$(human_duration "$SECONDS")"
  cat > "$LOCAL_CHANGES_MARKER_FILE" <<EOM
FINGERPRINT=$fingerprint
REMOTE_COMMIT=$REMOTE_COMMIT
CURRENT_COMMIT=$CURRENT_COMMIT
AT=$(date -u '+%Y-%m-%dT%H:%M:%SZ')
EOM
  chown ubuntu:ubuntu "$LOCAL_CHANGES_MARKER_FILE" 2>/dev/null || true

  body="Resumo: O updater parou antes do git pull porque existem alterações locais em arquivos rastreados. O Git bloqueou o merge para não sobrescrever seus testes na VPS.
Host: $HOSTNAME
Branch: $BRANCH
Serviço: tts-bot-updater
Serviço afetado: $UPDATER_UNIT
Commit anterior: $(short_commit "$CURRENT_COMMIT")
Commit alvo: $(short_commit "$REMOTE_COMMIT")
Commit: $(short_commit "$CURRENT_COMMIT") → $(short_commit "$REMOTE_COMMIT")
Update: ${COMMIT_SUBJECT:-sem mensagem}
Etapa: verificação de alterações locais
Código: 1
Rollback: não foi necessário
Commit sujo: sim
Diagnóstico: existem mudanças locais que seriam sobrescritas pelo merge.
Arquivos locais:
${files_text:-nenhum arquivo listado}
Status git:
$status_text
Ação sugerida: deixe o repo limpo antes do updater. Normalmente: git restore <arquivo> e rm -rf android/core-worker-app/app/build android/core-worker-app/releases. Se a alteração for intencional, envie como patch oficial em ZIP.
Duração: $duration
Hora: $(date '+%d/%m/%Y %H:%M:%S')"

  send_error "Falha no auto update: alterações locais" "$body"
  exit 1
}


sanitize_vps_lavalink_units() {
  # Remove dependências locais do Lavalink. O node de áudio válido fica no phone worker/Music Agent.
  local file changed_any=0
  for file in /etc/systemd/system/tts-bot.service /etc/systemd/system/tts-bot.service.d/*.conf; do
    [[ -f "$file" ]] || continue
    if grep -q 'lavalink.service' "$file" 2>/dev/null; then
      cp -a "$file" "$file.backup.$(date +%Y%m%d-%H%M%S)" 2>/dev/null || true
      python3 - "$file" <<'PY_SANITIZE_LAVALINK'
import sys
from pathlib import Path
p=Path(sys.argv[1])
text=p.read_text(encoding='utf-8', errors='replace')
out=[]
for line in text.splitlines():
    stripped=line.strip()
    if stripped.startswith(('Wants=', 'Requires=', 'After=', 'Before=')) and 'lavalink.service' in stripped:
        key, value = line.split('=', 1)
        parts=[part for part in value.split() if part != 'lavalink.service']
        if parts:
            out.append(key + '=' + ' '.join(parts))
        else:
            out.append('# ' + key + '=lavalink.service removido: Lavalink roda no phone worker/Music Agent')
        continue
    if stripped.startswith('ExecStartPre=') and 'wait-audio-node-ready.py' in stripped:
        out.append('# ExecStartPre wait-audio-node-ready.py removido: Lavalink local da VPS não é usado')
        continue
    out.append(line)
p.write_text('\n'.join(out).rstrip() + '\n', encoding='utf-8')
PY_SANITIZE_LAVALINK
      changed_any=1
    fi
  done
  if ! env_truthy VPS_LAVALINK_ENABLED; then
    systemctl stop lavalink.service >/dev/null 2>&1 || true
    systemctl disable lavalink.service >/dev/null 2>&1 || true
    systemctl reset-failed lavalink.service >/dev/null 2>&1 || true
    # systemctl mask falha quando /etc/systemd/system/lavalink.service é um arquivo
    # real. Fazemos a máscara idempotente manualmente para impedir restart-loop local.
    if [[ -e /etc/systemd/system/lavalink.service && ! -L /etc/systemd/system/lavalink.service ]]; then
      cp -a /etc/systemd/system/lavalink.service "/etc/systemd/system/lavalink.service.backup.$(date +%Y%m%d-%H%M%S)" 2>/dev/null || true
      mv /etc/systemd/system/lavalink.service "/etc/systemd/system/lavalink.service.disabled.$(date +%Y%m%d-%H%M%S)" 2>/dev/null || true
    fi
    ln -sfn /dev/null /etc/systemd/system/lavalink.service 2>/dev/null || true
    changed_any=1
  fi
  if (( changed_any == 1 )); then
    systemctl daemon-reload || true
  fi
}


normalize_healthcheck_crontab() {
  # Corrige linhas temporárias quebradas criadas durante diagnóstico manual.
  # Não reativa healthcheck/resource-check automaticamente: eles continuam
  # pausados até a correção passar pelo patch e pelo operador.
  STAGE="normalização do crontab de emergência"
  local tmp current
  tmp="${TMPDIR:-/tmp}/tts-bot-cron.$$"
  current="${TMPDIR:-/tmp}/tts-bot-cron-current.$$"
  if ! sudo -u ubuntu -H crontab -l > "$current" 2>/dev/null; then
    CRONTAB_HEALTH_STATUS="sem crontab do usuário ubuntu"
    rm -f "$tmp" "$current" 2>/dev/null || true
    return 0
  fi
  python3 - "$current" "$tmp" <<'PY_CRON'
import sys
from pathlib import Path

src = Path(sys.argv[1])
dst = Path(sys.argv[2])
text = src.read_text(encoding='utf-8', errors='replace')

HEALTH_DISABLED = '# TEMP_DISABLED_HEALTHCHECK_UNTIL_PATCH_20260524 * * * * * /home/ubuntu/bot/healthcheck.sh >/dev/null 2>&1'
RESOURCE_DISABLED = '# TEMP_DISABLED_EMERGENCY_20260524 */5 * * * * /home/ubuntu/bot/resource-check.sh >/dev/null 2>&1'
HEALTH_ACTIVE = '* * * * * /home/ubuntu/bot/healthcheck.sh >/dev/null 2>&1'
RESOURCE_ACTIVE = '*/5 * * * * /home/ubuntu/bot/resource-check.sh >/dev/null 2>&1'

out = []
has_disabled_health = False
has_active_health = False
has_disabled_resource = False
has_active_resource = False

def is_redirect_only(line):
    return line.strip() in {'>/dev/null 2>&1', '>>/dev/null 2>&1', '2>&1', '&>/dev/null'}

def kind_for(line):
    if 'healthcheck.sh' in line:
        return 'health'
    if 'resource-check.sh' in line:
        return 'resource'
    return None

def disabled(line):
    return line.lstrip().startswith('#') or 'TEMP_DISABLED' in line

for raw in text.splitlines():
    line = raw.rstrip('\r')
    if is_redirect_only(line):
        continue
    kind = kind_for(line)
    if kind == 'health':
        if disabled(line):
            if not has_disabled_health:
                out.append(HEALTH_DISABLED)
                has_disabled_health = True
        else:
            if not has_active_health:
                out.append(HEALTH_ACTIVE)
                has_active_health = True
        continue
    if kind == 'resource':
        if disabled(line):
            if not has_disabled_resource:
                out.append(RESOURCE_DISABLED)
                has_disabled_resource = True
        else:
            if not has_active_resource:
                out.append(RESOURCE_ACTIVE)
                has_active_resource = True
        continue
    out.append(line)

dst.write_text('\n'.join(out).rstrip() + '\n', encoding='utf-8')
PY_CRON
  if ! cmp -s "$current" "$tmp"; then
    cp -a /var/spool/cron/crontabs/ubuntu "$REPO_DIR/crontab.backup.auto-clean.$(date +%Y%m%d-%H%M%S)" 2>/dev/null || true
    sudo -u ubuntu -H crontab "$tmp" || true
    CRONTAB_HEALTH_STATUS="normalizado; healthcheck/resource-check seguem pausados se estavam pausados"
  else
    CRONTAB_HEALTH_STATUS="limpo"
  fi
  rm -f "$tmp" "$current" 2>/dev/null || true
}

deploy_vps_systemd_units() {
  if (( VPS_SYSTEMD_UNITS_CHANGED == 0 )); then
    VPS_SYSTEMD_UNITS_STATUS="não alterado"
    return 0
  fi

  STAGE="sincronização dos units systemd da VPS"
  if [[ ! -x "$REPO_DIR/scripts/install-vps-systemd-units.sh" && ! -f "$REPO_DIR/scripts/install-vps-systemd-units.sh" ]]; then
    VPS_SYSTEMD_UNITS_STATUS="script ausente"
    UPDATE_HAS_WARNINGS=1
    return 0
  fi

  if REPO_DIR="$REPO_DIR" bash "$REPO_DIR/scripts/install-vps-systemd-units.sh" --from-updater; then
    VPS_SYSTEMD_UNITS_STATUS="sincronizados"
  else
    VPS_SYSTEMD_UNITS_STATUS="falha ao sincronizar"
    UPDATE_HAS_WARNINGS=1
  fi
}

deploy_alert_unit() {
  STAGE="configuração do alerta systemd"
  local src=""
  if [[ -f "$REPO_DIR/deploy/systemd/vps/tts-bot-alert@.service" ]]; then
    src="$REPO_DIR/deploy/systemd/vps/tts-bot-alert@.service"
  elif [[ -f "$REPO_DIR/deploy/systemd/tts-bot-alert@.service" ]]; then
    src="$REPO_DIR/deploy/systemd/tts-bot-alert@.service"
  fi
  if [[ -n "$src" ]]; then
    cp "$src" /etc/systemd/system/tts-bot-alert@.service
    systemctl daemon-reload || true
    ALERT_UNIT_STATUS="unit instalada"
  else
    ALERT_UNIT_STATUS="unit ausente no deploy"
  fi
}

deploy_audio_services() {
  sanitize_vps_lavalink_units
  if (( AUDIO_SYSTEMD_CHANGED == 0 )); then
    AUDIO_SERVICES_STATUS="não alterado; Lavalink VPS sanitizado"
    return 0
  fi

  STAGE="configuração dos serviços de áudio"
  local installed=0 lavalink_unit_changed=0

  if printf '%s\n' "$CHANGED_FILES_RAW" | grep -q '^deploy/systemd/lavalink\.service$'; then
    lavalink_unit_changed=1
    if env_truthy VPS_LAVALINK_ENABLED && [[ -f "$REPO_DIR/deploy/systemd/lavalink.service" ]]; then
      cp "$REPO_DIR/deploy/systemd/lavalink.service" /etc/systemd/system/lavalink.service
      installed=1
    fi
  fi

  if printf '%s\n' "$CHANGED_FILES_RAW" | grep -q '^deploy/systemd/tts-bot\.service$'; then
    if [[ -f "$REPO_DIR/deploy/systemd/tts-bot.service" ]]; then
      cp "$REPO_DIR/deploy/systemd/tts-bot.service" /etc/systemd/system/tts-bot.service
      installed=1
    fi
  fi

  if (( installed == 1 )); then
    systemctl daemon-reload
  fi

  if (( lavalink_unit_changed == 1 )); then
    if env_truthy VPS_LAVALINK_ENABLED; then
      systemctl enable "$LAVALINK_SERVICE" >/dev/null 2>&1 || true
      systemctl restart "$LAVALINK_SERVICE" || true
      if systemctl is-active --quiet "$LAVALINK_SERVICE"; then
        AUDIO_SERVICES_STATUS="Lavalink ativo"
      else
        AUDIO_SERVICES_STATUS="Lavalink configurado, mas não ficou ativo"
      fi
    else
      AUDIO_SERVICES_STATUS="Lavalink VPS não iniciado; node de áudio roda no phone worker/Music Agent"
    fi
  else
    AUDIO_SERVICES_STATUS="units atualizadas; Lavalink não alterado"
  fi
}


deploy_cleanup_timer() {
  if (( CLEANUP_CHANGED == 0 )); then
    CLEANUP_STATUS="não alterada"
    return 0
  fi

  STAGE="configuração da limpeza de temporários"
  local installed=0

  local cleanup_service_src="$REPO_DIR/deploy/systemd/vps/cleanup-audio-temp.service"
  local cleanup_timer_src="$REPO_DIR/deploy/systemd/vps/cleanup-audio-temp.timer"
  [[ -f "$cleanup_service_src" ]] || cleanup_service_src="$REPO_DIR/deploy/systemd/cleanup-audio-temp.service"
  [[ -f "$cleanup_timer_src" ]] || cleanup_timer_src="$REPO_DIR/deploy/systemd/cleanup-audio-temp.timer"

  if [[ -f "$cleanup_service_src" ]]; then
    cp "$cleanup_service_src" /etc/systemd/system/cleanup-audio-temp.service
    installed=1
  fi
  if [[ -f "$cleanup_timer_src" ]]; then
    cp "$cleanup_timer_src" /etc/systemd/system/cleanup-audio-temp.timer
    installed=1
  fi

  if (( installed == 0 )); then
    CLEANUP_STATUS="units de limpeza não encontrados"
    return 0
  fi

  systemctl daemon-reload
  systemctl enable --now cleanup-audio-temp.timer >/dev/null 2>&1 || true
  systemctl start cleanup-audio-temp.service >/dev/null 2>&1 || true

  if systemctl is-active --quiet cleanup-audio-temp.timer; then
    CLEANUP_STATUS="timer ativo"
  else
    CLEANUP_STATUS="timer instalado, mas não ativo"
  fi
}


deploy_phone_lavalink_watch() {
  if (( PHONE_LAVALINK_WATCH_CHANGED == 0 )); then
    PHONE_LAVALINK_WATCH_STATUS="não alterado"
    return 0
  fi

  STAGE="configuração do watcher do Lavalink auxiliar"
  local installed=0

  local phone_lavalink_service_src="$REPO_DIR/deploy/systemd/vps/phone-lavalink-watch.service"
  local phone_lavalink_timer_src="$REPO_DIR/deploy/systemd/vps/phone-lavalink-watch.timer"
  [[ -f "$phone_lavalink_service_src" ]] || phone_lavalink_service_src="$REPO_DIR/deploy/systemd/phone-lavalink-watch.service"
  [[ -f "$phone_lavalink_timer_src" ]] || phone_lavalink_timer_src="$REPO_DIR/deploy/systemd/phone-lavalink-watch.timer"

  if [[ -f "$phone_lavalink_service_src" ]]; then
    cp "$phone_lavalink_service_src" /etc/systemd/system/phone-lavalink-watch.service
    installed=1
  fi
  if [[ -f "$phone_lavalink_timer_src" ]]; then
    cp "$phone_lavalink_timer_src" /etc/systemd/system/phone-lavalink-watch.timer
    installed=1
  fi

  if (( installed == 0 )); then
    PHONE_LAVALINK_WATCH_STATUS="units não encontradas"
    return 0
  fi

  systemctl daemon-reload

  local watch_value=""
  if [[ -f "$REPO_DIR/.env" ]]; then
    watch_value="$(grep -E '^PHONE_LAVALINK_WATCH_ENABLED=' "$REPO_DIR/.env" 2>/dev/null | tail -n 1 | cut -d= -f2- | tr -d ' "' || true)"
    watch_value="${watch_value,,}"
  fi

  if [[ "$watch_value" == "0" || "$watch_value" == "false" || "$watch_value" == "no" || "$watch_value" == "off" || "$watch_value" == "nao" || "$watch_value" == "não" ]]; then
    systemctl disable --now phone-lavalink-watch.timer >/dev/null 2>&1 || true
    PHONE_LAVALINK_WATCH_STATUS="timer desativado por PHONE_LAVALINK_WATCH_ENABLED=false"
    return 0
  fi

  if env_truthy AUX_LAVALINK_ENABLED; then
    systemctl enable --now phone-lavalink-watch.timer >/dev/null 2>&1 || true
    systemctl start phone-lavalink-watch.service >/dev/null 2>&1 || true
    if systemctl is-active --quiet phone-lavalink-watch.timer; then
      PHONE_LAVALINK_WATCH_STATUS="timer ativo"
    else
      PHONE_LAVALINK_WATCH_STATUS="timer instalado, mas não ativo"
    fi
  else
    systemctl disable --now phone-lavalink-watch.timer >/dev/null 2>&1 || true
    PHONE_LAVALINK_WATCH_STATUS="instalado; inativo porque AUX_LAVALINK_ENABLED=false"
  fi
}


deploy_phone_worker_watch() {
  if (( PHONE_WORKER_WATCH_CHANGED == 0 )); then
    PHONE_WORKER_WATCH_STATUS="não alterado"
    return 0
  fi

  STAGE="configuração do watcher do phone worker"
  local installed=0

  # Não chmod em script rastreado: systemd usa /usr/bin/env bash e isso evita repo sujo.
  local phone_worker_service_src="$REPO_DIR/deploy/systemd/vps/phone-worker-watch.service"
  local phone_worker_timer_src="$REPO_DIR/deploy/systemd/vps/phone-worker-watch.timer"
  [[ -f "$phone_worker_service_src" ]] || phone_worker_service_src="$REPO_DIR/deploy/systemd/phone-worker-watch.service"
  [[ -f "$phone_worker_timer_src" ]] || phone_worker_timer_src="$REPO_DIR/deploy/systemd/phone-worker-watch.timer"

  if [[ -f "$phone_worker_service_src" ]]; then
    cp "$phone_worker_service_src" /etc/systemd/system/phone-worker-watch.service
    installed=1
  fi
  if [[ -f "$phone_worker_timer_src" ]]; then
    cp "$phone_worker_timer_src" /etc/systemd/system/phone-worker-watch.timer
    installed=1
  fi

  if (( installed == 0 )); then
    PHONE_WORKER_WATCH_STATUS="units não encontradas"
    return 0
  fi

  systemctl daemon-reload

  local worker_value=""
  if [[ -f "$REPO_DIR/.env" ]]; then
    worker_value="$(grep -E '^PHONE_WORKER_ENABLED=' "$REPO_DIR/.env" 2>/dev/null | tail -n 1 | cut -d= -f2- | tr -d ' "' || true)"
    worker_value="${worker_value,,}"
  fi

  local watch_value=""
  if [[ -f "$REPO_DIR/.env" ]]; then
    watch_value="$(grep -E '^PHONE_WORKER_WATCH_ENABLED=' "$REPO_DIR/.env" 2>/dev/null | tail -n 1 | cut -d= -f2- | tr -d ' "' || true)"
    watch_value="${watch_value,,}"
  fi

  if [[ "$watch_value" == "1" || "$watch_value" == "true" || "$watch_value" == "yes" || "$watch_value" == "on" || "$watch_value" == "sim" ]]; then
    systemctl enable --now phone-worker-watch.timer >/dev/null 2>&1 || true
    systemctl start phone-worker-watch.service >/dev/null 2>&1 || true
    if systemctl is-active --quiet phone-worker-watch.timer; then
      PHONE_WORKER_WATCH_STATUS="timer ativo"
    else
      PHONE_WORKER_WATCH_STATUS="timer instalado, mas não ativo"
    fi
  else
    systemctl disable --now phone-worker-watch.timer phone-worker-watch.service >/dev/null 2>&1 || true
    PHONE_WORKER_WATCH_STATUS="instalado; inativo até PHONE_WORKER_WATCH_ENABLED=true"
  fi
}


deploy_phone_worker_sync() {
  if (( PHONE_WORKER_SYNC_REQUIRED == 0 )); then
    PHONE_WORKER_SYNC_STATUS="sem mudanças"
    return 0
  fi

  if ! env_truthy PHONE_WORKER_LEGACY_SSH_SYNC_ENABLED; then
    PHONE_WORKER_SYNC_STATUS="agendado para automação por jobs após restart"
    return 0
  fi

  STAGE="sincronização legada do phone-worker por SSH"

  if [[ ! -x "$REPO_DIR/scripts/sync-phone-worker.sh" ]]; then
    PHONE_WORKER_SYNC_STATUS="não executado: scripts/sync-phone-worker.sh ausente"
    return 0
  fi

  local output status_line
  output="$(sudo -u ubuntu -H bash "$REPO_DIR/scripts/sync-phone-worker.sh" 2>&1 || true)"
  status_line="$(printf '%s\n' "$output" | grep -E '\[phone-worker-sync\]' | tail -n 1 | sed -E 's/^\[phone-worker-sync\][[:space:]]*//' || true)"

  if [[ -n "${status_line//[[:space:]]/}" ]]; then
    PHONE_WORKER_SYNC_STATUS="$status_line"
  else
    PHONE_WORKER_SYNC_STATUS="executado; sem status legível"
  fi

  logger -t "$LOG_TAG" "Phone-worker sync legado: $PHONE_WORKER_SYNC_STATUS"
  return 0
}

run_core_worker_post_update_automation() {
  if (( CORE_WORKER_AUTOMATION_REQUIRED == 0 )); then
    CORE_WORKER_AGENT_UPDATE_STATUS="sem mudanças"
    CORE_WORKER_APK_BUILD_STATUS="sem mudanças"
    CORE_WORKER_NOTIFY_STATUS="sem mudanças"
    return 0
  fi

  STAGE="automação pós-update dos Core Workers"
  local py="$REPO_DIR/.venv/bin/python"
  [[ -x "$py" ]] || py="$(command -v python3 || true)"
  if [[ -z "$py" || ! -f "$REPO_DIR/scripts/core-worker-automation.py" ]]; then
    CORE_WORKER_AGENT_UPDATE_STATUS="não executado: core-worker-automation ausente"
    CORE_WORKER_APK_BUILD_STATUS="não executado"
    CORE_WORKER_NOTIFY_STATUS="não executado"
    return 0
  fi

  local output
  output="$(sudo -u ubuntu -H env CORE_WORKER_CHANGED_FILES="$CHANGED_FILES_RAW" "$py" "$REPO_DIR/scripts/core-worker-automation.py" after-update 2>&1 || true)"
  logger -t "$LOG_TAG" "Core Worker automation: $output"

  local parsed
  parsed="$(CORE_WORKER_AUTOMATION_RAW="$output" python3 - <<'PYJSON' 2>/dev/null || true
import json, os
raw = os.environ.get('CORE_WORKER_AUTOMATION_RAW') or '{}'
line = next((ln for ln in reversed(raw.splitlines()) if ln.strip().startswith('{')), '{}')
try:
    data = json.loads(line)
except Exception:
    data = {}
agent = data.get('agent_update') or {}
apk = data.get('apk_build') or {}
def brief_agent(obj):
    if not obj:
        return 'sem mudanças'
    queued = len(obj.get('queued') or [])
    skipped = len(obj.get('skipped') or [])
    errors = len(obj.get('errors') or [])
    version = obj.get('target_version') or '?'
    return f'agent {version}: {queued} job(s), {skipped} skip, {errors} erro(s)'
def brief_apk(obj):
    if not obj:
        return 'sem mudanças'
    if obj.get('ok'):
        job = obj.get('job') or {}
        return f"APK {obj.get('versionName') or '?'}: build job {job.get('job_id') or 'criado'}"
    return f"APK {obj.get('versionName') or '?'}: pendente ({obj.get('message') or obj.get('error') or 'sem builder'})"
print(brief_agent(agent))
print(brief_apk(apk))
print('apps verão banner/notify quando latest.json novo for publicado' if apk else 'sem notificação nova')
PYJSON
)"
  CORE_WORKER_AGENT_UPDATE_STATUS="$(printf '%s\n' "$parsed" | sed -n '1p')"
  CORE_WORKER_APK_BUILD_STATUS="$(printf '%s\n' "$parsed" | sed -n '2p')"
  CORE_WORKER_NOTIFY_STATUS="$(printf '%s\n' "$parsed" | sed -n '3p')"
  [[ -n "${CORE_WORKER_AGENT_UPDATE_STATUS//[[:space:]]/}" ]] || CORE_WORKER_AGENT_UPDATE_STATUS="executado; sem resumo"
  [[ -n "${CORE_WORKER_APK_BUILD_STATUS//[[:space:]]/}" ]] || CORE_WORKER_APK_BUILD_STATUS="executado; sem resumo"
  [[ -n "${CORE_WORKER_NOTIFY_STATUS//[[:space:]]/}" ]] || CORE_WORKER_NOTIFY_STATUS="executado"
  return 0
}


deploy_bot() {
  # Caminho rápido: não reinstale systemd/watchers/áudio em todo update.
  # Cada rotina só roda quando os arquivos dela mudaram; isso reduz bastante
  # patches comuns e mantém CallKeeper fora de qualquer fluxo amplo.
  if (( VPS_SYSTEMD_UNITS_CHANGED == 1 )); then
    normalize_healthcheck_crontab
    deploy_vps_systemd_units
  fi
  if (( ALERT_CHANGED == 1 || VPS_SYSTEMD_UNITS_CHANGED == 1 )); then
    deploy_alert_unit
  fi
  if (( AUDIO_SYSTEMD_CHANGED == 1 )); then
    deploy_audio_services
  fi
  if (( CLEANUP_CHANGED == 1 )); then
    deploy_cleanup_timer
  fi
  if (( PHONE_LAVALINK_WATCH_CHANGED == 1 )); then
    deploy_phone_lavalink_watch
  fi
  if (( PHONE_WORKER_WATCH_CHANGED == 1 )); then
    deploy_phone_worker_watch
  fi
  if (( PHONE_WORKER_SYNC_REQUIRED == 1 )); then
    deploy_phone_worker_sync
  fi

  if (( REQUIREMENTS_CHANGED == 1 )); then
    STAGE="dependências do bot"
    if [[ -x "$REPO_DIR/.venv/bin/pip" && -f "$REPO_DIR/requirements.txt" ]]; then
      sudo -u ubuntu -H "$REPO_DIR/.venv/bin/pip" install -r "$REPO_DIR/requirements.txt"
    fi
  fi

  if (( BOT_CHANGED == 1 )); then
    local fast_modules restart_epoch restarts_before
    fast_modules="$(fast_reload_modules_for_changed_files || true)"
    if [[ -n "${fast_modules//[[:space:]]/}" ]]; then
      STAGE="reload rápido de cogs"
      if try_fast_cog_reload "$fast_modules"; then
        return 0
      fi
      logger -t "$LOG_TAG" "Fast reload indisponível; usando restart completo seguro do bot principal. Status: $FAST_RELOAD_STATUS"
    fi

    restarts_before="$(service_restart_count "$SERVICE")"

    STAGE="reinício do bot"
    restart_epoch="$(date +%s)"
    systemctl restart "$SERVICE"

    if env_truthy LAVALINK_ENABLED; then
      STAGE="espera curta do Lavalink"
      wait_for_lavalink_ready || true
    fi

    STAGE="validação fatal do bot"
    verify_bot_after_restart "$restart_epoch" "$restarts_before"
    return $?
  fi

  STAGE="healthcheck do bot"
  if refresh_bot_health_status; then
    if has_real_warning_text "$BOT_WARNINGS_STATUS" || cogs_have_failures "$BOT_COGS_STATUS"; then
      BOT_HEALTHCHECK_STATUS="OK com avisos"
      UPDATE_HAS_WARNINGS=1
    else
      BOT_HEALTHCHECK_STATUS="OK"
    fi
  else
    if [[ "$BOT_HEALTH_DETAIL_STATUS" == "HTTP sem resposta" ]]; then
      BOT_HEALTHCHECK_STATUS="não alterado; health HTTP sem resposta"
      UPDATE_HAS_WARNINGS=1
    else
      BOT_HEALTHCHECK_STATUS="falhou: health não saudável ($BOT_HEALTH_DETAIL_STATUS)"
      return 1
    fi
  fi
  return 0
}


deploy_callkeeper() {
  # Regra de isolamento: updates comuns do bot/música/worker NÃO tocam nos
  # CallKeepers. Eles rodam separados e devem permanecer na call. Mesmo que
  # arquivos relacionados apareçam no diff, o updater só pode reinstalar ou
  # reiniciar CallKeeper com opt-in explícito para um patch de CallKeeper.
  if (( CALLKEEPER_CHANGED == 0 )); then
    CALLKEEPER_STATUS="não alterado"
    return 0
  fi
  if [[ "${UPDATE_TOUCH_CALLKEEPER:-}" != "1" && "${CALLKEEPER_UPDATE_ALLOWED:-}" != "1" ]]; then
    CALLKEEPER_STATUS="não alterado; isolado do updater"
    logger -t "$LOG_TAG" "CallKeeper não alterado: update sem opt-in UPDATE_TOUCH_CALLKEEPER=1"
    return 0
  fi

  STAGE="configuração do CallKeeper"
  if [[ -f "$REPO_DIR/deploy/systemd/callkeeper.service" ]]; then
    cp "$REPO_DIR/deploy/systemd/callkeeper.service" /etc/systemd/system/callkeeper.service
    systemctl daemon-reload
    systemctl enable "$CALLKEEPER_SERVICE" >/dev/null 2>&1 || true
  fi

  STAGE="reinício do CallKeeper"
  systemctl restart "$CALLKEEPER_SERVICE"
  sleep 2

  STAGE="healthcheck do CallKeeper"
  if systemctl is-active --quiet "$CALLKEEPER_SERVICE"; then
    CALLKEEPER_STATUS="OK"
    return 0
  fi

  CALLKEEPER_STATUS="falhou"
  return 1
}

deploy_frontend() {
  if (( FRONT_CHANGED == 0 )); then
    FRONT_STATUS="não alterado"
    return 0
  fi

  if [[ ! -d "$FRONT_DIR" ]]; then
    FRONT_STATUS="frontend não encontrado em $FRONT_DIR"
    return 1
  fi

  STAGE="dependências do frontend"
  run_as_ubuntu "cd \"$FRONT_DIR\" && if [ -f package-lock.json ]; then npm ci; else npm install; fi"

  STAGE="build do frontend"
  run_as_ubuntu "cd \"$FRONT_DIR\" && npm run build"

  STAGE="publicação do frontend"
  mkdir -p "$FRONT_PUBLISH_DIR"
  rm -rf "${FRONT_PUBLISH_DIR:?}/"*
  cp -r "$FRONT_DIR/dist/." "$FRONT_PUBLISH_DIR/"

  STAGE="limpeza do frontend"
  run_as_ubuntu "cd \"$FRONT_DIR\" && rm -rf node_modules && npm cache clean --force >/dev/null 2>&1 || true"

  FRONT_STATUS="frontend publicado em $FRONT_PUBLISH_DIR; node_modules removido após build"
  return 0
}

deploy_backend() {
  if (( BACK_CHANGED == 0 && FRONT_CHANGED == 0 )); then
    BACK_STATUS="não alterado"
    ACTIVITY_HEALTHCHECK_STATUS="não alterada"
    return 0
  fi

  if (( BACK_CHANGED == 0 )); then
    BACK_STATUS="não alterado"
    STAGE="healthcheck informativo da activity"
    if wait_for_health "$BACK_HEALTH_URL" 3 2; then
      ACTIVITY_HEALTHCHECK_STATUS="OK"
    else
      ACTIVITY_HEALTHCHECK_STATUS="indisponível; backend não foi alterado"
    fi
    return 0
  fi

  if [[ ! -d "$BACK_DIR" ]]; then
    BACK_STATUS="backend não encontrado em $BACK_DIR"
    return 1
  fi

  STAGE="dependências do backend"
  run_as_ubuntu "cd \"$BACK_DIR\" && if [ -f package-lock.json ]; then npm ci; else npm install; fi"

  STAGE="build do backend"
  run_as_ubuntu "cd \"$BACK_DIR\" && npm run build"

  STAGE="limpeza do backend"
  run_as_ubuntu "cd \"$BACK_DIR\" && npm prune --omit=dev && npm cache clean --force >/dev/null 2>&1 || true"

  STAGE="reinício do backend"
  fuser -k "${BACK_PORT}/tcp" >/dev/null 2>&1 || true
  run_as_ubuntu "cd \"$BACK_DIR\"; set -a; [ -f \"$REPO_DIR/.env\" ] && . \"$REPO_DIR/.env\" || true; [ -f .env ] && . ./.env || true; set +a; nohup node dist/index.js >> sinuca-server.log 2>&1 &"
  sleep 3
  BACK_STATUS="backend reiniciado na porta $BACK_PORT"

  STAGE="healthcheck da activity"
  if wait_for_health "$BACK_HEALTH_URL" 8 3; then
    ACTIVITY_HEALTHCHECK_STATUS="OK"
    BACK_STATUS="backend publicado e validado em $BACK_HEALTH_URL"
    return 0
  fi

  ACTIVITY_HEALTHCHECK_STATUS="falhou"
  BACK_STATUS="backend reiniciado, mas healthcheck falhou em $BACK_HEALTH_URL"
  return 1
}


rollback_after_failure() {
  local exit_code="${1:-1}"
  local failed_command="${2:-desconhecido}"
  register_error_context "$exit_code" "$failed_command"

  local rollback_bot_status="não executado"
  local rollback_front_status="não executado"
  local rollback_back_status="não executado"
  local rollback_activity_status="não executado"
  local rollback_callkeeper_status="não executado"
  local rollback_git_status="não executado"
  local rollback_success=1
  local reset_status=1
  local head_after_reset=""

  trap - ERR
  set +e

  if (( ROLLBACK_DONE == 1 )); then
    exit "$exit_code"
  fi
  ROLLBACK_DONE=1

  if (( LOCAL_CANDIDATE_MODE == 1 )); then
    logger -t "$LOG_TAG" "Erro fatal no candidato local. Tentando rollback para $(short_commit "$PREVIOUS_COMMIT") antes de push GitHub"
  else
    logger -t "$LOG_TAG" "Erro fatal após update. Tentando rollback de $(short_commit "$REMOTE_COMMIT") para $(short_commit "$PREVIOUS_COMMIT")"
  fi

  STAGE="rollback git"
  sudo -u ubuntu -H git reset --hard "$PREVIOUS_COMMIT" >/dev/null 2>&1
  reset_status=$?
  head_after_reset="$(sudo -u ubuntu -H git rev-parse HEAD 2>/dev/null || true)"

  if (( reset_status == 0 )) && [[ -n "$head_after_reset" && "$head_after_reset" == "$PREVIOUS_COMMIT" ]]; then
    cleanup_local_candidate_new_files_after_reset
    rollback_git_status="OK: repositório voltou para $(short_commit "$PREVIOUS_COMMIT")"
    if (( LOCAL_CANDIDATE_MODE == 1 )); then
      archive_local_candidate "failed"
      ROLLBACK_STATUS="aplicado para $(short_commit "$PREVIOUS_COMMIT"); GitHub não foi alterado"
    else
      write_dirty_marker "$REMOTE_COMMIT" "$PREVIOUS_COMMIT" "$FAILED_STAGE" "$failed_command"
      ROLLBACK_STATUS="aplicado para $(short_commit "$PREVIOUS_COMMIT"); commit remoto marcado como sujo"
    fi
  else
    rollback_success=0
    rollback_git_status="falhou: reset=$reset_status head=$(short_commit "$head_after_reset") esperado=$(short_commit "$PREVIOUS_COMMIT")"
    ROLLBACK_STATUS="falhou antes de restaurar o commit anterior"
  fi

  if (( rollback_success == 1 )); then
    if (( FRONT_CHANGED == 1 )); then
      if deploy_frontend; then
        rollback_front_status="${FRONT_STATUS:-}"
      else
        rollback_success=0
        rollback_front_status="falhou: $FRONT_STATUS"
      fi
    else
      rollback_front_status="não precisou republicar"
    fi

    if (( BACK_CHANGED == 1 )); then
      if deploy_backend; then
        rollback_back_status="${BACK_STATUS:-}"
        rollback_activity_status="${ACTIVITY_HEALTHCHECK_STATUS:-}"
      else
        rollback_success=0
        rollback_back_status="falhou: $BACK_STATUS"
        rollback_activity_status="${ACTIVITY_HEALTHCHECK_STATUS:-}"
      fi
    else
      if wait_for_health "$BACK_HEALTH_URL" 2 2; then
        rollback_activity_status="OK"
      else
        rollback_activity_status="não verificada/indisponível"
      fi
      rollback_back_status="não precisou reiniciar"
    fi

    if deploy_bot; then
      rollback_bot_status="$BOT_HEALTHCHECK_STATUS"
    else
      rollback_success=0
      rollback_bot_status="falhou: $BOT_HEALTHCHECK_STATUS"
    fi

    # CallKeeper é isolado: rollback/update geral não pode reiniciar nem reconciliar.
    rollback_callkeeper_status="protegido; não tocado"
  else
    rollback_front_status="não executado porque o git reset falhou"
    rollback_back_status="não executado porque o git reset falhou"
    rollback_activity_status="não executado porque o git reset falhou"
    rollback_bot_status="não executado porque o git reset falhou"
    rollback_callkeeper_status="não executado porque o git reset falhou"
  fi

  local duration title summary commit_dirty
  duration="$(human_duration "$SECONDS")"
  if (( rollback_success == 1 )); then
    if (( LOCAL_CANDIDATE_MODE == 1 )); then
      title="Update revertido"
      summary="O ZIP foi testado na VPS, falhou na validação e o bot voltou ao estado anterior. Nenhum commit foi enviado ao GitHub."
      commit_dirty="não; GitHub não foi alterado"
    else
      title="Rollback aplicado após erro fatal"
      summary="O update falhou, mas o rollback voltou o repositório para o commit anterior e os serviços foram validados."
      commit_dirty="sim"
    fi
  else
    title="Rollback falhou após erro fatal"
    summary="O update falhou e o rollback não conseguiu restaurar completamente o estado anterior. Verificação manual necessária."
    commit_dirty="não confirmado"
  fi

  local body
  body="Resumo: $summary
Host: $HOSTNAME
Branch: $BRANCH
Commit: $(short_commit "$PREVIOUS_COMMIT") ← $(short_commit "$REMOTE_COMMIT")
Mudança: ${COMMIT_SUBJECT:-sem mensagem}
Etapa: ${FAILED_STAGE:-$STAGE}
Comando: $failed_command
Código: $exit_code
Validações:
• Git reset: $rollback_git_status
• Bot: $rollback_bot_status
• Cogs: $BOT_COGS_STATUS
• Health: $BOT_HEALTH_DETAIL_STATUS
Serviços:
• CallKeeper: $rollback_callkeeper_status
• Frontend: $rollback_front_status
• Backend: $rollback_back_status
• Activity: $rollback_activity_status
Rollback: $ROLLBACK_STATUS
Commit sujo: $commit_dirty
Ação sugerida: Se o rollback falhou, verifique o serviço manualmente antes de aplicar outro update. Se foi aplicado, faça um novo commit corrigido para liberar o updater novamente.
Stderr:
${LAST_ERROR_STDERR:-nenhuma saída adicional capturada}
Últimas linhas:
${LAST_ERROR_LOGS:-nenhum log adicional encontrado}
Duração: $duration
Hora: $(date '+%d/%m/%Y %H:%M:%S')"

  notify_zip_status_message "error" "$title" "$summary" || true
  send_error "$title" "$body"
  exit "$exit_code"
}

on_error() {
  local exit_code="$?"
  if (( MANUAL_FAILURE_ALERT_SENT == 1 )); then
    exit "$exit_code"
  fi
  local failed_command="${BASH_COMMAND:-desconhecido}"
  FAILED_STAGE="$STAGE"
  register_error_context "$exit_code" "$failed_command"

  if (( UPDATE_APPLIED == 1 )) && [[ -n "$PREVIOUS_COMMIT" ]]; then
    rollback_after_failure "$exit_code" "$failed_command"
  fi
  if (( LOCAL_CANDIDATE_MODE == 1 )); then
    archive_local_candidate "failed"
  fi

  local dirty_status dirty_files
  dirty_status="não"
  dirty_files=""
  if [[ "$FAILED_STAGE" == "verificação de alterações locais" || "$STAGE" == "git pull" ]]; then
    dirty_files="$(collect_local_tracked_changes || true)"
    if [[ -n "${dirty_files//[[:space:]]/}" ]]; then
      dirty_status="sim"
    fi
  fi

  local body
  body="Resumo: O updater falhou antes de concluir a troca de commit.
Host: $HOSTNAME
Branch: $BRANCH
Serviço: tts-bot-updater
Serviço afetado: ${LAST_ERROR_SERVICE_UNIT:-$UPDATER_UNIT}
Commit anterior: $(short_commit "$CURRENT_COMMIT")
Commit alvo: $(short_commit "$REMOTE_COMMIT")
Commit: $(short_commit "$CURRENT_COMMIT") → $(short_commit "$REMOTE_COMMIT")
Update: ${COMMIT_SUBJECT:-sem mensagem}
Etapa: $STAGE
Comando: $failed_command
Código: $exit_code
Rollback: $ROLLBACK_STATUS
Commit sujo: $dirty_status
Arquivos sujos:
${dirty_files:-nenhum arquivo rastreado sujo detectado}
Stderr:
${LAST_ERROR_STDERR:-nenhuma saída adicional capturada}
Últimas linhas:
${LAST_ERROR_LOGS:-nenhum log adicional encontrado}
Hora: $(date '+%d/%m/%Y %H:%M:%S')"
  if (( ROLLBACK_CONTROL_MODE == 1 )); then
    fail_title="Falha ao reverter"
    [[ "$ROLLBACK_REQUEST_ACTION" == "redo" ]] && fail_title="Falha ao reaplicar"
    retry_control=""
    if (( UPDATE_APPLIED == 0 )); then
      retry_control="$(rollback_control_json "$ROLLBACK_REQUEST_ACTION" "$ROLLBACK_EXPECTED_HEAD" "$ROLLBACK_REVERT_COMMIT" 2>/dev/null || true)"
    fi
    post_direct_update_message "$ROLLBACK_MESSAGE_CHANNEL_ID" "$ROLLBACK_MESSAGE_ID" "error" "$fail_title" "O estado local foi mantido quando possível. Verifique o webhook/log interno." "$retry_control" || true
    archive_rollback_request "failed"
  else
    notify_zip_status_message "error" "Falha no update" "O updater falhou antes de concluir a finalização. Verifique o webhook/log interno." || true
  fi
  send_error "Falha no auto update" "$body"
  exit "$exit_code"
}

trap 'cleanup_runtime_artifacts' EXIT
trap 'on_error' ERR

SECONDS=0
cd "$REPO_DIR"

if load_pending_rollback_request; then
  logger -t "$LOG_TAG" "Controle de update recebido: $ROLLBACK_REQUEST_ACTION $ROLLBACK_REQUEST_ID"
  prepare_rollback_request_update
elif load_pending_local_candidate; then
  logger -t "$LOG_TAG" "Candidato local recebido: $LOCAL_CANDIDATE_ID"
  prepare_local_candidate_update
else
  STAGE="commit atual"
  CURRENT_COMMIT="$(sudo -u ubuntu -H git rev-parse HEAD)"
  PREVIOUS_COMMIT="$CURRENT_COMMIT"

  STAGE="fetch remoto"
  sudo -u ubuntu -H git fetch origin "$BRANCH"
  REMOTE_COMMIT="$(sudo -u ubuntu -H git rev-parse "origin/$BRANCH")"
  COMMIT_SUBJECT="$(sudo -u ubuntu -H git log -1 --pretty=%s "$REMOTE_COMMIT")"
  mark_update_timing "fetch"

  if [[ -f "$DIRTY_MARKER_FILE" ]]; then
    MARKED_FAILED_COMMIT="$(marker_value FAILED_REMOTE_COMMIT)"
    if [[ -n "$MARKED_FAILED_COMMIT" && "$REMOTE_COMMIT" == "$MARKED_FAILED_COMMIT" ]]; then
      logger -t "$LOG_TAG" "Commit remoto $(short_commit "$REMOTE_COMMIT") continua marcado como sujo após rollback fatal; aguardando um novo commit no GitHub."
      exit 0
    fi
    clear_dirty_marker
  fi

  if [[ "$CURRENT_COMMIT" == "$REMOTE_COMMIT" ]]; then
    logger -t "$LOG_TAG" "Sem mudanças em $BRANCH"
    exit 0
  fi

  SHORT_FROM="$(short_commit "$CURRENT_COMMIT")"
  SHORT_TO="$(short_commit "$REMOTE_COMMIT")"

  CHANGED_FILES_RAW="$(sudo -u ubuntu -H git diff --name-only "$CURRENT_COMMIT" "$REMOTE_COMMIT" || true)"
  CHANGED_DIFF_NUMSTAT_RAW="$(sudo -u ubuntu -H git diff --numstat "$CURRENT_COMMIT" "$REMOTE_COMMIT" || true)"
  mark_update_timing "diff"

  classify_changed_files

  if (( CALLKEEPER_CHANGED == 1 )) && [[ "${UPDATE_TOUCH_CALLKEEPER:-}" != "1" || "${CALLKEEPER_UPDATE_ALLOWED:-}" != "1" ]]; then
    CHANGED_FILES="$(format_changed_files)"
    body="Resumo: Update bloqueado antes do git pull porque contém arquivo protegido do CallKeeper.
Branch: $BRANCH
Commit: $(short_commit "$CURRENT_COMMIT") → $(short_commit "$REMOTE_COMMIT")
Mudança: ${COMMIT_SUBJECT:-sem mensagem}
Arquivos:
$CHANGED_FILES
Ação sugerida: Remova arquivos do CallKeeper deste patch ou faça um patch CallKeeper explícito e isolado.
Hora: $(date '+%d/%m/%Y %H:%M:%S')"
    send_error "Update bloqueado: CallKeeper protegido" "$body"
    exit 1
  fi

  STAGE="limpeza de artefatos gerados"
  cleanup_known_generated_update_artifacts

  STAGE="verificação de alterações locais"
  clear_local_changes_marker_if_clean
  fail_local_changes_before_pull

  logger -t "$LOG_TAG" "Atualizando de $CURRENT_COMMIT para $REMOTE_COMMIT"

  STAGE="git pull"
  sudo -u ubuntu -H git pull --ff-only origin "$BRANCH"
  UPDATE_APPLIED=1
  mark_update_timing "pull"
fi

FAILED_STAGE=""

if (( LOCAL_CANDIDATE_MODE == 1 || ROLLBACK_CONTROL_MODE == 1 )); then
  zip_progress_publish "Validando arquivos"
fi
run_preflight_checks
mark_update_timing "preflight"
if (( LOCAL_CANDIDATE_MODE == 1 || ROLLBACK_CONTROL_MODE == 1 )); then
  process_detail="$(zip_progress_process_detail)"
  zip_progress_done "Arquivos validados"
  if [[ -n "${process_detail//[[:space:]]/}" ]]; then
    zip_progress_publish "Atualizando processos" "$process_detail"
  else
    zip_progress_publish "Validando aplicação"
  fi
fi

deploy_bot
mark_update_timing "bot"
deploy_callkeeper
mark_update_timing "callkeeper"
deploy_frontend
mark_update_timing "frontend"
deploy_backend
mark_update_timing "backend"
run_core_worker_post_update_automation
mark_update_timing "worker"
if (( LOCAL_CANDIDATE_MODE == 1 || ROLLBACK_CONTROL_MODE == 1 )); then
  process_detail="$(zip_progress_process_detail)"
  if [[ -n "${process_detail//[[:space:]]/}" ]]; then
    zip_progress_done "Processos atualizados: **$process_detail**"
  else
    zip_progress_done "Aplicação validada"
  fi
fi

publish_rollback_request_after_validation
if (( ROLLBACK_CONTROL_MODE == 1 )); then
  finalize_rollback_request_success
fi

publish_local_candidate_after_validation

DURATION="$(human_duration "$SECONDS")"
ROLLBACK_STATUS="não foi necessário"
CHANGED_FILES="$(format_changed_files)"
DIFF_TOTAL_SUMMARY="$(format_diff_total_summary)"
CHANGED_FILES_COUNT="$(printf '%s\n' "$CHANGED_FILES_RAW" | awk 'NF {c++} END {print c+0}')"
# O resumo por phone-worker é caro e só deve ser chamado por fluxos de erro/anexo.
# No update saudável, o webhook compacto não precisa dessa análise.

# Atualiza o resumo do health no fim, mesmo que o bot não tenha reiniciado.
refresh_bot_health_status >/dev/null 2>&1 || true
normalize_final_health_warning_state

OVERALL_FATAL=0
if [[ "$BOT_HEALTHCHECK_STATUS" == falhou:* ]]; then
  OVERALL_FATAL=1
fi
if (( CALLKEEPER_CHANGED == 1 )) && [[ "${CALLKEEPER_STATUS:-}" != "OK" && "${CALLKEEPER_STATUS:-}" != "não alterado; isolado do updater" ]]; then
  OVERALL_FATAL=1
fi
if (( FRONT_CHANGED == 1 || BACK_CHANGED == 1 )); then
  [[ "${ACTIVITY_HEALTHCHECK_STATUS:-}" == "OK" ]] || OVERALL_FATAL=1
fi
recompute_update_warning_flag

# Trava final: a mensagem pública nunca deve ficar amarela quando a própria
# seção de avisos/saúde não tem aviso real. Detalhes informativos como
# "timer ativo", "unit instalada" ou "0 com falha" são sucesso, não warning.
if (( UPDATE_HAS_WARNINGS == 1 )); then
  if ! has_real_warning_text "$BOT_WARNINGS_STATUS" \
    && ! cogs_have_failures "$BOT_COGS_STATUS" \
    && [[ "$BOT_HEALTHCHECK_STATUS" != falhou:* ]] \
    && [[ "$BOT_HEALTHCHECK_STATUS" != *"sem resposta"* ]] \
    && [[ "$BOT_HEALTHCHECK_STATUS" != "OK com avisos" ]]; then
    logger -t "$LOG_TAG" "auto-update: warning público normalizado para sucesso; sem warning exibível"
    UPDATE_HAS_WARNINGS=0
  fi
fi

if (( OVERALL_FATAL == 0 && UPDATE_HAS_WARNINGS == 0 )); then
  ALERT_TYPE="success"
  ALERT_TITLE="✅ Update aplicado"
  ALERT_SUMMARY="Update aplicado e tudo está saudável."
elif (( OVERALL_FATAL == 0 )); then
  ALERT_TYPE="warn"
  ALERT_TITLE="⚠️ Update aplicado com avisos"
  ALERT_SUMMARY="Update aplicado, mas há avisos para revisar."
else
  ALERT_TYPE="warn"
  ALERT_TITLE="⚠️ Update aplicado com alerta"
  ALERT_SUMMARY="Update concluído com alerta. Verifique os pontos abaixo."
fi

APPLY_MODE="restart completo"
if [[ "$FAST_RELOAD_STATUS" == "OK" ]]; then
  APPLY_MODE="reload rápido de cog"
elif (( BOT_CHANGED == 0 )); then
  APPLY_MODE="sem restart do bot"
fi

PUBLIC_WARNINGS=""
if (( UPDATE_HAS_WARNINGS == 1 )); then
  if has_real_warning_text "$BOT_WARNINGS_STATUS"; then
    PUBLIC_WARNINGS+="${BOT_WARNINGS_STATUS}"
  fi
  if cogs_have_failures "$BOT_COGS_STATUS"; then
    [[ -z "$PUBLIC_WARNINGS" ]] || PUBLIC_WARNINGS+="; "
    PUBLIC_WARNINGS+="$BOT_COGS_STATUS"
  fi
  if has_real_warning_text "$PREFLIGHT_COG_IMPORT_STATUS"; then
    [[ -z "$PUBLIC_WARNINGS" ]] || PUBLIC_WARNINGS+="; "
    PUBLIC_WARNINGS+="$PREFLIGHT_COG_IMPORT_STATUS"
  fi
  if [[ "$BOT_HEALTHCHECK_STATUS" == *"sem resposta"* || "$BOT_HEALTHCHECK_STATUS" == falhou:* ]]; then
    [[ -z "$PUBLIC_WARNINGS" ]] || PUBLIC_WARNINGS+="; "
    PUBLIC_WARNINGS+="$BOT_HEALTHCHECK_STATUS"
  fi
fi
[[ -n "${PUBLIC_WARNINGS//[[:space:]]/}" ]] || PUBLIC_WARNINGS="sem avisos"

CHANGED_PROCESSES="$(format_changed_processes)"
BODY="Resumo: $ALERT_SUMMARY
Branch: $BRANCH
Commit: ${SHORT_FROM} → ${SHORT_TO}
Update: ${CHANGED_FILES_COUNT} arquivo(s) · $DIFF_TOTAL_SUMMARY
Aplicação: $APPLY_MODE
Processos alterados: $CHANGED_PROCESSES
Arquivos:
$CHANGED_FILES
Duração: $DURATION"

if (( UPDATE_HAS_WARNINGS == 1 || OVERALL_FATAL == 1 )); then
  BODY+=$'
'"Bot: $BOT_HEALTHCHECK_STATUS"
  BODY+=$'
'"Cogs: $BOT_COGS_STATUS"
  BODY+=$'
'"Health: $BOT_HEALTH_DETAIL_STATUS"
fi
if (( UPDATE_HAS_WARNINGS == 1 )); then
  BODY+=$'
'"Avisos: $PUBLIC_WARNINGS"
fi
BODY+=$'
'"Hora: $(date '+%d/%m/%Y %H:%M:%S')"
logger -t "$LOG_TAG" "timings: ${UPDATER_TIMINGS:-sem etapas}; total=$DURATION"
if (( LOCAL_CANDIDATE_MODE == 1 || ROLLBACK_CONTROL_MODE == 1 )); then
  zip_progress_publish "Finalizando..."
fi

ZIP_STATUS_DESCRIPTION="$ALERT_SUMMARY

$BRANCH · ${SHORT_FROM} → ${SHORT_TO}
${CHANGED_FILES_COUNT} arquivo(s) alterado(s) · ${DIFF_TOTAL_SUMMARY}
Aplicação: ${APPLY_MODE} · ${DURATION}

## Arquivos alterados
${CHANGED_FILES}"
ZIP_STATUS_CONTROL_JSON=""
if (( LOCAL_CANDIDATE_MODE == 1 && OVERALL_FATAL == 0 )); then
  source_author_id=""
  if [[ -f "$LOCAL_CANDIDATE_DIR/manifest.json" ]]; then
    source_author_id="$(json_field_from_file "$LOCAL_CANDIDATE_DIR/manifest.json" discord_status.source_author_id 2>/dev/null || true)"
  fi
  ZIP_STATUS_CONTROL_JSON="$(python3 - "$BRANCH" "$REMOTE_COMMIT" "$PREVIOUS_COMMIT" "$source_author_id" <<'PYCTRL'
import json, sys
branch, head, previous, author = sys.argv[1:5]
print(json.dumps({
    "enabled": True,
    "mode": "rollback",
    "branch": branch or "main",
    "expected_head": head,
    "revert_commit": head,
    "head_commit": head,
    "update_from": previous,
    "update_to": head,
    "source_author_id": author,
}, ensure_ascii=False))
PYCTRL
)"
fi
notify_zip_status_message "$ALERT_TYPE" "$ALERT_TITLE" "$ZIP_STATUS_DESCRIPTION" || true

/home/ubuntu/bot/alert.sh "$ALERT_TYPE" "$ALERT_TITLE" "$BODY" || true
if (( LOCAL_CANDIDATE_MODE == 1 )); then
  write_local_candidate_state "notified" "$REMOTE_COMMIT"
  archive_local_candidate "done"
fi
logger -t "$LOG_TAG" "$ALERT_TITLE"

