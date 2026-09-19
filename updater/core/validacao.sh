#!/usr/bin/env bash
# Validação, preflight e saúde dos candidatos e do bot.
# Carregado por atualizar.sh; não execute este módulo isoladamente.

run_as_ubuntu() {
  sudo -u ubuntu -H bash -lc "$1"
}

wait_for_service_active() {
  local unit="${1:?}" attempts="${2:-20}" delay="${3:-0.25}" i
  [[ "$attempts" =~ ^[0-9]+$ ]] || attempts=20
  (( attempts >= 1 )) || attempts=1

  for ((i=1; i<=attempts; i++)); do
    if systemctl is-active --quiet "$unit"; then
      return 0
    fi
    if systemctl is-failed --quiet "$unit"; then
      return 1
    fi
    sleep "$delay"
  done
  systemctl is-active --quiet "$unit"
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

wait_for_health_adaptive() {
  local url="${1:?}"
  local attempts="${2:-12}"
  local max_delay="${3:-2}"
  local i delay
  local -a delays_fast=(0.20 0.40 0.80 1.00)
  local -a delays_normal=(0.20 0.40 0.80 1.60 2.00)
  local -a delays=()
  [[ "$attempts" =~ ^[0-9]+$ ]] || attempts=12
  (( attempts >= 1 )) || attempts=1
  if [[ "$max_delay" == "1" || "$max_delay" == "1.0" ]]; then
    delays=("${delays_fast[@]}")
  else
    delays=("${delays_normal[@]}")
  fi

  for ((i=1; i<=attempts; i++)); do
    if curl -fsS --max-time "${UPDATE_LOCAL_HEALTH_CURL_TIMEOUT_SECONDS:-2}" "$url" >/dev/null 2>&1; then
      return 0
    fi
    (( i == attempts )) && break
    if (( i <= ${#delays[@]} )); then
      delay="${delays[$((i-1))]}"
    else
      delay="${delays[-1]}"
    fi
    sleep "$delay"
  done
  return 1
}

fetch_bot_health_json() {
  curl -fsS --max-time 2 "$BOT_HEALTH_URL" 2>/dev/null || true
}

parse_bot_health_snapshot() {
  BOT_HEALTH_JSON_INPUT="${BOT_HEALTH_JSON:-}" python3 - <<'PYHEALTHSNAPSHOT' 2>/dev/null
import json
import os

raw = os.environ.get("BOT_HEALTH_JSON_INPUT") or ""
try:
    data = json.loads(raw) if raw.strip() else {}
except Exception as exc:
    print(f"health inválido: {type(exc).__name__}: {exc}")
    print("indisponível")
    print("health inválido")
    print("0")
    print("0")
    raise SystemExit(0)

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

loaded = data.get("loaded_cogs_count")
failed = data.get("failed_cogs_count")
failed_cogs = data.get("failed_cogs") or {}
critical = data.get("critical_failed_cogs") or []
if loaded is None:
    loaded = len(data.get("loaded_extensions") or [])
if failed is None:
    failed = len(failed_cogs)
cog_parts = [f"{loaded or 0} carregada(s)"]
if failed:
    kind = "crítica(s)" if critical else "opcional(is)"
    names = []
    for name, details in list(failed_cogs.items())[:5]:
        summary = ""
        if isinstance(details, dict):
            summary = str(details.get("summary") or "").strip()
        names.append(f"{name}" + (f" — {summary}" if summary else ""))
    details = "; ".join(names)
    cog_parts.append(f"{failed} com falha {kind}" + (f": {details}" if details else ""))
else:
    cog_parts.append("0 com falha")
print("; ".join(cog_parts)[:1200])

warnings = data.get("warnings") or []
warnings = [str(item).strip() for item in warnings if str(item).strip()]
print(("; ".join(warnings)[:900]) if warnings else "sem avisos")

healthy = data.get("healthy") is True
ready_healthy = (
    healthy
    and data.get("status") == "ok"
    and data.get("discord_ready") is True
    and data.get("discord_closed") is not True
    and data.get("mongo_ok") is True
    and data.get("cog_loading_finished") is True
    and not critical
)
print("1" if healthy else "0")
print("1" if ready_healthy else "0")
PYHEALTHSNAPSHOT
}

refresh_bot_health_status() {
  BOT_HEALTH_JSON="$(fetch_bot_health_json)"
  BOT_HEALTH_IS_HEALTHY=0
  BOT_HEALTH_READY_HEALTHY=0
  if [[ -z "${BOT_HEALTH_JSON//[[:space:]]/}" ]]; then
    BOT_HEALTH_DETAIL_STATUS="HTTP sem resposta"
    BOT_COGS_STATUS="indisponível"
    BOT_WARNINGS_STATUS="health indisponível"
    return 1
  fi

  local -a health_fields=()
  mapfile -t health_fields < <(parse_bot_health_snapshot)
  if (( ${#health_fields[@]} < 5 )); then
    BOT_HEALTH_DETAIL_STATUS="health inválido"
    BOT_COGS_STATUS="indisponível"
    BOT_WARNINGS_STATUS="health inválido"
    return 1
  fi
  BOT_HEALTH_DETAIL_STATUS="${health_fields[0]}"
  BOT_COGS_STATUS="${health_fields[1]}"
  BOT_WARNINGS_STATUS="${health_fields[2]}"
  [[ "${health_fields[3]}" == "1" ]] && BOT_HEALTH_IS_HEALTHY=1
  [[ "${health_fields[4]}" == "1" ]] && BOT_HEALTH_READY_HEALTHY=1

  (( BOT_HEALTH_IS_HEALTHY == 1 ))
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
  local py
  if declare -F current_bot_python_bin >/dev/null 2>&1; then
    py="$(current_bot_python_bin)"
  else
    py="$REPO_DIR/.venv/bin/python"
    [[ -x "$py" ]] || py="$(command -v python3 || true)"
  fi
  local file module line checked_py=0 checked_sh=0 import_checked=0 import_failed=0 import_output=""
  local deleted_py=0 deleted_cogs=0 batch_output=""
  local -a python_files=() cog_modules=()
  [[ -x "$py" ]] || py="$(command -v python3 || true)"

  if [[ -n "$py" ]]; then
    STAGE="preflight Python"
    while IFS= read -r file; do
      [[ -n "$file" ]] || continue
      if [[ ! -f "$REPO_DIR/$file" ]]; then
        if printf '%s\n' "$CHANGED_STATUS_RAW" | grep -Fq $'D\t'"$file"; then
          deleted_py=$((deleted_py + 1))
          checked_py=1
        fi
        continue
      fi
      checked_py=1
      python_files+=("$REPO_DIR/$file")
    done < <(printf '%s\n' "$CHANGED_FILES_RAW" | grep -E '\.py$' | grep -v '^activity/' || true)

    # Compila todos os Python alterados em uma única inicialização do
    # interpretador. Mantém o mesmo py_compile/doraise por arquivo e relata
    # individualmente qualquer falha, sem pagar fork+startup do Python N vezes.
    if (( ${#python_files[@]} > 0 )); then
      if ! batch_output="$(sudo -u ubuntu -H "$py" - "${python_files[@]}" <<'PYCOMPILEBATCH' 2>&1
import py_compile
import sys

failed = False
for filename in sys.argv[1:]:
    try:
        py_compile.compile(filename, doraise=True)
    except Exception as exc:
        failed = True
        print(f"FAIL {filename}: {exc}", file=sys.stderr)
raise SystemExit(1 if failed else 0)
PYCOMPILEBATCH
)"; then
        PREFLIGHT_PY_STATUS="falhou"
        [[ -n "$batch_output" ]] && printf '%s\n' "$batch_output" >&2
        return 1
      fi
    fi

    if (( checked_py == 1 )); then
      if (( deleted_py > 0 )); then
        PREFLIGHT_PY_STATUS="OK; ${deleted_py} deleção(ões) Python reconhecida(s) no diff"
      else
        PREFLIGHT_PY_STATUS="OK"
      fi
    else
      PREFLIGHT_PY_STATUS="sem arquivos Python alterados"
    fi

    # `py_compile` não pega erro executado no import, como discord.ui.StringSelect.
    # Para cogs alteradas, reunimos os módulos e fazemos todos os imports em uma
    # única inicialização do Python. Falhas continuam sendo aviso, não rollback.
    STAGE="preflight import de cogs"
    while IFS= read -r file; do
      [[ -n "$file" ]] || continue
      [[ "$file" == cogs/*.py ]] || continue
      if [[ ! -f "$REPO_DIR/$file" ]]; then
        if printf '%s\n' "$CHANGED_STATUS_RAW" | grep -Fq $'D\t'"$file"; then
          deleted_cogs=$((deleted_cogs + 1))
          import_checked=1
        fi
        continue
      fi
      [[ "$(basename "$file")" == "__init__.py" ]] && continue
      import_checked=1
      module="${file%.py}"
      module="${module//\//.}"
      cog_modules+=("$module")
    done < <(printf '%s\n' "$CHANGED_FILES_RAW" | grep -E '^cogs/.*\.py$' || true)

    if (( ${#cog_modules[@]} > 0 )); then
      set +e
      import_output="$(cd "$REPO_DIR" && sudo -u ubuntu -H "$py" - "${cog_modules[@]}" <<'PYIMPORTBATCH' 2>&1
import importlib
import os
import sys

# Uma única inicialização do interpretador, mas cada cog continua isolada em
# um processo filho. Isso evita que side effects/sys.modules de uma cog mudem
# o resultado da próxima, preservando o isolamento do preflight anterior.
failed = 0
for module in sys.argv[1:]:
    read_fd, write_fd = os.pipe()
    pid = os.fork()
    if pid == 0:
        os.close(read_fd)
        try:
            importlib.import_module(module)
            payload = f"OK {module}\n"
            code = 0
        except BaseException as exc:
            payload = f"FAIL {module}: {type(exc).__name__}: {exc}\n"
            code = 1
        try:
            os.write(write_fd, payload.encode("utf-8", "replace"))
        finally:
            os.close(write_fd)
        os._exit(code)
    os.close(write_fd)
    chunks = []
    while True:
        chunk = os.read(read_fd, 65536)
        if not chunk:
            break
        chunks.append(chunk)
    os.close(read_fd)
    _, status = os.waitpid(pid, 0)
    sys.stdout.write(b"".join(chunks).decode("utf-8", "replace"))
    if not os.WIFEXITED(status) or os.WEXITSTATUS(status) != 0:
        failed += 1
print(f"__FAILED__={failed}")
raise SystemExit(0)
PYIMPORTBATCH
)"
      set -e
      line="$(printf '%s\n' "$import_output" | grep -E '^__FAILED__=[0-9]+$' | tail -n 1 || true)"
      import_failed="${line#__FAILED__=}"
      [[ "$import_failed" =~ ^[0-9]+$ ]] || import_failed=0
      import_output="$(printf '%s\n' "$import_output" | grep -v -E '^__FAILED__=[0-9]+$' || true)"
    fi

    if (( import_checked == 0 )); then
      PREFLIGHT_COG_IMPORT_STATUS="sem cogs Python alteradas"
    elif (( import_failed == 0 )); then
      if (( deleted_cogs > 0 )); then
        PREFLIGHT_COG_IMPORT_STATUS="OK; ${deleted_cogs} deleção(ões) de cog reconhecida(s) no diff"
      else
        PREFLIGHT_COG_IMPORT_STATUS="OK"
      fi
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
  local allowed_restart_delta="${3:-1}"
  local profile="${4:-standard}"
  local default_stability=7 default_successes=3 default_timeout=35
  case "$profile" in
    reload) default_stability=3; default_successes=2; default_timeout=18 ;;
    cogs) default_stability=5; default_successes=3; default_timeout=25 ;;
    critical) default_stability=10; default_successes=3; default_timeout=45 ;;
    *) profile="standard" ;;
  esac
  local timeout="${UPDATE_BOT_RESTART_TIMEOUT_SECONDS:-$default_timeout}"
  local interval="${UPDATE_BOT_RESTART_POLL_SECONDS:-1}"
  local required_successes="${UPDATE_BOT_HEALTH_CONSECUTIVE_SUCCESSES:-$default_successes}"
  local stability_seconds="${UPDATE_BOT_HEALTH_STABILITY_SECONDS:-$default_stability}"
  local waited=0 restarts_after health_ok=0 last_log_check=0
  local consecutive=0 healthy_since=0 now_epoch stable_for=0
  local verify_started_ms first_active_ms=0 first_health_ms=0 first_ready_ms=0 verify_finished_ms

  verify_started_ms="$(update_now_ms)"
  [[ "$timeout" =~ ^[0-9]+$ ]] || timeout=45
  [[ "$interval" =~ ^[0-9]+$ ]] || interval=1
  [[ "$required_successes" =~ ^[0-9]+$ ]] || required_successes=3
  [[ "$stability_seconds" =~ ^[0-9]+$ ]] || stability_seconds=10
  [[ "$allowed_restart_delta" =~ ^[0-9]+$ ]] || allowed_restart_delta=1
  (( timeout < stability_seconds + 8 )) && timeout=$((stability_seconds + 8))
  (( interval < 1 )) && interval=1
  (( required_successes < 2 )) && required_successes=2

  while (( waited <= timeout )); do
    if systemctl is-failed --quiet "$SERVICE"; then
      BOT_HEALTHCHECK_STATUS="falhou: serviço em failed"
      return 1
    fi

    if systemctl is-active --quiet "$SERVICE"; then
      if (( first_active_ms == 0 )); then
        first_active_ms="$(update_now_ms)"
      fi
      restarts_after="$(service_restart_count "$SERVICE")"
      if [[ "$restarts_after" =~ ^[0-9]+$ && "$restarts_before" =~ ^[0-9]+$ ]]; then
        if (( restarts_after > restarts_before + allowed_restart_delta )); then
          BOT_HEALTHCHECK_STATUS="falhou: restart inesperado detectado (${restarts_before}→${restarts_after})"
          return 1
        fi
      fi

      if (( waited == 0 || waited - last_log_check >= 4 )); then
        last_log_check="$waited"
        if has_fatal_boot_logs "$SERVICE" "$restart_epoch"; then
          BOT_HEALTHCHECK_STATUS="falhou: erro fatal de inicialização nos logs"
          return 1
        fi
      fi

      if refresh_bot_health_status; then
        if (( first_health_ms == 0 )); then
          first_health_ms="$(update_now_ms)"
        fi
        if (( BOT_HEALTH_READY_HEALTHY == 1 )); then
          now_epoch="$(date +%s)"
          if (( first_ready_ms == 0 )); then
            first_ready_ms="$(update_now_ms)"
          fi
          if (( consecutive == 0 )); then
            healthy_since="$now_epoch"
          fi
          consecutive=$((consecutive + 1))
          stable_for=$((now_epoch - healthy_since))
          BOT_HEALTHCHECK_STATUS="confirmando estabilidade (${stable_for}s/${stability_seconds}s; ${consecutive}/${required_successes})"
          if (( consecutive >= required_successes && stable_for >= stability_seconds )); then
            health_ok=1
            break
          fi
        else
          consecutive=0
          healthy_since=0
          stable_for=0
        fi
      else
        consecutive=0
        healthy_since=0
        stable_for=0
        if [[ -n "${BOT_HEALTH_DETAIL_STATUS//[[:space:]]/}" ]]; then
          logger -t "$LOG_TAG" "Health ainda não estável (${waited}s): $BOT_HEALTH_DETAIL_STATUS"
        fi
      fi
    elif (( waited >= 5 )); then
      BOT_HEALTHCHECK_STATUS="falhou: serviço não ficou active"
      return 1
    fi

    sleep "$interval"
    waited=$((waited + interval))
  done

  if (( health_ok == 1 )); then
    if has_fatal_boot_logs "$SERVICE" "$restart_epoch"; then
      BOT_HEALTHCHECK_STATUS="falhou: erro fatal durante a janela de estabilidade"
      return 1
    fi
    verify_finished_ms="$(update_now_ms)"
    (( first_active_ms > 0 )) && append_update_timing_ms "bot.service_active" "$((first_active_ms - verify_started_ms))"
    (( first_health_ms > 0 )) && append_update_timing_ms "bot.first_health" "$((first_health_ms - verify_started_ms))"
    (( first_ready_ms > 0 )) && append_update_timing_ms "bot.first_ready" "$((first_ready_ms - verify_started_ms))"
    if (( first_ready_ms > 0 )); then
      append_update_timing_ms "bot.stability" "$((verify_finished_ms - first_ready_ms))"
    fi
    append_update_timing_ms "bot.verify_total" "$((verify_finished_ms - verify_started_ms))"
    if has_real_warning_text "$BOT_WARNINGS_STATUS" || cogs_have_failures "$BOT_COGS_STATUS"; then
      BOT_HEALTHCHECK_STATUS="estável com avisos (${stability_seconds}s; perfil=$profile)"
      UPDATE_HAS_WARNINGS=1
    else
      BOT_HEALTHCHECK_STATUS="estável (${stability_seconds}s; perfil=$profile)"
    fi
    return 0
  fi

  if has_fatal_boot_logs "$SERVICE" "$restart_epoch"; then
    BOT_HEALTHCHECK_STATUS="falhou: erro fatal de inicialização nos logs"
    return 1
  fi
  if [[ "$BOT_HEALTH_DETAIL_STATUS" == "HTTP sem resposta" ]]; then
    BOT_HEALTHCHECK_STATUS="falhou: health HTTP sem resposta após ${timeout}s"
  else
    BOT_HEALTHCHECK_STATUS="falhou: não permaneceu saudável por ${stability_seconds}s ($BOT_HEALTH_DETAIL_STATUS)"
  fi
  return 1
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
  if has_real_warning_text "${PHONE_WORKER_WATCH_STATUS:-}"; then UPDATE_HAS_WARNINGS=1; fi
  if has_real_warning_text "${PHONE_WORKER_SYNC_STATUS:-}"; then UPDATE_HAS_WARNINGS=1; fi
  if has_real_warning_text "${CORE_WORKER_AGENT_UPDATE_STATUS:-}"; then UPDATE_HAS_WARNINGS=1; fi
  if has_real_warning_text "${CORE_WORKER_APK_BUILD_STATUS:-}"; then UPDATE_HAS_WARNINGS=1; fi
  if has_real_warning_text "${CORE_WORKER_NOTIFY_STATUS:-}"; then UPDATE_HAS_WARNINGS=1; fi
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

read_app_command_sync_status() {
  APP_COMMAND_SYNC_SUMMARY="Comandos sem mudanças"
  APP_COMMAND_SYNC_WEBHOOK_BLOCK=""
  APP_COMMAND_SYNC_ADDED_COUNT=0
  APP_COMMAND_SYNC_REMOVED_COUNT=0
  APP_COMMAND_SYNC_CHANGED=0
  APP_COMMAND_SYNC_PERFORMED=0
  [[ -f "$APP_COMMAND_SYNC_STATUS_FILE" ]] || return 0
  local output
  output="$(python3 - "$APP_COMMAND_SYNC_STATUS_FILE" <<'PYCMD' 2>/dev/null || true
import json, shlex, sys
path = sys.argv[1]
try:
    data = json.load(open(path, encoding='utf-8'))
except Exception:
    raise SystemExit(0)
added = [str(x) for x in data.get('added') or []]
removed = [str(x) for x in data.get('removed') or []]
changed = bool(data.get('manifest_changed'))
performed = bool(data.get('sync_performed'))
reason = str(data.get('reason') or '')

def assign(name, value):
    print(f"{name}={shlex.quote(str(value))}")

assign('APP_COMMAND_SYNC_ADDED_COUNT', len(added))
assign('APP_COMMAND_SYNC_REMOVED_COUNT', len(removed))
assign('APP_COMMAND_SYNC_CHANGED', 1 if changed else 0)
assign('APP_COMMAND_SYNC_PERFORMED', 1 if performed else 0)
if added or removed:
    summary = f"Comandos sincronizados: +{len(added)} -{len(removed)}"
    lines = [f"Comandos: +{len(added)} -{len(removed)}"]
    if added:
        lines.append('Adicionados: ' + ', '.join(added[:12]) + (f", +{len(added)-12}" if len(added) > 12 else ''))
    if removed:
        lines.append('Removidos: ' + ', '.join(removed[:12]) + (f", +{len(removed)-12}" if len(removed) > 12 else ''))
    webhook = '\n'.join(lines)
else:
    if changed and performed:
        summary = 'Comandos sincronizados'
    elif changed:
        summary = 'Comandos revisados'
    else:
        summary = 'Comandos sem mudanças'
    webhook = ''
assign('APP_COMMAND_SYNC_SUMMARY', summary)
assign('APP_COMMAND_SYNC_WEBHOOK_BLOCK', webhook)
assign('APP_COMMAND_SYNC_REASON', reason)
PYCMD
)"
  [[ -n "${output//[[:space:]]/}" ]] || return 0
  eval "$output"
}

sanitize_commit_ref() {
  local value="${1:-}"
  value="$(printf '%s' "$value" | tr -d '[:space:]')"
  if [[ "$value" =~ ^[0-9a-fA-F]{7,40}$ ]]; then
    printf '%s' "$value"
  fi
}

remote_commit_is_rejected() {
  local commit="$(sanitize_commit_ref "${1:-}")"
  [[ -n "$commit" && -f "$REMOTE_REJECTED_FILE" ]] || return 1
  python3 - "$REMOTE_REJECTED_FILE" "$commit" <<'PYREJ' >/dev/null 2>&1
import json, sys
path, commit = sys.argv[1], sys.argv[2]
try:
    data = json.load(open(path, encoding='utf-8'))
except Exception:
    raise SystemExit(1)
items = data.get('commits') if isinstance(data, dict) else None
if not isinstance(items, dict):
    raise SystemExit(1)
raise SystemExit(0 if commit in items else 1)
PYREJ
}

mark_remote_commit_rejected() {
  local commit="$(sanitize_commit_ref "${1:-}")"
  local reason="${2:-rejeitado}"
  [[ -n "$commit" ]] || return 0
  mkdir -p "$(dirname "$REMOTE_REJECTED_FILE")" 2>/dev/null || true
  python3 - "$REMOTE_REJECTED_FILE" "$commit" "$reason" "$CURRENT_COMMIT" <<'PYREJ' 2>/dev/null || true
import datetime, json, os, pathlib, sys
path = pathlib.Path(sys.argv[1])
commit, reason, live = sys.argv[2:5]
try:
    data = json.loads(path.read_text(encoding='utf-8')) if path.exists() else {}
except Exception:
    data = {}
if not isinstance(data, dict):
    data = {}
items = data.get('commits') if isinstance(data.get('commits'), dict) else {}
items[commit] = {
    'reason': reason,
    'live_commit': live,
    'rejected_at': datetime.datetime.now(datetime.timezone.utc).isoformat(),
}
data['commits'] = items
path.write_text(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True), encoding='utf-8')
PYREJ
  chown ubuntu:ubuntu "$REMOTE_REJECTED_FILE" 2>/dev/null || true
}

run_preflight_checks_in_dir() {
  local root="${1:?}"
  local py
  if declare -F current_bot_python_bin >/dev/null 2>&1; then
    py="$(current_bot_python_bin)"
  else
    py="$REPO_DIR/.venv/bin/python"
    [[ -x "$py" ]] || py="$(command -v python3 || true)"
  fi
  local file checked_py=0 checked_sh=0 deleted_py=0 deleted_sh=0 rc=0 batch_output=""
  local -a python_files=()
  [[ -x "$py" ]] || py="$(command -v python3 || true)"
  [[ -n "$py" ]] || { PREFLIGHT_PY_STATUS="python indisponível"; PREFLIGHT_BASH_STATUS="não executado"; return 1; }

  while IFS= read -r file; do
    [[ -n "$file" ]] || continue
    checked_py=1
    if [[ ! -f "$root/$file" ]]; then
      if printf '%s\n' "$CHANGED_STATUS_RAW" | grep -Fq $'D\t'"$file"; then
        deleted_py=$((deleted_py + 1))
      fi
      continue
    fi
    python_files+=("$root/$file")
  done < <(printf '%s\n' "$CHANGED_FILES_RAW" | grep -E '\.py$' | grep -v '^activity/' || true)

  # Valida todos os Python do worktree em um único processo, sem gerar
  # __pycache__. Cada arquivo continua sendo aberto com tokenize.open() e
  # compilado isoladamente para preservar encoding, filename e erro individual.
  if (( ${#python_files[@]} > 0 )); then
    if ! batch_output="$(sudo -u ubuntu -H "$py" - "${python_files[@]}" <<'PYSTATICCOMPILEBATCH' 2>&1
import pathlib
import sys
import tokenize

failed = False
for filename in sys.argv[1:]:
    path = pathlib.Path(filename)
    try:
        with tokenize.open(path) as fh:
            source = fh.read()
        compile(source, str(path), 'exec')
    except Exception as exc:
        failed = True
        print(f"FAIL {path}: {type(exc).__name__}: {exc}", file=sys.stderr)
raise SystemExit(1 if failed else 0)
PYSTATICCOMPILEBATCH
)"; then
      rc=1
      [[ -n "$batch_output" ]] && printf '%s\n' "$batch_output" >&2
    fi
  fi

  if (( checked_py == 1 && rc == 0 )); then
    if (( deleted_py > 0 )); then
      PREFLIGHT_PY_STATUS="OK; ${deleted_py} deleção(ões) Python reconhecida(s) no diff"
    else
      PREFLIGHT_PY_STATUS="OK"
    fi
  elif (( checked_py == 1 )); then
    PREFLIGHT_PY_STATUS="falhou"
  else
    PREFLIGHT_PY_STATUS="sem arquivos Python alterados"
  fi

  while IFS= read -r file; do
    [[ -n "$file" ]] || continue
    checked_sh=1
    if [[ ! -f "$root/$file" ]]; then
      if printf '%s\n' "$CHANGED_STATUS_RAW" | grep -Fq $'D\t'"$file"; then
        deleted_sh=$((deleted_sh + 1))
      fi
      continue
    fi
    # Bash não oferece um parser multi-arquivo independente em uma única
    # invocação. Manter `bash -n` por arquivo evita falsos positivos causados
    # por concatenação de scripts, enquanto o batch de Python remove o maior
    # custo de startup do preflight.
    if ! bash -n "$root/$file"; then
      rc=1
    fi
  done < <(printf '%s\n' "$CHANGED_FILES_RAW" | grep -E '\.sh$' || true)
  if (( checked_sh == 1 && rc == 0 )); then
    if (( deleted_sh > 0 )); then
      PREFLIGHT_BASH_STATUS="OK; ${deleted_sh} deleção(ões) Bash reconhecida(s) no diff"
    else
      PREFLIGHT_BASH_STATUS="OK"
    fi
  elif (( checked_sh == 1 )); then
    PREFLIGHT_BASH_STATUS="falhou"
  else
    PREFLIGHT_BASH_STATUS="sem scripts Bash alterados"
  fi
  PREFLIGHT_COG_IMPORT_STATUS="não executado no staging isolado"
  logger -t "$LOG_TAG" "Preflight staging: Python=$PREFLIGHT_PY_STATUS Bash=$PREFLIGHT_BASH_STATUS"
  return "$rc"
}

validate_remote_commit_in_staging() {
  local remote_commit="$(sanitize_commit_ref "${1:-}")"
  [[ -n "$remote_commit" ]] || return 1
  REMOTE_WORKTREE_DIR="$(mktemp -d "${TMPDIR:-/tmp}/tts-bot-remote-candidate.XXXXXX")"
  rmdir "$REMOTE_WORKTREE_DIR" 2>/dev/null || true
  repo_git worktree add --detach "$REMOTE_WORKTREE_DIR" "$remote_commit" >/dev/null || return 1
  run_preflight_checks_in_dir "$REMOTE_WORKTREE_DIR"
}

reject_remote_commit_without_live_apply() {
  local reason="${1:-validação local falhou}"
  mark_remote_commit_rejected "$REMOTE_COMMIT" "$reason"
  MANUAL_FAILURE_ALERT_SENT=1
  REMOTE_REJECT_REASON="$reason"
  local body
  body="Resumo: O commit do GitHub foi rejeitado antes de alterar a VPS.
Commit: $(short_commit "$REMOTE_COMMIT")
Estado preservado: $(short_commit "$CURRENT_COMMIT")
Motivo: $reason
Arquivos:
$(format_changed_files)
Hora: $(date '+%d/%m/%Y %H:%M:%S')"
  notify_zip_status_message "error" "❌ Atualização rejeitada" $'O commit do GitHub não passou na validação local.\nA VPS continuou no último estado saudável.' || true
  send_error "Atualização do GitHub rejeitada" "$body"
  logger -t "$LOG_TAG" "Commit remoto $(short_commit "$REMOTE_COMMIT") rejeitado antes do live: $reason"
  exit 0
}
