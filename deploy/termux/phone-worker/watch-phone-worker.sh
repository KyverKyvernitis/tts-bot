#!/data/data/com.termux/files/usr/bin/bash
# Watchdog local do Core Worker/phone-worker.
# Mantém o agent vivo no Termux e é o alvo oficial do Termux:Boot.
set -u

if [ -z "${BASH_VERSION:-}" ]; then
  exec bash "$0" "$@"
fi

ENV_FILE="${PHONE_WORKER_ENV:-$HOME/.phone-worker.env}"
if [[ -f "$ENV_FILE" ]]; then
  set -a
  # shellcheck disable=SC1090
  source "$ENV_FILE"
  set +a
fi

INTERVAL="${PHONE_WORKER_WATCH_INTERVAL_SECONDS:-60}"
WORKER_DIR="${PHONE_WORKER_DIR:-$HOME/phone-worker}"
START_SCRIPT="$WORKER_DIR/start-phone-worker.sh"
RUNTIME_ROOT="${PHONE_WORKER_RUNTIME_ROOT:-$HOME/.core-worker-runtime}"
WATCH_LOG="${PHONE_WORKER_WATCH_LOG_FILE:-$WORKER_DIR/phone-worker-watch.log}"
WATCH_PID_FILE="${PHONE_WORKER_WATCH_PID_FILE:-$WORKER_DIR/phone-worker-watch.pid}"
WATCH_LOCK_DIR="${PHONE_WORKER_WATCH_LOCK_DIR:-$WORKER_DIR/.phone-worker-watch.lock}"
STATUS_FILE="${PHONE_WORKER_STATUS_FILE:-$WORKER_DIR/phone-worker.status}"
MAX_LOG_BYTES="${PHONE_WORKER_LOG_MAX_BYTES:-1048576}"
SSHD_AUTO_START="${PHONE_WORKER_SSHD_AUTO_START:-true}"
SSHD_PORT="${PHONE_WORKER_SSH_PORT:-8022}"
LAST_SSHD_STATE=""
RUNTIME_ROOT="${PHONE_WORKER_RUNTIME_ROOT:-$HOME/.core-worker-runtime}"
BOOTSTRAP_INTERVAL="${PHONE_WORKER_BOOTSTRAP_INTERVAL_SECONDS:-300}"
BOOTSTRAP_JITTER="${PHONE_WORKER_BOOTSTRAP_JITTER_SECONDS:-30}"
BOOTSTRAP_MAX_BACKOFF="${PHONE_WORKER_BOOTSTRAP_MAX_BACKOFF_SECONDS:-1800}"

# Não use fallback para ~/start-phone-worker.sh: versões antigas nesse atalho
# já causaram loops de pip/clang e aquecimento. O instalador cria um wrapper
# em ~/ que aponta para o script oficial dentro de ~/phone-worker.

mkdir -p "$WORKER_DIR"

log() {
  mkdir -p "$(dirname "$WATCH_LOG")"
  printf '[phone-worker-watch] %s\n' "$*" | tee -a "$WATCH_LOG" >/dev/null
}

now_iso() {
  date '+%Y-%m-%dT%H:%M:%S%z' 2>/dev/null || date
}

write_status() {
  mkdir -p "$(dirname "$STATUS_FILE")"
  printf '%s\n' "$1" > "$STATUS_FILE" 2>/dev/null || true
}

active_start_script() {
  local active="" candidate=""
  if [[ -L "$RUNTIME_ROOT/current" || -d "$RUNTIME_ROOT/current" ]]; then
    active="$(cd "$RUNTIME_ROOT/current" 2>/dev/null && pwd -P || true)"
    candidate="$active/start-phone-worker.sh"
    if [[ -n "$active" && -f "$candidate" ]]; then
      printf '%s\n' "$candidate"
      return 0
    fi
  fi
  printf '%s\n' "$START_SCRIPT"
}

