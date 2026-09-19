#!/usr/bin/env bash
# Classificação de mudanças, diff, fast reload e proteção contra checkout sujo.
# Carregado por atualizar.sh; não execute este módulo isoladamente.

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
    remaining = len(lines) - limit
    label = 'arquivo restante' if remaining == 1 else 'arquivos restantes'
    print(f"+{remaining} {label}")
if not lines:
    print("• nenhum arquivo listado")
PYDIFF
  elif [[ -n "$CHANGED_FILES_RAW" ]]; then
    printf '%s\n' "$CHANGED_FILES_RAW" | head -n 20 | sed 's/^/• /'
    local total
    total="$(printf '%s\n' "$CHANGED_FILES_RAW" | awk 'NF {c++} END {print c+0}')"
    if [[ "$total" =~ ^[0-9]+$ && "$total" -gt 20 ]]; then
      local remaining=$((total - 20))
      if (( remaining == 1 )); then
        printf '+1 arquivo restante\n'
      else
        printf '+%s arquivos restantes\n' "$remaining"
      fi
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
    out += f" · {binaries} " + ('binário' if binaries == 1 else 'binários')
print(out)
PYDIFF
}

classify_changed_files() {
  FRONT_CHANGED=0
  BACK_CHANGED=0
  FRONT_TESTS_CHANGED=0
  BACK_TESTS_CHANGED=0
  FRONT_TESTS_REQUIRED=0
  FRONT_TYPECHECK_REQUIRED=0
  BACK_TESTS_REQUIRED=0
  FRONT_TEST_PLAN_STATUS="não executado"
  BACK_TEST_PLAN_STATUS="não executado"
  BOT_CHANGED=0
  REQUIREMENTS_CHANGED=0
  AUDIO_SYSTEMD_CHANGED=0
  CLEANUP_CHANGED=0
  PHONE_WORKER_WATCH_CHANGED=0
  VPS_SYSTEMD_UNITS_CHANGED=0
  ALERT_CHANGED=0
  PHONE_WORKER_SYNC_REQUIRED=0
  CORE_WORKER_APK_CHANGED=0
  CORE_WORKER_AUTOMATION_REQUIRED=0
  APP_COMMANDS_MAY_HAVE_CHANGED=0

  # Classificação é feita em uma única passagem Bash. A versão anterior
  # disparava dezenas de grep/pipes a cada refresh do diff, custo perceptível
  # em updates pequenos e repetido no staging/retomada.
  local file
  while IFS= read -r file; do
    [[ -n "$file" ]] || continue

    case "$file" in
      bot.py|cogs/*.py|utility/commands/*.py)
        APP_COMMANDS_MAY_HAVE_CHANGED=1
        ;;
    esac

    case "$file" in
      dashboard/frontend/tests/*)
        FRONT_TESTS_CHANGED=1
        FRONT_TESTS_REQUIRED=1
        ;;
      dashboard/frontend/*|activity/sinuca/*)
        FRONT_CHANGED=1
        case "$file" in
          dashboard/frontend/src/*.css|dashboard/frontend/index.html)
            ;;
          *)
            FRONT_TESTS_REQUIRED=1
            FRONT_TYPECHECK_REQUIRED=1
            ;;
        esac
        ;;
    esac

    case "$file" in
      dashboard/frontend/src/*.ts|dashboard/frontend/src/*.tsx|dashboard/frontend/tsconfig.json|dashboard/frontend/package.json|dashboard/frontend/package-lock.json)
        FRONT_TYPECHECK_REQUIRED=1
        ;;
    esac

    case "$file" in
      dashboard/backend/tests/*)
        BACK_TESTS_CHANGED=1
        BACK_TESTS_REQUIRED=1
        ;;
      dashboard/backend/*|activity/sinuca-server/*)
        BACK_CHANGED=1
        BACK_TESTS_REQUIRED=1
        ;;
      shared/help_catalog.json)
        BACK_CHANGED=1
        ;;
    esac

    case "$file" in
      bot.py|webserver.py|config.py|db.py|start.sh|requirements.txt|requirements.lock|cogs/*|utility/*|updater/discord/*|updater/utilitarios/*)
        BOT_CHANGED=1
        ;;
      deploy/systemd/tts-bot.service|deploy/systemd/vps/tts-bot.service)
        BOT_CHANGED=1
        ;;
    esac

    [[ "$file" == "requirements.txt" || "$file" == "requirements.lock" ]] && REQUIREMENTS_CHANGED=1

    case "$file" in
      deploy/systemd/tts-bot.service)
        AUDIO_SYSTEMD_CHANGED=1
        ;;
    esac
    case "$file" in
      alert.sh|updater/sistema/bot-updater-alert@.service)
        ALERT_CHANGED=1
        ;;
    esac
    case "$file" in
      updater/sistema/*.service|updater/sistema/*.timer|updater/sistema/*.path|updater/sistema/*.sh|updater/sudoers/*|deploy/systemd/tts-bot.service|deploy/systemd/cleanup-audio-temp.service|deploy/systemd/cleanup-audio-temp.timer|deploy/systemd/sinuca-activity-server.service|deploy/systemd/phone-worker-watch.service|deploy/systemd/phone-worker-watch.timer|deploy/systemd/tts-bot.service.d/*|deploy/systemd/vps/tts-bot.service|deploy/systemd/vps/cleanup-audio-temp.service|deploy/systemd/vps/cleanup-audio-temp.timer|deploy/systemd/vps/sinuca-activity-server.service|deploy/systemd/vps/phone-worker-watch.service|deploy/systemd/vps/phone-worker-watch.timer|deploy/systemd/vps/tts-bot.service.d/*|deploy/journald/*|deploy/tmpfiles.d/*)
        VPS_SYSTEMD_UNITS_CHANGED=1
        ;;
    esac
    case "$file" in
      cleanup-audio-temp.sh|deploy/systemd/cleanup-audio-temp.service|deploy/systemd/cleanup-audio-temp.timer)
        CLEANUP_CHANGED=1
        ;;
    esac
    case "$file" in
      scripts/phone-worker-watch.sh|scripts/phone-worker-client.py|deploy/systemd/phone-worker-watch.service|deploy/systemd/phone-worker-watch.timer|deploy/termux/phone-worker/*)
        PHONE_WORKER_WATCH_CHANGED=1
        ;;
    esac
    case "$file" in
      deploy/termux/phone-worker/*)
        PHONE_WORKER_SYNC_REQUIRED=1
        CORE_WORKER_AUTOMATION_REQUIRED=1
        ;;
      android/core-worker-app/*)
        CORE_WORKER_APK_CHANGED=1
        CORE_WORKER_AUTOMATION_REQUIRED=1
        ;;
      scripts/core-worker-automation.py|utility/commands/workers_registry.py|webserver.py)
        CORE_WORKER_AUTOMATION_REQUIRED=1
        ;;
    esac
  done <<< "$CHANGED_FILES_RAW"
}

fast_reload_modules_for_changed_files() {
  CHANGED_FILES_RAW_INPUT="$CHANGED_FILES_RAW" \
  HOT_RELOAD_ALLOW="${DISCORD_AUTO_UPDATE_HOT_RELOAD_ALLOW:-}" \
  HOT_RELOAD_DENY="${DISCORD_AUTO_UPDATE_HOT_RELOAD_DENY:-music,dashboard_sync,terminal_cmd}" \
  python3 - <<'PYFAST'
import os, pathlib, re
raw = [line.strip() for line in (os.environ.get("CHANGED_FILES_RAW_INPUT") or "").splitlines() if line.strip()]
if not raw:
    raise SystemExit(1)

def names(value):
    return {part.strip().removeprefix("cogs.").removesuffix(".py") for part in re.split(r"[,;\s]+", value or "") if part.strip()}

allow = names(os.environ.get("HOT_RELOAD_ALLOW") or "")
deny = names(os.environ.get("HOT_RELOAD_DENY") or "") | {"__init__"}
modules = []
for path in raw:
    parts = pathlib.PurePosixPath(path).parts
    if len(parts) != 2 or parts[0] != "cogs" or not parts[1].endswith(".py"):
        raise SystemExit(1)
    name = parts[1][:-3]
    if name in deny or (allow and name not in allow):
        raise SystemExit(1)
    modules.append("cogs." + name)
print("\n".join(dict.fromkeys(modules)))
PYFAST
}

try_fast_cog_reload() {
  local modules_text="${1:-}"
  local check_app_commands="${2:-0}"
  [[ -n "${modules_text//[[:space:]]/}" ]] || return 1
  local payload token header_args=() response http_code verification_epoch restarts_before
  verification_epoch="$(date +%s)"
  restarts_before="$(service_restart_count "$SERVICE")"
  payload="$(MODULES_TEXT="$modules_text" CHECK_APP_COMMANDS="$check_app_commands" python3 - <<'PYPAYLOAD'
import json, os
mods = [line.strip() for line in (os.environ.get("MODULES_TEXT") or "").splitlines() if line.strip()]
check = str(os.environ.get("CHECK_APP_COMMANDS") or "").strip().lower() in {"1", "true", "yes", "sim", "on"}
print(json.dumps({"modules": mods, "check_app_commands": check}, ensure_ascii=False))
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
  http_code="$(curl -sS -o "$response" -w '%{http_code}' --max-time 90 -H 'Content-Type: application/json' "${header_args[@]}" -d "$payload" http://127.0.0.1:10000/internal/update/reload-cogs 2>/dev/null || true)"
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
  STAGE="estabilidade após reload rápido"
  if verify_bot_after_restart "$verification_epoch" "$restarts_before" 0 reload; then
    FAST_RELOAD_STATUS="OK; estabilidade confirmada"
    return 0
  fi
  FAST_RELOAD_STATUS="reload executado; estabilidade falhou; fallback restart"
  return 1
}

cleanup_known_generated_update_artifacts() {
  # Estes arquivos/pastas são gerados por build/publicação do Core Worker e
  # não devem bloquear o auto updater. Não remove código fonte nem registry.
  sudo -u ubuntu -H rm -rf "$REPO_DIR/android/core-worker-app/app/build" 2>/dev/null || rm -rf "$REPO_DIR/android/core-worker-app/app/build" 2>/dev/null || true
  sudo -u ubuntu -H rm -rf "$REPO_DIR/android/core-worker-app/.gradle" 2>/dev/null || rm -rf "$REPO_DIR/android/core-worker-app/.gradle" 2>/dev/null || true
  sudo -u ubuntu -H rm -f "$REPO_DIR/android/core-worker-app/app/build.gradle.bak"* 2>/dev/null || rm -f "$REPO_DIR/android/core-worker-app/app/build.gradle.bak"* 2>/dev/null || true
  # Não removemos android/core-worker-app/releases aqui: é onde latest.json/APKs
  # privados ficam publicados para os celulares. O auto updater já ignora essa
  # pasta ao criar commits e alterações não rastreadas não bloqueiam git pull.
}

local_changes_fingerprint() {
  if declare -F load_repo_status_snapshot >/dev/null 2>&1 && load_repo_status_snapshot; then
    printf '%s\n' "${GIT_STATUS_FINGERPRINT:-}"
    return 0
  fi
  repo_git status --porcelain=v1 --untracked-files=no 2>/dev/null | sha256sum | awk '{print $1}'
}

clear_local_changes_marker_if_clean() {
  local reuse_snapshot="${1:-0}" status_text
  if [[ "$reuse_snapshot" == "1" && "${GIT_STATUS_SNAPSHOT_READY:-0}" == "1" ]]; then
    status_text="$(printf '%s' "${GIT_STATUS_RAW:-}" | trim_alert_text 1800)"
  else
    status_text="$(collect_local_tracked_changes)"
  fi
  if [[ -z "${status_text//[[:space:]]/}" ]]; then
    rm -f "$LOCAL_CHANGES_MARKER_FILE" 2>/dev/null || true
  fi
}

collect_local_tracked_changes() {
  # Um único status snapshot substitui status + diff + diff --cached.
  if declare -F load_repo_status_snapshot >/dev/null 2>&1 && load_repo_status_snapshot; then
    printf '%s' "${GIT_STATUS_RAW:-}" | trim_alert_text 1800
    return 0
  fi
  repo_git status --short --untracked-files=no 2>/dev/null | trim_alert_text 1800 || true
}

format_tracked_files_for_alert() {
  local raw="${1:-}" item count=0 text=""
  while IFS= read -r item; do
    [[ -n "$item" ]] || continue
    (( count < 40 )) || break
    [[ -z "$text" ]] || text+=$'\n'
    text+="• $item"
    count=$((count + 1))
  done <<< "$raw"
  if (( ${#text} > 1500 )); then
    text="${text:0:1499}…"
  fi
  printf '%s\n' "$text"
}

collect_local_tracked_files() {
  if declare -F load_repo_status_snapshot >/dev/null 2>&1 && load_repo_status_snapshot; then
    format_tracked_files_for_alert "${GIT_STATUS_FILES_RAW:-}"
    return 0
  fi
  {
    repo_git diff --name-only 2>/dev/null || true
    repo_git diff --name-only --cached 2>/dev/null || true
  } | awk '!seen[$0]++ && NF {print "• " $0; if (++n >= 40) exit}' | trim_alert_text 1500
}

candidate_local_changes_are_expected() {
  local reuse_snapshot="${1:-0}"
  (( LOCAL_CANDIDATE_MODE == 1 )) || return 1
  [[ -n "${CHANGED_FILES_RAW//[[:space:]]/}" ]] || return 1
  if ! declare -F load_repo_status_snapshot >/dev/null 2>&1; then
    CHANGED_FILES_RAW="$CHANGED_FILES_RAW" REPO_DIR="$REPO_DIR" python3 - <<'PYCANDIDATE_DIRTY_FALLBACK'
import os
import subprocess
import sys
expected = {line.strip() for line in os.environ.get("CHANGED_FILES_RAW", "").splitlines() if line.strip()}
repo = os.environ.get("REPO_DIR", "/home/ubuntu/bot")
def git_lines(*args: str) -> set[str]:
    cp = subprocess.run(
        ["sudo", "-u", "ubuntu", "-H", "git", *args],
        cwd=repo, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    if cp.returncode != 0:
        print((cp.stderr or "git falhou sem stderr").strip(), file=sys.stderr)
        raise SystemExit(2)
    return {line.strip() for line in cp.stdout.splitlines() if line.strip()}
dirty = git_lines("diff", "--name-only") | git_lines("diff", "--name-only", "--cached")
extra = dirty - expected
if extra:
    print("\n".join(sorted(extra)))
    raise SystemExit(1)
raise SystemExit(0)
PYCANDIDATE_DIRTY_FALLBACK
    return $?
  fi
  if [[ "$reuse_snapshot" != "1" || "${GIT_STATUS_SNAPSHOT_READY:-0}" != "1" ]]; then
    load_repo_status_snapshot || return 2
  fi
  [[ -n "${GIT_STATUS_FILES_RAW//[[:space:]]/}" ]] || return 0

  local dirty expected found
  while IFS= read -r dirty; do
    [[ -n "$dirty" ]] || continue
    found=0
    while IFS= read -r expected; do
      [[ "$dirty" == "$expected" ]] && { found=1; break; }
    done <<< "$CHANGED_FILES_RAW"
    (( found == 1 )) || {
      printf '%s\n' "$dirty"
      return 1
    }
  done <<< "$GIT_STATUS_FILES_RAW"
  return 0
}

ensure_no_unstaged_tracked_changes() {
  if declare -F load_repo_status_snapshot >/dev/null 2>&1; then
    if ! load_repo_status_snapshot; then
      LAST_ERROR_STDERR="não foi possível verificar alterações rastreadas após o build"
      return 1
    fi
    local dirty="${GIT_STATUS_UNSTAGED_FILES_RAW:-}"
    if [[ -z "${dirty//[[:space:]]/}" ]]; then
      return 0
    fi
    LAST_ERROR_STDERR="arquivos rastreados foram alterados durante build/deploy:
$(printf '%s\n' "$dirty" | trim_alert_text 1500)"
    logger -t "$LOG_TAG" "$LAST_ERROR_STDERR" 2>/dev/null || true
    return 1
  fi

  local dirty
  if ! dirty="$(repo_git diff --name-only 2>/dev/null)"; then
    LAST_ERROR_STDERR="não foi possível verificar alterações rastreadas após o build"
    return 1
  fi
  [[ -z "${dirty//[[:space:]]/}" ]] && return 0
  LAST_ERROR_STDERR="arquivos rastreados foram alterados durante build/deploy:
$(printf '%s\n' "$dirty" | trim_alert_text 1500)"
  logger -t "$LOG_TAG" "$LAST_ERROR_STDERR" 2>/dev/null || true
  return 1
}


fail_local_changes_before_pull() {
  local status_text files_text duration body fingerprint previous_fingerprint
  if declare -F load_repo_status_snapshot >/dev/null 2>&1 && load_repo_status_snapshot; then
    status_text="$(printf '%s' "${GIT_STATUS_RAW:-}" | trim_alert_text 1800)"
    fingerprint="${GIT_STATUS_FINGERPRINT:-}"
    files_text="$(format_tracked_files_for_alert "${GIT_STATUS_FILES_RAW:-}")"
  else
    status_text="$(collect_local_tracked_changes)"
    fingerprint="$(local_changes_fingerprint)"
    files_text="$(collect_local_tracked_files)"
  fi
  if [[ -z "${status_text//[[:space:]]/}" ]]; then
    return 0
  fi

  previous_fingerprint=""
  if [[ -f "$LOCAL_CHANGES_MARKER_FILE" ]]; then
    previous_fingerprint="$(awk -F= '$1 == "FINGERPRINT" { sub($1 "=", ""); print; exit }' "$LOCAL_CHANGES_MARKER_FILE" 2>/dev/null || true)"
  fi
  if [[ -n "$fingerprint" && "$fingerprint" == "$previous_fingerprint" ]]; then
    logger -t "$LOG_TAG" "Alterações locais ainda bloqueiam o update; alerta já enviado para este mesmo estado."
    exit 1
  fi
  MANUAL_FAILURE_ALERT_SENT=1
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
Serviço: $UPDATER_UNIT
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

  send_error "Falha na atualização automática: alterações locais" "$body"
  exit 1
}
