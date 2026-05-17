#!/data/data/com.termux/files/usr/bin/bash
# Instala os scripts do phone-worker no Termux.
set -Eeuo pipefail

install_core_worker_boot() {
  mkdir -p "$HOME/.termux/boot"
  printf '%s\n' \
'#!/data/data/com.termux/files/usr/bin/sh' \
'# Auto-start do Core Worker pelo Termux:Boot.' \
'# Criado/reparado pelo instalador do phone-worker. Não coloque segredos aqui.' \
'termux-wake-lock 2>/dev/null || true' \
'sleep "${PHONE_WORKER_BOOT_DELAY_SECONDS:-25}"' \
'cd "$HOME/phone-worker" || exit 0' \
'if [ -f "$HOME/phone-worker/watch-phone-worker.sh" ]; then' \
'  nohup /data/data/com.termux/files/usr/bin/bash "$HOME/phone-worker/watch-phone-worker.sh" >> "$HOME/phone-worker/phone-worker-watch.boot.log" 2>&1 &' \
'  exit 0' \
'fi' \
'echo "[core-worker-boot] watch-phone-worker.sh não encontrado" >> "$HOME/phone-worker.log"' \
> "$HOME/.termux/boot/10-core-worker"
  chmod +x "$HOME/.termux/boot/10-core-worker"
}


install_core_worker_shell_autostart() {
  local block file tmp
  block='# >>> core-worker-autostart >>>
# Bloco gerenciado pelo Core Worker. Não coloque segredos aqui.
if [ -z "${CORE_WORKER_SHELL_AUTOSTART_DONE:-}" ]; then
  export CORE_WORKER_SHELL_AUTOSTART_DONE=1
  if [ -f "$HOME/phone-worker/watch-phone-worker.sh" ]; then
    (
      termux-wake-lock >/dev/null 2>&1 || true
      cd "$HOME/phone-worker" >/dev/null 2>&1 || exit 0
      nohup /data/data/com.termux/files/usr/bin/bash "$HOME/phone-worker/watch-phone-worker.sh" >> "$HOME/phone-worker/phone-worker-watch.shell.log" 2>&1 &
    ) >/dev/null 2>&1 &
  fi
fi
# <<< core-worker-autostart <<<
'
  for file in "$HOME/.bashrc" "$HOME/.profile"; do
    mkdir -p "$(dirname "$file")"
    tmp="$file.core-worker.tmp"
    if [ -f "$file" ]; then
      sed '/# >>> core-worker-autostart >>>/,/# <<< core-worker-autostart <<</d' "$file" > "$tmp" 2>/dev/null || cp "$file" "$tmp"
    else
      : > "$tmp"
    fi
    if [ -s "$tmp" ]; then
      printf '\n%s\n' "$block" >> "$tmp"
    else
      printf '%s\n' "$block" > "$tmp"
    fi
    mv "$tmp" "$file"
  done
}

SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WORKER_DIR="${PHONE_WORKER_DIR:-$HOME/phone-worker}"

pkg install python tmux curl termux-api -y || pkg install python tmux curl -y
mkdir -p "$WORKER_DIR"
cp "$SRC_DIR/phone_worker.py" "$WORKER_DIR/phone_worker.py"
cp "$SRC_DIR/start-phone-worker.sh" "$WORKER_DIR/start-phone-worker.sh"
cp "$SRC_DIR/watch-phone-worker.sh" "$WORKER_DIR/watch-phone-worker.sh"
cp "$SRC_DIR/pair-phone-worker.sh" "$WORKER_DIR/pair-phone-worker.sh"
cp "$SRC_DIR/bootstrap-phone-worker.sh" "$WORKER_DIR/bootstrap-phone-worker.sh"
cp "$SRC_DIR/install.sh" "$WORKER_DIR/install.sh"
cp "$SRC_DIR/README.md" "$WORKER_DIR/README.md" 2>/dev/null || true
cp "$SRC_DIR/phone-worker.env.example" "$WORKER_DIR/phone-worker.env.example" 2>/dev/null || true
# Compatibilidade com atalhos antigos em ~/
cp "$SRC_DIR/start-phone-worker.sh" "$HOME/start-phone-worker.sh"
cp "$SRC_DIR/watch-phone-worker.sh" "$HOME/watch-phone-worker.sh"
cp "$SRC_DIR/pair-phone-worker.sh" "$HOME/pair-phone-worker.sh"
cp "$SRC_DIR/bootstrap-phone-worker.sh" "$HOME/bootstrap-phone-worker.sh"
chmod +x "$WORKER_DIR/phone_worker.py" "$WORKER_DIR/start-phone-worker.sh" "$WORKER_DIR/watch-phone-worker.sh" "$WORKER_DIR/pair-phone-worker.sh" "$WORKER_DIR/bootstrap-phone-worker.sh" "$WORKER_DIR/install.sh"
chmod +x "$HOME/start-phone-worker.sh" "$HOME/watch-phone-worker.sh" "$HOME/pair-phone-worker.sh" "$HOME/bootstrap-phone-worker.sh"
install_core_worker_boot || true
install_core_worker_shell_autostart || true

if [[ ! -f "$HOME/.phone-worker.env" ]]; then
  cp "$SRC_DIR/phone-worker.env.example" "$HOME/.phone-worker.env"
  chmod 600 "$HOME/.phone-worker.env"
  echo "Criado: $HOME/.phone-worker.env"
  echo "Edite PHONE_WORKER_TOKEN antes de iniciar."
  echo "Depois do pareamento em workers, o script ~/phone-worker/pair-phone-worker.sh preenche CORE_WORKER_* automaticamente."
fi

echo "Instalado. Para iniciar:"
echo "  nano ~/.phone-worker.env"
echo "  ~/phone-worker/pair-phone-worker.sh CORE-XXXX http://IP_TAILSCALE_DA_VPS:10000 \"Meu Worker\" midia"
echo "  # ou tudo de uma vez:"
echo "  ~/phone-worker/bootstrap-phone-worker.sh CORE-XXXX http://IP_TAILSCALE_DA_VPS:10000 \"Meu Worker\" midia"
echo "  nohup bash ~/phone-worker/watch-phone-worker.sh >> ~/phone-worker/phone-worker-watch.log 2>&1 &"
