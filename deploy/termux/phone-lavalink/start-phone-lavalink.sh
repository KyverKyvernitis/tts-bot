#!/data/data/com.termux/files/usr/bin/bash
# Script para rodar no Termux. Ele inicia/reinicia o Lavalink dentro do Debian
# do proot-distro e mantém o processo em tmux.
set -u

ENV_FILE="${PHONE_LAVALINK_ENV:-$HOME/.phone-lavalink.env}"
if [[ -f "$ENV_FILE" ]]; then
  # Arquivo local do celular, não versionado. Ex.: PHONE_LAVALINK_PASSWORD=...
  # shellcheck disable=SC1090
  source "$ENV_FILE"
fi

PORT="${PHONE_LAVALINK_PORT:-2333}"
PASSWORD="${PHONE_LAVALINK_PASSWORD:-${AUX_LAVALINK_PASSWORD:-}}"
SESSION="${PHONE_LAVALINK_TMUX_SESSION:-lavalink-debian}"
DISTRO="${PHONE_LAVALINK_PROOT_DISTRO:-debian}"
HOST_LAVALINK_DIR="${PHONE_LAVALINK_HOST_DIR:-$HOME/lavalink}"
PROOT_LAVALINK_DIR="${PHONE_LAVALINK_PROOT_DIR:-/root/lavalink}"
JAVA_BIN="${PHONE_LAVALINK_JAVA_BIN:-/usr/bin/java}"
JAVA_XMX="${PHONE_LAVALINK_JAVA_XMX:-768m}"
JAVA_TMPDIR="${PHONE_LAVALINK_JAVA_TMPDIR:-/tmp/lavalink}"
LOG_NAME="${PHONE_LAVALINK_LOG_NAME:-lavalink-proot.log}"
START_WAIT="${PHONE_LAVALINK_START_WAIT_SECONDS:-8}"

log() {
  printf '[phone-lavalink] %s\n' "$*"
}

termux-wake-lock 2>/dev/null || true

health_ok() {
  if [[ -n "$PASSWORD" ]]; then
    curl --max-time 4 -fsS -H "Authorization: $PASSWORD" "http://127.0.0.1:$PORT/version" >/dev/null 2>&1
  else
    # Sem senha configurada no script: aceita qualquer resposta HTTP do Lavalink
    # para não depender de imprimir segredo em tela.
    local code
    code="$(curl --max-time 4 -sS -o /dev/null -w '%{http_code}' "http://127.0.0.1:$PORT/version" 2>/dev/null || echo 000)"
    [[ "$code" != "000" ]]
  fi
}

if health_ok; then
  log "Lavalink já está online."
  exit 0
fi

if ! command -v tmux >/dev/null 2>&1; then
  log "tmux não encontrado. Rode: pkg install tmux -y"
  exit 1
fi
if ! command -v proot-distro >/dev/null 2>&1; then
  log "proot-distro não encontrado. Rode: pkg install proot-distro -y"
  exit 1
fi
if [[ ! -f "$HOST_LAVALINK_DIR/Lavalink.jar" ]]; then
  log "Lavalink.jar não encontrado em $HOST_LAVALINK_DIR"
  exit 1
fi

log "Lavalink offline. Reiniciando sessão $SESSION no Debian proot..."
tmux kill-session -t "$SESSION" 2>/dev/null || true
pkill -f 'java.*Lavalink.jar' 2>/dev/null || true

PROOT_CMD="cd '$PROOT_LAVALINK_DIR' && mkdir -p '$JAVA_TMPDIR' && exec '$JAVA_BIN' -Djava.io.tmpdir='$JAVA_TMPDIR' -Xmx$JAVA_XMX -jar Lavalink.jar >> '$LOG_NAME' 2>&1"

tmux new-session -d -s "$SESSION" \
  proot-distro login "$DISTRO" \
  --bind "$HOST_LAVALINK_DIR:$PROOT_LAVALINK_DIR" \
  -- bash -lc "$PROOT_CMD"

sleep "$START_WAIT"
if health_ok; then
  log "Lavalink iniciado com sucesso."
  exit 0
fi

log "Falha ao iniciar Lavalink. Veja: tmux attach -t $SESSION e tail -n 120 '$HOST_LAVALINK_DIR/$LOG_NAME'"
exit 1