rotate_watch_log_if_needed() {
  mkdir -p "$(dirname "$WATCH_LOG")"
  if [[ -f "$WATCH_LOG" ]]; then
    size=$(wc -c < "$WATCH_LOG" 2>/dev/null || echo 0)
    if [[ "$size" -gt "$MAX_LOG_BYTES" ]]; then
      mv -f "$WATCH_LOG" "$WATCH_LOG.1" 2>/dev/null || true
      : > "$WATCH_LOG"
    fi
  fi
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
    if [[ -n "$LAST_SSHD_STATE" && "$LAST_SSHD_STATE" != "listening" ]]; then
      log "sshd recuperado; porta ${SSHD_PORT} ouvindo"
    fi
    LAST_SSHD_STATE="listening"
    return 0
  fi
  if command -v pgrep >/dev/null 2>&1 && pgrep -f 'sshd' >/dev/null 2>&1; then
    if [[ "$LAST_SSHD_STATE" != "process_without_listener" ]]; then
      log "sshd rodando, mas porta ${SSHD_PORT} não apareceu; mantendo processo existente"
    fi
    LAST_SSHD_STATE="process_without_listener"
    return 0
  fi
  if [[ "$LAST_SSHD_STATE" != "starting" ]]; then
    log "sshd parado; tentando iniciar porta ${SSHD_PORT}"
  fi
  LAST_SSHD_STATE="starting"
  sshd -p "$SSHD_PORT" >/dev/null 2>&1 || sshd >/dev/null 2>&1 || true
}

# Impede múltiplos watchdogs competindo entre si. Lock velho é removido se o PID
# gravado nele não existir mais.
watch_pid_is_ours() {
  local pid="${1:-}" cmdline=""
  [[ "$pid" =~ ^[0-9]+$ ]] || return 1
  kill -0 "$pid" 2>/dev/null || return 1
  [[ -r "/proc/$pid/cmdline" ]] || return 1
  cmdline="$(tr '\0' ' ' < "/proc/$pid/cmdline" 2>/dev/null || true)"
  [[ "$cmdline" == *"watch-phone-worker.sh"* ]]
}

if ! mkdir "$WATCH_LOCK_DIR" 2>/dev/null; then
  old_pid=""
  [[ -f "$WATCH_LOCK_DIR/owner.pid" ]] && old_pid="$(head -n 1 "$WATCH_LOCK_DIR/owner.pid" 2>/dev/null || true)"
  if watch_pid_is_ours "$old_pid"; then
    log "watchdog já ativo; pid=$old_pid"
    write_status "watchdog_already_online pid=$old_pid $(now_iso)"
    exit 0
  fi
  log "lock do watchdog órfão confirmado; recuperando"
  rm -rf "$WATCH_LOCK_DIR" 2>/dev/null || exit 1
  mkdir "$WATCH_LOCK_DIR" 2>/dev/null || exit 1
fi
printf '%s\n' "$$" > "$WATCH_LOCK_DIR/owner.pid" 2>/dev/null || true
cleanup_watch() {
  if [[ -f "$WATCH_LOCK_DIR/owner.pid" ]] && [[ "$(cat "$WATCH_LOCK_DIR/owner.pid" 2>/dev/null)" == "$$" ]]; then
    rm -rf "$WATCH_LOCK_DIR" "$WATCH_PID_FILE" 2>/dev/null || true
  fi
}
# A signal must stop this watchdog, not just remove its lock and let its loop
# continue alongside the replacement. SSH masters and the worker are separate
# owned processes and deliberately survive a watchdog handoff.
trap cleanup_watch EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

printf '%s\n' "$$" > "$WATCH_PID_FILE" 2>/dev/null || true
termux-wake-lock 2>/dev/null || true
write_status "watchdog_running pid=$$ $(now_iso)"
log "watchdog ativo; worker_dir=$WORKER_DIR intervalo=${INTERVAL}s"

bootstrap_script() {
  local active=""
  if [[ -L "$RUNTIME_ROOT/current" || -d "$RUNTIME_ROOT/current" ]]; then
    active="$(cd "$RUNTIME_ROOT/current" 2>/dev/null && pwd -P || true)"
    if [[ -n "$active" && -f "$active/phone_worker_bootstrap.py" ]]; then
      printf '%s\n' "$active/phone_worker_bootstrap.py"
      return 0
    fi
  fi
  [[ -f "$WORKER_DIR/phone_worker_bootstrap.py" ]] && printf '%s\n' "$WORKER_DIR/phone_worker_bootstrap.py"
}

run_bootstrap_check() {
  local script="$(bootstrap_script)"
  [[ -n "$script" ]] || return 0  # protocolo antigo ainda em migração
  command -v python >/dev/null 2>&1 || return 1
  log "verificação pull do bootstrap iniciada"
  if python "$script" --check >> "$WATCH_LOG" 2>&1; then
    log "verificação pull do bootstrap concluída"
    return 0
  fi
  log "verificação pull do bootstrap falhou; worker atual será preservado"
  return 1
}

ssh_mode_configured() {
  local mode="${PHONE_WORKER_TRANSPORT:-legacy}" file_mode=""
  # Read this one setting as data. An already-running legacy watchdog must
  # notice activation without importing an arbitrary shell file every second.
  if [[ -r "$ENV_FILE" ]]; then
    file_mode="$(awk '
      /^[ \t]*(export[ \t]+)?PHONE_WORKER_TRANSPORT[ \t]*=/ {
        line=$0; sub(/^[^=]*=[ \t]*/, "", line)
        gsub(/[\047\042]/, "", line); sub(/[ \t]*#.*/, "", line)
        gsub(/[ \t\r]/, "", line); value=line
      }
      END {print value}
    ' "$ENV_FILE" 2>/dev/null || true)"
    [[ -n "$file_mode" ]] && mode="$file_mode"
  fi
  [[ "$mode" == "ssh" ]]
}

transport_script() {
  local candidate="$WORKER_DIR/phone_worker_runtime/ssh_transport.py" active=""
  # The canonical recovery core remains installed even when current is rolled
  # back to an older worker release without an SSH implementation.
  if [[ -f "$candidate" ]]; then
    printf '%s\n' "$candidate"
    return 0
  fi
  active="$(dirname "$(active_start_script)")"
  candidate="$active/phone_worker_runtime/ssh_transport.py"
  [[ -f "$candidate" ]] && printf '%s\n' "$candidate"
}

run_ssh_watch_loop() {
  local next_ssh=0 next_worker=0 next_pull=0 next_maintenance=0
  local ssh_pid="" worker_check_pid="" pull_pid="" now_epoch=0
  local ssh_ready=0 ssh_observed_at=0 helper="" current_start="" rc=0
  local pull_failures=0 worker_failures=0 backoff=0 jitter=0
  local python_bin="${PHONE_WORKER_PYTHON:-python}" bash_bin=""
  local ssh_rc="$WATCH_LOCK_DIR/ssh.rc" ssh_json="$WATCH_LOCK_DIR/ssh.json"
  local worker_rc="$WATCH_LOCK_DIR/worker.rc" pull_rc="$WATCH_LOCK_DIR/pull.rc"
  bash_bin="$(command -v bash || true)"
  [[ -n "$bash_bin" ]] || bash_bin="/data/data/com.termux/files/usr/bin/bash"
  export PHONE_WORKER_TRANSPORT=ssh
  log "supervisão SSH ativa; transporte=5s worker=${INTERVAL}s bootstrap=${BOOTSTRAP_INTERVAL}s"
  while true; do
    now_epoch="$(date +%s 2>/dev/null || echo 0)"
    [[ "$now_epoch" =~ ^[0-9]+$ ]] || now_epoch=0

    # All three operations are tracked one-shots. Neither a slow update nor a
    # local health check may hold the loop while the SSH transport is broken.
    if [[ -n "$ssh_pid" ]] && { [[ -f "$ssh_rc" ]] || ! kill -0 "$ssh_pid" 2>/dev/null; }; then
      wait "$ssh_pid" 2>/dev/null || true
      ssh_pid=""
      ssh_observed_at="$now_epoch"
      ssh_ready=0
      if [[ -f "$ssh_rc" ]]; then
        ssh_ready="$("$python_bin" -c 'import json,sys
try:
 d=json.load(open(sys.argv[1])); print(1 if d.get("vps_ready") is True else 0)
except (OSError,ValueError,TypeError): print(0)' "$ssh_json" 2>/dev/null || echo 0)"
      fi
      rm -f "$ssh_rc"
    fi
    if [[ -n "$worker_check_pid" ]] && { [[ -f "$worker_rc" ]] || ! kill -0 "$worker_check_pid" 2>/dev/null; }; then
      wait "$worker_check_pid" 2>/dev/null || true
      worker_check_pid=""
      rc="$(cat "$worker_rc" 2>/dev/null || echo 1)"
      rm -f "$worker_rc"
      if [[ "$rc" == "0" ]]; then
        worker_failures=0
        write_status "watchdog_ok pid=$$ $(now_iso)"
      else
        worker_failures=$((worker_failures + 1))
        log "start falhou; rc=$rc falhas=$worker_failures; nova tentativa em ${INTERVAL}s"
        write_status "watchdog_start_failed failures=$worker_failures $(now_iso)"
      fi
    fi
    if [[ -n "$pull_pid" ]] && { [[ -f "$pull_rc" ]] || ! kill -0 "$pull_pid" 2>/dev/null; }; then
      wait "$pull_pid" 2>/dev/null || true
      pull_pid=""
      rc="$(cat "$pull_rc" 2>/dev/null || echo 1)"
      rm -f "$pull_rc"
      if [[ "$rc" == "0" ]]; then
        pull_failures=0
        backoff="$BOOTSTRAP_INTERVAL"
      else
        pull_failures=$((pull_failures + 1))
        backoff=$((BOOTSTRAP_INTERVAL * (1 << (pull_failures > 3 ? 3 : pull_failures))))
        [[ "$backoff" -gt "$BOOTSTRAP_MAX_BACKOFF" ]] && backoff="$BOOTSTRAP_MAX_BACKOFF"
      fi
      jitter=0
      if [[ "$BOOTSTRAP_JITTER" =~ ^[0-9]+$ && "$BOOTSTRAP_JITTER" -gt 0 ]]; then jitter=$((RANDOM % (BOOTSTRAP_JITTER + 1))); fi
      next_pull=$((now_epoch + backoff + jitter))
    fi

    if [[ "$now_epoch" -ge "$next_maintenance" ]]; then
      rotate_watch_log_if_needed
      termux-wake-lock 2>/dev/null || true
      next_maintenance=$((now_epoch + 60))
    fi

    # Starting/checking the local backend is independent of connectivity. It
    # uses the existing start script and its identity/lock/health protections.
    if [[ -z "$worker_check_pid" && "$now_epoch" -ge "$next_worker" ]]; then
      current_start="$(active_start_script)"
      next_worker=$((now_epoch + INTERVAL))
      if [[ -f "$current_start" ]]; then
        rm -f "$worker_rc"
        (
          trap - EXIT INT TERM
          PHONE_WORKER_RELEASE_DIR="$(dirname "$current_start")" PHONE_WORKER_QUIET_HEALTHY=1 \
            PHONE_WORKER_SSH_ENSURE_BY_WATCH=1 "$bash_bin" "$current_start"
          result=$?
          printf '%s\n' "$result" > "$worker_rc" 2>/dev/null || true
          exit "$result"
        ) >> "$WATCH_LOG" 2>&1 &
        worker_check_pid=$!
      else
        log "start script não encontrado: $current_start"
        write_status "watchdog_missing_start failures=$worker_failures $(now_iso)"
      fi
    fi

    # A successful real request through L19000 is required before a pull.
    # An open local listener alone is not evidence that the VPS is reachable.
    if [[ -z "$pull_pid" && "$now_epoch" -ge "$next_pull" && "$ssh_ready" == "1" && $((now_epoch - ssh_observed_at)) -le 15 ]]; then
      rm -f "$pull_rc"
      (
        trap - EXIT INT TERM
        run_bootstrap_check
        result=$?
        printf '%s\n' "$result" > "$pull_rc" 2>/dev/null || true
        exit "$result"
      ) >> "$WATCH_LOG" 2>&1 &
      pull_pid=$!
    fi

    if [[ -z "$ssh_pid" && "$now_epoch" -ge "$next_ssh" ]]; then
      helper="$(transport_script)"
      next_ssh=$((now_epoch + 5))
      if [[ -n "$helper" ]]; then
        rm -f "$ssh_rc"
        (
          trap - EXIT INT TERM
          "$python_bin" "$helper" ensure > "$ssh_json.tmp" 2>> "$WATCH_LOG"
          result=$?
          mv -f "$ssh_json.tmp" "$ssh_json" 2>/dev/null || true
          printf '%s\n' "$result" > "$ssh_rc" 2>/dev/null || true
          exit "$result"
        ) &
        ssh_pid=$!
      else
        ssh_ready=0
        log "núcleo SSH ausente; reinstale os arquivos do transporte antes de ativar SSH"
      fi
    fi
    sleep 1 &
    wait $! || true
  done
}

next_bootstrap=0
bootstrap_failures=0
failures=0
while true; do
  if ssh_mode_configured; then
    run_ssh_watch_loop
    exit $?
  fi
  rotate_watch_log_if_needed
  termux-wake-lock 2>/dev/null || true
  ensure_sshd_running
  now_epoch="$(date +%s 2>/dev/null || echo 0)"
  if [[ "$now_epoch" =~ ^[0-9]+$ && "$now_epoch" -ge "$next_bootstrap" ]]; then
    if run_bootstrap_check; then
      bootstrap_failures=0
      backoff="$BOOTSTRAP_INTERVAL"
    else
      bootstrap_failures=$((bootstrap_failures + 1))
      backoff=$((BOOTSTRAP_INTERVAL * (1 << (bootstrap_failures > 3 ? 3 : bootstrap_failures))))
      [[ "$backoff" -gt "$BOOTSTRAP_MAX_BACKOFF" ]] && backoff="$BOOTSTRAP_MAX_BACKOFF"
    fi
    jitter=0
    if [[ "$BOOTSTRAP_JITTER" =~ ^[0-9]+$ && "$BOOTSTRAP_JITTER" -gt 0 ]]; then jitter=$((RANDOM % (BOOTSTRAP_JITTER + 1))); fi
    next_bootstrap=$((now_epoch + backoff + jitter))
  fi
  current_start="$(active_start_script)"
  if [[ -f "$current_start" ]]; then
    PHONE_WORKER_RELEASE_DIR="$(dirname "$current_start")" \
    PHONE_WORKER_QUIET_HEALTHY=1 \
      /data/data/com.termux/files/usr/bin/bash "$current_start" >> "$WATCH_LOG" 2>&1
    start_rc=$?
    if [[ "$start_rc" -eq 0 ]]; then
      failures=0
      write_status "watchdog_ok pid=$$ $(now_iso)"
    else
      failures=$((failures + 1))
      log "start falhou; script=$current_start rc=$start_rc falhas=$failures; nova tentativa em ${INTERVAL}s"
      write_status "watchdog_start_failed failures=$failures $(now_iso)"
    fi
  else
    failures=$((failures + 1))
    log "start script não encontrado: $current_start"
    write_status "watchdog_missing_start failures=$failures $(now_iso)"
  fi
  sleep "$INTERVAL" &
  wait $! || true
done
